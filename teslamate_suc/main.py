"""Add-on entry point: loads options, starts the Ingress panel and the token keepalive."""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from api import ChargingApi, Usage
from auth import AuthError, LoginRequired, TeslaAuth, TokenStore
from panel import App, serve

DATA_DIR = os.environ.get("SUC_DATA_DIR", "/data")
DEFAULTS = {
    "client_id": "",
    "client_secret": "",
    "region": "eu",
    "domain": "",
    "redirect_uri": "",
    "log_level": "info",
}
KEEPALIVE_EVERY_S = 3600
# The refresh token dies after 3 months without use. Rotate it weekly, auth calls are free.
KEEPALIVE_MAX_AGE_S = 7 * 86400

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


def main() -> None:
    opts = load_options(os.path.join(DATA_DIR, "options.json"))
    logging.basicConfig(
        level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.Formatter.converter = time.gmtime  # UTC timestamps in the log
    auth = TeslaAuth(
        client_id=opts["client_id"],
        client_secret=opts["client_secret"],
        region=opts["region"],
        redirect_uri=opts["redirect_uri"],
        store=TokenStore(DATA_DIR),
    )
    usage = Usage(DATA_DIR)
    app = App(opts=opts, auth=auth, api=ChargingApi(auth, usage), usage=usage, data_dir=DATA_DIR)
    stop = threading.Event()
    threading.Thread(target=keepalive, args=(auth, stop), daemon=True, name="keepalive").start()
    srv = serve(app, int(os.environ.get("SUC_PANEL_PORT", "8099")))
    log.info("panel na porcie %s, region %s, zalogowano: %s",
             srv.server_address[1], opts["region"], auth.status().get("logged_in"))
    try:
        srv.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
