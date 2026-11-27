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
import math
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from matcher import Allocation, Process
from parse import Invoice

log = logging.getLogger("suc.db")

SCHEMA_VERSION = 3

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
-- v2: several sources of costs. Session ids carry a prefix (gw-…, manual-…), so the PK stays the same.
ALTER TABLE suc.invoice ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'tesla';
ALTER TABLE suc.invoice ADD COLUMN IF NOT EXISTS charge_kind text;
CREATE INDEX IF NOT EXISTS invoice_provider_start ON suc.invoice (provider, start_utc);

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
-- v2: who owns the cost (tesla | greenway | manual). A sync updates only its own costs.
ALTER TABLE suc.cost_written ADD COLUMN IF NOT EXISTS provider text NOT NULL DEFAULT 'tesla';

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
ALTER TABLE suc.write_log ADD COLUMN IF NOT EXISTS provider text;
UPDATE suc.write_log SET provider = 'tesla' WHERE provider IS NULL;  -- writes from v1 = Tesla

-- Charging processes TeslaMate never closed (end_date NULL), closed by us the way TeslaMate closes them.
-- `before`/`after` = the charging_processes columns; undo restores `before` where nothing changed since.
CREATE TABLE IF NOT EXISTS suc.closed_process (
    charging_process_id  integer PRIMARY KEY,
    batch_id             uuid NOT NULL,
    before               jsonb NOT NULL,
    after                jsonb NOT NULL,
    at                   timestamp NOT NULL DEFAULT (now() AT TIME ZONE 'UTC'),
    undone_at            timestamp
);
CREATE INDEX IF NOT EXISTS closed_process_batch ON suc.closed_process (batch_id);

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
       -- price per kWh only for sessions paid in full (a partial credit would push the price down)
       CASE WHEN sum(a.energy_kwh) > 0 AND sum(a.charging_fee) > 0
                 AND bool_and(i.pricing_types <@ ARRAY['PAYMENT']::text[])
            THEN sum(a.charging_fee) / sum(a.energy_kwh) END AS price_per_kwh,
       min(i.site_name)                         AS site_name,
       min(i.currency)                          AS currency,
       bool_and(i.paid)                         AS paid,
       bool_and(i.total = 0)                    AS free,
       min(a.match_kind)                        AS match_kind,
       array_agg(i.session_id ORDER BY i.start_utc) AS session_ids,
       (cw.charging_process_id IS NOT NULL)     AS cost_written_by_suc,
       bool_and(i.pricing_types <@ ARRAY['PAYMENT']::text[]) AS full_payment,
       min(i.provider)                          AS provider,
       min(i.charge_kind)                       AS charge_kind
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
                    pricing_types, rates, invoice_ids, fees_hash, raw, provider, charge_kind)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (session_id) DO UPDATE SET
                    vin = excluded.vin, site_name = excluded.site_name, country = excluded.country,
                    start_utc = excluded.start_utc, stop_utc = excluded.stop_utc, unlatch_utc = excluded.unlatch_utc,
                    grid_kwh = excluded.grid_kwh, charging_fee = excluded.charging_fee,
                    congestion_fee = excluded.congestion_fee, other_fee = excluded.other_fee,
                    total = excluded.total, net_total = excluded.net_total, currency = excluded.currency,
                    paid = excluded.paid, pricing_types = excluded.pricing_types, rates = excluded.rates,
                    invoice_ids = excluded.invoice_ids, fees_hash = excluded.fees_hash, raw = excluded.raw,
                    provider = excluded.provider, charge_kind = excluded.charge_kind,
                    updated_at = now() AT TIME ZONE 'UTC'
                  WHERE (suc.invoice.fees_hash, suc.invoice.paid, suc.invoice.vin, suc.invoice.site_name,
                         suc.invoice.start_utc, suc.invoice.stop_utc, suc.invoice.unlatch_utc, suc.invoice.invoice_ids,
                         suc.invoice.provider, suc.invoice.charge_kind)
                        IS DISTINCT FROM
                        (excluded.fees_hash, excluded.paid, excluded.vin, excluded.site_name,
                         excluded.start_utc, excluded.stop_utc, excluded.unlatch_utc, excluded.invoice_ids,
                         excluded.provider, excluded.charge_kind)
                RETURNING (xmax = 0) AS inserted
                """,
                (i.session_id, i.vin, i.site_name, i.country, i.start, i.stop, i.unlatch, i.grid_kwh,
                 i.charging_fee, i.congestion_fee, i.other_fee, i.total, i.net_total, i.currency, i.paid,
                 i.pricing_types, i.rates, i.invoice_ids, i.fees_hash, json.dumps(i.raw), i.provider, i.charge_kind),
            ).fetchone()
            if row is None:
                stats["same"] += 1
            elif row["inserted"]:
                stats["new"] += 1
            else:
                stats["changed"] += 1
    return stats


def load_invoices(conn, start: datetime, end: datetime, provider: str = "tesla") -> list[Invoice]:
    rows = conn.execute(
        "SELECT * FROM suc.invoice WHERE provider = %s AND start_utc < %s AND stop_utc > %s ORDER BY start_utc",
        (provider, end, start)).fetchall()
    return [Invoice(
        session_id=r["session_id"], vin=r["vin"], site_name=r["site_name"], country=r["country"],
        start=r["start_utc"], stop=r["stop_utc"], unlatch=r["unlatch_utc"], grid_kwh=r["grid_kwh"],
        charging_fee=r["charging_fee"], congestion_fee=r["congestion_fee"], other_fee=r["other_fee"],
        total=r["total"], net_total=r["net_total"], currency=r["currency"], paid=r["paid"],
        pricing_types=list(r["pricing_types"]), rates=list(r["rates"]), invoice_ids=list(r["invoice_ids"]),
        fees_hash=r["fees_hash"], raw=r["raw"], provider=r["provider"], charge_kind=r["charge_kind"]) for r in rows]


def set_invoice_status(conn, statuses: dict[str, tuple[str, str | None]]) -> None:
    with conn.transaction():
        for sid, (st, detail) in statuses.items():
            conn.execute("UPDATE suc.invoice SET status = %s, status_detail = %s WHERE session_id = %s "
                         "AND (status IS DISTINCT FROM %s OR status_detail IS DISTINCT FROM %s)",
                         (st, detail, sid, st, detail))


# --------------------------------------------------------------------------- TeslaMate


def load_dc_processes(conn, start: datetime, end: datetime) -> list[Process]:
    return load_processes(conn, start, end, dc_only=True)


def load_processes(conn, start: datetime, end: datetime, dc_only: bool = True) -> list[Process]:
    """Finished processes overlapping the range. dc_only: only those with a DC sample (fast_charger_present)."""
    rows = conn.execute(
        """
        SELECT cp.id, cp.car_id, c.vin, cp.start_date, cp.end_date, cp.charge_energy_added, cp.cost,
               EXISTS (SELECT 1 FROM charges ch
                        WHERE ch.charging_process_id = cp.id AND ch.fast_charger_present) AS has_dc,
               (SELECT ch.fast_charger_brand FROM charges ch
                 WHERE ch.charging_process_id = cp.id AND ch.fast_charger_brand IS NOT NULL
                 ORDER BY ch.date DESC LIMIT 1) AS brand,
               CASE WHEN g.id IS NOT NULL THEN
                    g.name || ' (' || coalesce(g.cost_per_unit::text, '—') || '/' || g.billing_type::text
                    || coalesce(', opłata ' || g.session_fee::text, '') || ')' END AS geofence,
               coalesce(cs.free_supercharging, false) AS free_supercharging
          FROM charging_processes cp
          JOIN cars c ON c.id = cp.car_id
          LEFT JOIN geofences g ON g.id = cp.geofence_id
          LEFT JOIN car_settings cs ON cs.id = c.settings_id
         WHERE cp.end_date IS NOT NULL
           AND cp.start_date < %s AND cp.end_date > %s
           AND c.vin IS NOT NULL
           AND (NOT %s OR EXISTS (SELECT 1 FROM charges ch
                                   WHERE ch.charging_process_id = cp.id AND ch.fast_charger_present))
         ORDER BY cp.start_date
        """, (end, start, dc_only)).fetchall()
    return [Process(r["id"], r["car_id"], r["vin"], r["start_date"], r["end_date"],
                    r["charge_energy_added"], r["cost"], r["brand"], r["geofence"], r["free_supercharging"],
                    r["has_dc"])
            for r in rows]


def replace_allocations(conn, session_ids: list[str], allocs: list[Allocation], kinds: dict[str, str]) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM suc.allocation WHERE session_id = ANY(%s)", (session_ids,))
        for a in allocs:
            conn.execute(
                "INSERT INTO suc.allocation (session_id, charging_process_id, match_kind, energy_kwh, cost, "
                "charging_fee, congestion_fee) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (a.session_id, a.process_id, kinds[a.session_id], a.energy_kwh, a.cost,
                 a.charging_fee, a.congestion_fee))


def written_costs(conn, process_ids: list[int]) -> dict[int, tuple[Decimal, str]]:
    """{process_id: (cost, provider)} — what we wrote and on behalf of which source."""
    rows = conn.execute("SELECT charging_process_id, cost, provider FROM suc.cost_written "
                        "WHERE charging_process_id = ANY(%s)", (process_ids,)).fetchall()
    return {r["charging_process_id"]: (r["cost"], r["provider"]) for r in rows}


def write_cost(conn, *, batch_id: uuid.UUID, process_id: int, expected: Decimal | None,
               new: Decimal, reason: str, overwrite: bool, provider: str = "tesla") -> bool:
    """One process = one transaction. Returns False when the cost changed under us."""
    with conn.transaction():
        row = conn.execute("SELECT cost FROM charging_processes WHERE id = %s FOR UPDATE", (process_id,)).fetchone()
        if row is None:
            return False
        if not overwrite and row["cost"] != expected and not (row["cost"] is None and expected is None):
            return False
        conn.execute("INSERT INTO suc.write_log (batch_id, charging_process_id, cost_before, cost_after, reason, "
                     "provider) VALUES (%s,%s,%s,%s,%s,%s)", (batch_id, process_id, row["cost"], new, reason, provider))
        cur = conn.execute("UPDATE charging_processes SET cost = %s WHERE id = %s AND cost IS NOT DISTINCT FROM %s",
                           (new, process_id, row["cost"]))
        if cur.rowcount != 1:
            raise RuntimeError(f"UPDATE charging_processes {process_id}: rowcount {cur.rowcount}")
        conn.execute(
            "INSERT INTO suc.cost_written (charging_process_id, cost, batch_id, written_at, provider) "
            "VALUES (%s,%s,%s, now() AT TIME ZONE 'UTC', %s) ON CONFLICT (charging_process_id) DO UPDATE SET "
            "cost = excluded.cost, batch_id = excluded.batch_id, written_at = excluded.written_at, "
            "provider = excluded.provider",
            (process_id, new, batch_id, provider))
    return True


def undo_batch(conn, batch_id: str, exclude: bool = True) -> dict:
    """Restores cost_before, but only where the cost is still what we wrote (nobody changed it since).
    After the undo the process goes to suc.cost_excluded, otherwise sync would write the same value again.
    If cost_before was itself our earlier write (undoing a correction), cost_written goes back to that
    value, so ownership is not lost."""
    restored, skipped, done_ids = 0, 0, []
    with conn.transaction():
        rows = conn.execute(
            "SELECT DISTINCT ON (charging_process_id) id, charging_process_id, cost_before, cost_after, provider "
            "FROM suc.write_log WHERE batch_id = %s AND undone_at IS NULL "
            "ORDER BY charging_process_id, id ASC", (batch_id,)).fetchall()
        for r in rows:
            pid = r["charging_process_id"]
            owner = conn.execute("SELECT batch_id::text AS b FROM suc.cost_written WHERE charging_process_id = %s",
                                 (pid,)).fetchone()
            if owner is not None and owner["b"] != str(batch_id):
                skipped += 1  # someone else owns the cost since then (e.g. a manual one with the same value) — leave it
                continue
            cur = conn.execute("UPDATE charging_processes SET cost = %s WHERE id = %s AND cost IS NOT DISTINCT FROM %s",
                               (r["cost_before"], pid, r["cost_after"]))
            if cur.rowcount != 1:
                skipped += 1
                continue
            restored += 1
            done_ids.append(r["id"])
            prev = conn.execute(
                "SELECT batch_id, cost_after, at, provider FROM suc.write_log WHERE charging_process_id = %s "
                "AND id < %s AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (pid, r["id"])).fetchone()
            new_owner = None
            if prev is not None and r["cost_before"] is not None and prev["cost_after"] == r["cost_before"]:
                new_owner = prev["provider"] or "tesla"
                conn.execute("UPDATE suc.cost_written SET cost = %s, batch_id = %s, written_at = %s, "
                             "provider = %s WHERE charging_process_id = %s",
                             (prev["cost_after"], prev["batch_id"], prev["at"], new_owner, pid))
            else:
                conn.execute("DELETE FROM suc.cost_written WHERE charging_process_id = %s", (pid,))
            if r["provider"] == "manual" and new_owner != "manual":  # the manual cost leaves the dashboard
                conn.execute("DELETE FROM suc.invoice WHERE session_id = %s", (f"manual-{pid}",))
            if exclude:
                conn.execute("INSERT INTO suc.cost_excluded (charging_process_id, batch_id) VALUES (%s, %s) "
                             "ON CONFLICT (charging_process_id) DO UPDATE SET batch_id = excluded.batch_id, "
                             "excluded_at = now() AT TIME ZONE 'UTC'", (pid, batch_id))
        # Only the rows really undone; the skipped ones stay active (they still prove an earlier ownership).
        conn.execute("UPDATE suc.write_log SET undone_at = now() AT TIME ZONE 'UTC' WHERE id = ANY(%s)", (done_ids,))
    return {"batch_id": batch_id, "restored": restored, "skipped_changed_since": skipped}


def excluded_processes(conn, process_ids: list[int]) -> set[int]:
    rows = conn.execute("SELECT charging_process_id FROM suc.cost_excluded WHERE charging_process_id = ANY(%s)",
                        (process_ids,)).fetchall()
    return {r["charging_process_id"] for r in rows}


def last_batches(conn, limit: int = 8) -> list[dict]:
    return conn.execute(
        "SELECT batch_id::text, min(at) AS at, count(*) AS changes, bool_or(undone_at IS NOT NULL) AS undone, "
        "coalesce(min(provider), 'tesla') AS provider "
        "FROM (SELECT batch_id, at, undone_at, provider FROM suc.write_log "
        "      UNION ALL SELECT batch_id, at, undone_at, 'close' FROM suc.closed_process) b "
        "GROUP BY batch_id ORDER BY min(at) DESC LIMIT %s", (limit,)).fetchall()


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


# --------------------------------------------------------------------------- manual cost


def manual_candidates(conn, days: int = 90, query: str = "") -> list[dict]:
    """Charging processes to pick from in the panel: date, place, kWh, current cost and where it came from."""
    return conn.execute(
        """
        SELECT cp.id, cp.car_id, coalesce(c.name, 'Auto ' || c.id) AS car, cp.start_date, cp.end_date,
               cp.charge_energy_added AS kwh, cp.cost,
               coalesce(g.name, a.name, nullif(concat_ws(' ', a.road, a.house_number), ''), a.display_name) AS place,
               a.city,
               EXISTS (SELECT 1 FROM charges ch WHERE ch.charging_process_id = cp.id AND ch.fast_charger_present) AS dc,
               cw.provider AS cost_source,
               (SELECT string_agg(DISTINCT i.provider, ',') FROM suc.allocation al
                  JOIN suc.invoice i USING (session_id) WHERE al.charging_process_id = cp.id) AS invoice_source
          FROM charging_processes cp
          JOIN cars c ON c.id = cp.car_id
          LEFT JOIN addresses a ON a.id = cp.address_id
          LEFT JOIN geofences g ON g.id = cp.geofence_id
          LEFT JOIN suc.cost_written cw ON cw.charging_process_id = cp.id
         WHERE cp.end_date IS NOT NULL
           AND cp.start_date > (now() AT TIME ZONE 'UTC') - make_interval(days => %s)
           AND (%s = '' OR concat_ws(' ', g.name, a.name, a.road, a.city, a.display_name) ILIKE '%%' || %s || '%%'
                OR cp.id::text = %s)
         ORDER BY cp.start_date DESC
         LIMIT 200
        """, (days, query, query, query)).fetchall()


def save_manual(conn, *, process_id: int, amount: Decimal, grid_kwh: Decimal | None, note: str,
                batch_id: uuid.UUID) -> dict:
    """Manual cost for one process: a synthetic invoice `manual-<id>` + a 1:1 allocation + a cost write
    (overwrite, with write_log). Refused when the process already has an invoice from Tesla/Greenway —
    then the cost would be counted twice in suc.v_charges."""
    sid = f"manual-{process_id}"
    with conn.transaction():
        cp = conn.execute("SELECT c.vin, cp.start_date, cp.end_date, cp.charge_energy_added FROM charging_processes cp "
                          "JOIN cars c ON c.id = cp.car_id WHERE cp.id = %s", (process_id,)).fetchone()
        if cp is None:
            raise ValueError(f"nie ma ładowania #{process_id}")
        other = conn.execute("SELECT string_agg(DISTINCT i.provider, ',') AS p FROM suc.allocation al "
                             "JOIN suc.invoice i USING (session_id) WHERE al.charging_process_id = %s "
                             "AND i.provider <> 'manual'", (process_id,)).fetchone()["p"]
        if other:
            raise ValueError(f"ładowanie #{process_id} ma już fakturę ({other}) — koszt ręczny by ją zdublował")
    kwh = grid_kwh if grid_kwh is not None else (cp["charge_energy_added"] or Decimal(0))
    inv = Invoice(session_id=sid, vin=cp["vin"] or "", site_name=None, country=None,
                  start=cp["start_date"], stop=cp["end_date"], unlatch=None,
                  grid_kwh=Decimal(str(kwh)), charging_fee=amount, congestion_fee=Decimal(0), other_fee=Decimal(0),
                  total=amount, net_total=amount, currency="PLN", paid=True,
                  pricing_types=["PAYMENT"] if amount != 0 else ["FREE"], rates=[], invoice_ids=[],
                  fees_hash=f"manual:{amount}:{kwh}", raw={"note": note, "grid_kwh_given": grid_kwh is not None},
                  provider="manual")
    upsert_invoices(conn, [inv])
    replace_allocations(conn, [sid], [Allocation(sid, process_id, inv.grid_kwh, amount, amount, Decimal(0))],
                        {sid: "ręcznie"})
    set_invoice_status(conn, {sid: ("matched", "ręcznie")})
    cur = conn.execute("SELECT cost FROM charging_processes WHERE id = %s", (process_id,)).fetchone()["cost"]
    # Always through write_cost, also when the value is already right: write_log then records the previous owner
    # (Tesla/Greenway), and deleting the manual cost gives it back to that owner.
    ok = write_cost(conn, batch_id=batch_id, process_id=process_id, expected=cur, new=amount,
                    reason="koszt ręczny", overwrite=True, provider="manual")
    return {"process": process_id, "cost_before": cur, "cost_after": amount, "written": ok}


def delete_manual(conn, process_id: int) -> dict:
    """Removes the manual cost: undoes ALL manual writes of this process (newest first), until the cost belongs
    to someone else again or goes back to what it was before. Changed since by someone else → we leave it."""
    restored, skipped = 0, 0
    for _ in range(50):
        row = conn.execute("SELECT batch_id::text AS b FROM suc.cost_written WHERE charging_process_id = %s "
                           "AND provider = 'manual'", (process_id,)).fetchone()
        if not row:
            break
        n = conn.execute("SELECT count(*) AS n FROM suc.write_log WHERE batch_id = %s AND undone_at IS NULL "
                         "AND charging_process_id = %s", (row["b"], process_id)).fetchone()["n"]
        if not n:
            with conn.transaction():
                conn.execute("DELETE FROM suc.cost_written WHERE charging_process_id = %s AND provider = 'manual'",
                             (process_id,))
            break
        res = undo_batch(conn, row["b"], exclude=False)
        restored += res["restored"]
        skipped += res["skipped_changed_since"]
        if not res["restored"]:
            break
    with conn.transaction():
        conn.execute("DELETE FROM suc.invoice WHERE session_id = %s", (f"manual-{process_id}",))
    return {"process": process_id, "restored": restored, "skipped_changed_since": skipped}


def claimed_by_others(conn, process_ids: list[int], provider: str) -> set[int]:
    """Processes that already belong to another source: an invoice from another source or a cost written by
    someone else (also manual). A sync leaves them out of the matching — otherwise the cost would be counted twice."""
    rows = conn.execute(
        """
        SELECT al.charging_process_id AS id FROM suc.allocation al JOIN suc.invoice i USING (session_id)
         WHERE al.charging_process_id = ANY(%s) AND i.provider <> %s
        UNION
        SELECT charging_process_id FROM suc.cost_written WHERE charging_process_id = ANY(%s) AND provider <> %s
        """, (process_ids, provider, process_ids, provider)).fetchall()
    return {r["id"] for r in rows}


# --------------------------------------------------------------------------- unfinished charging processes
# TeslaMate leaves end_date NULL when it loses a charge (restart, crash, lost connection). Such a process has
# its charges but no energy, end date or cost, so it is missing from every statistic. We close it like
# TeslaMate.Log.complete_charging_process (TeslaMate 2.x) does, without the cost (the cost goes through the
# usual paths: TeslaMateAgile, sync, manual) and without recalculating the car's efficiency.

CLOSE_COLS = ("start_date", "end_date", "start_ideal_range_km", "end_ideal_range_km", "start_rated_range_km",
              "end_rated_range_km", "start_battery_level", "end_battery_level", "outside_temp_avg",
              "charge_energy_added", "charge_energy_used", "duration_min")


def unfinished_processes(conn, min_idle_hours: float = 6) -> list[dict]:
    """Processes with end_date NULL whose last charge is older than min_idle_hours (a charge in progress
    also has end_date NULL — that one must not be touched)."""
    return conn.execute(
        """
        SELECT cp.id, cp.car_id, coalesce(c.name, 'Auto ' || c.id) AS car, cp.start_date,
               coalesce(g.name, a.name, nullif(concat_ws(' ', a.road, a.house_number), ''), a.display_name) AS place,
               a.city, s.n, s.first_at, s.last_at, s.soc0, s.soc1
          FROM charging_processes cp
          JOIN cars c ON c.id = cp.car_id
          LEFT JOIN addresses a ON a.id = cp.address_id
          LEFT JOIN geofences g ON g.id = cp.geofence_id
          JOIN LATERAL (SELECT count(*) AS n, min(ch.date) AS first_at, max(ch.date) AS last_at,
                               min(ch.battery_level) AS soc0, max(ch.battery_level) AS soc1
                          FROM charges ch WHERE ch.charging_process_id = cp.id) s ON s.n > 0
         WHERE cp.end_date IS NULL
           AND s.last_at < (now() AT TIME ZONE 'UTC') - make_interval(secs => %s)
         ORDER BY cp.start_date
        """, (min_idle_hours * 3600,)).fetchall()


def _phases(conn, process_id: int):
    """TeslaMate determine_phases: the real number of phases (or √3 voltage correction), None = unknown."""
    r = conn.execute(
        "SELECT avg(charger_power * 1000.0 / nullif(charger_actual_current * charger_voltage, 0))::float AS p, "
        "avg(charger_phases)::integer AS r, count(*) AS n FROM charges WHERE charging_process_id = %s",
        (process_id,)).fetchone()
    p, ph, n = r["p"], r["r"], r["n"]
    if p is None or p <= 0 or n <= 15:
        return None
    rp = math.floor(p + 0.5)  # Elixir round/1: half away from zero
    if ph == rp:
        return ph
    if ph == 3 and abs(p / math.sqrt(ph) - 1) <= 0.1:
        return math.sqrt(ph)
    if abs(rp - p) <= 0.3:
        return rp
    return None


def close_stats(conn, process_id: int) -> dict | None:
    """The values TeslaMate would write when completing the process (without cost)."""
    st = conn.execute(
        """
        SELECT first_value(date) OVER w AS start_date, last_value(date) OVER w AS end_date,
               first_value(ideal_battery_range_km) OVER w AS start_ideal_range_km,
               last_value(ideal_battery_range_km) OVER w AS end_ideal_range_km,
               first_value(rated_battery_range_km) OVER w AS start_rated_range_km,
               last_value(rated_battery_range_km) OVER w AS end_rated_range_km,
               first_value(battery_level) OVER w AS start_battery_level,
               last_value(battery_level) OVER w AS end_battery_level,
               round((avg(outside_temp) OVER w)::numeric, 1) AS outside_temp_avg,
               coalesce(nullif(last_value(charge_energy_added) OVER w, 0), max(charge_energy_added) OVER w)
                 - first_value(charge_energy_added) OVER w AS charge_energy_added,
               (extract(epoch FROM last_value(date) OVER w - first_value(date) OVER w) / 60)::integer AS duration_min
          FROM charges WHERE charging_process_id = %s
        WINDOW w AS (ORDER BY date RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
         LIMIT 1
        """, (process_id,)).fetchone()
    if st is None:
        return None
    st = dict(st)
    if st["charge_energy_added"] is not None and st["charge_energy_added"] < 0:
        st["charge_energy_added"] = None
    used = conn.execute(
        """
        SELECT round(sum(e)::numeric, 2) AS kwh FROM (
          SELECT CASE WHEN charger_phases IS NULL THEN charger_power
                      ELSE charger_actual_current * charger_voltage * %s::float / 1000.0 END
                 * extract(epoch FROM date - lag(date) OVER (ORDER BY date)) / 3600 AS e
            FROM charges WHERE charging_process_id = %s) x
         WHERE e >= 0
        """, (_phases(conn, process_id), process_id)).fetchone()["kwh"]
    st["charge_energy_used"] = used
    return st


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, datetime) else str(v) if isinstance(v, Decimal) else v)
            for k, v in d.items()}


def close_processes(conn, process_ids: list[int], *, batch_id: uuid.UUID, min_idle_hours: float = 6) -> dict:
    """Closes the given unfinished processes; one process = one transaction. Skips a process that is
    no longer unfinished or has a charge newer than min_idle_hours."""
    allowed = {r["id"] for r in unfinished_processes(conn, min_idle_hours)}
    closed, skipped = [], []
    cols = ", ".join(CLOSE_COLS)
    for pid in process_ids:
        if pid not in allowed:
            skipped.append(pid)
            continue
        with conn.transaction():
            before = conn.execute(f"SELECT {cols} FROM charging_processes WHERE id = %s AND end_date IS NULL "
                                  "FOR UPDATE", (pid,)).fetchone()
            st = close_stats(conn, pid) if before else None
            if not st or st["end_date"] is None:
                skipped.append(pid)
                continue
            conn.execute("INSERT INTO suc.closed_process (charging_process_id, batch_id, before, after) "
                         "VALUES (%s, %s, %s, %s) ON CONFLICT (charging_process_id) DO UPDATE SET "
                         "batch_id = excluded.batch_id, before = excluded.before, after = excluded.after, "
                         "at = now() AT TIME ZONE 'UTC', undone_at = NULL",
                         (pid, batch_id, json.dumps(_jsonable(dict(before))), json.dumps(_jsonable(st))))
            sets = ", ".join(f"{c} = %s" for c in CLOSE_COLS)
            cur = conn.execute(f"UPDATE charging_processes SET {sets} WHERE id = %s AND end_date IS NULL",
                               [st[c] for c in CLOSE_COLS] + [pid])
            if cur.rowcount != 1:
                raise RuntimeError(f"UPDATE charging_processes {pid}: rowcount {cur.rowcount}")
        closed.append({"process": pid, **_jsonable({k: st[k] for k in
                       ("start_date", "end_date", "charge_energy_added", "charge_energy_used",
                        "start_battery_level", "end_battery_level", "duration_min")})})
    return {"batch_id": str(batch_id), "closed": closed, "skipped": skipped}


def undo_close(conn, batch_id: str) -> dict:
    """Reopens the processes of a close batch, only where end_date is still what we wrote."""
    restored, skipped = 0, 0
    with conn.transaction():
        rows = conn.execute("SELECT charging_process_id AS pid, before, after FROM suc.closed_process "
                            "WHERE batch_id = %s AND undone_at IS NULL", (batch_id,)).fetchall()
        for r in rows:
            sets = ", ".join(f"{c} = %s" for c in CLOSE_COLS)
            cur = conn.execute(f"UPDATE charging_processes SET {sets} WHERE id = %s AND end_date = %s::timestamp",
                               [r["before"].get(c) for c in CLOSE_COLS] + [r["pid"], r["after"]["end_date"]])
            if cur.rowcount != 1:
                skipped += 1
                continue
            restored += 1
            conn.execute("UPDATE suc.closed_process SET undone_at = now() AT TIME ZONE 'UTC' "
                         "WHERE charging_process_id = %s", (r["pid"],))
    return {"batch_id": batch_id, "restored": restored, "skipped_changed_since": skipped}
