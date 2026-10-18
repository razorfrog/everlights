import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scheduler
from scheduler import Config, Controller, Store, desired, runs_on, sun_times, upcoming, window

TZ = ZoneInfo("America/Los_Angeles")
CFG = Config(lat=37.77, lon=-122.46, tz=TZ)
scheduler.VERIFY_DELAY = 0


def sched(**kw):
    s = {"id": "a", "name": "Halloween", "enabled": True, "sequenceId": "seq-a",
         "start": {"kind": "sunset", "offset": 0}, "end": {"kind": "time", "time": "22:30"},
         "days": [], "dateFrom": None, "dateTo": None}
    s.update(kw)
    return s


class SunTests(unittest.TestCase):
    def assertNear(self, actual, expected, minutes=4):
        self.assertLess(abs((actual - expected).total_seconds()), minutes * 60, f"{actual} vs {expected}")

    def test_matches_bridge_sunset(self):
        # The bridge's own sunset-based event started at 01:30:01Z on Oct 18 (Oct 17 local).
        _, sunset = sun_times(date(2026, 10, 17), CFG.lat, CFG.lon)
        self.assertNear(sunset, datetime(2026, 10, 18, 1, 30, 1, tzinfo=timezone.utc))

    def test_summer_solstice(self):
        rise, sunset = sun_times(date(2026, 6, 21), CFG.lat, CFG.lon)
        self.assertNear(rise.astimezone(TZ), datetime(2026, 6, 21, 5, 48, tzinfo=TZ), 5)
        self.assertNear(sunset.astimezone(TZ), datetime(2026, 6, 21, 20, 35, tzinfo=TZ), 5)


class WindowTests(unittest.TestCase):
    def test_crosses_midnight(self):
        s = sched(end={"kind": "time", "time": "01:00"})
        start, end = window(s, date(2026, 10, 17), CFG)
        self.assertEqual(end, datetime(2026, 10, 18, 1, 0, tzinfo=TZ))
        key, hit = desired([s], datetime(2026, 10, 18, 0, 30, tzinfo=TZ), CFG)
        self.assertIs(hit, s)

    def test_date_range_wraps_new_year(self):
        s = sched(dateFrom="12-01", dateTo="01-06")
        self.assertTrue(runs_on(s, date(2026, 12, 25)))
        self.assertTrue(runs_on(s, date(2027, 1, 3)))
        self.assertFalse(runs_on(s, date(2027, 1, 7)))

    def test_weekdays(self):
        s = sched(days=[4, 5])  # Fri, Sat
        self.assertTrue(runs_on(s, date(2026, 10, 16)))
        self.assertFalse(runs_on(s, date(2026, 10, 18)))

    def test_offset(self):
        s = sched(start={"kind": "sunset", "offset": -15})
        start, _ = window(s, date(2026, 10, 17), CFG)
        _, sunset = sun_times(date(2026, 10, 17), CFG.lat, CFG.lon)
        self.assertEqual(start, sunset.astimezone(TZ) - timedelta(minutes=15))


class DesiredTests(unittest.TestCase):
    def test_states(self):
        s = sched()
        self.assertEqual(desired([], datetime(2026, 10, 17, 20, tzinfo=TZ), CFG), (None, None))
        self.assertEqual(desired([s], datetime(2026, 10, 17, 12, tzinfo=TZ), CFG)[0], "off")
        key, hit = desired([s], datetime(2026, 10, 17, 20, tzinfo=TZ), CFG)
        self.assertTrue(key.startswith("a:seq-a@"))
        self.assertEqual(desired([s], datetime(2026, 10, 17, 23, tzinfo=TZ), CFG)[0], "off")

    def test_first_schedule_wins(self):
        a = sched()
        b = sched(id="b", name="Party", sequenceId="seq-b", days=[5])
        _, hit = desired([b, a], datetime(2026, 10, 17, 20, tzinfo=TZ), CFG)  # Saturday
        self.assertEqual(hit["id"], "b")

    def test_disabled_ignored(self):
        self.assertEqual(desired([sched(enabled=False)], datetime(2026, 10, 17, 20, tzinfo=TZ), CFG)[0], None)

    def test_upcoming(self):
        now = datetime(2026, 10, 17, 12, tzinfo=TZ)
        events = upcoming([sched()], now, CFG, limit=4)
        self.assertEqual([e["action"] for e in events], ["on", "off", "on", "off"])
        self.assertTrue(events[1]["at"].startswith("2026-10-17T22:30"))

    def test_validate(self):
        s = scheduler.validate_schedule({"name": " X ", "sequenceId": "q",
                                         "start": {"kind": "time", "time": "6:5"},
                                         "end": {"kind": "sunrise", "offset": "10"}, "days": [1, 1, 9]})
        self.assertEqual(s["start"], {"kind": "time", "time": "06:05"})
        self.assertEqual(s["end"], {"kind": "sunrise", "offset": 10})
        self.assertEqual(s["days"], [1])
        with self.assertRaises(ValueError):
            scheduler.validate_schedule({"name": "x"})


