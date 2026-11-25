"""Tesla Fleet API OAuth: user login (authorization_code), refresh, partner registration.

Key Tesla rule: the refresh token is SINGLE-USE. Every refresh returns a new one,
and the old one keeps working for only about 24 h. So we save the new token to disk
(atomically) BEFORE anyone gets the access token. If the save fails, the exception goes
up and the previous refresh token stays in the file, still inside its grace period.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import urllib.parse

import http_util

log = logging.getLogger("suc.auth")

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
FLEET_BASE = {
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
}
# Only what charging history and invoices need. No vehicle_cmds.
USER_SCOPES = "openid offline_access vehicle_charging_cmds"
PARTNER_SCOPES = "openid vehicle_charging_cmds"

STATE_TTL_S = 15 * 60
REFRESH_MARGIN_S = 5 * 60


class AuthError(Exception):
    pass


class LoginRequired(AuthError):
    """No token, or Tesla rejected the refresh token: log in again in the panel."""


def fleet_base(region: str) -> str:
    try:
        return FLEET_BASE[region]
    except KeyError:
        raise AuthError(f"nieznany region {region!r}") from None


def normalize_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if "://" in d:
        d = urllib.parse.urlsplit(d).netloc
    return d.strip("/")


# --------------------------------------------------------------------------- store


class TokenStore:
    """JSON files in /data. Atomic write, 0600 permissions."""

    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.token_path = os.path.join(data_dir, "token.json")
        self.state_path = os.path.join(data_dir, "oauth_state.json")

    def _read(self, path: str):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def _write(self, path: str, obj) -> None:
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(obj, f)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        os.replace(tmp, path)
        try:
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _delete(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def load_token(self):
        return self._read(self.token_path)

    def save_token(self, tok: dict) -> None:
        self._write(self.token_path, tok)

    def clear_token(self) -> None:
        self._delete(self.token_path)

    def load_state(self):
        return self._read(self.state_path)

    def save_state(self, st: dict) -> None:
        self._write(self.state_path, st)

    def clear_state(self) -> None:
        self._delete(self.state_path)


# --------------------------------------------------------------------------- auth


class TeslaAuth:
    def __init__(self, *, client_id: str, client_secret: str, region: str,
                 redirect_uri: str, store: TokenStore, now=time.time):
        self.client_id = client_id
        self.client_secret = client_secret
        self.region = region
        self.audience = fleet_base(region)
        self.redirect_uri = redirect_uri
        self.store = store
        self.now = now
        self._lock = threading.Lock()

    # ---- user login

    def start_login(self) -> str:
        if not (self.client_id and self.redirect_uri):
            raise AuthError("uzupełnij client_id i redirect_uri w konfiguracji dodatku")
        state = secrets.token_urlsafe(24)
        self.store.save_state({"state": state, "created": self.now()})
        q = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": USER_SCOPES,
            "state": state,
            "prompt_missing_scopes": "true",
        }
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(q)

    def finish_login(self, pasted: str) -> dict:
        """Takes the address copied from the /callback page. Returns status() after login."""
        pasted = (pasted or "").strip()
        query = urllib.parse.urlsplit(pasted).query if "?" in pasted else pasted.lstrip("?")
        params = urllib.parse.parse_qs(query)
        if "error" in params:
            desc = params.get("error_description", [""])[0]
            raise AuthError(f"Tesla odmówiła: {params['error'][0]} {desc}".strip())
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if not code or not state:
            raise AuthError("w wklejonym adresie brakuje code albo state — wklej cały adres ze strony /callback")
        saved = self.store.load_state()
        if not saved or not secrets.compare_digest(str(saved.get("state", "")), state):
            raise AuthError("state się nie zgadza — kliknij „Zaloguj do Tesli” jeszcze raz i użyj nowego linku")
        if self.now() - float(saved.get("created", 0)) > STATE_TTL_S:
            self.store.clear_state()
            raise AuthError("link logowania wygasł (15 min) — zacznij od nowa")

        r = http_util.request("POST", TOKEN_URL, form={
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "audience": self.audience,
            "redirect_uri": self.redirect_uri,
        })
        body = r.json() or {}
        if not r.ok or "access_token" not in body:
            raise AuthError(f"wymiana kodu nie powiodła się: HTTP {r.status} {body.get('error', '')} "
                            f"{body.get('error_description', '') or r.text(200)}".strip())
        if "refresh_token" not in body:
            raise AuthError("Tesla nie zwróciła refresh_token — czy zakres offline_access jest włączony w aplikacji?")
        with self._lock:
            self.store.save_token(self._token_record(body, previous=None))
            self.store.clear_state()
        log.info("zalogowano do Tesli (zakresy: %s)", body.get("scope", "?"))
        return self.status()

    def logout(self) -> None:
        with self._lock:
            self.store.clear_token()

    # ---- access token

    def access_token(self) -> str:
        with self._lock:
            tok = self.store.load_token()
            if not tok:
                raise LoginRequired("brak tokenu — zaloguj się w panelu")
            if tok.get("login_required"):
                raise LoginRequired("Tesla odrzuciła token — zaloguj się ponownie w panelu")
            if float(tok.get("expires_at", 0)) - self.now() > REFRESH_MARGIN_S:
                return tok["access_token"]
            return self._refresh_locked(tok)["access_token"]

    def force_refresh(self) -> str:
        """After a 401 from the API: refresh even if the token looks fresh."""
        with self._lock:
            tok = self.store.load_token()
            if not tok:
                raise LoginRequired("brak tokenu — zaloguj się w panelu")
            return self._refresh_locked(tok)["access_token"]

    def refresh_if_older_than(self, seconds: float) -> bool:
        """Keepalive: the refresh token expires after 3 months without use."""
        with self._lock:
            tok = self.store.load_token()
            if not tok or tok.get("login_required"):
                return False
            if self.now() - float(tok.get("refreshed_at", 0)) < seconds:
                return False
            self._refresh_locked(tok)
            return True

    def _refresh_locked(self, tok: dict) -> dict:
        r = http_util.request("POST", TOKEN_URL, form={
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": tok["refresh_token"],
        })
        body = r.json() or {}
        if r.ok and "access_token" in body:
            new = self._token_record(body, previous=tok)
            self.store.save_token(new)  # BEFORE returning: the old refresh token is now used up
            log.info("odświeżono token Tesli (ważny do %s UTC)",
                     time.strftime("%Y-%m-%d %H:%M", time.gmtime(new["expires_at"])))
            return new
        err = str(body.get("error", ""))
        if r.status in (400, 401) and err in ("login_required", "invalid_grant", "unauthorized_client"):
            tok = dict(tok, login_required=True, login_required_reason=err)
            self.store.save_token(tok)
            raise LoginRequired(f"Tesla odrzuciła refresh token ({err}) — zaloguj się ponownie w panelu")
        raise AuthError(f"odświeżenie tokenu nie powiodło się: HTTP {r.status} {err} {r.text(200)}".strip())

    def _token_record(self, body: dict, previous: dict | None) -> dict:
        now = self.now()
        rec = {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or (previous or {}).get("refresh_token"),
            "expires_at": now + int(body.get("expires_in", 3600)),
            "refreshed_at": now,
            "scope": body.get("scope") or (previous or {}).get("scope", ""),
            "region": self.region,
            "logged_in_at": (previous or {}).get("logged_in_at", now),
        }
        if not rec["refresh_token"]:
            raise AuthError("brak refresh_token w odpowiedzi i w poprzednim tokenie")
        return rec

    def status(self) -> dict:
        tok = self.store.load_token()
        if not tok:
            return {"logged_in": False}
        return {
            "logged_in": not tok.get("login_required", False),
            "login_required": bool(tok.get("login_required")),
            "login_required_reason": tok.get("login_required_reason"),
            "expires_at": tok.get("expires_at"),
            "refreshed_at": tok.get("refreshed_at"),
            "logged_in_at": tok.get("logged_in_at"),
            "scope": tok.get("scope", ""),
        }

    # ---- partner (one-time domain registration)

    def partner_token(self) -> str:
        if not (self.client_id and self.client_secret):
            raise AuthError("uzupełnij client_id i client_secret w konfiguracji dodatku")
        r = http_util.request("POST", TOKEN_URL, form={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": PARTNER_SCOPES,
            "audience": self.audience,
        })
        body = r.json() or {}
        if not r.ok or "access_token" not in body:
            raise AuthError(f"token partnera: HTTP {r.status} {body.get('error', '')} {r.text(200)}".strip())
        return body["access_token"]

    def register_partner(self, domain: str) -> dict:
        domain = normalize_domain(domain)
        if not domain:
            raise AuthError("uzupełnij domain (np. suc.twojadomena.pl) w konfiguracji dodatku")
        tok = self.partner_token()
        r = http_util.request("POST", self.audience + "/api/1/partner_accounts",
                              json_body={"domain": domain},
                              headers={"Authorization": f"Bearer {tok}"})
        body = r.json()
        if not r.ok:
            raise AuthError(f"rejestracja domeny {domain}: HTTP {r.status} {r.text(300)}")
        log.info("zarejestrowano domenę partnera %s w regionie %s", domain, self.region)
        return {"domain": domain, "region": self.region, "response": body}

    def check_partner(self, domain: str) -> dict:
        domain = normalize_domain(domain)
        tok = self.partner_token()
        r = http_util.request("GET", self.audience + "/api/1/partner_accounts/public_key",
                              params={"domain": domain},
                              headers={"Authorization": f"Bearer {tok}"})
        if not r.ok:
            raise AuthError(f"sprawdzenie domeny {domain}: HTTP {r.status} {r.text(300)}")
        return {"domain": domain, "region": self.region, "response": r.json()}
