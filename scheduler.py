"""Schedule evaluation and the send-verify-retry loop for EverLights.

The bridge's own schedules fire once and never check that the zone listened.
This module decides what the lights should be doing right now, sends it,
reads it back, retries with backoff, and re-sends a couple of times after
each transition in case the radio dropped the first one.
"""

import json
import math
import os
import threading
import time
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import everlights

MAX_ATTEMPTS = 20
VERIFY_DELAY = 1.5
CACHE_SECONDS = 300
UNREACHABLE_ALERT = timedelta(minutes=10)
HEALTH_EVERY = timedelta(minutes=5)
HISTORY_DAYS = 30


@dataclass
class Config:
    lat: float
    lon: float
    tz: ZoneInfo


# --- sun ---------------------------------------------------------------------

def _jd_to_utc(jd):
    return datetime.fromtimestamp((jd - 2440587.5) * 86400, tz=timezone.utc)


def sun_times(d, lat, lon):
    """Return (sunrise, sunset) in UTC for local calendar date d (sunrise equation)."""
    jd0 = 2440587.5 + datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() / 86400
    n = math.ceil(jd0 - 2451545.0 + 0.0008)
    jstar = n - lon / 360
    m = math.radians((357.5291 + 0.98560028 * jstar) % 360)
    c = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    transit = 2451545.0 + jstar + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = (math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl)) / (
        math.cos(phi) * math.cos(decl))
    w = math.degrees(math.acos(max(-1.0, min(1.0, cos_w))))
    return _jd_to_utc(transit - w / 360), _jd_to_utc(transit + w / 360)


# --- schedules -----------------------------------------------------------------
#
# A schedule is a dict:
#   id, name, enabled, sequenceId,
#   start / end: {"kind": "sunset"|"sunrise"|"time", "offset": minutes, "time": "HH:MM"}
#   days: weekday numbers, Monday=0 (empty means every day)
#   dateFrom / dateTo: "MM-DD", optional, may wrap the new year
# Earlier schedules in the list win when windows overlap.

def resolve(anchor, d, cfg):
    kind = anchor.get("kind", "time")
    if kind == "time":
        h, m = (int(x) for x in anchor["time"].split(":"))
        return datetime(d.year, d.month, d.day, h, m, tzinfo=cfg.tz)
    rise, set_ = sun_times(d, cfg.lat, cfg.lon)
    base = (rise if kind == "sunrise" else set_).astimezone(cfg.tz)
    return base + timedelta(minutes=int(anchor.get("offset") or 0))


def runs_on(s, d):
    days = s.get("days") or range(7)
    if d.weekday() not in days:
        return False
    lo, hi = s.get("dateFrom"), s.get("dateTo")
    if lo and hi:
        md = d.strftime("%m-%d")
        return lo <= md <= hi if lo <= hi else (md >= lo or md <= hi)
    return True


def window(s, d, cfg):
    if not runs_on(s, d):
        return None
    start = resolve(s["start"], d, cfg)
    end = resolve(s["end"], d, cfg)
    if end <= start:
        end = resolve(s["end"], d + timedelta(days=1), cfg)
    return start, end


def desired(schedules, now, cfg):
    """Return (key, schedule) for what the lights should be doing at `now`.

    key is None when no schedule is enabled (leave the lights alone), "off"
    between windows, or "<id>:<pattern>@<window start>" inside one. A new key
    means a transition, so editing the active schedule's pattern re-sends it
    but editing only its end time does not.
    """
    enabled = [s for s in schedules if s.get("enabled", True)]
    if not enabled:
        return None, None
    now = now.astimezone(cfg.tz)
    today = now.date()
    for s in enabled:
        for d in (today, today - timedelta(days=1)):
            w = window(s, d, cfg)
            if w and w[0] <= now < w[1]:
                return f"{s['id']}:{s['sequenceId']}@{w[0].isoformat()}", s
    return "off", None


