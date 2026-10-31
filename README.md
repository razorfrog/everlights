# everlights

A local web app and scheduler for [EverLights](https://www.everlights.com/) permanent
roofline lighting. It talks to the EverLights bridge directly over your home network,
so it doesn't depend on the EverLights cloud or phone app.

I built it because my lights weren't reliably turning on and off on schedule. The
bridge fires each scheduled change once and never checks whether the roofline
controller heard it. This app does the scheduling itself, confirms every change, and
retries until it sticks.

![Patterns, the pattern maker and schedules on an iPhone](docs/screenshots/overview.jpg)

It's built for phones first: add it to your home screen and it opens like an app.

> Not affiliated with or endorsed by EverLights. It uses the bridge's undocumented
> local API, which could change with any firmware update.
>
> **Firmware B5010 (October 30, 2026) may break it.** This app is tested on bridge
> firmware B4042. Another local app [stopped working after B5010](https://github.com/CWSpear/everlights/issues/3),
> where the zone's pattern endpoint (`/v1/zones/{serial}/sequence`) now answers
> `410 Gone` with `"cloud": true`: control appears to have moved to the cloud. See [Holding off firmware updates](#holding-off-firmware-updates).

## Features

- **Pattern chooser:** every pattern saved on the bridge, with animated previews,
  search, groups and favorites.
- **Pattern maker:** build your own patterns, including the strip's separate white
  LEDs (RGBW), with effects and speeds. Preview on the real lights before saving.
- **Schedules that confirm:** sunset/sunrise with offsets or clock times, weekdays,
  and yearly date ranges (e.g. Oct 1–31). Each change is read back from the bridge,
  retried with backoff, and re-sent 2 and 10 minutes later in case the radio dropped it.
- **Reliability history:** signal strength (RSSI/SNR) over time, bridge and zone
  restarts, and how every scheduled change went: first try, after retries, gave up,
  or already done by the bridge's own schedule.

  <img src="docs/screenshots/health.jpg" width="300" alt="Health tab: signal strength over a day, with the jump after moving the bridge">

- **Push alerts** via [ntfy](https://ntfy.sh): get a notification on your phone when a
  scheduled change gives up or only worked after retries, when the bridge stops
  answering (and when it's back), or when the bridge or controller restarts.
- **Home Assistant integration:** a light with your patterns as effects, plus sensors.
  See [Home Assistant](#home-assistant).
- **Siri Shortcuts links:** `/go/on`, `/go/off`, `/go/play?name=Candy%20Corn`.
- **Home screen web app** for iOS.
- **CLI** (`everlights.py`) for scripting and poking at the bridge.

Plain Python 3.10+ standard library; the only dependency is `tzdata` in the container.

## Quick start

Find your bridge's IP address (the EverLights app shows it under Settings → Bridge, or
run `./everlights.py discover`), then:

```bash
EVERLIGHTS_HOST=192.168.1.50 LAT=37.7749 LON=-122.4194 python3 server.py
```

Open http://localhost:8080/admin for the app (`/` is the live view). Schedules, your patterns and the logs are saved in `./data`.

Run the tests with `python3 -m unittest discover -s tests`.

### Configuration

| Variable | Default | |
| --- | --- | --- |
| `EVERLIGHTS_HOST` | (required) | Bridge IP address |
| `LAT`, `LON` | San Francisco | Your location, for sunrise and sunset |
| `TZ` | `America/Los_Angeles` | Time zone |
| `PORT` | `8080` | HTTP port |
| `DATA_DIR` | `./data` | Schedules, patterns, logs and history |
| `REASSERT_MINUTES` | `2,10` | Re-send after each scheduled change |
| `SCHEDULER` | `on` | `off` serves the app without running schedules (for testing) |

The ntfy topic is set in the app's Health tab. Links like `/#schedules` or
`/#health` open a tab directly.

## Running it on a NAS or server

It needs to run somewhere that's always on and on the same network as the bridge.

- **Docker:** edit `compose.yaml` and run `docker compose up -d --build`.
- **QNAP Container Station (no build):** copy this folder to a share, edit the paths
  and settings in `compose.qnap.yaml`, and paste it into Applications → Create. It runs
  the code straight from the share, so updating means copying new files and restarting
  the container.

**Don't expose the main port to the internet.** It has no password, by design, for
use at home. For access from anywhere, use the separate public entrance below.

### Remote access and a public live page

Every copy of the app has the same layout: **`/` is a live view** of what the
roofline is showing (animated, with upcoming shows), and **`/admin` is the app**.

To reach it from anywhere, set `PUBLIC_PORT` and point a
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
at it. Through the tunnel the live view is public, and everything else asks for a
password (or a token, for Siri Shortcuts). The home port stays passwordless and is never
reachable from the internet.

**Step-by-step setup: [docs/remote-access.md](docs/remote-access.md).**

## Home Assistant

`custom_components/everlights_local/` is a Home Assistant integration that talks to this
app (not to the bridge), so the app keeps doing the scheduling and confirming. (Home
Assistant's built-in EverLights integration only works with the older controllers and
their `/status/get` API.)

It adds:

- **A light** for the roofline: on, off, any saved pattern as an effect, or a solid color.
  A change from Home Assistant holds until the app's next scheduled change, the same as a
  change made in the app.
- **Sensors:** next scheduled change (with the pattern), what's scheduled now, the
  controller's signal strength, bridge and controller restarts, and bridge firmware.
- **Binary sensors:** bridge answering, and (off by default) bridge reaching the
  EverLights cloud.

<img src="docs/screenshots/home-assistant.jpg" width="300" alt="A Home Assistant dashboard with the roofline light, pattern buttons, the next scheduled change and bridge health">

An example dashboard: the roofline light, buttons that play saved patterns, and the
schedule and health sensors. (Power draw comes from a separate smart plug.)

To install, copy `custom_components/everlights_local` into your Home Assistant
`config/custom_components/` folder (or add this repo to HACS as a custom repository),
restart Home Assistant, then add **EverLights Local** under Settings → Devices &
services. Give it the app's home address, like `http://192.168.1.20:8080`. Only if you
use the remote (tunnel) address, also paste the Shortcut token from the app's Health tab.

## CLI

```bash
export EVERLIGHTS_HOST=192.168.1.50
./everlights.py discover          # find the bridge on your /24
./everlights.py status            # bridge and zone health
./everlights.py patterns hallow   # search saved patterns
./everlights.py play "Halloween Night"
./everlights.py on FF6600 7E1EFF -e chase:120
./everlights.py off
./everlights.py events            # schedules stored on the bridge
./everlights.py raw GET zones     # any other endpoint
```

## The bridge's local API

The bridge serves unauthenticated JSON at `http://<bridge-ip>/v1/`. Checked against
bridge firmware B4042, radio R3052, zone controller N3063:

| Method | Path | |
| --- | --- | --- |
| GET | `/v1` | Bridge status, network, zones |
| GET | `/v1/zones` | Zone serial, LED count, RSSI/SNR, reboot count, last response |
| GET / POST / DELETE | `/v1/zones/{serial}/sequence` | What's playing / play a pattern / off |
| GET | `/v1/sequences` | Saved patterns (`alias`, `pattern`, `effects`, `colorMode`) |
| GET / DELETE | `/v1/events`, `/v1/events/{id}` | Schedules, with precomputed UTC `occurrences` |

- Colors are `RRGGBB`, or `RRGGBBWW` with `"colorMode": "rgbw"` to drive the white LEDs.
- Effects seen in saved patterns: `bars`, `blink`, `chase`, `fade`, `gradient`,
  `lightning`, `melt`, `pingPong`, `random`, `reverseChase`, `strobe`, `twinkle`, `wipe`.
  Speeds are 0–255, and a pattern can combine two effects.
- There's no brightness endpoint; use darker colors.
- Reading a zone's sequence back only shows what the bridge *accepted*, not what the
  roofline controller received over the radio. That's why the scheduler re-sends.

## Holding off firmware updates

The bridge updates itself from the EverLights cloud, and there's no setting to turn that
off. If your bridge is still on a firmware version this app works with, you can block
just the update server at your router. The bridge downloads firmware from
`update.myeverlights.com` (also `fw.myeverlights.com`), separate from the cloud
connection it uses day to day (`mqtt.myeverlights.com` on port 1883 and
`telemetry-v2.myeverlights.com`).

**Don't block the bridge from the internet entirely.** I tried: without its cloud
connection the bridge freezes for 5–6 seconds every minute or two while it retries,
and its watchdog restarts it after about 20 minutes. Blocking only the update server
leaves it stable and online.

What I did on a UniFi gateway:

1. Give the bridge a fixed IP (a DHCP reservation).
2. Add an "internet out" (WAN_OUT) firewall rule that rejects traffic from the bridge
   to the update server's address (`dig +short update.myeverlights.com`).
3. Add local DNS records pointing `update.myeverlights.com` and `fw.myeverlights.com`
   to `0.0.0.0`, in case that address changes.
4. Check `GET /v1`: `serverReachable` stays `true` and `firmwareVersion` stays put.

This is a best guess from the hostnames, not something I've seen work against a real
update yet. To let it update later, remove the rule and the DNS records, and back up
the SD card first (see below).

## Notes on the bridge's SD card

The bridge keeps its schedules and patterns on a microSD card (FAT32): `events/`,
`sequences/`, `firmware/`, `zones.json` and `.bootcount`. If deleting a schedule fails
with **"SD card error"**, the card's filesystem is probably damaged, often from losing
power mid-write. What worked for me:

1. Back up the whole card first. Your computer may write to it as soon as it mounts.
2. **Repair the original card in place** (on a Mac: `diskutil repairVolume`), then
   delete the broken schedule file.
3. Remove the hidden folders macOS adds before putting it back:

   ```bash
   sudo mdutil -i off "/Volumes/NO NAME"
   sudo rm -rf "/Volumes/NO NAME/.Spotlight-V100" "/Volumes/NO NAME/.fseventsd"
   ```

**Don't count on copying the files to a new card.** I tried a freshly formatted card
with an exact copy of the files, first with 4 KB and then with 32 KB clusters like the
original, and with the macOS folders removed. The bridge booted from it and read
`zones.json`, but couldn't list its schedules or patterns ("Failed to get array
stream"). The original card, repaired, worked fine. My guess is the bridge's firmware
doesn't like folders created by macOS, but I haven't pinned it down.

## Credits

- [CWSpear/everlights](https://github.com/CWSpear/everlights), Cameron Spear's
  JavaScript SDK, reverse-engineered the bridge's v1 API from the phone app's traffic.
  This project started from what that SDK documents.
- [ccutrer's gist](https://gist.github.com/ccutrer/8894f62eadeaae145eebbab0a49adb1f)
  covers the older v2-era controller API.

## License

[MIT](LICENSE)
