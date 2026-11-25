"""TeslaMate database: our `suc` schema and the only write into TeslaMate (`charging_processes.cost`).

Rules:
- `SET TIME ZONE 'UTC'`: TeslaMate keeps `timestamp` without a zone, in UTC.
- No foreign keys to TeslaMate tables: we never block its migrations or deletes.
- We write `cost` with an expected-old-value condition (`IS NOT DISTINCT FROM`) in the same
  transaction as the undo log (`suc.write_log`).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from matcher import Allocation, Process
from parse import Invoice

log = logging.getLogger("suc.db")

SCHEMA_VERSION = 1

DDL = """
CREATE SCHEMA IF NOT EXISTS suc;

CREATE TABLE IF NOT EXISTS suc.meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS suc.invoice (
    session_id      text PRIMARY KEY,
    vin             text NOT NULL,
    site_name       text,
    country         text,
    start_utc       timestamp NOT NULL,
    stop_utc        timestamp NOT NULL,
    unlatch_utc     timestamp,
    grid_kwh        numeric(10,4) NOT NULL,
    charging_fee    numeric(10,2) NOT NULL,
    congestion_fee  numeric(10,2) NOT NULL,
    other_fee       numeric(10,2) NOT NULL,
    total           numeric(10,2) NOT NULL,
    net_total       numeric(10,2) NOT NULL,
    currency        text,
    paid            boolean NOT NULL,
    pricing_types   text[] NOT NULL,
    rates           double precision[] NOT NULL,
    invoice_ids     text[] NOT NULL,
    fees_hash       text NOT NULL,
    raw             jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'new',
    status_detail   text,
    first_seen      timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    updated_at      timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);
CREATE INDEX IF NOT EXISTS invoice_vin_start ON suc.invoice (vin, start_utc);

-- Matching result. Rebuilt for every group on every pass (also in dry-run: it does not touch TeslaMate).
CREATE TABLE IF NOT EXISTS suc.allocation (
    session_id           text NOT NULL REFERENCES suc.invoice(session_id) ON DELETE CASCADE,
    charging_process_id  integer NOT NULL,
    match_kind           text NOT NULL,
    energy_kwh           numeric(10,4) NOT NULL,
    cost                 numeric(10,2) NOT NULL,
    charging_fee         numeric(10,2) NOT NULL,
    congestion_fee       numeric(10,2) NOT NULL,
    updated_at           timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    PRIMARY KEY (session_id, charging_process_id)
);
CREATE INDEX IF NOT EXISTS allocation_cp ON suc.allocation (charging_process_id);

-- Proof that the value in charging_processes.cost was written by us.
CREATE TABLE IF NOT EXISTS suc.cost_written (
    charging_process_id  integer PRIMARY KEY,
    cost                 numeric(6,2) NOT NULL,
    batch_id             uuid NOT NULL,
    written_at           timestamp NOT NULL
);

-- Processes whose write was undone by hand: sync skips them (otherwise it would write the same value again
-- 6 h later). Back into sync: DELETE FROM suc.cost_excluded WHERE charging_process_id = …
CREATE TABLE IF NOT EXISTS suc.cost_excluded (
    charging_process_id  integer PRIMARY KEY,
    batch_id             uuid NOT NULL,
    excluded_at          timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')
);

-- Undo log: every cost change, written BEFORE the UPDATE, in the same transaction.
CREATE TABLE IF NOT EXISTS suc.write_log (
    id                   bigserial PRIMARY KEY,
    batch_id             uuid NOT NULL,
    charging_process_id  integer NOT NULL,
    cost_before          numeric(6,2),
    cost_after           numeric(6,2),
    reason               text NOT NULL,
    at                   timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    undone_at            timestamp
);
CREATE INDEX IF NOT EXISTS write_log_batch ON suc.write_log (batch_id);

