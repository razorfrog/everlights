# Remote access and a public live page

This puts the app on the internet at an address like `https://lights.example.com`,
using a free [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).
Nothing is opened on your router: a small `cloudflared` container dials out to
Cloudflare and carries requests back in.

The address looks the same as the app at home:

| Path | At home (`http://<server>:8080`) | Through the tunnel |
| --- | --- | --- |
| `/` | Live view of the lights | Live view of the lights, public |
| `/admin` | The full app | The full app, **after signing in** |
| `/api/...`, `/go/...` | Open | Need a sign-in, or a Shortcut token |

## Why there are two ports

The app serves the same pages on two ports:

- **`PORT` (default 8080), the home entrance.** No password, so it's quick to use on
  your own network. Publish it on your LAN only.
- **`PUBLIC_PORT` (e.g. 8095), the tunnel entrance.** It asks for a password for
  everything except the live view. **Don't publish it at all**: only the
  `cloudflared` container talks to it, over Docker's internal network.

Because the tunnel can only reach `PUBLIC_PORT`, the passwordless home entrance can
never be reached from the internet, even if something else were misconfigured. You
never type `PUBLIC_PORT` in a browser; it only appears once, in the tunnel's route.

## Setup

You need a domain on Cloudflare (the free plan is fine) and the app running in Docker.

### 1. Turn on the tunnel entrance

Add `PUBLIC_PORT` to the app's environment, without adding it to `ports:`:

```yaml
services:
  everlights:
    # ...as in compose.yaml...
    ports:
      - "8080:8080"          # home entrance, LAN only
    environment:
      EVERLIGHTS_HOST: 192.168.1.50
      PUBLIC_PORT: "8095"    # tunnel entrance: not published
```

### 2. Create a tunnel

In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a tunnel →
Cloudflared**. Name it (e.g. `lights`), choose **Docker** on the install step, and copy
the long token after `--token`.

Save it in a private file next to your compose file, never in the compose file
itself:

```bash
umask 077
mkdir -p cloudflared
printf 'TUNNEL_TOKEN=%s\n' 'PASTE-TOKEN-HERE' > cloudflared/tunnel.env
```

### 3. Add the `cloudflared` container

In the same compose file, so both containers share a Docker network:

```yaml
  cloudflared:
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel --no-autoupdate run
    env_file:
      - ./cloudflared/tunnel.env
    depends_on:
      - everlights
```

Run `docker compose up -d`. In the dashboard the tunnel should turn **Healthy** within
a minute.

### 4. Route your hostname to the app

On the tunnel's page, open **Published application routes** (older layouts call it
**Public hostnames**) and add:

| Field | Value |
| --- | --- |
| Subdomain | `lights` |
| Domain | your domain |
| Service type | `HTTP` |
| Service URL | `everlights:8095` (the app's service name and `PUBLIC_PORT`) |

Use `HTTP`, not HTTPS: Cloudflare handles HTTPS for visitors, and the hop between the
two containers stays inside your server.

If the hostname doesn't resolve after a few minutes, Cloudflare may not have created
its DNS record. Add it yourself under **DNS → Records**: type `CNAME`, name `lights`,
target `<tunnel-id>.cfargotunnel.com`, **Proxied**. The tunnel ID is shown on the
tunnel's page.

### 5. Set a password

At home, open `http://<server>:8080/admin`, go to **Health → Remote access**, and set a
password (at least 8 characters). It's stored only as a salted PBKDF2 hash. Setting it
again signs out every remote session. The password and token can only be changed
from the home entrance.

Then on your phone, open `https://lights.example.com/admin`, sign in, and use **Share →
Add to Home Screen**. It stays signed in for about six months, works anywhere, and
shows a "can't reach the lights" page instead of a blank screen when home is offline.

### 6. Siri Shortcuts away from home

The same Health section shows a **Shortcut token**. In a Shortcut's **Get Contents of
URL** action, use `https://lights.example.com/go/off` (or `/go/on`,
`/go/play?name=...`) and add a header named `Authorization` with the value `Bearer`,
a space, and the token. **New token** replaces it; Shortcuts using the old one stop
working.

## Security notes

- Five wrong passwords from one address lock it out for 15 minutes. Sign-ins and failed
  attempts appear in the Log tab.
- The live view shows only the current pattern, its name and upcoming shows. It's
  cached for 15 seconds, so visitors can't flood the bridge.
- If you'd like a second lock, put [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/policies/access/)
  in front of `/admin*`, `/api/*` and `/go/*` with a bypass for `/`, `/static/*` and
  `/api/live`. The app's own password still applies behind it.