class FakeBridge:
    """Zone that drops the first `drops` writes, like a weak radio link."""

    def __init__(self, drops=0):
        self.drops = drops
        self.state = {"pattern": [], "effects": []}
        self.writes = 0

    def __call__(self, host, method, path, body=None):
        if path == "zones":
            return [{"serial": "z1"}]
        if path == "sequences":
            return [{"id": "seq-a", "alias": "Candy Corn", "pattern": ["ff0000"], "effects": [],
                     "colorMode": "rgb"}]
        if method == "GET":
            return self.state
        self.writes += 1
        if self.writes <= self.drops:
            return None
        self.state = {"pattern": [], "effects": []} if method == "DELETE" else \
            {"pattern": body["pattern"], "effects": body["effects"]}
        return None


class ControllerTests(unittest.TestCase):
    def make(self, bridge, schedules):
        store = Store(tempfile.mkdtemp())
        store.data["schedules"] = schedules
        return Controller("h", CFG, store, reassert_minutes=(2,), call=bridge), store

    def test_turns_on_and_reasserts(self):
        bridge = FakeBridge()
        c, _ = self.make(bridge, [sched()])
        t = datetime(2026, 10, 17, 19, tzinfo=TZ)
        c.tick(t)
        self.assertEqual(bridge.state["pattern"], ["ff0000"])
        c.tick(t + timedelta(minutes=1))
        self.assertEqual(bridge.writes, 1)
        c.tick(t + timedelta(minutes=2))
        self.assertEqual(bridge.writes, 2)
        self.assertIsNone(c.pending)
        c.tick(t + timedelta(minutes=30))
        self.assertEqual(bridge.writes, 2)

    def test_retries_dropped_writes(self):
        bridge = FakeBridge(drops=2)
        c, store = self.make(bridge, [sched()])
        t = datetime(2026, 10, 17, 19, tzinfo=TZ)
        c.tick(t)
        c.tick(t + timedelta(seconds=30))
        self.assertEqual(bridge.state["pattern"], [])
        c.tick(t + timedelta(seconds=90))
        self.assertEqual(bridge.state["pattern"], ["ff0000"])
        self.assertTrue(any("after 3 tries" in e["message"] for e in store.log))

    def test_turns_off_after_window(self):
        bridge = FakeBridge()
        c, _ = self.make(bridge, [sched()])
        c.tick(datetime(2026, 10, 17, 19, tzinfo=TZ))
        c.tick(datetime(2026, 10, 17, 22, 31, tzinfo=TZ))
        self.assertEqual(bridge.state["pattern"], [])

    def test_manual_holds_until_next_transition(self):
        bridge = FakeBridge()
        c, _ = self.make(bridge, [sched()])
        t = datetime(2026, 10, 17, 19, tzinfo=TZ)
        c.tick(t)
        c.manual(None, now=t)
        c.tick(t + timedelta(minutes=5))
        self.assertEqual(bridge.state["pattern"], [])

    def test_logs_outside_changes(self):
        bridge = FakeBridge()
        c, store = self.make(bridge, [sched()])
        t = datetime(2026, 10, 17, 12, tzinfo=TZ)
        c.tick(t)  # off, baseline
        bridge.state = {"pattern": ["ff0000"], "effects": []}  # bridge's own schedule fires
        c.tick(t + timedelta(minutes=1))
        self.assertTrue(any("outside this app: now Candy Corn" in e["message"] for e in store.log))

    def test_notes_when_already_on(self):
        bridge = FakeBridge()
        c, store = self.make(bridge, [sched()])
        c.tick(datetime(2026, 10, 17, 18, tzinfo=TZ))
        bridge.state = {"pattern": ["FF0000"], "effects": []}
        c.tick(datetime(2026, 10, 17, 19, tzinfo=TZ))
        msgs = [e["message"] for e in store.log]
        self.assertTrue(any("already showing before this app sent" in m for m in msgs))
        n = len(store.log)
        c.tick(datetime(2026, 10, 17, 19, 0, 30, tzinfo=TZ))
        self.assertFalse(any("outside this app" in e["message"] for e in list(store.log)[n:]))