def upcoming(schedules, now, cfg, limit=10, horizon_days=21):
    """List the next transitions as {"at", "action", "name", "sequenceId"}."""
    enabled = [s for s in schedules if s.get("enabled", True)]
    if not enabled:
        return []
    today = now.astimezone(cfg.tz).date()
    edges = set()
    for s in enabled:
        for i in range(-1, horizon_days):
            w = window(s, today + timedelta(days=i), cfg)
            if w:
                edges.update(t for t in w if t > now)
    out, prev = [], desired(schedules, now, cfg)[0]
    for t in sorted(edges):
        key, s = desired(schedules, t + timedelta(seconds=1), cfg)
        if key != prev:
            out.append({"at": t.isoformat(), "action": "on" if s else "off",
                        "name": s["name"] if s else None,
                        "sequenceId": s["sequenceId"] if s else None})
            prev = key
            if len(out) >= limit:
                break
    return out


def validate_schedule(s):
    s = dict(s)
    s.setdefault("id", uuid.uuid4().hex[:12])
    s["name"] = str(s.get("name") or "Untitled").strip()[:80]
    s["enabled"] = bool(s.get("enabled", True))
    if not s.get("sequenceId"):
        raise ValueError("schedule needs a pattern")
    for k in ("start", "end"):
        a = s.get(k) or {}
        kind = a.get("kind", "time")
        if kind not in ("time", "sunset", "sunrise"):
            raise ValueError(f"bad {k} kind: {kind}")
        if kind == "time":
            h, m = (int(x) for x in str(a.get("time", "")).split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(f"bad {k} time")
            s[k] = {"kind": "time", "time": f"{h:02d}:{m:02d}"}
        else:
            s[k] = {"kind": kind, "offset": int(a.get("offset") or 0)}
    s["days"] = sorted({int(x) for x in s.get("days") or [] if 0 <= int(x) <= 6})
    for k in ("dateFrom", "dateTo"):
        v = s.get(k)
        if v:
            datetime.strptime(f"2000-{v}", "%Y-%m-%d")
        s[k] = v or None
    if bool(s["dateFrom"]) != bool(s["dateTo"]):
        raise ValueError("date range needs both ends")
    return s


MY_GROUP = "Mine/"
PERSONAL_GROUP = "Personal"


def validate_pattern(p):
    """Clean a pattern made in the app: {id, alias, pattern, effects}."""
    colors = []
    for c in p.get("pattern") or []:
        c = str(c).lstrip("#").upper()
        if len(c) not in (6, 8) or any(ch not in "0123456789ABCDEF" for ch in c):
            raise ValueError(f"not a hex color: {c}")
        colors.append(c)
    # RRGGBBWW drives the strip's white LEDs; the bridge wants one format per pattern.
    rgbw = any(len(c) == 8 for c in colors)
    if rgbw:
        colors = [c if len(c) == 8 else c + "00" for c in colors]
    if not colors:
        raise ValueError("a pattern needs at least one color")
    if len(colors) > 100:
        raise ValueError("at most 100 colors")
    effects = []
    for e in (p.get("effects") or [])[:2]:
        kind = str(e.get("effectType") or "")
        if not kind or not kind.isalnum():
            raise ValueError(f"bad effect: {kind!r}")
        effects.append({"effectType": kind, "value": max(0, min(255, int(e.get("value", 128))))})
    pid = str(p.get("id") or "")
    if not pid.startswith("mine-"):
        pid = "mine-" + uuid.uuid4().hex[:10]
    out = {"id": pid, "alias": str(p.get("alias") or "My pattern").strip()[:60],
           "groups": [MY_GROUP], "colorMode": "rgbw" if rgbw else "rgb",
           "pattern": colors, "effects": effects}
    if p.get("importedFrom"):
        out["importedFrom"] = str(p["importedFrom"])
    return out


# --- storage -----------------------------------------------------------------

class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "state.json")
        self.log_path = os.path.join(data_dir, "log.jsonl")
        self.history_paths = {k: os.path.join(data_dir, f"{k}.jsonl") for k in ("health", "changes")}
        self.lock = threading.Lock()
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            self.data = {}
        self.data.setdefault("schedules", [])
        self.data.setdefault("favorites", [])
        self.data.setdefault("patterns", [])
        self.log = deque(maxlen=500)
        try:
            with open(self.log_path) as f:
                for line in f:
                    self.log.append(json.loads(line))
        except FileNotFoundError:
            pass

    @property
    def schedules(self):
        return self.data["schedules"]

    @property
    def patterns(self):
        return self.data["patterns"]

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def record(self, kind, entry):
        """Append a history row (health samples or change outcomes); keeps 30 days."""
        path = self.history_paths[kind]
        with self.lock:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            if os.path.getsize(path) > 3_000_000:
                cutoff = (datetime.fromisoformat(entry["at"]) - timedelta(days=HISTORY_DAYS)).isoformat()
                rows = [r for r in self.history(kind) if r["at"] >= cutoff]
                with open(path, "w") as f:
                    f.writelines(json.dumps(r) + "\n" for r in rows)

    def history(self, kind, since=None):
        rows = []
        try:
            with open(self.history_paths[kind]) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if since is None or datetime.fromisoformat(r["at"]) >= since:
                        rows.append(r)
        except FileNotFoundError:
            pass
        return rows

    def add_log(self, level, message, tz):
        entry = {"at": datetime.now(tz).isoformat(timespec="seconds"), "level": level, "message": message}
        with self.lock:
            self.log.append(entry)
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            if len(self.log) == self.log.maxlen and os.path.getsize(self.log_path) > 1_000_000:
                with open(self.log_path, "w") as f:
                    f.writelines(json.dumps(e) + "\n" for e in self.log)
        print(f"[{entry['at']}] {level}: {message}", flush=True)


