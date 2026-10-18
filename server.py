#!/usr/bin/env python3
"""EverLights web app: pattern chooser, schedules, and a scheduler that confirms.

Environment:
  EVERLIGHTS_HOST   bridge IP (required)
  LAT, LON          location for sunrise/sunset (default: San Francisco)
  TZ                time zone (default America/Los_Angeles)
  DATA_DIR          where schedules and the log live (default ./data)
  PORT              HTTP port (default 8080)
  REASSERT_MINUTES  re-send after each transition, minutes (default 2,10)
"""

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import everlights
import scheduler

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
INDEX = os.path.join(STATIC, "index.html")
TYPES = {".png": "image/png", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}
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


def main():
    ctl = make_app()
    Handler.ctl = ctl
    port = int(os.environ.get("PORT", "8080"))
    threading.Thread(target=ctl.run_forever, daemon=True).start()
    ctl.log("info", f"Started; bridge {ctl.host}, listening on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
