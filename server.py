#!/usr/bin/env python3
"""EverLights web app: pattern chooser, schedules, and a scheduler that confirms.

Environment:
  EVERLIGHTS_HOST   bridge IP (required)
  LAT, LON          location for sunrise/sunset (default: San Francisco)
  TZ                time zone (default America/Los_Angeles)
  DATA_DIR          where schedules and the log live (default ./data)
  PORT              HTTP port (default 8080)
  REASSERT_MINUTES  re-send after each transition, minutes (default 2,10)
  SCHEDULER         set to "off" to serve the app without running schedules
  PUBLIC_PORT       optional second port for a tunnel (e.g. Cloudflare): a public
                    live-show page at /, everything else behind a password at /admin

Never publish PUBLIC_PORT on your network or PORT to the internet: PORT has no
password, by design, for use at home.
"""

import hashlib
import json
import os
import sys
import threading
import time
from http import cookies
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import auth
import everlights
import scheduler

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
INDEX = os.path.join(STATIC, "index.html")
TYPES = {".png": "image/png", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
         ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
def file_version(*names):
    h = hashlib.sha1()
    for n in names:
        with open(os.path.join(ROOT, n), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:7]


SERVER_VERSION = file_version("server.py", "scheduler.py", "everlights.py")


def page_version():
    return file_version(os.path.join("static", "index.html"))


ALIASES = {"/favicon.ico": "favicon.png", "/apple-touch-icon.png": "apple-touch-icon.png",
           "/apple-touch-icon-precomposed.png": "apple-touch-icon.png"}


def make_app():
    host = os.environ.get("EVERLIGHTS_HOST")
    if not host:
        sys.exit("set EVERLIGHTS_HOST to the bridge IP")
    cfg = scheduler.Config(
        lat=float(os.environ.get("LAT", "37.7749")),
        lon=float(os.environ.get("LON", "-122.4194")),
        tz=ZoneInfo(os.environ.get("TZ", "America/Los_Angeles")),
    )
    store = scheduler.Store(os.environ.get("DATA_DIR", os.path.join(ROOT, "data")))
    reassert = [int(x) for x in os.environ.get("REASSERT_MINUTES", "2,10").split(",") if x.strip()]
    return scheduler.Controller(host, cfg, store, reassert_minutes=reassert)


def bridge_summary(ctl):
    info = ctl.call(ctl.host, "GET", "")
    zones = [{
        "serial": z["serial"], "leds": z.get("configuredLength"), "rssi": z.get("rssi"),
        "snr": z.get("snr"), "reboots": z.get("rebootCount"), "health": everlights.zone_health(z),
        "lastResponse": z.get("lastResponseDate"),
    } for z in info.get("zones", [])]
    return {
        "serial": info.get("serial"), "firmware": info.get("firmwareVersion"),
        "ip": info.get("localIp"), "reboots": info.get("rebootCount"),
        "cloud": info.get("serverReachable"), "lastCheckin": info.get("lastServerCheckin"),
    }, zones


def match_sequence(ctl, current):
    if not current or not current.get("pattern"):
        return None
    want = scheduler._norm(current)
    for s in ctl.sequences():
        if scheduler._norm(s) == want:
            return {"id": s["id"], "alias": s.get("alias")}
    return None


def history(ctl, query):
    days = max(1, min(scheduler.HISTORY_DAYS, int((query.get("days") or ["7"])[0])))
    since = datetime.now(ctl.cfg.tz) - timedelta(days=days)
    samples = ctl.store.history("health", since)
    changes = ctl.store.history("changes", since)
    sched = [c for c in changes if c["result"] in ("ok", "gaveup")]
    rssi = [x["rssi"] for x in samples if x.get("ok") and x.get("rssi") is not None]
    return {
        "days": days, "samples": samples, "changes": changes,
        "stats": {
            "scheduled": len(sched),
            "firstTry": sum(1 for c in sched if c["result"] == "ok" and c["tries"] <= 1),
            "afterRetries": sum(1 for c in sched if c["result"] == "ok" and c["tries"] > 1),
            "gaveUp": sum(1 for c in sched if c["result"] == "gaveup"),
            "alreadySet": sum(1 for c in sched if c.get("alreadySet")),
            "outside": sum(1 for c in changes if c["result"] == "outside"),
            "rssiMedian": sorted(rssi)[len(rssi) // 2] if rssi else None,
            "unreachable": sum(1 for x in samples if not x.get("ok")),
            "samples": len(samples),
        },
    }


_live_cache = {"at": 0.0, "data": None}
_live_lock = threading.Lock()


def live_state(ctl):
    """What the public page shows: the current pattern and the next few shows.
    Cached for 15 seconds so visitors can't flood the bridge with requests."""
    with _live_lock:
        if _live_cache["data"] and time.time() - _live_cache["at"] < 15:
            return _live_cache["data"]
        now = datetime.now(ctl.cfg.tz)
        out = {"tz": str(ctl.cfg.tz), "updated": now.isoformat(timespec="seconds"), "reachable": True}
        try:
            cur = ctl.current() or {}
            match = match_sequence(ctl, cur)
            out.update(on=bool(cur.get("pattern")), name=match["alias"] if match else None,
                       pattern=cur.get("pattern", []), effects=cur.get("effects", []))
        except Exception:
            out.update(reachable=False, on=None, name=None, pattern=[], effects=[])
        shows = []
        for u in scheduler.upcoming(ctl.store.schedules, now, ctl.cfg, limit=8):
            item = {"at": u["at"], "action": u["action"], "name": u["name"]}
            if u["action"] == "on":
                seq = ctl.sequence(u["sequenceId"]) if u.get("sequenceId") else None
                if seq:
                    item.update(pattern=seq.get("pattern", []), effects=seq.get("effects", []))
            shows.append(item)
        out["upcoming"] = shows
        _live_cache.update(at=time.time(), data=out)
        return out


class Handler(BaseHTTPRequestHandler):
    ctl = None  # set in main

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, name):
        ext = os.path.splitext(name)[1]
        path = os.path.join(STATIC, os.path.basename(name))
        if ext not in TYPES or not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[ext])
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"null") if n else None

    def handle_errors(self, fn):
        try:
            fn()
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except LookupError as e:
            self.send_json({"error": str(e)}, 404)
        except Exception as e:
            self.send_json({"error": f"bridge: {e}"}, 502)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                body = f.read()
            body = body.replace(b"__PAGE_VERSION__", hashlib.sha1(body).hexdigest()[:7].encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path.startswith("/go/"):
            return self.handle_go(url)
        name = ALIASES.get(url.path) or (url.path[len("/static/"):] if url.path.startswith("/static/") else None)
        if name is not None:
            return self.send_static(name)
        routes = {
            "/api/state": self.get_state,
            "/api/patterns": lambda: self.send_json(self.ctl.sequences(
                refresh="refresh" in parse_qs(url.query))),
            "/api/schedules": lambda: self.send_json(self.ctl.store.schedules),
            "/api/log": lambda: self.send_json(list(reversed(self.ctl.store.log))),
            "/api/settings": lambda: self.send_json(self.ctl.settings),
            "/api/history": lambda: self.send_json(history(self.ctl, parse_qs(url.query))),
            "/api/live": lambda: self.send_json(live_state(self.ctl)),
            "/api/remote": lambda: self.send_json(self.remote_settings()),
        }
        fn = routes.get(url.path)
        if not fn:
            return self.send_json({"error": "not found"}, 404)
        self.handle_errors(fn)

    def get_state(self):
        ctl = self.ctl
        now = datetime.now(ctl.cfg.tz)
        out = {
            "now": now.isoformat(), "tz": str(ctl.cfg.tz), "favorites": ctl.store.data["favorites"],
            "serverVersion": SERVER_VERSION, "pageVersion": page_version(),
            "scheduler": ctl.status(),
            "upcoming": scheduler.upcoming(ctl.store.schedules, now, ctl.cfg),
        }
        key, s = scheduler.desired(ctl.store.schedules, now, ctl.cfg)
        out["desired"] = None if key is None else {"on": bool(s), "name": s["name"] if s else None}
        try:
            out["bridge"], out["zones"] = bridge_summary(ctl)
            cur = ctl.current()
            out["current"] = {"pattern": cur.get("pattern", []), "effects": cur.get("effects", []),
                              "match": match_sequence(ctl, cur)}
        except Exception as e:
            out["bridgeError"] = str(e)
        self.send_json(out)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path.startswith("/go/"):
            return self.handle_go(url)
        self.handle_errors(self.post)

    def send_text(self, text, code=200):
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def find_pattern(self, name):
        name = name.strip().lower()
        seqs = self.ctl.sequences()
        exact = [s for s in seqs if (s.get("alias") or "").lower() == name]
        if exact:
            return exact[0]
        partial = [s for s in seqs if name in (s.get("alias") or "").lower()]
        if len(partial) > 1:
            raise ValueError("more than one pattern matches: " + ", ".join(s["alias"] for s in partial[:5]))
        return partial[0] if partial else None

    def handle_go(self, url):
        """Plain links for iOS Shortcuts, Siri and bookmarks. They answer in a
        sentence so a Shortcut can show or speak the result."""
        ctl, action = self.ctl, url.path[len("/go/"):].strip("/")
        try:
            if action == "off":
                ok = ctl.manual(None)
                return self.send_text("Lights off." if ok else "Sent off, but the bridge didn't confirm.")
            if action == "on":
                now = datetime.now(ctl.cfg.tz)
                _, sched = scheduler.desired(ctl.store.schedules, now, ctl.cfg)
                ids = [sched["sequenceId"]] if sched else []
                ids += [ctl.store.data.get("lastPattern")] + ctl.store.data["favorites"]
                seq = next((x for x in (ctl.sequence(i) for i in ids if i) if x), None) \
                    or self.find_pattern("warm")
                if not seq:
                    return self.send_text("No pattern to turn on. Use /go/play?name=...", 404)
            elif action == "play":
                name = (parse_qs(url.query).get("name") or [""])[0]
                if not name:
                    return self.send_text("Add ?name= with a pattern name.", 400)
                seq = self.find_pattern(name)
                if not seq:
                    return self.send_text(f"No pattern called {name}.", 404)
            else:
                return self.send_text("Use /go/on, /go/off or /go/play?name=...", 404)
            ok = ctl.manual(seq, seq.get("alias"))
            self.remember(seq)
            alias = seq.get("alias") or "the pattern"
            self.send_text(f"Playing {alias}." if ok else f"Sent {alias}, but the bridge didn't confirm.")
        except ValueError as e:
            self.send_text(str(e), 400)
        except Exception as e:
            self.send_text(f"Couldn't reach the bridge: {e}", 502)

    def remote_settings(self):
        st = self.ctl.settings
        return {"passwordSet": bool(st.get("adminPasswordHash")), "token": st.get("apiToken"),
                "publicPort": os.environ.get("PUBLIC_PORT")}

    def remember(self, seq):
        if seq.get("id") and not seq.get("transient"):
            self.ctl.store.data["lastPattern"] = seq["id"]
            self.ctl.store.save()

    def do_PUT(self):
        self.handle_errors(self.put)

    def post(self):
        path = urlparse(self.path).path
        body = self.read_json() or {}
        ctl = self.ctl
        if path == "/api/play":
            if body.get("sequenceId"):
                seq = ctl.sequence(body["sequenceId"])
                if not seq:
                    raise LookupError("unknown pattern")
                label = seq.get("alias")
            else:
                seq = scheduler.validate_pattern(body)
                label = str(body.get("label") or "custom colors")[:60]
            ok = ctl.manual(seq, label)
            if body.get("sequenceId"):
                self.remember(seq)
            return self.send_json({"ok": ok})
        if path == "/api/remote/token":
            self.ctl.settings["apiToken"] = auth.new_secret()
            self.ctl.store.save()
            self.ctl.log("info", "New Shortcut token created; the old one stops working")
            return self.send_json(self.remote_settings())
        if path == "/api/settings/test":
            err = ctl.notify("EverLights test alert", "Alerts from the EverLights app are working.",
                             3, ["bulb"], wait=True)
            return self.send_json({"ok": not err, "error": err})
        if path == "/api/off":
            return self.send_json({"ok": ctl.manual(None)})
        if path == "/api/patterns":
            pat = scheduler.validate_pattern(body)
            with ctl.lock:
                mine = ctl.store.patterns
                idx = next((i for i, p in enumerate(mine) if p["id"] == pat["id"]), None)
                if idx is None:
                    mine.insert(0, pat)
                else:
                    mine[idx] = pat
                ctl.store.save()
            ctl.log("info", f"Pattern saved: {pat['alias']}")
            return self.send_json(pat)
        if path == "/api/patterns/delete":
            pid = body.get("id")
            with ctl.lock:
                if any(s["sequenceId"] == pid for s in ctl.store.schedules):
                    raise ValueError("a schedule uses this pattern; change that schedule first")
                before = len(ctl.store.patterns)
                ctl.store.data["patterns"] = [p for p in ctl.store.patterns if p["id"] != pid]
                if pid in ctl.store.data["favorites"]:
                    ctl.store.data["favorites"].remove(pid)
                ctl.store.save()
            if before == len(ctl.store.patterns):
                raise LookupError("only patterns made in this app can be deleted")
            return self.send_json({"ok": True})
        if path == "/api/favorites":
            favs = ctl.store.data["favorites"]
            sid = body.get("sequenceId")
            if body.get("on") and sid not in favs:
                favs.append(sid)
            elif not body.get("on") and sid in favs:
                favs.remove(sid)
            ctl.store.save()
            return self.send_json(favs)
        self.send_json({"error": "not found"}, 404)

    def put(self):
        if urlparse(self.path).path == "/api/remote":
            password = str((self.read_json() or {}).get("password") or "")
            st = self.ctl.settings
            st["adminPasswordHash"] = auth.hash_password(password)
            st["sessionSecret"] = auth.new_secret()  # signs everyone out
            st.setdefault("apiToken", auth.new_secret())
            self.ctl.store.save()
            self.ctl.log("info", "Remote password set; existing remote sessions signed out")
            return self.send_json(self.remote_settings())
        if urlparse(self.path).path == "/api/settings":
            body = self.read_json() or {}
            url = str(body.get("ntfyUrl") or "").strip()
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("the alert URL should start with https://")
            self.ctl.settings["ntfyUrl"] = url
            self.ctl.store.save()
            self.ctl.log("info", "Alert settings saved" + ("" if url else " (alerts off)"))
            return self.send_json(self.ctl.settings)
        if urlparse(self.path).path != "/api/schedules":
            return self.send_json({"error": "not found"}, 404)
        items = self.read_json()
        if not isinstance(items, list):
            raise ValueError("expected a list of schedules")
        clean = [scheduler.validate_schedule(s) for s in items]
        ctl = self.ctl
        with ctl.lock:
            ctl.store.data["schedules"] = clean
            ctl.store.save()
        ctl.log("info", f"Schedules saved ({len(clean)})")
        self.send_json(clean)


LOGIN = os.path.join(STATIC, "login.html")
LIVE = os.path.join(STATIC, "live.html")
PUBLIC_PATHS = {"/live", "/api/live", "/sw.js", "/admin", "/admin/", "/admin/login", "/admin/logout"}


class RemoteHandler(Handler):
    """The tunnel entrance. Public: the live show and static files. Everything
    else needs the admin session cookie or the Shortcut bearer token. Remote
    access settings can only be changed from the LAN port."""

    limiter = auth.LoginLimiter()

    def client(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def authed(self):
        st = self.ctl.settings
        if auth.check_token(st.get("apiToken"), self.headers.get("Authorization")):
            return True
        jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        value = jar[auth.COOKIE].value if auth.COOKIE in jar else None
        return auth.check_session(st.get("sessionSecret"), value)

    def send_page(self, path, replace=None, code=200, headers=()):
        with open(path, "rb") as f:
            body = f.read()
        for k, v in (replace or {}).items():
            body = body.replace(k.encode(), v.encode())
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to, headers=()):
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def login_page(self, error="", code=200):
        if not self.ctl.settings.get("adminPasswordHash"):
            error = error or "Remote access isn't set up yet. Set a password in the app's Health tab, on home Wi-Fi."
        self.send_page(LOGIN, {"__ERROR__": error}, code)

    def deny(self, path):
        if path.startswith("/api/"):
            return self.send_json({"error": "sign in at /admin"}, 401)
        if path.startswith("/go/"):
            return self.send_text("Not signed in. Add your Shortcut token as an Authorization header.", 401)
        self.redirect("/admin")

    def gate(self, path):
        """True if the request may continue to the full app."""
        if path.startswith("/api/remote"):
            self.send_json({"error": "remote access settings can only be changed at home"}, 403)
            return False
        if not self.authed():
            self.deny(path)
            return False
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return Handler.do_GET(self) if self.authed() else self.send_page(LIVE)
        if path == "/live":
            return self.send_page(LIVE)
        if path == "/api/live":
            return self.handle_errors(lambda: self.send_json(live_state(self.ctl)))
        if path in ("/admin", "/admin/"):
            return self.redirect("/") if self.authed() else self.login_page()
        if path == "/sw.js":
            return self.send_static("sw.js")
        if path in ALIASES or path.startswith("/static/"):
            return Handler.do_GET(self)
        if self.gate(path):
            Handler.do_GET(self)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/admin/login":
            return self.login()
        if path == "/admin/logout":
            return self.redirect("/admin", [("Set-Cookie", f"{auth.COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")])
        if self.gate(path):
            Handler.do_POST(self)

    def do_PUT(self):
        if self.gate(urlparse(self.path).path):
            Handler.do_PUT(self)

    def login(self):
        who = self.client()
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(n).decode(errors="replace"))
        password = (form.get("password") or [""])[0]
        st = self.ctl.settings
        if self.limiter.blocked(who):
            return self.login_page("Too many wrong passwords. Try again in 15 minutes.", 429)
        if not st.get("adminPasswordHash") or not auth.verify_password(password, st["adminPasswordHash"]):
            self.limiter.failed(who)
            time.sleep(1)
            self.ctl.log("warn", "Remote sign-in failed")
            return self.login_page("That password didn't work.", 401)
        self.limiter.succeeded(who)
        if not st.get("sessionSecret"):
            st["sessionSecret"] = auth.new_secret()
            self.ctl.store.save()
        value = auth.make_session(st["sessionSecret"])
        self.ctl.log("info", "Remote sign-in")
        self.redirect("/", [("Set-Cookie", f"{auth.COOKIE}={value}; Path=/; Max-Age={auth.SESSION_DAYS * 86400}; "
                                           "HttpOnly; Secure; SameSite=Lax")])


def main():
    ctl = make_app()
    Handler.ctl = ctl
    port = int(os.environ.get("PORT", "8080"))
    if os.environ.get("SCHEDULER", "on") != "off":
        threading.Thread(target=ctl.run_forever, daemon=True).start()
    else:
        ctl.log("info", "Scheduler is off (SCHEDULER=off); only manual control works")
    public = os.environ.get("PUBLIC_PORT")
    if public:
        RemoteHandler.ctl = ctl
        remote = ThreadingHTTPServer(("0.0.0.0", int(public)), RemoteHandler)
        threading.Thread(target=remote.serve_forever, daemon=True).start()
    ctl.log("info", f"Started; bridge {ctl.host}, listening on :{port}"
                    + (f" (public entrance on :{public})" if public else ""))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
