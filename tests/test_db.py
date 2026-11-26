"""Integration with PostgreSQL. The TeslaMate schema is a subset of the DDL from a real dump (TeslaMate 2.x).

Database: SUC_TEST_PG="host=… port=… user=… dbname=…" (CI), otherwise a temporary instance
via initdb/pg_ctl, when present. Without either the tests are skipped.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teslamate_suc"))
sys.path.insert(0, os.path.dirname(__file__))

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

from test_matcher import load  # noqa: E402

TESLAMATE_DDL = """
CREATE TYPE public.billing_type AS ENUM ('per_kwh', 'per_minute');
CREATE TABLE public.car_settings (id bigserial PRIMARY KEY, free_supercharging boolean NOT NULL DEFAULT false);
INSERT INTO car_settings (free_supercharging) VALUES (false);
CREATE TABLE public.geofences (
    id serial PRIMARY KEY, name varchar(255) NOT NULL, latitude numeric(8,6) NOT NULL, longitude numeric(9,6) NOT NULL,
    radius smallint NOT NULL DEFAULT 25, inserted_at timestamp(0) NOT NULL DEFAULT now(),
    updated_at timestamp(0) NOT NULL DEFAULT now(), cost_per_unit numeric(6,4), session_fee numeric(6,2),
    billing_type public.billing_type NOT NULL DEFAULT 'per_kwh');
CREATE TABLE public.cars (
    id smallint PRIMARY KEY, eid bigint NOT NULL, vid bigint NOT NULL, model varchar(255),
    efficiency double precision, inserted_at timestamp(0) NOT NULL DEFAULT now(),
    updated_at timestamp(0) NOT NULL DEFAULT now(), vin text, name text, settings_id bigint NOT NULL DEFAULT 1);
CREATE TABLE public.charging_processes (
    id serial PRIMARY KEY, start_date timestamp NOT NULL, end_date timestamp,
    charge_energy_added numeric(8,2), start_ideal_range_km numeric(6,2), end_ideal_range_km numeric(6,2),
    start_battery_level smallint, end_battery_level smallint, duration_min smallint,
    outside_temp_avg numeric(4,1), car_id smallint NOT NULL REFERENCES cars(id), position_id integer NOT NULL DEFAULT 1,
    address_id integer, start_rated_range_km numeric(6,2), end_rated_range_km numeric(6,2),
    geofence_id integer, charge_energy_used numeric(8,2), cost numeric(6,2));
CREATE TABLE public.charges (
    id serial PRIMARY KEY, date timestamp NOT NULL, charge_energy_added numeric(8,2) NOT NULL,
    charger_power smallint NOT NULL, fast_charger_present boolean, conn_charge_cable varchar(255),
    fast_charger_brand varchar(255), fast_charger_type varchar(255), ideal_battery_range_km numeric(6,2) NOT NULL,
    charging_process_id integer NOT NULL REFERENCES charging_processes(id) ON DELETE CASCADE);
CREATE TABLE public.addresses (
    id serial PRIMARY KEY, display_name varchar(512), name varchar(255), house_number varchar(255),
    road varchar(255), city varchar(255));
