"""End-to-end checks of the tunnel entrance (RemoteHandler) and LAN settings."""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import scheduler
import server
from test_scheduler import CFG, FakeBridge, sched

scheduler.VERIFY_DELAY = 0
server.time.sleep = lambda s: None  # skip the wrong-password delay in tests


class RemoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = scheduler.Store(tempfile.mkdtemp())
        store.data["schedules"] = [sched()]
        cls.bridge = FakeBridge()
        cls.ctl = scheduler.Controller("h", CFG, store, call=cls.bridge)
        server.Handler.ctl = cls.ctl
        server.RemoteHandler.ctl = cls.ctl
        cls.lan = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.remote = ThreadingHTTPServer(("127.0.0.1", 0), server.RemoteHandler)
        for s in (cls.lan, cls.remote):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.lan.shutdown()
        cls.remote.shutdown()

    def req(self, srv, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def set_password(self, pw="hunter2hunter2"):
        server.RemoteHandler.limiter = server.auth.LoginLimiter()
        st, _, body = self.req(self.lan, "PUT", "/api/remote", json.dumps({"password": pw}),
                               {"Content-Type": "application/json"})
        self.assertEqual(st, 200)
        return json.loads(body)

    def login(self, pw="hunter2hunter2", ip="9.9.9.9"):
        return self.req(self.remote, "POST", "/admin/login", urlencode({"password": pw}),
                        {"Content-Type": "application/x-www-form-urlencoded", "CF-Connecting-IP": ip})

    def test_public_pages(self):
        st, _, body = self.req(self.remote, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Roofline Live", body)
        st, _, body = self.req(self.remote, "GET", "/api/live")
        live = json.loads(body)
        self.assertEqual(st, 200)
        self.assertIn("upcoming", live)
        self.assertNotIn(b"serial", body)
        self.assertNotIn(b"ntfy", body)
        self.assertEqual(self.req(self.remote, "GET", "/static/favicon.png")[0], 200)
        self.assertEqual(self.req(self.remote, "GET", "/sw.js")[0], 200)

    def test_everything_else_needs_login(self):
        self.assertEqual(self.req(self.remote, "GET", "/api/state")[0], 401)
        self.assertEqual(self.req(self.remote, "GET", "/api/settings")[0], 401)
        self.assertEqual(self.req(self.remote, "POST", "/api/off")[0], 401)
        self.assertEqual(self.req(self.remote, "PUT", "/api/schedules", "[]")[0], 401)
        self.assertEqual(self.req(self.remote, "GET", "/go/off")[0], 401)
        st, headers, _ = self.req(self.remote, "GET", "/index.html")
        self.assertEqual((st, headers.get("Location")), (303, "/admin"))
        writes = self.bridge.writes
        self.req(self.remote, "POST", "/go/on")
        self.assertEqual(self.bridge.writes, writes)

    def test_login_session_and_token(self):
        token = self.set_password()["token"]
        st, headers, _ = self.login("wrong password")
        self.assertEqual(st, 401)
        st, headers, _ = self.login()
        self.assertEqual((st, headers.get("Location")), (303, "/admin/"))
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        jar = {"Cookie": cookie.split(";")[0]}
        self.assertEqual(self.req(self.remote, "GET", "/api/state", headers=jar)[0], 200)
        st, _, body = self.req(self.remote, "GET", "/admin/", headers=jar)
        self.assertIn(b"Everlights</title>", body)  # the full app
        st, _, body = self.req(self.remote, "GET", "/", headers=jar)
        self.assertIn(b"Roofline Live", body)  # / stays the live view, signed in or not
        # remote access settings are LAN-only, even when signed in
        self.assertEqual(self.req(self.remote, "GET", "/api/remote", headers=jar)[0], 403)
        self.assertEqual(self.req(self.remote, "PUT", "/api/remote", '{"password":"x"}', jar)[0], 403)
        # settings never include secrets
        st, _, body = self.req(self.remote, "GET", "/api/settings", headers=jar)
        self.assertEqual(set(json.loads(body)), {"ntfyUrl"})
        # Shortcut token
        st, _, body = self.req(self.remote, "GET", "/go/off", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual((st, body.strip()), (200, b"Lights off."))
        self.assertEqual(self.req(self.remote, "GET", "/go/off", headers={"Authorization": "Bearer nope"})[0], 401)
        # a new password signs existing sessions out
        self.set_password("another password")
        self.assertEqual(self.req(self.remote, "GET", "/api/state", headers=jar)[0], 401)

    def test_same_layout_at_home(self):
        st, _, body = self.req(self.lan, "GET", "/")
        self.assertIn(b"Roofline Live", body)
        for path in ("/admin", "/admin/", "/index.html"):
            st, _, body = self.req(self.lan, "GET", path)
            self.assertIn(b"Everlights</title>", body, path)
        self.assertEqual(self.req(self.lan, "GET", "/api/state")[0], 200)  # no password at home
        st, _, body = self.req(self.remote, "GET", "/admin/")
        self.assertIn(b"Sign in", body)  # but the tunnel asks for one

    def test_lockout(self):
        self.set_password()
        for _ in range(server.auth.MAX_FAILURES):
            self.assertEqual(self.login("nope", ip="7.7.7.7")[0], 401)
        self.assertEqual(self.login(ip="7.7.7.7")[0], 429)  # even the right password
        self.assertEqual(self.login(ip="8.8.8.8")[0], 303)


if __name__ == "__main__":
    unittest.main()