class AlertTests(unittest.TestCase):
    def make(self, bridge):
        store = Store(tempfile.mkdtemp())
        store.data["schedules"] = [sched()]
        c = Controller("h", CFG, store, reassert_minutes=(), call=bridge)
        alerts = []
        c.alert_sink = lambda *a: alerts.append(a)
        return c, alerts

    def test_retry_success_alerts_low(self):
        c, alerts = self.make(FakeBridge(drops=1))
        t = datetime(2026, 10, 17, 19, tzinfo=TZ)
        c.tick(t)
        c.tick(t + timedelta(seconds=30))
        self.assertEqual([(a[0], a[2]) for a in alerts], [("Lights changed, after retries", 2)])

    def test_give_up_alerts_high(self):
        c, alerts = self.make(FakeBridge(drops=999))
        t = datetime(2026, 10, 17, 19, tzinfo=TZ)
        for i in range(scheduler.MAX_ATTEMPTS):
            c.tick(t + timedelta(hours=i / 10))
        self.assertEqual(alerts[-1][0], "Lights didn't change")
        self.assertEqual(alerts[-1][2], 4)

    def test_unreachable_then_back(self):
        class Flaky(FakeBridge):
            down = False
            def __call__(self, host, method, path, body=None):
                if self.down and path != "zones":
                    raise OSError("timed out")
                return super().__call__(host, method, path, body)
        b = Flaky()
        c, alerts = self.make(b)
        t = datetime(2026, 10, 17, 12, tzinfo=TZ)
        c.tick(t)
        b.down = True
        for m in (1, 5, 11, 12):
            c.tick(t + timedelta(minutes=m))
        self.assertEqual([a[0] for a in alerts], ["EverLights bridge unreachable"])
        b.down = False
        c.tick(t + timedelta(minutes=13))
        self.assertEqual(alerts[-1][0], "EverLights bridge is back")

    def test_reboot_alert(self):
        class Rebooty(FakeBridge):
            count = 5
            def __call__(self, host, method, path, body=None):
                if path == "":
                    return {"rebootCount": self.count, "zones": [{"serial": "z1", "rebootCount": 2}]}
                return super().__call__(host, method, path, body)
        b = Rebooty()
        c, alerts = self.make(b)
        t = datetime(2026, 10, 17, 12, tzinfo=TZ)
        c.tick(t)
        b.count = 6
        c.tick(t + timedelta(minutes=2))
        self.assertEqual(alerts, [])
        c.tick(t + timedelta(minutes=6))
        self.assertEqual(alerts[0][0], "EverLights bridge restarted")


