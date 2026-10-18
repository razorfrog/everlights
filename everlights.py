#!/usr/bin/env python3
"""Control an EverLights bridge over its local HTTP API (no dependencies).

The bridge serves unauthenticated JSON at http://<bridge-ip>/v1/. Endpoints
come from the community SDK (npm: everlights) and were checked against a
B4042-firmware bridge.

Host: --host, or $EVERLIGHTS_HOST, or `discover` to scan the local /24.
"""

import argparse
import ipaddress
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

DEFAULT_TIMEOUT = 5
EFFECTS = ("bars", "blink", "chase", "fade", "gradient", "lightning", "melt",
           "pingPong", "random", "reverseChase", "strobe", "twinkle", "wipe")


def request(host, method, path, body=None, timeout=DEFAULT_TIMEOUT):
    url = f"http://{host}/v1/{path.lstrip('/')}".rstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw.strip() else None


def local_subnet():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def probe(ip):
    try:
        info = request(str(ip), "GET", "", timeout=1.5)
    except Exception:
        return None
    if isinstance(info, dict) and "zones" in info and "serial" in info:
        return str(ip), info
    return None


def discover():
    with ThreadPoolExecutor(max_workers=48) as pool:
        return [hit for hit in pool.map(probe, local_subnet().hosts()) if hit]


def zone_serials(host, zone):
    if zone:
        return [zone]
    return [z["serial"] for z in request(host, "GET", "zones")]


def parse_effects(specs):
    effects = []
    for spec in specs or []:
        name, _, value = spec.partition(":")
        value = int(value) if value else 128
        if not 0 <= value <= 255:
            raise SystemExit(f"effect speed must be 0-255: {spec}")
        effects.append({"effectType": name, "value": value})
    return effects


def parse_colors(colors):
    out = []
    for c in colors:
        c = c.lstrip("#").upper()
        if len(c) != 6 or any(ch not in "0123456789ABCDEF" for ch in c):
            raise SystemExit(f"not a hex color: {c}")
        out.append(c)
    return out


def set_sequence(host, zone, sequence):
    for serial in zone_serials(host, zone):
        request(host, "POST", f"zones/{serial}/sequence", sequence)
        print(f"{serial}: set")


def zone_health(z):
    # Judged on RSSI alone: the bridge's "snr" stays at about -109 whatever the
    # signal does, and rebootCount is a lifetime total (the app alerts on new ones).
    rssi = z.get("rssi")
    if rssi is not None and rssi <= -95:
        return f"weak radio (RSSI {rssi} dBm)"
    return "ok"


def print_json(obj):
    print(json.dumps(obj, indent=2))


def cmd_discover(args):
    hits = discover()
    if not hits:
        print("No bridge found on", local_subnet())
        return 1
    for ip, info in hits:
        print(f"{ip}  serial={info['serial']}  firmware={info.get('firmwareVersion')}"
              f"  zones={len(info.get('zones', []))}")


def cmd_status(args):
    info = request(args.host, "GET", "")
    if args.json:
        return print_json(info)
    net = info.get("network", {})
    print(f"Bridge {info['serial']} at {info.get('localIp')}")
    print(f"  firmware {info.get('firmwareVersion')}, radio {info.get('radioFirmwareVersion')}")
    print(f"  network {net.get('interface')}, cloud reachable: {info.get('serverReachable')}")
    print(f"  reboots {info.get('rebootCount')}, last cloud check-in {info.get('lastServerCheckin')}")
    for z in info.get("zones", []):
        print(f"  zone {z['serial']}: {z.get('configuredLength')} LEDs, {zone_health(z)}")


def cmd_zones(args):
    zones = request(args.host, "GET", "zones")
    if args.json:
        return print_json(zones)
    for z in zones:
        print(f"{z['serial']}  firmware={z.get('firmwareVersion')}  leds={z.get('configuredLength')}"
              f"  rssi={z.get('rssi')}  snr={z.get('snr')}  reboots={z.get('rebootCount')}"
              f"  last_response={z.get('lastResponseDate')}")
        print(f"    health: {zone_health(z)}")


def cmd_current(args):
    for serial in zone_serials(args.host, args.zone):
        seq = request(args.host, "GET", f"zones/{serial}/sequence")
        print(f"{serial}: {json.dumps(seq)}")


