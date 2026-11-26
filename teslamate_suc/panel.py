"""Ingress panel: Tesla login, partner registration, probe of the charging history.

Only reachable through HA Ingress (requests come from the Supervisor, 172.30.32.2), so
whoever reaches it is already signed in to HA. Other addresses get a 403.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from datetime import datetime, timedelta, timezone

import uuid
from decimal import Decimal, InvalidOperation

import db
import greenway as gw
import ha
from api import ApiError, ChargingApi, Usage, run_probe
from auth import AuthError, LoginRequired, TeslaAuth, normalize_domain
from sync import PROVIDERS, summary, summary_all

log = logging.getLogger("suc.panel")

INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}
MAX_BODY = 64 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))


class App:
    def __init__(self, *, opts: dict, auth: TeslaAuth, api: ChargingApi, usage: Usage, data_dir: str,
                 syncer=None, greenway=None):
        self.syncer = syncer
        self.greenway = greenway
        self.opts = opts
        self.auth = auth
        self.api = api
        self.usage = usage
        self.data_dir = data_dir

    def _read_json(self, name: str):
        try:
            with open(os.path.join(self.data_dir, name), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def status(self) -> dict:
        o = self.opts
        missing = [k for k in ("client_id", "client_secret", "domain", "redirect_uri") if not o.get(k)]
        return {
            "config_missing": missing,
            "region": o.get("region"),
            "domain": normalize_domain(o.get("domain", "")),
            "redirect_uri": o.get("redirect_uri"),
            "public_key_url": (f"https://{normalize_domain(o.get('domain', ''))}"
                               "/.well-known/appspecific/com.tesla.3p.public-key.pem") if o.get("domain") else None,
            "token": self.auth.status(),
            "partner": self._read_json("partner.json"),
            "probe": self._read_json("probe_summary.json"),
            "usage": self.usage.read(),
            "db_configured": bool(o.get("db_host") and o.get("db_user") and o.get("db_name")),
            "dry_run": bool(o.get("dry_run", True)),
            "overwrite_cost": bool(o.get("overwrite_cost", False)),
            "sync": summary(self.syncer.last_report()) if self.syncer else {},
            "sync_greenway": summary(self.syncer.last_report("greenway")) if self.syncer else {},
            "greenway_enabled": bool(o.get("greenway_enabled", False)),
            "greenway": self.greenway.status() if self.greenway else {"configured": False},
            "greenway_bookmarklet": gw.BOOKMARKLET,
        }

    def _publish(self):
        if self.syncer:
            ha.publish(summary_all(self.syncer), self.auth.status() if self.auth else {},
                       self.greenway.status() if self.greenway else None)

    def _db(self):
        if not (self.opts.get("db_host") and self.opts.get("db_user")):
            raise ValueError("uzupełnij db_host, db_user i db_password w konfiguracji dodatku")
        conn = db.connect(self.opts)
        db.migrate(conn)
        return conn

    def _require_write(self, what: str):
        if self.opts.get("dry_run", True):
            raise ValueError(f"{what} zmienia TeslaMate — wyłącz dry_run w konfiguracji dodatku")

    # ---- Greenway

    def greenway_token(self, body: dict) -> dict:
        if not self.greenway:
            raise ValueError("Greenway niedostępny")
        if body.get("clear"):
            self.greenway.clear()
            return self.greenway.status()
        try:
            st = self.greenway.set_token(str(body.get("token", "")))
        except gw.GreenwayError as e:
            raise ValueError(str(e)) from None
        self._publish()
        return st

    # ---- manual cost

    def manual_candidates(self, body: dict) -> list:
        conn = self._db()
        try:
            rows = db.manual_candidates(conn, int(body.get("days") or 90), str(body.get("q") or "").strip()[:80])
        finally:
            conn.close()
        return [dict(r, start_date=r["start_date"].isoformat(), end_date=r["end_date"].isoformat(),
                     kwh=None if r["kwh"] is None else float(r["kwh"]),
                     cost=None if r["cost"] is None else float(r["cost"])) for r in rows]

    def manual_save(self, body: dict) -> dict:
        self._require_write("koszt ręczny")
        try:
            pid = int(body["process"])
            amount = Decimal(str(body["amount"]).replace(",", ".")).quantize(Decimal("0.01"))
            kwh = body.get("kwh")
            kwh = None if kwh in (None, "") else Decimal(str(kwh).replace(",", ".")).quantize(Decimal("0.0001"))
        except (KeyError, ValueError, InvalidOperation):
            raise ValueError("podaj ładowanie i kwotę (np. 54,20)") from None
        if amount < 0 or amount > Decimal("9999.99"):
            raise ValueError("kwota poza zakresem 0–9999,99")
        if kwh is not None and (kwh < 0 or kwh > 500):
            raise ValueError("kWh poza zakresem 0–500")
        conn = self._db()
        try:
            res = db.save_manual(conn, process_id=pid, amount=amount, grid_kwh=kwh,
                                 note=str(body.get("note") or "")[:200], batch_id=uuid.uuid4())
        finally:
            conn.close()
        return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in res.items()}

    def manual_delete(self, body: dict) -> dict:
        self._require_write("usunięcie kosztu ręcznego")
        conn = self._db()
        try:
            return db.delete_manual(conn, int(body.get("process")))
        finally:
            conn.close()

    def run_sync(self, body: dict) -> dict:
        if not self.syncer:
            raise ValueError("synchronizacja niedostępna")
        if not (self.opts.get("db_host") and self.opts.get("db_user")):
            raise ValueError("uzupełnij db_host, db_user i db_password w konfiguracji dodatku")

        def day(key):
            v = body.get(key)
            return datetime.fromisoformat(v).replace(tzinfo=timezone.utc) if v else None
        start, end = day("from"), day("to")
        if end:
            end += timedelta(days=1)  # "to" includes the whole chosen day
        if start and end and start >= end:
            raise ValueError("data „od” musi być przed „do”")
        provider = str(body.get("provider") or "tesla")
        if provider not in PROVIDERS:
            raise ValueError("nieznane źródło")
        # dry_run=False from the panel counts only when the dry_run option is also off.
        try:
            return self.syncer.run(start=start, end=end, dry_run=body.get("dry_run", True), provider=provider)
        except gw.GreenwayError as e:
            raise ValueError(str(e)) from None
        finally:  # sensors also after a pass from the panel, not only from the 6-hour loop
            self._publish()

    def undo(self, batch_id: str) -> dict:
        if self.opts.get("dry_run", True):
            raise ValueError("cofanie zmienia TeslaMate — wyłącz dry_run w konfiguracji, żeby cofnąć zapis")
        conn = db.connect(self.opts)
        try:
            return db.undo_batch(conn, batch_id)
        finally:
            conn.close()

    def batches(self) -> list:
        conn = db.connect(self.opts)
        try:
            db.migrate(conn)
            return [dict(r, at=r["at"].isoformat()) for r in db.last_batches(conn)]
        finally:
            conn.close()

    def save_partner(self, result: dict) -> None:
        with open(os.path.join(self.data_dir, "partner.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)


def make_handler(app: App, allowed_peers=INGRESS_PEERS):
    class Handler(BaseHTTPRequestHandler):
        server_version = "suc-panel"

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.client_address[0], *args)

        # ---- helpers

        def _allowed(self) -> bool:
            if self.client_address[0] in allowed_peers:
                return True
            self._send(403, {"error": "tylko przez Ingress Home Assistanta"})
            return False

        def _send(self, code: int, obj, ctype="application/json; charset=utf-8", extra=None):
            body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("za duże żądanie")
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw.decode()) if raw else {}

        def _run(self, fn):
            try:
                self._send(200, fn())
            except LoginRequired as e:
                self._send(401, {"error": str(e), "login_required": True})
            except (AuthError, ApiError, ValueError, RuntimeError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 — the panel must not crash
                log.exception("błąd panelu")
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        # ---- routes

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "panel.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/status":
                return self._run(app.status)
            if path == "/api/report":
                q = self.path.split("?", 1)[1] if "?" in self.path else ""
                prov = "greenway" if "provider=greenway" in q else "tesla"
                return self._run(lambda: (app.syncer.last_report(prov) if app.syncer else None) or {})
            if path == "/api/batches":
                return self._run(app.batches)
            if path == "/api/probe/download":
                p = os.path.join(app.data_dir, "probe.json")
                if not os.path.exists(p):
                    return self._send(404, {"error": "brak sondy"})
                with open(p, "rb") as f:
                    return self._send(200, f.read(), "application/json",
                                      {"Content-Disposition": 'attachment; filename="suc-probe.json"'})
            self._send(404, {"error": "nie ma takiej ścieżki"})

        def do_POST(self):
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/login/start":
                return self._run(lambda: {"url": app.auth.start_login()})
            if path == "/api/login/finish":
                return self._run(lambda: app.auth.finish_login(self._body().get("url", "")))
            if path == "/api/logout":
                return self._run(lambda: (app.auth.logout(), {"ok": True})[1])
            if path == "/api/partner/register":
                def reg():
                    res = app.auth.register_partner(app.opts.get("domain", ""))
                    app.save_partner(res)
                    return res
                return self._run(reg)
            if path == "/api/partner/check":
                return self._run(lambda: app.auth.check_partner(app.opts.get("domain", "")))
            if path == "/api/sync":
                return self._run(lambda: app.run_sync(self._body()))
            if path == "/api/greenway/token":
                return self._run(lambda: app.greenway_token(self._body()))
            if path == "/api/manual/candidates":
                return self._run(lambda: app.manual_candidates(self._body()))
            if path == "/api/manual/save":
                return self._run(lambda: app.manual_save(self._body()))
            if path == "/api/manual/delete":
                return self._run(lambda: app.manual_delete(self._body()))
            if path == "/api/undo":
                return self._run(lambda: app.undo(str(self._body().get("batch_id", ""))))
            if path == "/api/probe":
                def probe():
                    days = int(self._body().get("days") or 90)
                    if not 1 <= days <= 3650:
                        raise ValueError("days poza zakresem 1–3650")
                    return run_probe(app.api, app.data_dir, days)
                return self._run(probe)
            self._send(404, {"error": "nie ma takiej ścieżki"})

    return Handler


def serve(app: App, port: int = 8099) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    srv.daemon_threads = True
    return srv