class HistoryTests(unittest.TestCase):
    def test_records_changes_and_samples(self):
        class B(FakeBridge):
            def __call__(self, host, method, path, body=None):
                if path == "":
                    return {"rebootCount": 3, "zones": [{"serial": "z1", "rssi": -101, "snr": -109, "rebootCount": 2}]}
                return super().__call__(host, method, path, body)
        b = B()
        store = Store(tempfile.mkdtemp())
        store.data["schedules"] = [sched()]
        c = Controller("h", CFG, store, reassert_minutes=(), call=b)
        c.alert_sink = lambda *a: None
        t = datetime(2026, 10, 17, 18, tzinfo=TZ)
        c.tick(t)                                      # off, baseline
        b.state = {"pattern": ["FF0000"], "effects": []}  # outside change
        c.tick(t + timedelta(minutes=1))
        b.drops = b.writes + 1
        c.tick(t + timedelta(hours=1))                 # already set, so the read-back confirms anyway
        c.tick(t + timedelta(hours=1, seconds=30))
        changes = store.history("changes")
        self.assertEqual([(r["action"], r["result"]) for r in changes],
                         [("off", "ok"), ("on", "outside"), ("on", "ok")])
        self.assertEqual((changes[2]["tries"], changes[2]["alreadySet"]), (1, True))
        health = store.history("health")
        self.assertEqual(health[0]["rssi"], -101)
        self.assertEqual(len(store.history("health", since=t + timedelta(minutes=30))), 1)

    def test_patterns_survive_bridge_list_failure(self):
        class B(FakeBridge):
            broken = False
            def __call__(self, host, method, path, body=None):
                if path == "sequences" and self.broken:
                    raise OSError("HTTP 500: Failed to get array stream")
                return super().__call__(host, method, path, body)
        b = B()
        store = Store(tempfile.mkdtemp())
        store.data["patterns"] = [scheduler.validate_pattern({"alias": "Mine1", "pattern": ["FF0000"]})]
        c = Controller("h", CFG, store, call=b)
        self.assertEqual(len(c.sequences()), 2)
        b.broken = True
        c2 = Controller("h", CFG, Store(store.dir), call=b)  # fresh start, bridge broken
        self.assertEqual([s["alias"] for s in c2.sequences()], ["Mine1", "Candy Corn"])
        self.assertIsNotNone(c2.sequence("seq-a"))


class PatternTests(unittest.TestCase):
    def test_validate_pattern(self):
        p = scheduler.validate_pattern({"alias": "Pumpkin", "pattern": ["#ff6600", "000000"],
                                        "effects": [{"effectType": "chase", "value": 999}]})
        self.assertTrue(p["id"].startswith("mine-"))
        self.assertEqual(p["pattern"], ["FF6600", "000000"])
        self.assertEqual(p["effects"], [{"effectType": "chase", "value": 255}])
        self.assertEqual(scheduler.validate_pattern({**p, "alias": "x"})["id"], p["id"])
        with self.assertRaises(ValueError):
            scheduler.validate_pattern({"pattern": ["nope"]})
        with self.assertRaises(ValueError):
            scheduler.validate_pattern({"pattern": []})

    def test_schedule_plays_my_pattern_without_bridge_lookup(self):
        bridge = FakeBridge()
        store = Store(tempfile.mkdtemp())
        mine = scheduler.validate_pattern({"alias": "Pumpkin", "pattern": ["FF6600"]})
        store.data["patterns"] = [mine]
        store.data["schedules"] = [sched(sequenceId=mine["id"])]
        c = Controller("h", CFG, store, reassert_minutes=(), call=bridge)
        c.tick(datetime(2026, 10, 17, 19, tzinfo=TZ))
        self.assertEqual(bridge.state["pattern"], ["FF6600"])

    def test_rgbw_colors(self):
        p = scheduler.validate_pattern({"pattern": ["FFDE8CFF", "FF0000"]})
        self.assertEqual(p["pattern"], ["FFDE8CFF", "FF000000"])
        self.assertEqual(p["colorMode"], "rgbw")

    def test_imports_personal_patterns(self):
        class Bridge(FakeBridge):
            def __call__(self, host, method, path, body=None):
                if path == "sequences":
                    return [{"id": "p1", "alias": "Warm", "groups": ["Personal/"], "pattern": ["FFDE8CFF"],
                             "effects": [], "colorMode": "rgbw"},
                            {"id": "e1", "alias": "Easter", "groups": ["EverLights/Other Holidays/"],
                             "pattern": ["FF00FF"], "effects": []}]
                return super().__call__(host, method, path, body)
        store = Store(tempfile.mkdtemp())
        store.data["schedules"] = [sched(sequenceId="p1")]
        store.data["favorites"] = ["p1", "e1"]
        c = Controller("h", CFG, store, call=Bridge())
        seqs = c.sequences()
        self.assertEqual([s["alias"] for s in seqs], ["Warm", "Easter"])
        mine = store.patterns[0]
        self.assertEqual((mine["importedFrom"], mine["colorMode"]), ("p1", "rgbw"))
        self.assertEqual(store.schedules[0]["sequenceId"], mine["id"])
        self.assertEqual(store.data["favorites"], [mine["id"], "e1"])
        self.assertIs(c.sequence("p1"), mine)
        c.sequences(refresh=True)
        self.assertEqual(len(store.patterns), 1)


if __name__ == "__main__":
    unittest.main()
