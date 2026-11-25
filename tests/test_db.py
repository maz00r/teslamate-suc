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
INSERT INTO cars (id, eid, vid, vin, name) VALUES (1, 1, 1, 'XP71006', 'Model Y');
"""

VIN = "XP71006"


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
        self.sessions = [all_s[k] for k in ("812654702", "811893606", "810768431", "810730258", "808957706", "806721000")]
        self.api = FakeApi(self.sessions)
        self.now = utc("2026-11-25T12:00:00+00:00")
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
                              (self.pids["812654702"],)).fetchone()
        self.assertAlmostEqual(float(v[0]), 65.36 / 34.403, places=3)
        self.assertAlmostEqual(float(v[1]), 8.0, delta=0.1)

    def test_panel_cannot_force_write_while_option_on(self):
        rep = self.syncer.run(dry_run=False)
        self.assertTrue(rep["dry_run"])
        self.assertTrue(all(c is None for c in self.costs().values()))

    def test_write_then_idempotent(self):
        self.opts["dry_run"] = False
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["written"], 6)
        self.assertEqual(self.costs()["812654702"], D("65.36"))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM suc.write_log").fetchone()[0], 6)
        rep2 = self.syncer.run(dry_run=False)
        self.assertEqual(rep2["written"], 0)
        self.assertEqual({r["action"] for r in rep2["plan"]}, {"none"})

    def test_foreign_cost_kept_unless_overwrite(self):
        self.conn.execute("UPDATE charging_processes SET cost = 12.34 WHERE id=%s", (self.pids["811893606"],))
        self.opts["dry_run"] = False
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["written"], 5)
        self.assertEqual(self.costs()["811893606"], D("12.34"))
        self.opts["overwrite_cost"] = True
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["written"], 1)
        self.assertEqual(self.costs()["811893606"], D("70.21"))

    def test_invoice_correction_updates_our_cost_only(self):
        self.opts["dry_run"] = False
        self.syncer.run(dry_run=False)
        self.conn.execute("UPDATE charging_processes SET cost = 1.00 WHERE id=%s", (self.pids["810730258"],))  # user
        for s in self.sessions:
            for f in s["fees"]:
                if f["feeType"] == "CHARGING":
                    f["totalDue"] = round(f["totalDue"] + 1, 2)
        rep = self.syncer.run(dry_run=False)
        self.assertEqual(rep["invoices"]["changed"], 6)
        self.assertEqual(rep["written"], 5)
        self.assertEqual(self.costs()["812654702"], D("66.36"))
        self.assertEqual(self.costs()["810730258"], D("1.00"))

    def test_undo_restores_and_respects_later_changes(self):
        self.conn.execute("UPDATE charging_processes SET cost = 5.00 WHERE id=%s", (self.pids["806721000"],))
        self.opts["dry_run"] = False
        self.opts["overwrite_cost"] = True
        rep = self.syncer.run(dry_run=False)
        self.conn.execute("UPDATE charging_processes SET cost = 99.00 WHERE id=%s", (self.pids["808957706"],))
        conn = self.db.connect(self.opts)
        res = self.db.undo_batch(conn, rep["batch_id"])
        conn.close()
        self.assertEqual(res, {"batch_id": rep["batch_id"], "restored": 5, "skipped_changed_since": 1})
        c = self.costs()
        self.assertEqual(c["806721000"], D("5.00"))
        self.assertIsNone(c["812654702"])
        self.assertEqual(c["808957706"], D("99.00"))

    def test_non_dc_process_ignored(self):
        all_s = {str(s["sessionId"]): s for s in load()}
        s = all_s["795975621"]
        self.api.sessions.append(s)
        self.add_process("ac", s, dc=False)
        rep = self.syncer.run()
        self.assertIn("795975621", [u["session"] for u in rep["unmatched"]])

    def test_tariff_split_and_credit(self):
        all_s = {str(s["sessionId"]): s for s in load()}
        a, b, free = all_s["716916527"], all_s["716937037"], all_s["584760314"]
        self.api.sessions = [a, b, free]
        import parse
        ia, ib = parse.parse_session(a), parse.parse_session(b)
        pid = self.conn.execute("INSERT INTO charging_processes (start_date, end_date, charge_energy_added, car_id) "
                                "VALUES (%s,%s,58.0,1) RETURNING id", (ia.start, ib.stop)).fetchone()[0]
        self.conn.execute("INSERT INTO charges (date, charge_energy_added, charger_power, fast_charger_present, "
                          "ideal_battery_range_km, charging_process_id) VALUES (%s,1,150,true,300,%s)", (ia.start, pid))
        self.add_process("free", free)
        self.opts["dry_run"] = False
        rep = self.syncer.run(start=utc("2025-12-01T00:00:00+00:00"), end=self.now, dry_run=False)
        cost = self.conn.execute("SELECT cost FROM charging_processes WHERE id=%s", (pid,)).fetchone()[0]
        self.assertEqual(cost, D("86.06"))
        self.assertEqual(self.costs()["free"], D("0.00"))
        self.assertEqual(rep["groups"].get("N:1"), 1)

    def test_session_timezone_does_not_matter(self):
        self.conn.execute(f"ALTER DATABASE {self.dbname} SET timezone TO 'Europe/Warsaw'")
        rep = self.syncer.run()
        self.assertEqual(rep["groups"], {"1:1": 6})

    def test_teslamate_can_delete_process(self):
        self.syncer.run()
        self.conn.execute("DELETE FROM charging_processes WHERE id=%s", (self.pids["812654702"],))  # no FK blocks it
        rep = self.syncer.run()
        self.assertIn("812654702", [u["session"] for u in rep["unmatched"]])
        n = self.conn.execute("SELECT count(*) FROM suc.allocation WHERE charging_process_id=%s",
                              (self.pids["812654702"],)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_api_error_saved_in_report(self):
        def boom(**kw):
            raise RuntimeError("Fleet API leży")
        self.api.history = boom
        with self.assertRaises(RuntimeError):
            self.syncer.run()
        self.assertIn("Fleet API leży", self.syncer.last_report()["error"])


if __name__ == "__main__":
    unittest.main()
