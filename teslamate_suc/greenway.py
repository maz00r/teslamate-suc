"""Greenway Polska: sessions from the client account (api.greenwaypolska.pl) → Invoice.

Facts from the reconnaissance (2026-11):
- logging in at client.greenwaypolska.pl requires reCAPTCHA Enterprise, so the add-on does NOT log in with a password;
  it uses the session token `gw_token` (JWT, valid ~60 days) pasted in the panel;
- `GET /api/client/sessions` with `Authorization: Bearer <token>`, newest first, 10 per page, a cursor in
  the headers `x-pagination-has_more_pages` / `x-pagination-next_page_url`;
- times are ISO with +00:00, `price_float` is the gross amount, `total_kwh_float` is energy from the meter,
  `connector_ac_dc` is AC/DC, there is no VIN (only `car_licence_plate`).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal

import http_util
from parse import Invoice, ParseError, fees_hash, money, to_utc_naive

log = logging.getLogger("suc.greenway")

API = "https://api.greenwaypolska.pl/api"
SESSIONS_URL = API + "/client/sessions"
MAX_PAGES = 60
PREFIX = "gw-"


class GreenwayError(Exception):
    pass


class GreenwayTokenExpired(GreenwayError):
    """No token, it has expired, or Greenway returned 401: paste a new one in the panel."""


def clean_token(raw: str) -> str:
    """Accepts: the bare JWT, `gw_token=…`, a URL-encoded value, `Bearer …`."""
    t = (raw or "").strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if t.startswith("gw_token="):
        t = t.split("=", 1)[1].split(";", 1)[0]
    t = urllib.parse.unquote(t).strip()
    if t.count(".") != 2 or len(t) < 40 or any(c.isspace() for c in t):
        raise GreenwayError("to nie wygląda na token Greenway (JWT: trzy części rozdzielone kropkami)")
    return t


def jwt_claims(token: str) -> dict:
    """The JWT payload WITHOUT signature verification — only for the expiry date in the status."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode()).decode())
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}


