"""Stage 1 tests: OAuth, token rotation, API pagination, probe, panel."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "teslamate_suc"))

import api  # noqa: E402
import auth  # noqa: E402
import http_util  # noqa: E402
import panel  # noqa: E402


class FakeNet:
    """Swaps http_util.send. `script` is a list of (predicate, Response)."""

    def __init__(self):
        self.calls = []
        self.script = []

    def add(self, match, status, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.script.append((match, http_util.Response(status, raw)))

    def __call__(self, req, timeout):
        form = urllib.parse.parse_qs(req.data.decode()) if req.data and b"=" in req.data and not req.data.startswith(b"{") else {}
        call = {"method": req.get_method(), "url": req.full_url, "form": {k: v[0] for k, v in form.items()},
                "headers": dict(req.header_items()), "data": req.data}
        self.calls.append(call)
        for i, (match, resp) in enumerate(self.script):
            if match(call):
                self.script.pop(i)
                return resp
        raise AssertionError(f"nieoczekiwane żądanie {call['method']} {call['url']} {call['form']}")


def is_grant(g):
    return lambda c: c["url"] == auth.TOKEN_URL and c["form"].get("grant_type") == g


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.net = FakeNet()
        p = mock.patch.object(http_util, "send", self.net)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = auth.TokenStore(self.dir)
        self.auth = auth.TeslaAuth(client_id="cid", client_secret="sec", region="eu",
                                   redirect_uri="https://suc.example.com/callback",
                                   store=self.store, now=self.clock)

    def login(self, refresh="R1", expires_in=28800):
        url = self.auth.start_login()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.net.add(is_grant("authorization_code"), 200,
                     {"access_token": "A1", "refresh_token": refresh, "expires_in": expires_in,
                      "scope": auth.USER_SCOPES})
        return self.auth.finish_login(f"https://suc.example.com/callback?code=C&state={state}&issuer=x")


class TestLogin(Base):
    def test_authorize_url(self):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.auth.start_login()).query)
        self.assertEqual(q["client_id"], ["cid"])
        self.assertEqual(q["redirect_uri"], ["https://suc.example.com/callback"])
        self.assertIn("offline_access", q["scope"][0])
        self.assertNotIn("vehicle_cmds", q["scope"][0].split())
        self.assertGreater(len(q["state"][0]), 20)

    def test_finish_login_saves_token_0600(self):
        st = self.login()
        self.assertTrue(st["logged_in"])
        exch = self.net.calls[-1]["form"]
        self.assertEqual(exch["audience"], "https://fleet-api.prd.eu.vn.cloud.tesla.com")
        self.assertEqual(exch["client_secret"], "sec")
        self.assertEqual(exch["redirect_uri"], "https://suc.example.com/callback")
        self.assertEqual(os.stat(self.store.token_path).st_mode & 0o777, 0o600)
        self.assertIsNone(self.store.load_state())

    def test_state_mismatch(self):
        self.auth.start_login()
        with self.assertRaisesRegex(auth.AuthError, "state"):
            self.auth.finish_login("https://x/callback?code=C&state=zly")
        self.assertEqual(self.net.calls, [])

    def test_state_expired(self):
        url = self.auth.start_login()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.clock.t += auth.STATE_TTL_S + 1
        with self.assertRaisesRegex(auth.AuthError, "wygasł"):
            self.auth.finish_login(f"https://x/callback?code=C&state={state}")

    def test_error_from_tesla(self):
        with self.assertRaisesRegex(auth.AuthError, "access_denied"):
            self.auth.finish_login("https://x/callback?error=access_denied&error_description=nope")

    def test_missing_refresh_token(self):
        url = self.auth.start_login()
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["state"][0]
        self.net.add(is_grant("authorization_code"), 200, {"access_token": "A"})
        with self.assertRaisesRegex(auth.AuthError, "offline_access"):
            self.auth.finish_login(f"?code=C&state={state}")
        self.assertIsNone(self.store.load_token())


class TestRefresh(Base):
    def test_fresh_token_no_network(self):
        self.login()
        n = len(self.net.calls)
        self.assertEqual(self.auth.access_token(), "A1")
        self.assertEqual(len(self.net.calls), n)

    def test_rotation_saved_before_use(self):
        self.login(expires_in=100)  # expires within REFRESH_MARGIN
        self.net.add(is_grant("refresh_token"), 200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 28800})
        self.assertEqual(self.auth.access_token(), "A2")
        call = self.net.calls[-1]["form"]
        self.assertEqual(call["refresh_token"], "R1")
        self.assertNotIn("client_secret", call)
        self.assertEqual(self.store.load_token()["refresh_token"], "R2")

    def test_save_failure_keeps_old_refresh_token(self):
        self.login(expires_in=100)
        self.net.add(is_grant("refresh_token"), 200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 28800})
        with mock.patch.object(self.store, "save_token", side_effect=OSError("dysk pełny")):
            with self.assertRaises(OSError):
                self.auth.access_token()
        self.assertEqual(self.store.load_token()["refresh_token"], "R1")

    def test_login_required_marks_token(self):
        self.login(expires_in=100)
        self.net.add(is_grant("refresh_token"), 401, {"error": "login_required"})
        with self.assertRaises(auth.LoginRequired):
            self.auth.access_token()
        st = self.auth.status()
        self.assertFalse(st["logged_in"])
        self.assertTrue(st["login_required"])
        n = len(self.net.calls)
        with self.assertRaises(auth.LoginRequired):  # no further attempts with a dead token
            self.auth.access_token()
        self.assertEqual(len(self.net.calls), n)

    def test_server_error_is_not_login_required(self):
        self.login(expires_in=100)
        self.net.add(is_grant("refresh_token"), 503, b"busy")
        with self.assertRaises(auth.AuthError) as cm:
            self.auth.access_token()
        self.assertNotIsInstance(cm.exception, auth.LoginRequired)
        self.assertTrue(self.auth.status()["logged_in"])

    def test_keepalive(self):
        self.login()
        self.assertFalse(self.auth.refresh_if_older_than(3600))
        self.clock.t += 7200
        self.net.add(is_grant("refresh_token"), 200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 28800})
        self.assertTrue(self.auth.refresh_if_older_than(3600))

    def test_concurrent_refresh_single_call(self):
        self.login(expires_in=100)
        self.net.add(is_grant("refresh_token"), 200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 28800})
        out = []
        ts = [threading.Thread(target=lambda: out.append(self.auth.access_token())) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(out, ["A2"] * 5)
        self.assertEqual(sum(1 for c in self.net.calls if c["form"].get("grant_type") == "refresh_token"), 1)


class TestPartner(Base):
    def test_register(self):
        self.net.add(is_grant("client_credentials"), 200, {"access_token": "P"})
        self.net.add(lambda c: c["url"].endswith("/api/1/partner_accounts"), 200, {"response": {"ok": True}})
        res = self.auth.register_partner("https://SUC.example.com/")
        self.assertEqual(res["domain"], "suc.example.com")
        body = json.loads(self.net.calls[-1]["data"])
        self.assertEqual(body, {"domain": "suc.example.com"})
        self.assertEqual(self.net.calls[-1]["headers"]["Authorization"], "Bearer P")


def page(n, more=None):
    d = {"data": [{"sessionId": i, "vin": "V", "fees": []} for i in range(n)]}
    if more is not None:
        d["hasMoreData"] = more
    return {"response": d}


class TestApi(Base):
    def setUp(self):
        super().setUp()
        self.login()
        self.api = api.ChargingApi(self.auth, api.Usage(self.dir))

    def test_pagination_stops_on_short_page(self):
        hist = lambda c: "/dx/charging/history" in c["url"]
        self.net.add(hist, 200, page(50))
        self.net.add(hist, 200, page(3))
        self.assertEqual(len(self.api.history()), 53)
        urls = [c["url"] for c in self.net.calls if "history" in c["url"]]
        self.assertIn("pageNo=1", urls[0])
        self.assertIn("pageNo=2", urls[1])
        self.assertNotIn("sortBy", urls[0])

    def test_has_more_false_stops(self):
        self.net.add(lambda c: "history" in c["url"], 200, page(50, more=False))
        self.assertEqual(len(self.api.history()), 50)

    def test_401_refresh_and_retry_once(self):
        hist = lambda c: "history" in c["url"]
        self.net.add(hist, 401, {"error": "token expired"})
        self.net.add(is_grant("refresh_token"), 200, {"access_token": "A2", "refresh_token": "R2", "expires_in": 28800})
        self.net.add(hist, 200, page(1))
        self.assertEqual(len(self.api.history()), 1)
        self.assertEqual(self.net.calls[-1]["headers"]["Authorization"], "Bearer A2")
        self.assertEqual(self.api.usage.read()[next(iter(self.api.usage.read()))]["history"], 2)

    def test_error_raises(self):
        self.net.add(lambda c: "history" in c["url"], 403, {"error": "missing scopes"})
        with self.assertRaisesRegex(api.ApiError, "403"):
            self.api.history()

    def test_extract_page_shapes(self):
        self.assertEqual(len(api.extract_page({"data": [1, 2]})[0]), 2)
        self.assertEqual(len(api.extract_page({"response": [1]})[0]), 1)
        self.assertEqual(api.extract_page("x"), ([], {}))

    def test_probe_writes_files(self):
        s = {"sessionId": 1, "vin": "VIN123", "siteLocationName": "Kraków",
             "chargeStartDateTime": "2026-01-01T10:00:00+01:00",
             "fees": [{"feeType": "CHARGING", "uom": "kwh", "currencyCode": "PLN", "totalDue": 50.0, "status": "PAID"}],
             "invoices": [{"fileName": "f.pdf", "contentId": "abc", "invoiceType": "IMMEDIATE"}]}
        self.net.add(lambda c: "history" in c["url"], 200, {"response": {"data": [s], "totalResults": 1}})
        summ = api.run_probe(self.api, self.dir, 30)
        self.assertEqual(summ["sessions"], 1)
        self.assertEqual(summ["fee_types"], ["CHARGING"])
        self.assertEqual(summ["shape"]["vin"], "str")
        self.assertNotIn("VIN123", json.dumps(summ["shape"]))
        self.assertEqual(summ["meta_first_page"], {"totalResults": 1})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "probe.json")))


class TestPanel(Base):
    def setUp(self):
        super().setUp()
        usage = api.Usage(self.dir)
        opts = {"client_id": "cid", "client_secret": "sec", "region": "eu",
                "domain": "suc.example.com", "redirect_uri": "https://suc.example.com/callback"}
        self.app = panel.App(opts=opts, auth=self.auth, api=api.ChargingApi(self.auth, usage),
                             usage=usage, data_dir=self.dir)

    def serve(self, peers):
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), panel.make_handler(self.app, peers))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def req(self, base, method, path, body=None):
        r = urllib.request.Request(base + path, method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_rejects_non_ingress(self):
        # The test calls the panel through plain urllib.urlopen (FakeNet only swaps http_util.send).
        base = self.serve({"10.9.9.9"})
        code, _ = self.req(base, "GET", "/api/status")
        self.assertEqual(code, 403)
        code, _ = self.req(base, "POST", "/api/logout", {})
        self.assertEqual(code, 403)

    def test_status_and_html(self):
        base = self.serve({"127.0.0.1"})
        code, body = self.req(base, "GET", "/api/status")
        self.assertEqual(code, 200)
        s = json.loads(body)
        self.assertEqual(s["config_missing"], [])
        self.assertFalse(s["token"]["logged_in"])
        self.assertNotIn("sec", body.decode())  # client_secret never goes out
        code, html = self.req(base, "GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"api/status", html)
        self.assertNotIn(b'"/api/', html)  # relative paths only (Ingress prefix)

    def test_probe_without_login_is_401(self):
        base = self.serve({"127.0.0.1"})
        code, body = self.req(base, "POST", "/api/probe", {"days": 10})
        self.assertEqual(code, 401)
        self.assertTrue(json.loads(body)["login_required"])

    def test_login_flow_via_panel(self):
        base = self.serve({"127.0.0.1"})
        code, body = self.req(base, "POST", "/api/login/start", {})
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(json.loads(body)["url"]).query)["state"][0]
        self.net.add(is_grant("authorization_code"), 200, {"access_token": "A", "refresh_token": "R", "expires_in": 3600})
        code, body = self.req(base, "POST", "/api/login/finish", {"url": f"https://suc.example.com/callback?code=C&state={state}"})
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["logged_in"])
        self.assertNotIn(b'"R"', body)  # the refresh token never goes out


if __name__ == "__main__":
    unittest.main()