CREATE OR REPLACE VIEW suc.v_charges AS
SELECT cp.id                                    AS charging_process_id,
       cp.car_id,
       cp.start_date,
       cp.end_date,
       cp.charge_energy_added,
       cp.cost                                  AS teslamate_cost,
       sum(a.energy_kwh)                        AS grid_kwh,
       sum(a.cost)                              AS invoice_cost,
       sum(a.charging_fee)                      AS charging_fee,
       sum(a.congestion_fee)                    AS idle_fee,
       sum(a.energy_kwh) - cp.charge_energy_added AS loss_kwh,
       CASE WHEN sum(a.energy_kwh) > 0
            THEN 100 * (sum(a.energy_kwh) - cp.charge_energy_added) / sum(a.energy_kwh) END AS loss_pct,
       CASE WHEN sum(a.energy_kwh) > 0 AND sum(a.charging_fee) > 0
            THEN sum(a.charging_fee) / sum(a.energy_kwh) END AS price_per_kwh,
       min(i.site_name)                         AS site_name,
       min(i.currency)                          AS currency,
       bool_and(i.paid)                         AS paid,
       bool_and(i.total = 0)                    AS free,
       min(a.match_kind)                        AS match_kind,
       array_agg(i.session_id ORDER BY i.start_utc) AS session_ids,
       (cw.charging_process_id IS NOT NULL)     AS cost_written_by_suc
