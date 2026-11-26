"""Greenway: token, pagination, parser. Synthetic data in the shape from the reconnaissance (no personal data)."""

import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal as D
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teslamate_suc"))

import greenway as gw  # noqa: E402
import http_util  # noqa: E402
import matcher  # noqa: E402

NOW = 1_800_000_000.0


def jwt(exp):
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'HS256'})}.{enc({'exp': exp, 'sub': 'x'})}.{'s' * 43}"


def session(sid, start, stop, kwh, price, kind="DC", running=False, connected_from=None, connected_to=None):
    return {"session_id": sid, "charging_from": start, "charging_to": stop,
            "connected_from": connected_from or start, "connected_to": connected_to or stop,
            "total_kwh_float": kwh, "total_kwh": str(kwh), "price_float": price, "price": f"{price:.2f}",
            "currency": "PLN", "connector_ac_dc": kind, "location_name": "Galeria X", "location_city": "Miasto",
            "location_country": "PL", "session_payment_status": "SessionPaymentStatus_Paid",
            "invoice_id": 7, "invoice_number": "FV/1", "running_session_boolean": running,
            "car_licence_plate": "XX 00000", "free_kwh_float": 0}


class Net:
    def __init__(self):
        self.calls, self.pages = [], []

    def __call__(self, req, timeout):
        self.calls.append(req)
        status, body, headers = self.pages.pop(0)
        return http_util.Response(status, json.dumps(body).encode(), "application/json", headers)


class TestToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.net = Net()
        p = mock.patch.object(http_util, "send", self.net)
        p.start()
        self.addCleanup(p.stop)
        self.c = gw.GreenwayClient(self.tmp.name, now=lambda: NOW)

    def test_clean_token_forms(self):
        t = jwt(NOW + 86400)
        for raw in (t, f"gw_token={t}", f"Bearer {t}", f'"{t}"', t.replace(".", "%2E")):
            self.assertEqual(gw.clean_token(raw), t)
        with self.assertRaises(gw.GreenwayError):
            gw.clean_token("hasło123")

    def test_set_token_verifies_and_saves_0600(self):
        self.net.pages.append((200, [], {}))
        st = self.c.set_token(jwt(NOW + 30 * 86400))
        self.assertTrue(st["valid"])
        self.assertEqual(st["days_left"], 30.0)
        self.assertEqual(os.stat(self.c.path).st_mode & 0o777, 0o600)
        self.assertTrue(self.net.calls[0].get_header("Authorization").startswith("Bearer "))

    def test_expired_and_rejected(self):
        with self.assertRaisesRegex(gw.GreenwayError, "wygasł"):
            self.c.set_token(jwt(NOW - 1))
        self.net.pages.append((401, {"message": "Unauthenticated."}, {}))
        with self.assertRaisesRegex(gw.GreenwayError, "401"):
            self.c.set_token(jwt(NOW + 86400))
        self.assertFalse(self.c.status()["configured"])

    def test_pagination_cursor_and_since(self):
        self.net.pages.append((200, [], {}))
        self.c.set_token(jwt(NOW + 86400))
        nxt = gw.API + "/client/sessions?cursor=abc"
        p1 = [session(i, f"2026-11-{20 - i:02d}T10:00:00+00:00", f"2026-11-{20 - i:02d}T10:30:00+00:00", 30, 90)
              for i in range(10)]
        p2 = [session(10 + i, f"2026-10-{20 - i:02d}T10:00:00+00:00", f"2026-10-{20 - i:02d}T10:30:00+00:00", 30, 90)
              for i in range(10)]
        self.net.pages += [(200, p1, {"x-pagination-has_more_pages": "true", "x-pagination-next_page_url": nxt}),
                           (200, p2, {"x-pagination-has_more_pages": "true", "x-pagination-next_page_url": nxt + "2"})]
        out = self.c.fetch_sessions(since=datetime(2026, 10, 15))
        self.assertEqual(len(out), 20)  # it stopped after reaching `since`, it did not ask for page 3
        self.assertEqual(self.net.calls[-1].full_url, nxt)

    def test_suspicious_next_url_refused(self):
        self.net.pages.append((200, [], {}))
        self.c.set_token(jwt(NOW + 86400))
        self.net.pages.append((200, [session(1, "2026-11-01T10:00:00+00:00", "2026-11-01T10:30:00+00:00", 1, 1)],
                               {"x-pagination-has_more_pages": "true",
                                "x-pagination-next_page_url": "https://evil.example/steal"}))
        with self.assertRaisesRegex(gw.GreenwayError, "podejrzany"):
            self.c.fetch_sessions()

    def test_401_marks_rejected(self):
        self.net.pages.append((200, [], {}))
        self.c.set_token(jwt(NOW + 86400))
        self.net.pages.append((401, {"message": "Unauthenticated."}, {}))
        with self.assertRaises(gw.GreenwayTokenExpired):
            self.c.fetch_sessions()
        self.assertFalse(self.c.status()["valid"])
        with self.assertRaises(gw.GreenwayTokenExpired):  # no further requests with a dead token
            self.c.fetch_sessions()


