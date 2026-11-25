"""Ingress panel: Tesla login, partner registration, probe of the charging history.

Only reachable through HA Ingress (requests come from the Supervisor, 172.30.32.2), so
whoever reaches it is already signed in to HA. Other addresses get a 403.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api import ApiError, ChargingApi, Usage, run_probe
from auth import AuthError, LoginRequired, TeslaAuth, normalize_domain

log = logging.getLogger("suc.panel")

INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}
MAX_BODY = 64 * 1024
HERE = os.path.dirname(os.path.abspath(__file__))


class App:
    def __init__(self, *, opts: dict, auth: TeslaAuth, api: ChargingApi, usage: Usage, data_dir: str):
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
        }

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
            except (AuthError, ApiError, ValueError) as e:
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