# --- controller ----------------------------------------------------------------

def _norm(seq):
    pattern = [p.upper() for p in (seq or {}).get("pattern") or []]
    effects = sorted((e.get("effectType"), int(e.get("value", 0))) for e in (seq or {}).get("effects") or [])
    return pattern, effects


class Controller:
    def __init__(self, host, cfg, store, reassert_minutes=(2, 10), call=everlights.request):
        self.host, self.cfg, self.store = host, cfg, store
        self.reassert = list(reassert_minutes)
        self.call = call
        self.lock = threading.RLock()
        self.applied_key = None
        self.pending = None
        self._seqs = (0.0, [])
        self._seqs_failed = False
        self._zones = (0.0, [])
        self.last_seen = None  # normalized pattern last read from the first zone
        self.observations = 0
        self.unreachable_since = None
        self.unreachable_alerted = False
        self.reboots = None  # {"bridge" or zone serial: reboot count}
        self.next_health = None
        self.alert_sink = None  # tests replace the HTTP post with a callable

    @property
    def settings(self):
        return self.store.data.setdefault("settings", {})

    def notify(self, title, message, priority=3, tags=(), wait=False):
        """Push an alert to ntfy if a topic URL is set. Returns an error string or None."""
        if self.alert_sink:
            self.alert_sink(title, message, priority, list(tags))
            return None
        url = self.settings.get("ntfyUrl")
        if not url:
            return "no alert URL set"

        def post():
            req = urllib.request.Request(url, data=message.encode(), method="POST", headers={
                "Title": title.encode("ascii", "replace").decode(), "Priority": str(priority),
                "Tags": ",".join(tags)})
            try:
                urllib.request.urlopen(req, timeout=10).read()
            except Exception as e:
                self.log("warn", f"Couldn't send alert: {e}")
                return str(e)
            return None

        if wait:
            return post()
        threading.Thread(target=post, daemon=True).start()
        return None

    def log(self, level, message):
        self.store.add_log(level, message, self.cfg.tz)

    # bridge access

    def bridge_sequences(self, refresh=False):
        """The bridge's saved patterns. If the bridge can't list them (it's offline,
        or its SD card can't be read), fall back to the last list it gave us, which
        is kept on disk so it survives restarts."""
        ts, seqs = self._seqs
        if refresh or not seqs or time.time() - ts > CACHE_SECONDS:
            try:
                fresh = self.call(self.host, "GET", "sequences")
                if not isinstance(fresh, list):
                    raise ValueError("unexpected pattern list")
            except Exception as e:
                cached = seqs or self.store.data.get("bridgeSequences") or []
                if not self._seqs_failed:
                    self.log("warn", f"Bridge couldn't list its patterns ({e}); "
                                     f"using the last known list ({len(cached)})")
                self._seqs_failed = True
                self._seqs = (time.time(), cached)
                return cached
            if self._seqs_failed:
                self.log("info", "Bridge is listing its patterns again")
            self._seqs_failed = False
            self._seqs = (time.time(), fresh)
            if fresh != self.store.data.get("bridgeSequences"):
                self.store.data["bridgeSequences"] = fresh
                self.store.save()
            self.import_personal(fresh)
            seqs = fresh
        return seqs

    def import_personal(self, bridge_seqs):
        """Copy patterns saved on the bridge from the EverLights app ("Personal")
        into this app, and point schedules and favorites at the copies."""
        with self.lock:
            done = {p.get("importedFrom") for p in self.store.patterns}
            changed = False
            for s in bridge_seqs:
                if s["id"] in done or not any(g.startswith(PERSONAL_GROUP) for g in s.get("groups", [])):
                    continue
                try:
                    mine = validate_pattern({"alias": s.get("alias"), "pattern": s.get("pattern"),
                                             "effects": s.get("effects"), "importedFrom": s["id"]})
                except ValueError as e:
                    self.log("warn", f"Couldn't import {s.get('alias')}: {e}")
                    continue
                self.store.patterns.append(mine)
                for sched in self.store.schedules:
                    if sched.get("sequenceId") == s["id"]:
                        sched["sequenceId"] = mine["id"]
                favs = self.store.data["favorites"]
                self.store.data["favorites"] = [mine["id"] if f == s["id"] else f for f in favs]
                self.log("info", f"Imported {mine['alias']} from the bridge into Mine")
                changed = True
            if changed:
                self.store.save()

    def sequences(self, refresh=False):
        """Patterns made in this app first, then the bridge's saved ones
        (minus any already imported)."""
        bridge = self.bridge_sequences(refresh)
        imported = {p.get("importedFrom") for p in self.store.patterns}
        return self.store.patterns + [s for s in bridge if s["id"] not in imported]

    def sequence(self, seq_id):
        for s in self.store.patterns:
            if s["id"] == seq_id or s.get("importedFrom") == seq_id:
                return s
        for s in self.sequences():
            if s["id"] == seq_id:
                return s
        for s in self.sequences(refresh=True):
            if s["id"] == seq_id:
                return s
        return None

    def zone_serials(self):
        ts, zones = self._zones
        if not zones or time.time() - ts > CACHE_SECONDS:
            zones = [z["serial"] for z in self.call(self.host, "GET", "zones")]
            self._zones = (time.time(), zones)
        return zones

    def target_for(self, seq):
        target = {"pattern": seq["pattern"], "effects": seq.get("effects", [])}
        if seq.get("colorMode") and seq["colorMode"] != "none":
            target["colorMode"] = seq["colorMode"]
        return target

    def send(self, target):
        """Send target (None = off) to every zone, then read it back."""
        serials = self.zone_serials()
        for serial in serials:
            if target is None:
                self.call(self.host, "DELETE", f"zones/{serial}/sequence")
            else:
                self.call(self.host, "POST", f"zones/{serial}/sequence", target)
        time.sleep(VERIFY_DELAY)
        want = _norm(target) if target else ([], [])
        seen = [_norm(self.call(self.host, "GET", f"zones/{serial}/sequence")) for serial in serials]
        if seen:
            self.last_seen = seen[0]
        return all(x == want for x in seen)

    def describe(self, seq):
        if not (seq or {}).get("pattern"):
            return "off"
        want = _norm(seq)
        try:
            for s in self.sequences():
                if _norm(s) == want:
                    return s.get("alias") or "a saved pattern"
        except Exception:
            pass
        return "custom colors"

    def observe(self, now):
        """Log changes to the lights that this app didn't make (the bridge's own
        schedule, or someone using the EverLights app), and alert if the bridge
        stops answering."""
        try:
            cur = self.current()
        except Exception as e:
            if self.unreachable_since is None:
                self.unreachable_since = now
                self.log("warn", f"Bridge not answering: {e}")
            elif not self.unreachable_alerted and now - self.unreachable_since >= UNREACHABLE_ALERT:
                self.unreachable_alerted = True
                self.notify("EverLights bridge unreachable",
                            f"No answer from the bridge at {self.host} since "
                            f"{self.unreachable_since.strftime('%-I:%M %p')}. Scheduled changes can't happen.",
                            4, ["warning"])
            return
        if self.unreachable_since is not None:
            down = int((now - self.unreachable_since).total_seconds() // 60)
            self.log("info", f"Bridge answering again after {down} min")
            if self.unreachable_alerted:
                self.notify("EverLights bridge is back", f"Answering again after {down} minutes.", 3, ["white_check_mark"])
            self.unreachable_since, self.unreachable_alerted = None, False
        seen = _norm(cur)
        if self.last_seen is not None and seen != self.last_seen:
            what = self.describe(cur)
            self.log("info", f"Lights changed outside this app: now {what}")
            self.store.record("changes", {"at": now.isoformat(timespec="seconds"), "result": "outside",
                                          "name": what, "action": "off" if what == "off" else "on"})
        self.last_seen = seen
        self.observations += 1

    def check_health(self, now):
        """Every few minutes, alert when the bridge or a zone controller has rebooted."""
        if self.next_health and now < self.next_health:
            return
        self.next_health = now + HEALTH_EVERY
        stamp = now.isoformat(timespec="seconds")
        try:
            info = self.call(self.host, "GET", "")
        except Exception:
            self.store.record("health", {"at": stamp, "ok": False})
            return
        zone = (info.get("zones") or [{}])[0]
        self.store.record("health", {"at": stamp, "ok": True, "rssi": zone.get("rssi"), "snr": zone.get("snr"),
                                     "bridgeReboots": info.get("rebootCount"),
                                     "zoneReboots": zone.get("rebootCount")})
        counts = {"bridge": info.get("rebootCount")}
        counts.update({z["serial"]: z.get("rebootCount") for z in info.get("zones", [])})
        if self.reboots is not None:
            for k, n in counts.items():
                old = self.reboots.get(k)
                if n is not None and old is not None and n > old:
                    what = "bridge" if k == "bridge" else "roofline controller"
                    self.log("warn", f"The {what} restarted (reboot count {n})")
                    self.notify(f"EverLights {what} restarted",
                                f"Reboot count went from {old} to {n}.", 3, ["arrows_counterclockwise"])
        self.reboots = counts

    def record_change(self, p, now, result):
        self.store.record("changes", {
            "at": p["started"].isoformat(timespec="seconds"), "done": now.isoformat(timespec="seconds"),
            "name": p["name"] or "off", "action": "on" if p["name"] else "off",
            "tries": p["attempts"] + (1 if result == "ok" else 0), "result": result,
            "alreadySet": p["outside"]})

    def current(self):
        serials = self.zone_serials()
        return self.call(self.host, "GET", f"zones/{serials[0]}/sequence") if serials else None

    # actions

    def manual(self, seq=None, label=None, now=None):
        """Play a sequence dict now (None = off). Holds until the next transition."""
        with self.lock:
            target = self.target_for(seq) if seq else None
            try:
                ok = self.send(target)
            except Exception as e:
                self.log("error", f"Manual {label or 'off'} failed: {e}")
                raise
            self.log("info" if ok else "warn",
                     f"Manual: {label or 'off'}" + ("" if ok else " (bridge did not confirm)"))
            key, _ = desired(self.store.schedules, now or datetime.now(self.cfg.tz), self.cfg)
            if key is not None:
                self.applied_key = key
            self.pending = None
            return ok

    def tick(self, now=None):
        now = now or datetime.now(self.cfg.tz)
        with self.lock:
            self.observe(now)
            self.check_health(now)
            key, sched = desired(self.store.schedules, now, self.cfg)
            if key is None:
                self.pending = None
                return
            if key == self.applied_key and self.pending and self.pending["key"] != key:
                self.pending = None
            if key != self.applied_key and (not self.pending or self.pending["key"] != key):
                self.pending = {
                    "key": key, "name": sched["name"] if sched else None,
                    "sequenceId": sched["sequenceId"] if sched else None,
                    "attempts": 0, "confirmed": False, "next": now, "started": now, "outside": False,
                    "reassert": [now + timedelta(minutes=m) for m in self.reassert],
                }
                self.log("info", f"Schedule: {'start ' + sched['name'] if sched else 'lights off'}")
            p = self.pending
            if not p or now < p["next"]:
                return

            label = p["name"] or "off"
            err = None
            try:
                target = None
                if sched:
                    seq = self.sequence(sched["sequenceId"])
                    if seq is None:
                        raise LookupError("pattern no longer exists on the bridge")
                    target = self.target_for(seq)
                want = _norm(target) if target else ([], [])
                # On the first tick after a restart there's no history to blame.
                if (not p["confirmed"] and not p["attempts"] and self.observations > 1
                        and self.last_seen == want):
                    self.log("info", f"{label}: already {'showing' if target else 'off'} before "
                                     "this app sent anything (changed outside this app)")
                    p["outside"] = True
                ok = self.send(target)
            except Exception as e:
                ok, err = False, str(e)

            if ok:
                if not p["confirmed"]:
                    tries = f" after {p['attempts'] + 1} tries" if p["attempts"] else ""
                    self.log("info", f"Confirmed {label}{tries}")
                    self.record_change(p, now, "ok")
                    if p["attempts"]:
                        self.notify("Lights changed, after retries",
                                    f"{label} confirmed{tries}.", 2, ["hourglass"])
                p["confirmed"], p["attempts"] = True, 0
                self.applied_key = key
                if p["reassert"]:
                    p["next"] = p["reassert"].pop(0)
                else:
                    self.pending = None
                return

            p["attempts"] += 1
            if p["attempts"] >= MAX_ATTEMPTS:
                self.log("error", f"Gave up on {label} after {MAX_ATTEMPTS} tries: {err or 'not confirmed'}")
                self.record_change(p, now, "gaveup")
                self.notify("Lights didn't change",
                            f"Gave up on {label} after {MAX_ATTEMPTS} tries: {err or 'bridge did not confirm'}.",
                            4, ["warning"])
                self.applied_key = key
                self.pending = None
                return
            delay = min(30 * 2 ** (p["attempts"] - 1), 300)
            p["next"] = now + timedelta(seconds=delay)
            self.log("warn", f"{label}: try {p['attempts']} failed ({err or 'bridge did not confirm'}), "
                             f"retrying in {delay}s")

    def status(self):
        with self.lock:
            p = self.pending
            return {
                "appliedKey": self.applied_key,
                "pending": None if not p else {
                    "name": p["name"], "attempts": p["attempts"], "confirmed": p["confirmed"],
                    "next": p["next"].isoformat(),
                },
            }

    def run_forever(self, interval=15):
        while True:
            try:
                self.tick()
            except Exception as e:
                self.log("error", f"Scheduler error: {e}")
            time.sleep(interval)
