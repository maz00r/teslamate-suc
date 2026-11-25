"""One sync pass: Tesla → suc.invoice → matching → cost plan → (write | dry-run report)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import db
import matcher
import parse

log = logging.getLogger("suc.sync")

# Tesla processes without an invoice: report them only after this long (the invoice can show up late).
LONELY_GRACE = timedelta(days=2)


def _f(x):
    return None if x is None else float(x)


def _iso(d):
    return d.isoformat(timespec="seconds") if d else None


class Syncer:
    def __init__(self, *, api, opts: dict, data_dir: str, connect=db.connect, now=lambda: datetime.now(timezone.utc)):
        self.api = api
        self.opts = opts
        self.data_dir = data_dir
        self.connect = connect
        self.now = now
        self.lock = threading.Lock()

    @property
    def report_path(self):
        return os.path.join(self.data_dir, "last_report.json")

    def last_report(self):
        try:
            with open(self.report_path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def run(self, *, start: datetime | None = None, end: datetime | None = None,
            dry_run: bool | None = None, fetch: bool = True) -> dict:
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("synchronizacja już trwa")
        try:
            return self._run(start, end, dry_run, fetch)
        finally:
            self.lock.release()

    def _run(self, start, end, dry_run, fetch) -> dict:
        t0 = time.monotonic()
        o = self.opts
        dry = bool(o.get("dry_run", True)) if dry_run is None else bool(dry_run)
        if not dry and o.get("dry_run", True):
            dry = True  # a real write only when the dry_run option is off
        end = end or self.now()
        start = start or end - timedelta(days=int(o.get("lookback_days") or 45))
        report = {"started_at": _iso(self.now()), "range": [_iso(start), _iso(end)], "dry_run": dry,
                  "fetched": 0, "parse_errors": [], "invoices": {}, "groups": {}, "plan": [],
                  "unmatched": [], "lonely_tesla": [], "review": [], "written": 0, "skipped": 0,
                  "batch_id": None, "error": None}

        conn = None
        try:
            sessions = []
            if fetch:
                sessions = self.api.history(start=start, end=end)
                report["fetched"] = len(sessions)
            invoices, errors = parse.parse_all(sessions)
            report["parse_errors"] = errors

            conn = self.connect(o)
            db.migrate(conn)
            report["invoices"] = db.upsert_invoices(conn, invoices)
            s_naive = start.astimezone(timezone.utc).replace(tzinfo=None)
            e_naive = end.astimezone(timezone.utc).replace(tzinfo=None)
            ps, pe = db.range_pad(s_naive, e_naive)
            all_invs = db.load_invoices(conn, ps, pe)
            procs = db.load_dc_processes(conn, ps, pe)
            groups, unmatched, lonely = matcher.build_groups(all_invs, procs, float(o.get("match_tolerance_min") or 10))

            allocs, kinds, statuses = [], {}, {}
            for g in groups:
                for i in g.invoices:
                    kinds[i.session_id] = g.kind
                    statuses[i.session_id] = (("review" if g.kind == "review" else ("free" if i.is_free else "matched")),
                                              f"{g.kind} → " + ",".join(str(p.id) for p in g.processes))
                report["groups"][g.kind] = report["groups"].get(g.kind, 0) + 1
                if g.kind == "review":
                    report["review"].append({"sessions": [i.session_id for i in g.invoices],
                                             "processes": [p.id for p in g.processes],
                                             "start": _iso(g.invoices[0].start), "site": g.invoices[0].site_name})
                allocs.extend(matcher.allocate(g))
            for i in unmatched:
                statuses[i.session_id] = ("unmatched", None)
                report["unmatched"].append({"session": i.session_id, "start": _iso(i.start),
                                            "site": i.site_name, "total": _f(i.total)})
            cutoff = e_naive - LONELY_GRACE
            report["lonely_tesla"] = [{"process": p.id, "start": _iso(p.start), "kwh": _f(p.energy_added)}
                                      for p in lonely if (p.brand or "").lower() == "tesla" and p.end < cutoff
                                      and s_naive <= p.start]

            db.replace_allocations(conn, [i.session_id for i in all_invs], allocs, kinds)
            db.set_invoice_status(conn, statuses)
            db.cleanup_orphans(conn)

            targets = matcher.costs_by_process(allocs)
            proc_by = {p.id: p for p in procs}
            ours = db.written_costs(conn, list(targets))
            overwrite = bool(o.get("overwrite_cost", False))
            batch = uuid.uuid4()
            sites = {}
            for a in allocs:
                sites.setdefault(a.process_id, next((i.site_name for i in all_invs if i.session_id == a.session_id), None))
            for pid in sorted(targets, key=lambda k: proc_by[k].start):
                p, target = proc_by[pid], targets[pid]
                action, reason = matcher.decide(target, p.cost, ours.get(pid), overwrite)
                row = {"process": pid, "start": _iso(p.start), "site": sites.get(pid),
                       "kwh_added": _f(p.energy_added), "cost_before": _f(p.cost), "cost_after": _f(target),
                       "action": action, "reason": reason}
                if action == "write" and not dry:
                    ok = db.write_cost(conn, batch_id=batch, process_id=pid, expected=p.cost, new=target,
                                       reason=reason, overwrite=overwrite)
                    if ok:
                        report["written"] += 1
                        report["batch_id"] = str(batch)
                    else:
                        row["action"], row["reason"] = "skip", "koszt zmienił się w trakcie"
                        report["skipped"] += 1
                elif action == "skip":
                    report["skipped"] += 1
                report["plan"].append(row)
        except Exception as e:  # noqa: BLE001 — the report must reach the panel and the sensors
            report["error"] = f"{type(e).__name__}: {e}"
            log.exception("synchronizacja przerwana")
            raise
        finally:
            if conn is not None:
                conn.close()
            report["duration_s"] = round(time.monotonic() - t0, 2)
            report["finished_at"] = _iso(self.now())
            self._save(report)
        w = sum(1 for r in report["plan"] if r["action"] == "write")
        log.info("synchronizacja %s: pobrano %d, grupy %s, do zapisu %d, zapisano %d, pominięto %d, "
                 "niedopasowane %d, do przeglądu %d",
                 "DRY-RUN" if dry else "ZAPIS", report["fetched"], report["groups"], w, report["written"],
                 report["skipped"], len(report["unmatched"]), len(report["review"]))
        return report

    def _save(self, report: dict) -> None:
        tmp = self.report_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, self.report_path)


def summary(report: dict | None) -> dict:
    if not report:
        return {}
    plan = report.get("plan", [])
    return {
        "finished_at": report.get("finished_at"),
        "dry_run": report.get("dry_run"),
        "to_write": sum(1 for r in plan if r["action"] == "write"),
        "written": report.get("written", 0),
        "skipped": report.get("skipped", 0),
        "unmatched": len(report.get("unmatched", [])),
        "review": len(report.get("review", [])),
        "lonely_tesla": len(report.get("lonely_tesla", [])),
        "error": report.get("error"),
    }


__all__ = ["Syncer", "summary", "Decimal"]