FROM suc.allocation a
JOIN suc.invoice i USING (session_id)
JOIN public.charging_processes cp ON cp.id = a.charging_process_id
LEFT JOIN suc.cost_written cw ON cw.charging_process_id = cp.id
GROUP BY cp.id, cw.charging_process_id;
"""


def connect(opts: dict) -> psycopg.Connection:
    conn = psycopg.connect(
        host=opts["db_host"], port=int(opts.get("db_port") or 5432), dbname=opts["db_name"],
        user=opts["db_user"], password=opts.get("db_password") or None,
        connect_timeout=10, application_name="teslamate-suc", row_factory=dict_row,
        # autocommit: reads do not hold a transaction open, and every `with conn.transaction()`
        # is a real BEGIN/COMMIT (without autocommit it would only be a SAVEPOINT in an uncommitted transaction).
        autocommit=True,
    )
    conn.execute("SET TIME ZONE 'UTC'")
    return conn


def migrate(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('teslamate-suc'))")
        conn.execute(DDL)
        conn.execute("INSERT INTO suc.meta (key, value) VALUES ('schema_version', %s) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (str(SCHEMA_VERSION),))


# --------------------------------------------------------------------------- invoices


def upsert_invoices(conn, invoices: list[Invoice]) -> dict:
    """Returns {"new": n, "changed": n, "same": n}. A change = a different fees_hash (invoice correction)."""
    stats = {"new": 0, "changed": 0, "same": 0}
    with conn.transaction():
        for i in invoices:
            row = conn.execute(
                """
                INSERT INTO suc.invoice (session_id, vin, site_name, country, start_utc, stop_utc, unlatch_utc,
                    grid_kwh, charging_fee, congestion_fee, other_fee, total, net_total, currency, paid,
                    pricing_types, rates, invoice_ids, fees_hash, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (session_id) DO UPDATE SET
                    vin = excluded.vin, site_name = excluded.site_name, country = excluded.country,
                    start_utc = excluded.start_utc, stop_utc = excluded.stop_utc, unlatch_utc = excluded.unlatch_utc,
                    grid_kwh = excluded.grid_kwh, charging_fee = excluded.charging_fee,
                    congestion_fee = excluded.congestion_fee, other_fee = excluded.other_fee,
                    total = excluded.total, net_total = excluded.net_total, currency = excluded.currency,
                    paid = excluded.paid, pricing_types = excluded.pricing_types, rates = excluded.rates,
                    invoice_ids = excluded.invoice_ids, fees_hash = excluded.fees_hash, raw = excluded.raw,
                    updated_at = now() AT TIME ZONE 'UTC'
                  WHERE (suc.invoice.fees_hash, suc.invoice.paid, suc.invoice.vin, suc.invoice.site_name,
                         suc.invoice.start_utc, suc.invoice.stop_utc, suc.invoice.unlatch_utc, suc.invoice.invoice_ids)
                        IS DISTINCT FROM
                        (excluded.fees_hash, excluded.paid, excluded.vin, excluded.site_name,
                         excluded.start_utc, excluded.stop_utc, excluded.unlatch_utc, excluded.invoice_ids)
                RETURNING (xmax = 0) AS inserted
                """,
                (i.session_id, i.vin, i.site_name, i.country, i.start, i.stop, i.unlatch, i.grid_kwh,
                 i.charging_fee, i.congestion_fee, i.other_fee, i.total, i.net_total, i.currency, i.paid,
                 i.pricing_types, i.rates, i.invoice_ids, i.fees_hash, json.dumps(i.raw)),
            ).fetchone()
            if row is None:
                stats["same"] += 1
            elif row["inserted"]:
                stats["new"] += 1
            else:
                stats["changed"] += 1
    return stats


def load_invoices(conn, start: datetime, end: datetime) -> list[Invoice]:
    rows = conn.execute(
        "SELECT * FROM suc.invoice WHERE start_utc < %s AND stop_utc > %s ORDER BY start_utc",
        (end, start)).fetchall()
    return [Invoice(
        session_id=r["session_id"], vin=r["vin"], site_name=r["site_name"], country=r["country"],
        start=r["start_utc"], stop=r["stop_utc"], unlatch=r["unlatch_utc"], grid_kwh=r["grid_kwh"],
        charging_fee=r["charging_fee"], congestion_fee=r["congestion_fee"], other_fee=r["other_fee"],
        total=r["total"], net_total=r["net_total"], currency=r["currency"], paid=r["paid"],
        pricing_types=list(r["pricing_types"]), rates=list(r["rates"]), invoice_ids=list(r["invoice_ids"]),
        fees_hash=r["fees_hash"], raw=r["raw"]) for r in rows]


def set_invoice_status(conn, statuses: dict[str, tuple[str, str | None]]) -> None:
    with conn.transaction():
        for sid, (st, detail) in statuses.items():
            conn.execute("UPDATE suc.invoice SET status = %s, status_detail = %s WHERE session_id = %s "
                         "AND (status IS DISTINCT FROM %s OR status_detail IS DISTINCT FROM %s)",
                         (st, detail, sid, st, detail))


# --------------------------------------------------------------------------- TeslaMate


def load_dc_processes(conn, start: datetime, end: datetime) -> list[Process]:
    """Finished processes with at least one DC sample (fast_charger_present), overlapping the range."""
    rows = conn.execute(
        """
        SELECT cp.id, cp.car_id, c.vin, cp.start_date, cp.end_date, cp.charge_energy_added, cp.cost,
               (SELECT ch.fast_charger_brand FROM charges ch
                 WHERE ch.charging_process_id = cp.id AND ch.fast_charger_brand IS NOT NULL
                 ORDER BY ch.date DESC LIMIT 1) AS brand
          FROM charging_processes cp
          JOIN cars c ON c.id = cp.car_id
         WHERE cp.end_date IS NOT NULL
           AND cp.start_date < %s AND cp.end_date > %s
           AND c.vin IS NOT NULL
           AND EXISTS (SELECT 1 FROM charges ch
                        WHERE ch.charging_process_id = cp.id AND ch.fast_charger_present)
         ORDER BY cp.start_date
        """, (end, start)).fetchall()
    return [Process(r["id"], r["car_id"], r["vin"], r["start_date"], r["end_date"],
                    r["charge_energy_added"], r["cost"], r["brand"]) for r in rows]


def replace_allocations(conn, session_ids: list[str], allocs: list[Allocation], kinds: dict[str, str]) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM suc.allocation WHERE session_id = ANY(%s)", (session_ids,))
        for a in allocs:
            conn.execute(
                "INSERT INTO suc.allocation (session_id, charging_process_id, match_kind, energy_kwh, cost, "
                "charging_fee, congestion_fee) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (a.session_id, a.process_id, kinds[a.session_id], a.energy_kwh, a.cost,
                 a.charging_fee, a.congestion_fee))


def written_costs(conn, process_ids: list[int]) -> dict[int, Decimal]:
    rows = conn.execute("SELECT charging_process_id, cost FROM suc.cost_written WHERE charging_process_id = ANY(%s)",
                        (process_ids,)).fetchall()
    return {r["charging_process_id"]: r["cost"] for r in rows}


def write_cost(conn, *, batch_id: uuid.UUID, process_id: int, expected: Decimal | None,
               new: Decimal, reason: str, overwrite: bool) -> bool:
    """One process = one transaction. Returns False when the cost changed under us."""
    with conn.transaction():
        row = conn.execute("SELECT cost FROM charging_processes WHERE id = %s FOR UPDATE", (process_id,)).fetchone()
        if row is None:
            return False
        if not overwrite and row["cost"] != expected and not (row["cost"] is None and expected is None):
            return False
        conn.execute("INSERT INTO suc.write_log (batch_id, charging_process_id, cost_before, cost_after, reason) "
                     "VALUES (%s,%s,%s,%s,%s)", (batch_id, process_id, row["cost"], new, reason))
        cur = conn.execute("UPDATE charging_processes SET cost = %s WHERE id = %s AND cost IS NOT DISTINCT FROM %s",
                           (new, process_id, row["cost"]))
        if cur.rowcount != 1:
            raise RuntimeError(f"UPDATE charging_processes {process_id}: rowcount {cur.rowcount}")
        conn.execute(
            "INSERT INTO suc.cost_written (charging_process_id, cost, batch_id, written_at) "
            "VALUES (%s,%s,%s, now() AT TIME ZONE 'UTC') ON CONFLICT (charging_process_id) DO UPDATE SET "
            "cost = excluded.cost, batch_id = excluded.batch_id, written_at = excluded.written_at",
            (process_id, new, batch_id))
    return True


def undo_batch(conn, batch_id: str) -> dict:
    """Restores cost_before, but only where the cost is still what we wrote (nobody changed it since).
    After the undo the process goes to suc.cost_excluded, otherwise sync would write the same value again.
    If cost_before was itself our earlier write (undoing a correction), cost_written goes back to that
    value, so ownership is not lost."""
    restored, skipped = 0, 0
    with conn.transaction():
        rows = conn.execute(
            "SELECT DISTINCT ON (charging_process_id) id, charging_process_id, cost_before, cost_after "
            "FROM suc.write_log WHERE batch_id = %s AND undone_at IS NULL "
            "ORDER BY charging_process_id, id ASC", (batch_id,)).fetchall()
        for r in rows:
            pid = r["charging_process_id"]
            cur = conn.execute("UPDATE charging_processes SET cost = %s WHERE id = %s AND cost IS NOT DISTINCT FROM %s",
                               (r["cost_before"], pid, r["cost_after"]))
            if cur.rowcount != 1:
                skipped += 1
                continue
            restored += 1
            prev = conn.execute(
                "SELECT batch_id, cost_after, at FROM suc.write_log WHERE charging_process_id = %s AND id < %s "
                "AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (pid, r["id"])).fetchone()
            if prev is not None and r["cost_before"] is not None and prev["cost_after"] == r["cost_before"]:
                conn.execute("UPDATE suc.cost_written SET cost = %s, batch_id = %s, written_at = %s "
                             "WHERE charging_process_id = %s", (prev["cost_after"], prev["batch_id"], prev["at"], pid))
            else:
                conn.execute("DELETE FROM suc.cost_written WHERE charging_process_id = %s", (pid,))
            conn.execute("INSERT INTO suc.cost_excluded (charging_process_id, batch_id) VALUES (%s, %s) "
                         "ON CONFLICT (charging_process_id) DO UPDATE SET batch_id = excluded.batch_id, "
                         "excluded_at = now() AT TIME ZONE 'UTC'", (pid, batch_id))
        conn.execute("UPDATE suc.write_log SET undone_at = now() AT TIME ZONE 'UTC' WHERE batch_id = %s", (batch_id,))
    return {"batch_id": batch_id, "restored": restored, "skipped_changed_since": skipped}


def excluded_processes(conn, process_ids: list[int]) -> set[int]:
    rows = conn.execute("SELECT charging_process_id FROM suc.cost_excluded WHERE charging_process_id = ANY(%s)",
                        (process_ids,)).fetchall()
    return {r["charging_process_id"] for r in rows}


def last_batches(conn, limit: int = 5) -> list[dict]:
    return conn.execute(
        "SELECT batch_id::text, min(at) AS at, count(*) AS changes, bool_or(undone_at IS NOT NULL) AS undone "
        "FROM suc.write_log GROUP BY batch_id ORDER BY min(at) DESC LIMIT %s", (limit,)).fetchall()


def cleanup_orphans(conn) -> int:
    with conn.transaction():
        n = conn.execute("DELETE FROM suc.allocation a WHERE NOT EXISTS "
                         "(SELECT 1 FROM charging_processes cp WHERE cp.id = a.charging_process_id)").rowcount
        for t in ("cost_written", "cost_excluded"):
            n += conn.execute(f"DELETE FROM suc.{t} w WHERE NOT EXISTS "
                              "(SELECT 1 FROM charging_processes cp WHERE cp.id = w.charging_process_id)").rowcount
    return n


def range_pad(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    return start - timedelta(days=1), end + timedelta(days=1)
