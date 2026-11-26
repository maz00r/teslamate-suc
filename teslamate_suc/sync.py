"""One sync pass of one source (Tesla | Greenway) → suc.invoice → matching → cost plan → (write | dry-run report)."""

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
import greenway as gw
import matcher
import parse

log = logging.getLogger("suc.sync")

# Tesla processes without an invoice: report them only after this long (the invoice can show up late).
LONELY_GRACE = timedelta(days=2)


def _f(x):
    return None if x is None else float(x)


def _iso(d):
    return d.isoformat(timespec="seconds") if d else None


PROVIDERS = ("tesla", "greenway")


class Syncer:
    def __init__(self, *, api, opts: dict, data_dir: str, connect=db.connect, now=lambda: datetime.now(timezone.utc),
                 greenway=None):
        self.api = api
        self.greenway = greenway
        self.opts = opts
        self.data_dir = data_dir
        self.connect = connect
        self.now = now
        self.lock = threading.Lock()

    def report_path(self, provider: str = "tesla"):
        return os.path.join(self.data_dir, "last_report.json" if provider == "tesla" else f"last_report_{provider}.json")

    def last_report(self, provider: str = "tesla"):
        try:
            with open(self.report_path(provider), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return None

    def run(self, *, start: datetime | None = None, end: datetime | None = None,
            dry_run: bool | None = None, fetch: bool = True, provider: str = "tesla") -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"nieznane źródło {provider!r}")
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("synchronizacja już trwa")
        try:
            return self._run(start, end, dry_run, fetch, provider)
        finally:
            self.lock.release()

    def _fetch(self, provider, start, end):
        if provider == "tesla":
            return parse.parse_all(self.api.history(start=start, end=end))
        if self.greenway is None:
            raise RuntimeError("Greenway nie jest skonfigurowany")
        raw = self.greenway.fetch_sessions(since=start - timedelta(days=1))
        invs, errs = gw.parse_all(raw)
        since = (start - timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        return [i for i in invs if i.stop >= since], errs

    def _run(self, start, end, dry_run, fetch, provider="tesla") -> dict:
        t0 = time.monotonic()
        o = self.opts
        dry = bool(o.get("dry_run", True)) if dry_run is None else bool(dry_run)
        if not dry and o.get("dry_run", True):
            dry = True  # a real write only when the dry_run option is off
        end = end or self.now()
        start = start or end - timedelta(days=int(o.get("lookback_days") or 45))
        report = {"provider": provider, "started_at": _iso(self.now()), "range": [_iso(start), _iso(end)], "dry_run": dry,
                  "fetched": 0, "parse_errors": [], "invoices": {}, "groups": {}, "plan": [],
                  "unmatched": [], "lonely_tesla": [], "review": [], "stale": [], "written": 0, "skipped": 0,
                  "batch_id": None, "error": None}

        conn = None
        try:
            invoices, errors = [], []
            if fetch:
                invoices, errors = self._fetch(provider, start, end)
                report["fetched"] = len(invoices) + len(errors)
                if provider == "greenway" and getattr(self.greenway, "truncated", False):
                    errors = errors + [f"Greenway: przerwano po {gw.MAX_PAGES} stronach — starsze sesje pominięte"]
            report["parse_errors"] = errors

            conn = self.connect(o)
            db.migrate(conn)
            report["invoices"] = db.upsert_invoices(conn, invoices)
            s_naive = start.astimezone(timezone.utc).replace(tzinfo=None)
            e_naive = end.astimezone(timezone.utc).replace(tzinfo=None)
            ps, pe = db.range_pad(s_naive, e_naive)
            all_invs = db.load_invoices(conn, ps, pe, provider)
            # Tesla: only DC processes and the same VIN. Greenway: AC and DC (the session says which), any car.
            procs = db.load_processes(conn, ps, pe, dc_only=(provider == "tesla"))
            # Processes already belonging to another source (or with a manual cost) are not matched at all:
            # otherwise suc.v_charges would add up two invoices for one charge.
            claimed = db.claimed_by_others(conn, [p.id for p in procs], provider)
            if provider == "greenway":  # a Greenway session never takes place at a Tesla Supercharger
                claimed |= {p.id for p in procs if (p.brand or "").lower() == "tesla"}
            report["claimed_elsewhere"] = len(claimed)
            procs = [p for p in procs if p.id not in claimed]
            groups, unmatched, lonely = matcher.build_groups(all_invs, procs, float(o.get("match_tolerance_min") or 10),
                                                             match_vin=(provider == "tesla"))

            # We load the range padded by a day, but act ONLY on groups that touch the core range.
            # A group at the edge of the padding can be cut in half (e.g. a session Tesla split at midnight)
            # and its partial sum would overwrite the correct cost from an earlier pass.
            def in_core(x) -> bool:
                return x.start < e_naive and x.end > s_naive
            groups = [g for g in groups if any(in_core(x) for x in g.invoices + g.processes)]
            unmatched = [i for i in unmatched if in_core(i)]
            core_procs = [p for p in procs if in_core(p)]

            allocs, kinds, statuses, touched = [], {}, {}, []
            for g in groups:
                for i in g.invoices:
                    kinds[i.session_id] = g.kind
                    touched.append(i.session_id)
                    statuses[i.session_id] = (("review" if g.kind == "review" else ("free" if i.is_free else "matched")),
                                              f"{g.kind} → " + ",".join(str(p.id) for p in g.processes))
                report["groups"][g.kind] = report["groups"].get(g.kind, 0) + 1
                if g.kind == "review":
                    report["review"].append({"sessions": [i.session_id for i in g.invoices],
                                             "processes": [p.id for p in g.processes],
                                             "start": _iso(g.invoices[0].start), "site": g.invoices[0].site_name})
                allocs.extend(matcher.allocate(g))
            for i in unmatched:
                touched.append(i.session_id)
                statuses[i.session_id] = ("unmatched", None)
                report["unmatched"].append({"session": i.session_id, "start": _iso(i.start),
                                            "site": i.site_name, "total": _f(i.total)})
            cutoff = e_naive - LONELY_GRACE
            report["lonely_tesla"] = [{"process": p.id, "start": _iso(p.start), "kwh": _f(p.energy_added)}
                                      for p in lonely if provider == "tesla" and (p.brand or "").lower() == "tesla"
                                      and p.end < cutoff and in_core(p)]

            db.replace_allocations(conn, touched, allocs, kinds)
            db.set_invoice_status(conn, statuses)
            db.cleanup_orphans(conn)

            targets = matcher.costs_by_process(allocs)
            proc_by = {p.id: p for p in procs}
            written = db.written_costs(conn, list(set(targets) | {p.id for p in core_procs}))
            # "Ours" = written by THIS source. A cost from another source (or manual) is someone else's and untouchable.
            ours = {pid: c for pid, (c, prov) in written.items() if prov == provider}
            foreign_src = {pid: prov for pid, (c, prov) in written.items() if prov != provider}
            excluded = db.excluded_processes(conn, list(targets))
            overwrite = bool(o.get("overwrite_cost", False))
            batch = uuid.uuid4()
            site_of = {i.session_id: i.site_name for i in all_invs}
            sites = {}
            for a in allocs:
                sites.setdefault(a.process_id, site_of.get(a.session_id))
            for pid in sorted(targets, key=lambda k: proc_by[k].start):
                p, target = proc_by[pid], targets[pid]
                if pid in foreign_src and p.cost == written[pid][0]:
                    src = foreign_src[pid]
                    action, reason = "skip", ("koszt ręczny — nie nadpisuję" if src == "manual"
                                              else f"koszt z innego źródła ({src})")
                elif pid in excluded and not overwrite:
                    action, reason = "skip", "zapis cofnięty ręcznie — wykluczone z synchronizacji"
                else:
                    action, reason = matcher.decide(target, p.cost, ours.get(pid), overwrite,
                                                    bool(o.get("zero_is_empty", True)))
                row = {"process": pid, "start": _iso(p.start), "site": sites.get(pid),
                       "kwh_added": _f(p.energy_added), "cost_before": _f(p.cost), "cost_after": _f(target),
                       "action": action, "reason": reason, "geofence": p.geofence,
                       "free_supercharging": p.free_supercharging}
                if action == "write" and not dry:
                    # The concurrency check is dropped ONLY for a deliberate overwrite of someone else's cost.
                    ok = db.write_cost(conn, batch_id=batch, process_id=pid, expected=p.cost, new=target,
                                       reason=reason, overwrite=(reason == matcher.REASON_OVERWRITE),
                                       provider=provider)
                    if ok:
                        report["written"] += 1
                        report["batch_id"] = str(batch)
                    else:
                        row["action"], row["reason"] = "skip", "koszt zmienił się w trakcie"
                        report["skipped"] += 1
                elif action == "skip":
                    report["skipped"] += 1
                report["plan"].append(row)
            # Our cost with no basis now (invoice gone, group went to review, TeslaMate merged processes).
            # We do not undo it on our own: we report it, and it can be undone from the panel.
            for p in core_procs:
                if p.id not in targets and p.id in ours and p.cost == ours[p.id]:
                    report["stale"].append({"process": p.id, "start": _iso(p.start), "cost": _f(p.cost)})
        except Exception as e:  # noqa: BLE001 — the report must reach the panel and the sensors
            report["error"] = f"{type(e).__name__}: {e}"
            log.exception("synchronizacja przerwana")
            raise
        finally:
            if conn is not None:
                conn.close()
            report["duration_s"] = round(time.monotonic() - t0, 2)
            report["finished_at"] = _iso(self.now())
            self._save(report, provider)
        w = sum(1 for r in report["plan"] if r["action"] == "write")
        log.info("[%s] synchronizacja %s: pobrano %d, grupy %s, do zapisu %d, zapisano %d, pominięto %d, "
                 "niedopasowane %d, do przeglądu %d",
                 provider, "DRY-RUN" if dry else "ZAPIS", report["fetched"], report["groups"], w, report["written"],
                 report["skipped"], len(report["unmatched"]), len(report["review"]))
        return report

    def _save(self, report: dict, provider: str = "tesla") -> None:
        path = self.report_path(provider)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, path)


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
        "stale": len(report.get("stale", [])),
        "error": report.get("error"),
    }


def summary_all(syncer: "Syncer") -> dict:
    """Sum over the sources, for the HA sensors. The time = the latest pass."""
    parts = [summary(syncer.last_report(p)) for p in PROVIDERS]
    parts = [x for x in parts if x]
    if not parts:
        return {}
    out = {k: sum(x.get(k) or 0 for x in parts)
           for k in ("to_write", "written", "skipped", "unmatched", "review", "lonely_tesla", "stale")}
    out["finished_at"] = max(x["finished_at"] for x in parts if x.get("finished_at"))
    out["dry_run"] = any(x.get("dry_run") for x in parts)
    out["error"] = "; ".join(x["error"] for x in parts if x.get("error")) or None
    return out


__all__ = ["Syncer", "summary", "summary_all", "PROVIDERS", "Decimal"]