def cmd_on(args):
    set_sequence(args.host, args.zone, {
        "pattern": parse_colors(args.colors),
        "effects": parse_effects(args.effect),
    })


def cmd_off(args):
    for serial in zone_serials(args.host, args.zone):
        request(args.host, "DELETE", f"zones/{serial}/sequence")
        print(f"{serial}: off")


def cmd_patterns(args):
    seqs = request(args.host, "GET", "sequences")
    if args.json:
        return print_json(seqs)
    for s in seqs:
        if args.search and args.search.lower() not in s.get("alias", "").lower():
            continue
        fx = ",".join(f"{e['effectType']}:{e['value']}" for e in s.get("effects", []))
        print(f"{s.get('alias')}  [{' '.join(s.get('pattern', []))}]  {fx}")


def cmd_play(args):
    seqs = request(args.host, "GET", "sequences")
    name = args.name.lower()
    matches = [s for s in seqs if s.get("alias", "").lower() == name] or \
              [s for s in seqs if name in s.get("alias", "").lower()]
    if not matches:
        raise SystemExit(f"no saved pattern matching {args.name!r}")
    if len(matches) > 1 and matches[0]["alias"].lower() != name:
        raise SystemExit("ambiguous: " + ", ".join(s["alias"] for s in matches))
    s = matches[0]
    print(f"playing {s['alias']}")
    set_sequence(args.host, args.zone, {"pattern": s["pattern"], "effects": s.get("effects", [])})


def cmd_events(args):
    events = request(args.host, "GET", "events")
    if args.json:
        return print_json(events)
    for e in events:
        occ = e.get("occurrences", [])
        now = datetime.now(timezone.utc).isoformat()
        upcoming = [o for o in occ if o["end"] > now]
        nxt = upcoming[0] if upcoming else None
        print(f"{e.get('alias')} ({e.get('group')}): {len(upcoming)} of {len(occ)} occurrences left"
              + (f", next {nxt['start']} -> {nxt['end']}" if nxt else ""))


def cmd_raw(args):
    body = json.loads(args.body) if args.body else None
    print_json(request(args.host, args.method.upper(), args.path, body))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=os.environ.get("EVERLIGHTS_HOST"), help="bridge IP")
    p.add_argument("--json", action="store_true", help="print raw JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="scan the local /24 for a bridge").set_defaults(fn=cmd_discover, local=True)
    sub.add_parser("status", help="bridge status and zone health").set_defaults(fn=cmd_status)
    sub.add_parser("zones", help="zone details (RSSI, reboots, last response)").set_defaults(fn=cmd_zones)

    sp = sub.add_parser("current", help="show what each zone is playing")
    sp.add_argument("--zone")
    sp.set_defaults(fn=cmd_current)

    sp = sub.add_parser("on", help="set colors, e.g. on FF0000 00FF00 -e chase:120")
    sp.add_argument("colors", nargs="+")
    sp.add_argument("-e", "--effect", action="append",
                    help=f"name[:speed 0-255], repeatable ({', '.join(EFFECTS)})")
    sp.add_argument("--zone")
    sp.set_defaults(fn=cmd_on)

    sp = sub.add_parser("off", help="turn zones off")
    sp.add_argument("--zone")
    sp.set_defaults(fn=cmd_off)

    sp = sub.add_parser("patterns", help="list saved patterns")
    sp.add_argument("search", nargs="?")
    sp.set_defaults(fn=cmd_patterns)

    sp = sub.add_parser("play", help="play a saved pattern by name")
    sp.add_argument("name")
    sp.add_argument("--zone")
    sp.set_defaults(fn=cmd_play)

    sub.add_parser("events", help="list schedules").set_defaults(fn=cmd_events)

    sp = sub.add_parser("raw", help="call any endpoint, e.g. raw GET zones")
    sp.add_argument("method")
    sp.add_argument("path")
    sp.add_argument("body", nargs="?", help="JSON body")
    sp.set_defaults(fn=cmd_raw)

    args = p.parse_args(argv)
    if not getattr(args, "local", False) and not args.host:
        p.error("set --host or EVERLIGHTS_HOST (or run `discover`)")
    try:
        return args.fn(args)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} from bridge: {e.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
        raise SystemExit(f"could not reach bridge at {args.host}: {e}")


if __name__ == "__main__":
    sys.exit(main())