class GreenwayClient:
    def __init__(self, data_dir: str, now=time.time):
        self.path = os.path.join(data_dir, "greenway_token.json")
        self.dir = data_dir
        self.now = now

    # ---- token

    def _load(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def _save(self, rec: dict) -> None:
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def set_token(self, raw: str, verify: bool = True) -> dict:
        token = clean_token(raw)
        exp = jwt_claims(token).get("exp")
        if exp and float(exp) < self.now():
            raise GreenwayError("ten token już wygasł — zaloguj się na client.greenwaypolska.pl ponownie")
        if verify:
            r = http_util.request("GET", SESSIONS_URL, headers=self._headers(token), timeout=30)
            if r.status == 401:
                raise GreenwayError("Greenway odrzucił token (401) — skopiuj go jeszcze raz po zalogowaniu")
            if not r.ok:
                raise GreenwayError(f"Greenway: HTTP {r.status} {r.text(200)}")
        self._save({"token": token, "exp": exp, "saved_at": self.now(), "rejected": False})
        log.info("zapisano token Greenway (ważny do %s)",
                 time.strftime("%Y-%m-%d", time.gmtime(exp)) if exp else "?")
        return self.status()

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def status(self) -> dict:
        rec = self._load()
        if not rec:
            return {"configured": False}
        exp = rec.get("exp")
        left = (float(exp) - self.now()) / 86400 if exp else None
        return {"configured": True, "expires_at": exp, "days_left": None if left is None else round(left, 1),
                "rejected": bool(rec.get("rejected")), "saved_at": rec.get("saved_at"),
                "valid": not rec.get("rejected") and (left is None or left > 0)}

    def _token(self) -> str:
        rec = self._load()
        if not rec:
            raise GreenwayTokenExpired("brak tokenu Greenway — wklej go w panelu")
        st = self.status()
        if not st["valid"]:
            raise GreenwayTokenExpired("token Greenway wygasł albo został odrzucony — wklej nowy w panelu")
        return rec["token"]

    def _reject(self) -> None:
        rec = self._load()
        if rec:
            rec["rejected"] = True
            self._save(rec)

    @staticmethod
    def _headers(token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Language": "pl"}

    # ---- sessions

    def fetch_sessions(self, since: datetime | None = None) -> list[dict]:
        """Sessions from newest to `since` (going by charging_from), page by page following the cursor."""
        token = self._token()
        url, out = SESSIONS_URL, []
        self.truncated = False
        since_naive = since.astimezone(timezone.utc).replace(tzinfo=None) if since and since.tzinfo else since
        for _ in range(MAX_PAGES):
            r = http_util.request("GET", url, headers=self._headers(token), timeout=30)
            if r.status == 401:
                self._reject()
                raise GreenwayTokenExpired("Greenway odrzucił token (401) — wklej nowy w panelu")
            if not r.ok:
                raise GreenwayError(f"Greenway sessions: HTTP {r.status} {r.text(200)}")
            page = r.json()
            if not isinstance(page, list):
                raise GreenwayError("Greenway: nieoczekiwana odpowiedź (to nie lista sesji)")
            out.extend(page)
            oldest = min((to_utc_naive(s.get("charging_from") or s.get("connected_from")) for s in page
                          if s.get("charging_from") or s.get("connected_from")), default=None)
            more = (r.headers.get("x-pagination-has_more_pages") or "").lower() == "true"
            nxt = r.headers.get("x-pagination-next_page_url")
            if not page or not more or not nxt or (since_naive and oldest and oldest < since_naive):
                break
            if not nxt.startswith(API + "/"):
                raise GreenwayError(f"Greenway: podejrzany adres następnej strony {nxt[:80]!r}")
            url = nxt
        else:
            self.truncated = True
            log.warning("Greenway: przerwano po %d stronach — starsze sesje pominięte", MAX_PAGES)
        log.info("Greenway: pobrano %d sesji", len(out))
        return out


# --------------------------------------------------------------------------- parser


def parse_session(s: dict) -> Invoice | None:
    """None for a session still running (running_session_boolean)."""
    if s.get("running_session_boolean"):
        return None
    sid = s.get("session_id")
    start = to_utc_naive(s.get("charging_from") or s.get("connected_from"))
    stop = to_utc_naive(s.get("charging_to") or s.get("connected_to"))
    unplug = to_utc_naive(s.get("connected_to"))
    plug = to_utc_naive(s.get("connected_from"))
    if sid is None or start is None or stop is None:
        raise ParseError(f"sesja Greenway bez id/czasu: {sid!r}")
    if plug and plug < start:
        start = plug  # the interval from plugging in: TeslaMate counts from the start of the charge, the tolerance takes the rest
    price = money(s.get("price_float") if s.get("price_float") is not None else s.get("price"))
    kwh = s.get("total_kwh_float")
    if kwh is None:
        kwh = s.get("total_kwh") or 0
    kind = str(s.get("connector_ac_dc") or "").upper() or None
    site = ", ".join(x for x in (s.get("location_name"), s.get("location_city")) if x) or None
    status = str(s.get("session_payment_status") or "")
    fee = {"price": str(price), "kwh": str(kwh), "status": status, "kind": kind,
           "invoice": s.get("invoice_number") or s.get("invoice_id")}
    return Invoice(
        session_id=f"{PREFIX}{sid}",
        vin="",
        site_name=site,
        country=s.get("location_country"),
        start=start,
        stop=stop,
        unlatch=unplug if unplug and unplug > stop else None,
        grid_kwh=Decimal(str(kwh)).quantize(Decimal("0.0001")),
        charging_fee=price,
        congestion_fee=Decimal(0),
        other_fee=Decimal(0),
        total=price,
        net_total=(price / Decimal("1.23")).quantize(Decimal("0.01")),
        currency=s.get("currency") or "PLN",
        paid=status.endswith("_Paid") or price == 0,
        pricing_types=["PAYMENT"] if price > 0 else ["FREE"],
        rates=[],
        invoice_ids=[str(s["invoice_id"])] if s.get("invoice_id") is not None else [],
        fees_hash=fees_hash([fee]),
        raw=s,
        provider="greenway",
        charge_kind=kind if kind in ("AC", "DC") else None,
    )


def parse_all(sessions: list[dict]) -> tuple[list[Invoice], list[str]]:
    out, errors, seen = [], [], set()
    for s in sessions:
        try:
            inv = parse_session(s)
        except (ParseError, ValueError, TypeError, KeyError, ArithmeticError) as e:
            errors.append(f"{s.get('session_id')!r}: {e}")
            continue
        if inv is None or inv.session_id in seen:
            continue
        seen.add(inv.session_id)
        out.append(inv)
    return out, errors


BOOKMARKLET = (
    "javascript:(()=>{const m=document.cookie.match(/(?:^|; )gw_token=([^;]+)/);"
    "if(!m){alert('Najpierw zaloguj się na client.greenwaypolska.pl');return}"
    "const t=decodeURIComponent(m[1]);"
    "(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject())"
    ".then(()=>alert('Token Greenway skopiowany — wklej go w panelu SuC'))"
    ".catch(()=>prompt('Skopiuj token Greenway:',t))})()"
)
