"""Add-on entry point: options, Ingress panel, sync loop, token keepalive."""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import ha
from api import ChargingApi, Usage
from auth import AuthError, LoginRequired, TeslaAuth, TokenStore
from panel import App, serve
from sync import Syncer, summary

DATA_DIR = os.environ.get("SUC_DATA_DIR", "/data")
DEFAULTS = {
    "client_id": "",
    "client_secret": "",
    "region": "eu",
    "domain": "",
    "redirect_uri": "",
    "db_host": "",
    "db_port": 5432,
    "db_name": "teslamate",
    "db_user": "",
    "db_password": "",
    "sync_interval_hours": 6,
    "lookback_days": 45,
    "match_tolerance_min": 10,
    "dry_run": True,
    "overwrite_cost": False,
    "log_level": "info",
}
KEEPALIVE_EVERY_S = 3600
KEEPALIVE_MAX_AGE_S = 7 * 86400  # the refresh token dies after 3 months without use

log = logging.getLogger("suc")


def load_options(path: str) -> dict:
    opts = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    for k, v in raw.items():
        if v is not None:
            opts[k] = v.strip() if isinstance(v, str) else v
    return opts


def db_configured(opts: dict) -> bool:
    return bool(opts.get("db_host") and opts.get("db_user") and opts.get("db_name"))


def keepalive(auth: TeslaAuth, stop: threading.Event) -> None:
    while not stop.wait(KEEPALIVE_EVERY_S):
        try:
            if auth.refresh_if_older_than(KEEPALIVE_MAX_AGE_S):
                log.info("keepalive: zrotowano refresh token")
        except LoginRequired as e:
            log.warning("keepalive: %s", e)
        except AuthError as e:
            log.error("keepalive: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("keepalive: nieoczekiwany błąd")


def sync_loop(app: App, stop: threading.Event) -> None:
    interval = max(1, int(app.opts.get("sync_interval_hours") or 6)) * 3600
    stop.wait(60)  # let the panel and the network come up
    notified = False
    while not stop.is_set():
        if db_configured(app.opts) and app.auth.status().get("logged_in"):
            try:
                app.syncer.run()
                notified = False
            except LoginRequired as e:
                log.warning("sync: %s", e)
                if not notified:
                    ha.notify("TeslaMate SuC: zaloguj się ponownie",
                              "Tesla odrzuciła token. Otwórz panel SuC i kliknij „Zaloguj do Tesli”.")
                    notified = True
            except Exception as e:  # noqa: BLE001
                log.error("sync: %s", e)
        ha.publish(summary(app.syncer.last_report()), app.auth.status())
        stop.wait(interval)


def main() -> None:
    opts = load_options(os.path.join(DATA_DIR, "options.json"))
    logging.basicConfig(
        level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.Formatter.converter = time.gmtime
    auth = TeslaAuth(client_id=opts["client_id"], client_secret=opts["client_secret"], region=opts["region"],
                     redirect_uri=opts["redirect_uri"], store=TokenStore(DATA_DIR))
    usage = Usage(DATA_DIR)
    api = ChargingApi(auth, usage)
    app = App(opts=opts, auth=auth, api=api, usage=usage, data_dir=DATA_DIR,
              syncer=Syncer(api=api, opts=opts, data_dir=DATA_DIR))
    stop = threading.Event()
    threading.Thread(target=keepalive, args=(auth, stop), daemon=True, name="keepalive").start()
    threading.Thread(target=sync_loop, args=(app, stop), daemon=True, name="sync").start()
    srv = serve(app, int(os.environ.get("SUC_PANEL_PORT", "8099")))
    log.info("panel na porcie %s, region %s, zalogowano: %s, baza: %s, dry_run: %s",
             srv.server_address[1], opts["region"], auth.status().get("logged_in"),
             "tak" if db_configured(opts) else "NIE SKONFIGUROWANA", opts.get("dry_run"))
    try:
        srv.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