INSERT INTO cars (id, eid, vid, vin, name) VALUES (1, 1, 1, 'XP70000', 'Model Y');
INSERT INTO cars (id, eid, vid, vin, name) VALUES (2, 2, 2, 'XP79999', 'Drugie');
"""

VIN = "XP70000"


class PG:
    """Temporary PostgreSQL instance (initdb + pg_ctl) or an external one from SUC_TEST_PG."""

    def __init__(self):
        self.dir = None
        self.dsn = os.environ.get("SUC_TEST_PG")
        if self.dsn:
            return
        initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
        if not (initdb and pg_ctl and psycopg):
            raise unittest.SkipTest("brak PostgreSQL (initdb/pg_ctl) albo psycopg")
        self.dir = tempfile.mkdtemp(prefix="sucpg", dir="/tmp")  # short path: the socket limit is ~100 characters
        env = dict(os.environ, LC_ALL="C", LANG="C")  # macOS: without this postmaster dies ("became multithreaded")
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        subprocess.run([initdb, "-D", self.dir + "/d", "-U", "postgres", "-A", "trust", "--no-locale", "-E", "UTF8"],
                       check=True, capture_output=True, env=env)
        subprocess.run([pg_ctl, "-D", self.dir + "/d", "-o", f"-p {self.port} -k {self.dir} -c listen_addresses=127.0.0.1",
                        "-l", self.dir + "/log", "-w", "start"], check=True, capture_output=True, env=env)
        self.dsn = f"host=127.0.0.1 port={self.port} user=postgres dbname=postgres"

    def stop(self):
        if self.dir:
            subprocess.run([shutil.which("pg_ctl"), "-D", self.dir + "/d", "-m", "immediate", "stop"],
                           capture_output=True)
            shutil.rmtree(self.dir, ignore_errors=True)


_pg = None


def setUpModule():
    global _pg
    if psycopg is None:
        raise unittest.SkipTest("brak psycopg")
    _pg = PG()


def tearDownModule():
    if _pg:
        _pg.stop()


def utc(s):
    return datetime.fromisoformat(s)


class FakeApi:
    def __init__(self, sessions):
        self.sessions = sessions
        self.calls = []

    def history(self, start=None, end=None, **kw):
        self.calls.append((start, end))
        return [json.loads(json.dumps(s)) for s in self.sessions]


class Base(unittest.TestCase):
    def setUp(self):
        import db
        import sync
        self.db, self.sync = db, sync
        self.dbname = "t_" + os.urandom(4).hex()
        with psycopg.connect(_pg.dsn, autocommit=True) as c:
            c.execute(f"CREATE DATABASE {self.dbname}")
        kv = dict(p.split("=", 1) for p in _pg.dsn.split())
        self.opts = {"db_host": kv["host"], "db_port": int(kv.get("port", 5432)), "db_name": self.dbname,
                     "db_user": kv["user"], "db_password": kv.get("password", ""), "dry_run": True,
                     "overwrite_cost": False, "lookback_days": 45, "match_tolerance_min": 10}
        self.conn = psycopg.connect(f"{_pg.dsn.replace('dbname=' + kv['dbname'], '')} dbname={self.dbname}",
                                    autocommit=True)
        self.conn.execute(TESLAMATE_DDL)
        self.tmp = tempfile.TemporaryDirectory()
        all_s = {str(s["sessionId"]): s for s in load()}
        # November 2026: 6 sessions; plus the tariff split at midnight in July and a credit (free) in December.
        self.sessions = [all_s[k] for k in ("900053000", "900052000", "900051000", "900050000", "900049000", "900048000")]
        self.api = FakeApi(self.sessions)
        self.now = utc("2025-11-26T12:00:00+00:00")
        self.syncer = sync.Syncer(api=self.api, opts=self.opts, data_dir=self.tmp.name, now=lambda: self.now)
        self.pids = {}
        for s in self.sessions:
            self.add_process(str(s["sessionId"]), s)

    def tearDown(self):
        self.conn.close()
        with psycopg.connect(_pg.dsn, autocommit=True) as c:
            c.execute(f"DROP DATABASE {self.dbname} WITH (FORCE)")
        self.tmp.cleanup()

    def add_process(self, key, s, cost=None, kwh=None, dc=True, shift_s=45):
        import parse
        inv = parse.parse_session(s)
        r = self.conn.execute(
            "INSERT INTO charging_processes (start_date, end_date, charge_energy_added, car_id, cost) "
            "VALUES (%s,%s,%s,1,%s) RETURNING id",
            (inv.start + timedelta(seconds=shift_s), inv.stop + timedelta(seconds=30),
             kwh if kwh is not None else float(inv.grid_kwh) * 0.92, cost)).fetchone()
        self.conn.execute("INSERT INTO charges (date, charge_energy_added, charger_power, fast_charger_present, "
                          "fast_charger_brand, ideal_battery_range_km, charging_process_id) "
                          "VALUES (%s, 1, 150, %s, %s, 300, %s)",
                          (inv.start + timedelta(minutes=2), dc, "Tesla" if dc else None, r[0]))
        self.pids[key] = r[0]
        return r[0]

    def costs(self):
        return {k: self.conn.execute("SELECT cost FROM charging_processes WHERE id=%s", (v,)).fetchone()[0]
                for k, v in self.pids.items()}


class TestSync(Base):
    def test_dry_run_changes_nothing_in_teslamate(self):
        before = self.conn.execute("SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM charging_processes t").fetchone()[0]
        rep = self.syncer.run()
        after = self.conn.execute("SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM charging_processes t").fetchone()[0]
        self.assertEqual(before, after)
        self.assertTrue(rep["dry_run"])
        self.assertEqual(rep["groups"], {"1:1": 6})
        self.assertEqual(sum(1 for r in rep["plan"] if r["action"] == "write"), 6)
        self.assertEqual(rep["written"], 0)
        n = self.conn.execute("SELECT count(*) FROM suc.allocation").fetchone()[0]
        self.assertEqual(n, 6)  # the dashboard has data even in dry-run
        v = self.conn.execute("SELECT price_per_kwh, loss_pct FROM suc.v_charges WHERE charging_process_id=%s",
                              (self.pids["900053000"],)).fetchone()
        self.assertAlmostEqual(float(v[0]), 65.36 / 34.403, places=3)
        self.assertAlmostEqual(float(v[1]), 8.0, delta=0.1)
        fp = self.conn.execute("SELECT bool_and(full_payment) FROM suc.v_charges").fetchone()[0]
        self.assertTrue(fp)

    def test_panel_cannot_force_write_while_option_on(self):
        rep = self.syncer.run(dry_run=False)
        self.assertTrue(rep["dry_run"])
        self.assertTrue(all(c is None for c in self.costs().values()))

    def test_write_then_idempotent(self):
        self.opts["dry_run"] = False
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["written"], 6)
        self.assertEqual(self.costs()["900053000"], D("65.36"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM suc.write_log").fetchone()[0], 6)
        rep2 = self.syncer.run(dry_run=False)
        self.assertEqual(rep2["written"], 0)
        self.assertEqual({r["action"] for r in rep2["plan"]}, {"none"})

    def test_foreign_cost_kept_unless_overwrite(self):
        gid = self.conn.execute("INSERT INTO geofences (name, latitude, longitude, cost_per_unit) "
                                "VALUES ('SuC A', 52, 21, 0) RETURNING id").fetchone()[0]
        self.conn.execute("UPDATE charging_processes SET cost = 12.34, geofence_id = %s WHERE id=%s",
                          (gid, self.pids["900052000"]))
        self.opts["dry_run"] = False
        rep = self.syncer.run(dry_run=False)
        skip = [r for r in rep["plan"] if r["action"] == "skip"]
        self.assertEqual(skip[0]["geofence"], "SuC A (0.0000/per_kwh)")
        self.assertEqual(rep["written"], 5)
        self.assertEqual(self.costs()["900052000"], D("12.34"))
        self.opts["overwrite_cost"] = True
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["written"], 1)
        self.assertEqual(self.costs()["900052000"], D("70.21"))

    def test_invoice_correction_updates_our_cost_only(self):
        self.opts["dry_run"] = False
        self.syncer.run(dry_run=False)
        self.conn.execute("UPDATE charging_processes SET cost = 1.00 WHERE id=%s", (self.pids["900050000"],))  # user
        for s in self.sessions:
            for f in s["fees"]:
                if f["feeType"] == "CHARGING":
                    f["totalDue"] = round(f["totalDue"] + 1, 2)
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["invoices"]["changed"], 6)
        self.assertEqual(rep["written"], 5)
        self.assertEqual(self.costs()["900053000"], D("66.36"))
        self.assertEqual(self.costs()["900050000"], D("1.00"))

    def test_undo_restores_and_respects_later_changes(self):
        self.conn.execute("UPDATE charging_processes SET cost = 5.00 WHERE id=%s", (self.pids["900048000"],))
        self.opts["dry_run"] = False
        self.opts["overwrite_cost"] = True
        rep = self.syncer.run(dry_run=False)
        self.conn.execute("UPDATE charging_processes SET cost = 99.00 WHERE id=%s", (self.pids["900049000"],))
        conn = self.db.connect(self.opts)
        res = self.db.undo_batch(conn, rep["batch_id"])
        conn.close()
        self.assertEqual(res, {"batch_id": rep["batch_id"], "restored": 5, "skipped_changed_since": 1})
        c = self.costs()
        self.assertEqual(c["900048000"], D("5.00"))
        self.assertIsNone(c["900053000"])
        self.assertEqual(c["900049000"], D("99.00"))

    def test_non_dc_process_ignored(self):
        all_s = {str(s["sessionId"]): s for s in load()}
        s = all_s["900047000"]
        self.api.sessions.append(s)
        self.add_process("ac", s, dc=False)
        rep = self.syncer.run()
        self.assertIn("900047000", [u["session"] for u in rep["unmatched"]])

    def test_tariff_split_and_credit(self):
        all_s = {str(s["sessionId"]): s for s in load()}
        a, b, free = all_s["900033000"], all_s["900034000"], all_s["900000000"]
        self.api.sessions = [a, b, free]
        import parse
        ia, ib = parse.parse_session(a), parse.parse_session(b)
        pid = self.conn.execute("INSERT INTO charging_processes (start_date, end_date, charge_energy_added, car_id) "
                                "VALUES (%s,%s,58.0,1) RETURNING id", (ia.start, ib.stop)).fetchone()[0]
        self.conn.execute("INSERT INTO charges (date, charge_energy_added, charger_power, fast_charger_present, "
                          "ideal_battery_range_km, charging_process_id) VALUES (%s,1,150,true,300,%s)", (ia.start, pid))
        self.add_process("free", free)
        self.opts["dry_run"] = False
        rep = self.syncer.run(start=utc("2024-12-02T00:00:00+00:00"), end=self.now, dry_run=False)
        cost = self.conn.execute("SELECT cost FROM charging_processes WHERE id=%s", (pid,)).fetchone()[0]
        self.assertEqual(cost, D("86.06"))
        self.assertEqual(self.costs()["free"], D("0.00"))
        v = self.conn.execute("SELECT price_per_kwh, full_payment FROM suc.v_charges WHERE charging_process_id=%s",
                              (self.pids["free"],)).fetchone()
        self.assertEqual(v, (None, False))
        self.assertEqual(rep["groups"].get("N:1"), 1)

    def _tariff_split(self):
        all_s = {str(s["sessionId"]): s for s in load()}
        a, b = all_s["900033000"], all_s["900034000"]
        self.api.sessions = [a, b]
        import parse
        ia, ib = parse.parse_session(a), parse.parse_session(b)
        pid = self.conn.execute("INSERT INTO charging_processes (start_date, end_date, charge_energy_added, car_id) "
                                "VALUES (%s,%s,58.0,1) RETURNING id", (ia.start, ib.stop)).fetchone()[0]
        self.conn.execute("INSERT INTO charges (date, charge_energy_added, charger_power, fast_charger_present, "
                          "ideal_battery_range_km, charging_process_id) VALUES (%s,1,150,true,300,%s)", (ia.start, pid))
        return ia, ib, pid

    def test_window_edge_does_not_cut_group(self):  # review #1
        ia, ib, pid = self._tariff_split()
        self.opts["dry_run"] = False
        self.syncer.run(start=utc("2024-07-01T00:00:00+00:00"), end=self.now, dry_run=False)
        get = lambda: self.conn.execute("SELECT cost FROM charging_processes WHERE id=%s", (pid,)).fetchone()[0]
        self.assertEqual(get(), D("86.06"))
        self.api.sessions = []  # later passes: Tesla returns nothing new, the invoices are already in the database
        # (a) the core starts between the two invoices: the first is still in the padding, so the group is whole
        start_a = (ia.end + timedelta(minutes=1)).replace(tzinfo=timezone.utc)
        rep = self.syncer.run(start=start_a, end=self.now, dry_run=False)
        self.assertEqual(get(), D("86.06"))
        self.assertEqual(rep["written"], 0)
        # (b) the padding cuts the group off (the first invoice is not loaded): the group is outside the core, untouched
        start_b = (ia.end + timedelta(days=1, minutes=1)).replace(tzinfo=timezone.utc)
        rep = self.syncer.run(start=start_b, end=self.now, dry_run=False)
        self.assertEqual(get(), D("86.06"))
        n = self.conn.execute("SELECT count(*) FROM suc.allocation WHERE charging_process_id=%s", (pid,)).fetchone()[0]
        self.assertEqual(n, 2)

    def test_zero_from_geofence_is_filled(self):
        self.conn.execute("UPDATE charging_processes SET cost = 0 WHERE id=%s", (self.pids["900052000"],))
        self.opts["dry_run"] = False
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["written"], 6)
        self.assertEqual(self.costs()["900052000"], D("70.21"))
        before = self.conn.execute("SELECT cost_before FROM suc.write_log WHERE charging_process_id=%s",
                                   (self.pids["900052000"],)).fetchone()[0]
        self.assertEqual(before, D("0.00"))  # undo restores 0

    def test_stale_cost_reported(self):  # review #2
        self.opts["dry_run"] = False
        self.syncer.run(dry_run=False)
        pid = self.pids["900053000"]
        self.conn.execute("UPDATE charging_processes SET start_date = start_date + interval '3 hours', "
                          "end_date = end_date + interval '3 hours' WHERE id=%s", (pid,))
        rep = self.syncer.run(dry_run=False)
        self.assertEqual([x["process"] for x in rep["stale"]], [pid])
        self.assertEqual(self.costs()["900053000"], D("65.36"))  # not undone on its own

    def test_undo_sticks_and_restores_ownership(self):  # review #3
        self.opts["dry_run"] = False
        rep1 = self.syncer.run(dry_run=False)
        for s in self.sessions:  # correction
            for f in s["fees"]:
                if f["feeType"] == "CHARGING":
                    f["totalDue"] = round(f["totalDue"] + 1, 2)
        rep2 = self.syncer.run(dry_run=False)
        self.assertEqual(self.costs()["900053000"], D("66.36"))
        conn = self.db.connect(self.opts)
        self.db.undo_batch(conn, rep2["batch_id"])
        conn.close()
        self.assertEqual(self.costs()["900053000"], D("65.36"))
        own = self.conn.execute("SELECT cost, batch_id::text FROM suc.cost_written WHERE charging_process_id=%s",
                                (self.pids["900053000"],)).fetchone()
        self.assertEqual(own, (D("65.36"), rep1["batch_id"]))  # ownership back to batch 1
        rep3 = self.syncer.run(dry_run=False)  # the undo sticks: sync does not write 66.36 again
        self.assertEqual(rep3["written"], 0)
        self.assertEqual(self.costs()["900053000"], D("65.36"))
        self.assertTrue(all("cofnięty" in r["reason"] for r in rep3["plan"] if r["action"] == "skip"))
        conn = self.db.connect(self.opts)
        self.db.undo_batch(conn, rep1["batch_id"])
        conn.close()
        self.assertIsNone(self.costs()["900053000"])
        self.assertEqual(self.syncer.run(dry_run=False)["written"], 0)

    def test_undo_refused_in_dry_run(self):  # review #4
        import panel
        app = panel.App(opts=self.opts, auth=None, api=None, usage=None, data_dir=self.tmp.name, syncer=self.syncer)
        with self.assertRaisesRegex(ValueError, "dry_run"):
            app.undo("00000000-0000-0000-0000-000000000000")

    def test_invoice_time_change_is_stored(self):  # review #7
        self.syncer.run()
        s = self.sessions[0]
        s["chargeStopDateTime"] = s["chargeStopDateTime"].replace(":54:42", ":55:42")
        rep = self.syncer.run()
        self.assertEqual(rep["invoices"]["changed"], 1)

    def test_session_timezone_does_not_matter(self):
        self.conn.execute(f"ALTER DATABASE {self.dbname} SET timezone TO 'Europe/Warsaw'")
        rep = self.syncer.run()
        self.assertEqual(rep["groups"], {"1:1": 6})

    def test_teslamate_can_delete_process(self):
        self.syncer.run()
        self.conn.execute("DELETE FROM charging_processes WHERE id=%s", (self.pids["900053000"],))  # no FK blocks it
        rep = self.syncer.run()
        self.assertIn("900053000", [u["session"] for u in rep["unmatched"]])
        n = self.conn.execute("SELECT count(*) FROM suc.allocation WHERE charging_process_id=%s",
                              (self.pids["900053000"],)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_api_error_saved_in_report(self):
        def boom(**kw):
            raise RuntimeError("Fleet API leży")
        self.api.history = boom
        with self.assertRaises(RuntimeError):
            self.syncer.run()
        self.assertIn("Fleet API leży", self.syncer.last_report()["error"])


class FakeGreenway:
    def __init__(self, sessions):
        self.sessions = sessions

    def fetch_sessions(self, since=None):
        return [json.loads(json.dumps(x)) for x in self.sessions]

    def status(self):
        return {"configured": True, "valid": True}


def gw_session(sid, start, stop, kwh, price, kind):
    return {"session_id": sid, "charging_from": start, "charging_to": stop, "connected_from": start,
            "connected_to": stop, "total_kwh_float": kwh, "price_float": price, "currency": "PLN",
            "connector_ac_dc": kind, "location_name": "Galeria", "location_city": "Miasto",
            "session_payment_status": "SessionPaymentStatus_Paid", "invoice_id": None,
            "running_session_boolean": False}


class TestMultiSource(Base):
    def setUp(self):
        super().setUp()
        self.opts["dry_run"] = False
        # AC at Greenway on the second car + DC at Greenway on the first one
        self.ac = self.proc(2, "2025-11-10T09:00:00", "2025-11-10T11:58:00", 18.5, dc=False)
        self.dc = self.proc(1, "2025-11-12T15:00:30", "2025-11-12T15:29:00", 28.0, dc=True)
        self.manual_target = self.proc(1, "2025-11-14T08:00:00", "2025-11-14T10:00:00", 11.0, dc=False)
        self.syncer.greenway = FakeGreenway([
            gw_session(501, "2025-11-10T09:01:00+00:00", "2025-11-10T12:00:00+00:00", 20.1, 30.15, "AC"),
            gw_session(502, "2025-11-12T15:00:00+00:00", "2025-11-12T15:30:00+00:00", 30.0, 93.00, "DC"),
        ])

    def proc(self, car, start, end, kwh, dc):
        r = self.conn.execute("INSERT INTO charging_processes (start_date, end_date, charge_energy_added, car_id) "
                              "VALUES (%s,%s,%s,%s) RETURNING id", (start, end, kwh, car)).fetchone()[0]
        self.conn.execute("INSERT INTO charges (date, charge_energy_added, charger_power, fast_charger_present, "
                          "ideal_battery_range_km, charging_process_id) VALUES (%s,1,11,%s,300,%s)", (start, dc, r))
        return r

    def cost(self, pid):
        return self.conn.execute("SELECT cost FROM charging_processes WHERE id=%s", (pid,)).fetchone()[0]

    def test_greenway_ac_dc_any_car(self):
        rep = self.syncer.run(provider="greenway", dry_run=False)
        self.assertEqual(rep["written"], 2)
        self.assertEqual(self.cost(self.ac), D("30.15"))
        self.assertEqual(self.cost(self.dc), D("93.00"))
        v = self.conn.execute("SELECT provider, charge_kind, grid_kwh FROM suc.v_charges WHERE charging_process_id=%s",
                              (self.ac,)).fetchone()
        self.assertEqual(v, ("greenway", "AC", D("20.1000")))
        self.assertEqual(self.syncer.last_report("greenway")["provider"], "greenway")

    def test_sources_do_not_touch_each_other(self):
        self.syncer.run(provider="tesla", dry_run=False)
        self.syncer.run(provider="greenway", dry_run=False)
        rep_t = self.syncer.run(provider="tesla", dry_run=False)
        rep_g = self.syncer.run(provider="greenway", dry_run=False)
        self.assertEqual(rep_t["stale"], [])  # the Greenway DC cost is not "ours without a match" for Tesla
        self.assertEqual(rep_g["stale"], [])
        self.assertEqual(rep_t["written"] + rep_g["written"], 0)
        self.assertEqual(self.cost(self.dc), D("93.00"))
        self.assertEqual(self.costs()["900053000"], D("65.36"))
        src = dict(self.conn.execute("SELECT charging_process_id, provider FROM suc.cost_written").fetchall())
        self.assertEqual(src[self.dc], "greenway")
        self.assertEqual(src[self.pids["900053000"]], "tesla")

    def test_manual_cost_protected_and_removable(self):
        conn = self.db.connect(self.opts)
        self.db.migrate(conn)
        res = self.db.save_manual(conn, process_id=self.ac, amount=D("44.00"), grid_kwh=D("21"), note="test",
                                  batch_id=__import__("uuid").uuid4())
        conn.close()
        self.assertTrue(res["written"])
        self.assertEqual(self.cost(self.ac), D("44.00"))
        rep = self.syncer.run(provider="greenway", dry_run=False)  # Greenway does not touch the manual cost
        self.assertNotIn(self.ac, [r["process"] for r in rep["plan"]])
        self.assertEqual(self.cost(self.ac), D("44.00"))
        conn = self.db.connect(self.opts)
        self.db.migrate(conn)
        out = self.db.delete_manual(conn, self.ac)
        conn.close()
        self.assertEqual(out["restored"], 1)
        self.assertIsNone(self.cost(self.ac))
        n = self.conn.execute("SELECT count(*) FROM suc.invoice WHERE provider='manual'").fetchone()[0]
        self.assertEqual(n, 0)
        self.assertEqual(self.syncer.run(provider="greenway", dry_run=False)["written"], 1)  # not excluded
        self.assertEqual(self.cost(self.ac), D("30.15"))

    def test_manual_on_process_with_invoice_refused(self):
        self.syncer.run(provider="tesla", dry_run=False)
        conn = self.db.connect(self.opts)
        self.db.migrate(conn)
        with self.assertRaisesRegex(ValueError, "fakturę"):
            self.db.save_manual(conn, process_id=self.pids["900053000"], amount=D("1"), grid_kwh=None, note="",
                                batch_id=__import__("uuid").uuid4())
        conn.close()

    def test_manual_undo_from_batches_clears_dashboard(self):
        conn = self.db.connect(self.opts)
        self.db.migrate(conn)
        import uuid as _u
        b = _u.uuid4()
        self.db.save_manual(conn, process_id=self.manual_target, amount=D("12.50"), grid_kwh=None, note="",
                            batch_id=b)
        self.db.undo_batch(conn, str(b))
        conn.close()
        self.assertIsNone(self.cost(self.manual_target))
        n = self.conn.execute("SELECT count(*) FROM suc.v_charges WHERE charging_process_id=%s",
                              (self.manual_target,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_manual_refused_in_dry_run(self):
        import panel
        self.opts["dry_run"] = True
        app = panel.App(opts=self.opts, auth=None, api=None, usage=None, data_dir=self.tmp.name, syncer=self.syncer)
        with self.assertRaisesRegex(ValueError, "dry_run"):
            app.manual_save({"process": self.ac, "amount": "10"})
        self.opts["dry_run"] = False
        rows = app.manual_candidates({"q": "", "days": 3650})
        self.assertIn(self.ac, [r["id"] for r in rows])
        res = app.manual_save({"process": self.manual_target, "amount": "12,34", "kwh": "13,5"})
        self.assertEqual(res["cost_after"], 12.34)

    def _manual(self, pid, amount, kwh=None):
        import uuid as _u
        conn = self.db.connect(self.opts)
        self.db.migrate(conn)
        try:
            return self.db.save_manual(conn, process_id=pid, amount=D(amount), grid_kwh=kwh, note="",
                                       batch_id=_u.uuid4())
        finally:
            conn.close()

    def _delete_manual(self, pid):
        conn = self.db.connect(self.opts)
        try:
            return self.db.delete_manual(conn, pid)
        finally:
            conn.close()

    def test_review_1_manual_then_invoice_not_double_counted(self):
        self._manual(self.ac, "44.00")
        rep = self.syncer.run(provider="greenway", dry_run=False)
        v = self.conn.execute("SELECT invoice_cost, session_ids FROM suc.v_charges WHERE charging_process_id=%s",
                              (self.ac,)).fetchone()
        self.assertEqual(v, (D("44.00"), [f"manual-{self.ac}"]))
        self.assertIn("gw-501", [u["session"] for u in rep["unmatched"]])
        self.assertGreaterEqual(rep["claimed_elsewhere"], 1)  # the manual one + Supercharger processes

    def test_review_2_delete_after_two_edits(self):
        self._manual(self.manual_target, "60.00")
        self._manual(self.manual_target, "70.00")
        out = self._delete_manual(self.manual_target)
        self.assertEqual(out["restored"], 2)
        self.assertIsNone(self.cost(self.manual_target))
        n = self.conn.execute("SELECT count(*) FROM suc.cost_written WHERE charging_process_id=%s",
                              (self.manual_target,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_review_3_and_5_takeover_same_value(self):
        rep = self.syncer.run(provider="tesla", dry_run=False)
        pid = self.pids["900053000"]
        # the allocation disappears (the process moves), the user types the same value by hand
        self.conn.execute("UPDATE charging_processes SET start_date = start_date + interval '3 hours', "
                          "end_date = end_date + interval '3 hours' WHERE id=%s", (pid,))
        self.syncer.run(provider="tesla", dry_run=False)
        self._manual(pid, "65.36")
        conn = self.db.connect(self.opts)
        res = self.db.undo_batch(conn, rep["batch_id"])  # undoing the old Tesla batch
        conn.close()
        self.assertEqual(self.cost(pid), D("65.36"))  # the manual cost survived
        own = self.conn.execute("SELECT provider FROM suc.cost_written WHERE charging_process_id=%s", (pid,)).fetchone()
        self.assertEqual(own[0], "manual")
        self.assertGreaterEqual(res["skipped_changed_since"], 1)
        out = self._delete_manual(pid)  # deleting the manual one gives the cost back to Tesla
        self.assertEqual(out["restored"], 1)
        own = self.conn.execute("SELECT provider, cost FROM suc.cost_written WHERE charging_process_id=%s",
                                (pid,)).fetchone()
        self.assertEqual(own, ("tesla", D("65.36")))

    def test_review_6_greenway_skips_tesla_supercharger_process(self):
        self.syncer.greenway = FakeGreenway([gw_session(
            777, "2025-11-24T20:27:00+00:00", "2025-11-24T20:55:00+00:00", 34.0, 99.0, "DC")])
        rep = self.syncer.run(provider="greenway", dry_run=False,
                              start=utc("2025-11-01T00:00:00+00:00"), end=self.now)
        self.assertEqual(rep["written"], 0)
        self.assertIn("gw-777", [u["session"] for u in rep["unmatched"]])

    def test_migration_from_v1(self):
        self.syncer.run(provider="tesla", dry_run=False)
        self.conn.execute("DROP VIEW suc.v_charges")
        for t, c in (("invoice", "provider"), ("invoice", "charge_kind"), ("cost_written", "provider"),
                     ("write_log", "provider")):
            self.conn.execute(f"ALTER TABLE suc.{t} DROP COLUMN {c}")
        rep = self.syncer.run(provider="tesla", dry_run=False)
        self.assertEqual(rep["written"], 0)
        self.assertEqual({r["action"] for r in rep["plan"]}, {"none"})
        n = self.conn.execute("SELECT count(*) FROM suc.cost_written WHERE provider='tesla'").fetchone()[0]
        self.assertEqual(n, 6)


if __name__ == "__main__":
    unittest.main()
