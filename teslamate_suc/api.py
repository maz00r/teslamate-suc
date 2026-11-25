"""Fleet API client for charging: history (paginated) and invoice PDF.

Every Fleet API request with a status below 500 is billable, so we count requests per
month in /data/usage.json and show the count in the panel.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

import http_util
from auth import AuthError, TeslaAuth

log = logging.getLogger("suc.api")

HISTORY_PATH = "/api/1/dx/charging/history"
INVOICE_PATH = "/api/1/dx/charging/invoice/{id}"
MAX_PAGES = 100


class ApiError(Exception):
    pass


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_page(body) -> tuple[list, dict]:
    """Returns (sessions, metadata). Handles both {"response": {...}} and a bare object,
    because the response shape is not documented. The probe will tell which one it is."""
    if not isinstance(body, dict):
        return [], {}
    inner = body.get("response", body)
    if isinstance(inner, list):
        return inner, {}
    if not isinstance(inner, dict):
        return [], {}
    data = inner.get("data")
    if data is None:
        data = inner.get("results", [])
    meta = {k: v for k, v in inner.items() if k not in ("data", "results")}
    return (data if isinstance(data, list) else []), meta


class Usage:
    """Counts billable requests per month (UTC)."""

    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "usage.json")
        self._lock = threading.Lock()

    def add(self, kind: str, status: int) -> None:
        if status >= 500:
            return
        month = time.strftime("%Y-%m", time.gmtime())
        with self._lock:
            data = self.read()
            m = data.setdefault(month, {})
            m[kind] = m.get(kind, 0) + 1
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)

    def read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}


class ChargingApi:
    def __init__(self, auth: TeslaAuth, usage: Usage):
        self.auth = auth
        self.base = auth.audience
        self.usage = usage

    def _get(self, kind: str, path: str, params: dict | None = None, accept: str = "application/json"):
        token = self.auth.access_token()
        for attempt in (1, 2):
            r = http_util.request("GET", self.base + path, params=params,
                                  headers={"Authorization": f"Bearer {token}", "Accept": accept},
                                  timeout=60)
            self.usage.add(kind, r.status)
            if r.status == 401 and attempt == 1:
                log.info("401 z Fleet API — odświeżam token i ponawiam raz")
                token = self.auth.force_refresh()
                continue
            return r
        return r

    def history_pages(self, *, start: datetime | None = None, end: datetime | None = None,
                      vin: str | None = None, page_size: int = 50) -> list[dict]:
        """Returns the raw pages: [{"page": n, "status": s, "body": ...}, ...].
        Stops on an empty page, a short page, a `hasMoreData` of false, or the MAX_PAGES guard."""
        pages = []
        page_no = 1
        while page_no <= MAX_PAGES:
            params = {
                "pageNo": page_no,
                "pageSize": page_size,
                "vin": vin,
                "startTime": iso_utc(start) if start else None,
                "endTime": iso_utc(end) if end else None,
                # sortBy/sortOrder are deliberately left out: their allowed values are undocumented,
                # and a wrong one can mean a 400 (which is still billed).
            }
            r = self._get("history", HISTORY_PATH, params)
            body = r.json()
            pages.append({"page": page_no, "status": r.status, "params": params,
                          "body": body if body is not None else r.text(2000)})
            if not r.ok:
                raise ApiError(f"charging/history strona {page_no}: HTTP {r.status} {r.text(300)}")
            sessions, meta = extract_page(body)
            log.info("charging/history strona %d: %d sesji", page_no, len(sessions))
            if not sessions or len(sessions) < page_size or meta.get("hasMoreData") is False:
                break
            page_no += 1
        return pages

    def history(self, **kw) -> list[dict]:
        out = []
        for p in self.history_pages(**kw):
            out.extend(extract_page(p["body"])[0])
        return out

    def invoice_pdf(self, invoice_id: str) -> bytes:
        r = self._get("invoice", INVOICE_PATH.format(id=invoice_id), accept="application/pdf")
        if not r.ok:
            raise ApiError(f"charging/invoice {invoice_id}: HTTP {r.status} {r.text(300)}")
        if not r.body.startswith(b"%PDF"):
            raise ApiError(f"charging/invoice {invoice_id}: odpowiedź to nie PDF ({r.content_type})")
        return r.body


# --------------------------------------------------------------------------- probe


def _shape(v, depth=0):
    """Data structure without values: field names and types. Safe to paste into chat."""
    if depth > 6:
        return "…"
    if isinstance(v, dict):
        return {k: _shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        return [_shape(v[0], depth + 1)] if v else []
    return type(v).__name__


def summarize(sessions: list[dict]) -> dict:
    """Summary of the probe: structure, fee types, units, time formats, example values
    of non-identifying fields."""
    fee_types, uoms, statuses, currencies, invoice_types = set(), set(), set(), set(), set()
    time_examples = {}
    for s in sessions:
        for f in s.get("fees") or []:
            fee_types.add(str(f.get("feeType")))
            uoms.add(str(f.get("uom")))
            statuses.add(str(f.get("status")))
            currencies.add(str(f.get("currencyCode")))
        for inv in s.get("invoices") or []:
            invoice_types.add(str(inv.get("invoiceType")))
        for k, v in s.items():
            if isinstance(v, str) and ("Time" in k or "Date" in k) and k not in time_examples:
                time_examples[k] = v
    first = sessions[0] if sessions else {}
    return {
        "sessions": len(sessions),
        "shape": _shape(first),
        "fee_types": sorted(fee_types),
        "fee_uoms": sorted(uoms),
        "fee_statuses": sorted(statuses),
        "currencies": sorted(currencies),
        "invoice_types": sorted(invoice_types),
        "time_examples": time_examples,
        "example_fees": (first.get("fees") or [])[:4],
        "example_invoices": [{k: v for k, v in i.items() if k != "fileName"}
                             for i in (first.get("invoices") or [])[:2]],
    }


def run_probe(api: ChargingApi, data_dir: str, days: int) -> dict:
    end = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(end.timestamp() - days * 86400, tz=timezone.utc)
    pages = api.history_pages(start=start, end=end)
    sessions = []
    for p in pages:
        sessions.extend(extract_page(p["body"])[0])
    raw = {"fetched_at": iso_utc(end), "days": days, "pages": pages}
    path = os.path.join(data_dir, "probe.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    summary = summarize(sessions)
    summary["pages"] = len(pages)
    summary["meta_first_page"] = extract_page(pages[0]["body"])[1] if pages else {}
    summary["fetched_at"] = raw["fetched_at"]
    summary["days"] = days
    with open(os.path.join(data_dir, "probe_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    return summary


__all__ = ["ApiError", "AuthError", "ChargingApi", "Usage", "extract_page", "run_probe", "summarize"]