class TestParse(unittest.TestCase):
    def test_parse(self):
        s = session(123, "2026-11-15T12:31:56+00:00", "2026-11-15T12:48:10+00:00", 31.13, 93.08,
                    connected_from="2026-11-15T12:31:40+00:00", connected_to="2026-11-15T12:49:00+00:00")
        i = gw.parse_session(s)
        self.assertEqual(i.session_id, "gw-123")
        self.assertEqual(i.provider, "greenway")
        self.assertEqual(i.charge_kind, "DC")
        self.assertEqual(i.total, D("93.08"))
        self.assertEqual(i.grid_kwh, D("31.1300"))
        self.assertEqual(i.start, datetime(2026, 11, 15, 12, 31, 40))
        self.assertEqual(i.end, datetime(2026, 11, 15, 12, 49, 0))
        self.assertEqual(i.site_name, "Galeria X, Miasto")
        self.assertEqual(i.pricing_types, ["PAYMENT"])
        self.assertEqual(i.vin, "")

    def test_free_running_and_bad(self):
        free = gw.parse_session(session(1, "2025-12-14T16:06:03+00:00", "2025-12-14T16:55:12+00:00", 68.11, 0))
        self.assertTrue(free.is_free)
        self.assertEqual(free.pricing_types, ["FREE"])
        self.assertIsNone(gw.parse_session(session(2, "2026-11-25T10:00:00+00:00", "2026-11-25T10:10:00+00:00",
                                                   1, 1, running=True)))
        invs, errs = gw.parse_all([session(3, "2026-01-01T10:00:00+00:00", "2026-01-01T11:00:00+00:00", 5, 5),
                                   {"session_id": 4}])
        self.assertEqual(len(invs), 1)
        self.assertEqual(len(errs), 1)

    def test_ac_dc_and_any_car_matching(self):
        dc = gw.parse_session(session(5, "2026-11-01T10:00:00+00:00", "2026-11-01T10:30:00+00:00", 30, 90, "DC"))
        ac = gw.parse_session(session(6, "2026-11-02T10:00:00+00:00", "2026-11-02T13:00:00+00:00", 20, 30, "AC"))
        P = matcher.Process
        p_dc_car2 = P(1, 2, "VIN2", datetime(2026, 11, 1, 10, 1), datetime(2026, 11, 1, 10, 31), D(28), None, has_dc=True)
        p_ac_wrongkind = P(2, 1, "VIN1", datetime(2026, 11, 1, 10, 0), datetime(2026, 11, 1, 10, 30), D(5), None, has_dc=False)
        p_ac = P(3, 1, "VIN1", datetime(2026, 11, 2, 10, 2), datetime(2026, 11, 2, 12, 58), D(19), None, has_dc=False)
        g, um, lone = matcher.build_groups([dc, ac], [p_dc_car2, p_ac_wrongkind, p_ac], match_vin=False)
        self.assertEqual({(x.invoices[0].session_id, x.processes[0].id) for x in g}, {("gw-5", 1), ("gw-6", 3)})
        self.assertEqual([p.id for p in lone], [2])
        # with match_vin (the Tesla way) nothing matches: there is no VIN
        self.assertEqual(matcher.build_groups([dc], [p_dc_car2])[0], [])

    def test_two_cars_same_time_is_review(self):
        dc = gw.parse_session(session(7, "2026-11-01T10:00:00+00:00", "2026-11-01T10:30:00+00:00", 30, 90, "DC"))
        P = matcher.Process
        a = P(1, 1, "VIN1", datetime(2026, 11, 1, 10, 0), datetime(2026, 11, 1, 10, 30), D(28), None)
        b = P(2, 2, "VIN2", datetime(2026, 11, 1, 10, 0), datetime(2026, 11, 1, 10, 30), D(28), None)
        g, _, _ = matcher.build_groups([dc], [a, b], match_vin=False)
        self.assertEqual(g[0].kind, "review")  # one session on two cars: never split the cost between cars
        self.assertEqual(matcher.allocate(g[0]), [])


if __name__ == "__main__":
    unittest.main()
