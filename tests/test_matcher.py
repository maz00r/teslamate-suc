"""Parser and matcher on real data from the probe (tests/fixtures/probe_compact.json) plus edge cases."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal as D

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teslamate_suc"))

import matcher  # noqa: E402
import parse  # noqa: E402
from matcher import Process  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "probe_compact.json")


def raw_from_compact(c):
    sid, vin4, site, start, stop, unlatch, billing, make, fees, invs = c
    return {
        "sessionId": sid, "vin": "XP7" + vin4, "siteLocationName": site,
        "chargeStartDateTime": start, "chargeStopDateTime": stop, "unlatchDateTime": unlatch,
        "billingType": billing, "vehicleMakeType": make, "countryCode": "PL",
        "fees": [{"sessionFeeId": sid * 10 + k, "feeType": f[0], "pricingType": f[1], "uom": f[2],
                  "usageBase": f[3], "rateBase": f[4], "totalDue": f[5], "netDue": f[6], "status": f[7],
                  "usageTier1": f[8], "usageTier2": f[9], "totalTier1": f[10], "totalTier2": f[11],
                  "currencyCode": "PLN", "isPaid": f[7] == "PAID"} for k, f in enumerate(fees)],
        "invoices": [{"contentId": i[0], "invoiceType": i[1], "fileName": "x.pdf"} for i in invs],
    }


def load():
    with open(FIX, encoding="utf-8") as f:
        return [raw_from_compact(c) for c in json.load(f)["sessions"]]


def proc(pid, start, end, kwh=None, cost=None, vin="XP71006"):
    return Process(pid, 1, vin, start, end, None if kwh is None else D(str(kwh)), cost)


def dt(s):
    return datetime.fromisoformat(s)


class TestParseReal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.invs, cls.errors = parse.parse_all(load())
        cls.by = {i.session_id: i for i in cls.invs}

    def test_all_parsed(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.invs), 54)

    def test_paid_session(self):
        i = self.by["812654702"]
        self.assertEqual(i.start, dt("2026-11-23T20:27:37"))  # +01:00 → UTC
        self.assertEqual(i.total, D("65.36"))
        self.assertEqual(i.net_total, D("53.14"))
        self.assertEqual(i.grid_kwh, D("34.4030"))
        self.assertEqual(i.congestion_fee, D("0"))
        self.assertEqual(i.price_per_kwh, D("1.8998"))  # 65.36 / 34.403 — the gross amount is rounded to the grosz
        self.assertEqual(i.invoice_ids, ["dbb22ded-4cf2-40bd-bc90-57d3e0260a0e"])
        self.assertTrue(i.paid)

    def test_summer_offset(self):
        i = self.by["668002441"]  # +02:00
        self.assertEqual(i.start, dt("2026-05-08T19:14:23"))
        self.assertFalse(i.paid)  # UNPAID, but the cost still counts
        self.assertEqual(i.total, D("69.32"))

    def test_credit_is_free(self):
        i = self.by["584760314"]
        self.assertTrue(i.is_free)
        self.assertEqual(i.invoice_ids, [])
        self.assertIsNone(i.price_per_kwh)
        self.assertGreater(i.grid_kwh, 0)

    def test_partial_credit(self):
        i = self.by["617792215"]
        self.assertEqual(i.total, D("8.10"))
        self.assertEqual(i.pricing_types, ["CREDIT_PARTIAL_PAYMENT"])

    def test_hash_stable_and_sensitive(self):
        s = load()[0]
        h1 = parse.parse_session(s).fees_hash
        s["fees"] = list(reversed(s["fees"]))
        self.assertEqual(parse.parse_session(s).fees_hash, h1)
        s["fees"][0]["totalDue"] = 1.0
        self.assertNotEqual(parse.parse_session(s).fees_hash, h1)

    def test_bad_session_does_not_stop_others(self):
        sessions = load()[:3] + [{"sessionId": 1}]
        invs, errs = parse.parse_all(sessions)
        self.assertEqual(len(invs), 3)
        self.assertEqual(len(errs), 1)


class TestGroups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.by = {i.session_id: i for i in parse.parse_all(load())[0]}

    def inv(self, sid):
        return self.by[sid]

    def test_1_1(self):
        i = self.inv("812654702")  # 20:27:37–20:54:44 UTC
        p = proc(1, dt("2026-11-23T20:27:00"), dt("2026-11-23T20:55:30"), 33.1)
        g, um, lone = matcher.build_groups([i], [p])
        self.assertEqual([x.kind for x in g], ["1:1"])
        a = matcher.allocate(g[0])
        self.assertEqual(matcher.costs_by_process(a), {1: D("65.36")})

    def test_tariff_split_at_midnight_is_n_1(self):
        a, b = self.inv("716916527"), self.inv("716937037")  # 47.21 + 38.85, 15 s apart
        p = proc(7, dt("2026-07-16T21:55:00"), dt("2026-07-16T22:37:30"), 60.0)
        g, _, _ = matcher.build_groups([a, b], [p])
        self.assertEqual(g[0].kind, "N:1")
        self.assertEqual(matcher.costs_by_process(matcher.allocate(g[0])), {7: D("86.06")})

    def test_back_to_back_two_processes_resolved(self):
        a, b = self.inv("644806016"), self.inv("644809325")  # 07:33–07:43 and 07:44–07:57 UTC
        p1 = proc(10, dt("2026-04-01T07:33:20"), dt("2026-04-01T07:43:50"), 21.0)
        p2 = proc(11, dt("2026-04-01T07:44:20"), dt("2026-04-01T07:57:20"), 18.5)
        g, _, _ = matcher.build_groups([a, b], [p1, p2])
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].kind, "M:N")
        self.assertEqual(matcher.costs_by_process(matcher.allocate(g[0])), {10: D("38.51"), 11: D("33.40")})

    def test_1_n_split_by_energy(self):
        i = self.inv("810768431")  # 95.27 zł, 52.9297 kWh, 10:24–11:01 UTC
        p1 = proc(20, dt("2026-11-21T10:24:00"), dt("2026-11-21T10:40:00"), 30.0)
        p2 = proc(21, dt("2026-11-21T10:41:00"), dt("2026-11-21T11:02:00"), 20.0)
        g, _, _ = matcher.build_groups([i], [p1, p2])
        self.assertEqual(g[0].kind, "1:N")
        al = matcher.allocate(g[0])
        c = matcher.costs_by_process(al)
        self.assertEqual(sum(c.values()), D("95.27"))
        self.assertEqual(c[20], D("57.16"))
        self.assertEqual(sum(a.energy_kwh for a in al), D("52.9297"))

    def test_ambiguous_is_review(self):
        i1, i2 = self.inv("644806016"), self.inv("644809325")
        p1 = proc(30, dt("2026-04-01T07:30:00"), dt("2026-04-01T08:00:00"), 20)  # both inside p1
        p2 = proc(31, dt("2026-04-01T07:35:00"), dt("2026-04-01T07:56:00"), 20)  # and inside p2
        g, _, _ = matcher.build_groups([i1, i2], [p1, p2])
        self.assertEqual(g[0].kind, "review")
        self.assertEqual(matcher.allocate(g[0]), [])

    def test_unmatched_and_lonely(self):
        i = self.inv("812654702")
        far = proc(40, dt("2026-11-23T23:00:00"), dt("2026-11-23T23:30:00"), 10)
        other_vin = proc(41, dt("2026-11-23T20:27:00"), dt("2026-11-23T20:55:00"), 10, vin="OTHER")
        g, um, lone = matcher.build_groups([i], [far, other_vin])
        self.assertEqual(g, [])
        self.assertEqual([x.session_id for x in um], ["812654702"])
        self.assertEqual({p.id for p in lone}, {40, 41})

    def test_tolerance_boundary(self):
        i = self.inv("812654702")  # end (unlatch) 20:54:44 UTC
        p = proc(50, dt("2026-11-23T21:04:40"), dt("2026-11-23T21:30:00"), 10)  # 9:56 after the end
        self.assertEqual(len(matcher.build_groups([i], [p], 10)[0]), 1)
        self.assertEqual(len(matcher.build_groups([i], [p], 9)[0]), 0)

    def test_real_history_against_synthetic_teslamate(self):
        # Every session → a process with an offset of ±1–2 min. Back-to-back sessions <2 min apart → one process.
        invs = sorted(self.by.values(), key=lambda i: i.start)
        procs, pid, cur = [], 100, None
        for i in invs:
            if cur and (i.start - cur[1]) < timedelta(minutes=2):
                cur[1] = i.end
                continue
            if cur:
                procs.append(proc(pid, cur[0], cur[1] + timedelta(minutes=1), 10)); pid += 1
            cur = [i.start + timedelta(seconds=40), i.end]
        procs.append(proc(pid, cur[0], cur[1] + timedelta(minutes=1), 10))
        g, um, lone = matcher.build_groups(invs, procs)
        self.assertEqual(um, [])
        self.assertEqual(lone, [])
        self.assertNotIn("review", [x.kind for x in g])
        total = sum(sum(matcher.costs_by_process(matcher.allocate(x)).values()) for x in g)
        self.assertEqual(total, sum(i.total for i in invs))


class TestSplitAndDecide(unittest.TestCase):
    def test_split_exact(self):
        parts = matcher.split_exact(D("10.00"), [D(1), D(1), D(1)])
        self.assertEqual(sum(parts), D("10.00"))
        self.assertEqual(sorted(parts), [D("3.33"), D("3.33"), D("3.34")])
        self.assertEqual(matcher.split_exact(D("5.00"), [D(0), D(0)]), [D("2.50"), D("2.50")])

    def test_decide(self):
        d = matcher.decide
        self.assertEqual(d(D("10"), None, None, False)[0], "write")
        self.assertEqual(d(D("10"), D("10"), None, False)[0], "none")
        self.assertEqual(d(D("10"), D("8"), D("8"), False), ("write", "korekta faktury"))
        self.assertEqual(d(D("10"), D("8"), None, False)[0], "skip")
        self.assertEqual(d(D("10"), D("8"), D("7"), False)[0], "skip")  # someone changed what we wrote
        self.assertEqual(d(D("10"), D("8"), None, True)[0], "write")
        self.assertEqual(d(D("10000"), None, None, True)[0], "skip")
        self.assertEqual(d(D("0.00"), None, None, False)[0], "write")  # free → 0.00


if __name__ == "__main__":
    unittest.main()
