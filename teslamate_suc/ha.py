"""Status in Home Assistant through the Supervisor (homeassistant_api: true). Errors never stop the sync."""

from __future__ import annotations

import logging
import os

import http_util

log = logging.getLogger("suc.ha")

BASE = "http://supervisor/core/api"


def _token():
    return os.environ.get("SUPERVISOR_TOKEN")


def _post(path: str, body: dict) -> None:
    tok = _token()
    if not tok:
        return
    r = http_util.request("POST", BASE + path, json_body=body,
                          headers={"Authorization": f"Bearer {tok}"}, timeout=10)
    if not r.ok:
        log.warning("HA %s: HTTP %s %s", path, r.status, r.text(200))


def set_state(entity: str, state, attrs: dict) -> None:
    try:
        _post(f"/states/{entity}", {"state": state, "attributes": attrs})
    except Exception as e:  # noqa: BLE001
        log.warning("HA %s: %s", entity, e)


def notify(title: str, message: str, nid: str = "teslamate_suc") -> None:
    try:
        _post("/services/persistent_notification/create",
              {"title": title, "message": message, "notification_id": nid})
    except Exception as e:  # noqa: BLE001
        log.warning("HA notify: %s", e)


def publish(summary: dict, token_status: dict) -> None:
    dev = {"friendly_name": None, "icon": "mdi:ev-station"}
    if summary:
        set_state("sensor.suc_ostatnia_synchronizacja", summary.get("finished_at") or "unknown",
                  dict(dev, friendly_name="SuC: ostatnia synchronizacja", device_class="timestamp",
                       dry_run=summary.get("dry_run"), error=summary.get("error")))
        for key, name in (("unmatched", "niedopasowane faktury"), ("review", "do przeglądu"),
                          ("written", "zapisane koszty"), ("to_write", "koszty do zapisu"),
                          ("lonely_tesla", "ładowania Tesla bez faktury"),
                          ("stale", "nasze koszty bez dopasowania")):
            set_state(f"sensor.suc_{key}", summary.get(key, 0),
                      dict(dev, friendly_name=f"SuC: {name}", state_class="measurement"))
    tok = "ok" if token_status.get("logged_in") else (
        "wymaga logowania" if token_status.get("login_required") else "brak")
    set_state("sensor.suc_token", tok, dict(dev, friendly_name="SuC: token Tesli",
                                            expires_at=token_status.get("expires_at")))
