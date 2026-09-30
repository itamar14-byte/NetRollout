# Plan — System settings + startup reverse-proxy check

_Status 2026-09-30: **Parts A and B done** on `system-settings`; Part C in 4.1._

_Approved 2026-09-29. Branch `system-settings`. Parts A and B are built on this branch, before the Phase 4 planning session; Part C is specified here and built in 4.1 with the compose layout it depends on._

## Why

NetRollout targets two deployments:

1. **An engineer's laptop** — one user, on the same machine.
2. **An organisation VM** — the IT team connects remotely; nginx is the only real entry point. This is the scenario the app's scale is built for.

Today the startup message hardcodes `127.0.0.1:8080 or localhost:8080`, retention periods are code constants, and the worker count is an env var. Admins need to see and change operational settings after installation, and the app should tell people the address they can actually use.

## Terms

_Revised 2026-09-30: the Public URL is two settings — **Hostname** (canonical) and **HTTPS port** — and the app builds `https://<hostname>[:<port>]` from them (the scheme is always https, no path). Empty hostname → auto-detect from the nginx config. The page title is **System Settings**._

- **Public URL** — what people in the organisation type: nginx's hostname and port, e.g. `https://netrollout.corp.local:8443`. Admins can change it.
- **Internal app port** — the port Waitress listens on (default 8080). Set once at install (so it can move off 8080 if another app uses it) and never changed from the GUI. nginx forwards to it; it's also the local-only fallback.

## Part A — startup check and announcement (this branch)

1. **Internal port** comes from the install-time `PORT` value; every message is built from the port actually bound — nothing hardcoded.
2. **Instance token** — a random per-run token served at `/_netrollout/instance` (no login, no DB, reveals nothing).
3. **Public URL source**: the Public URL setting (its row, seeded from `NETROLLOUT_PUBLIC_URL` at install) > auto-detect from the nginx config in use while the setting is empty (`NETROLLOUT_NGINX_CONF`, default `docs/nginx/nginx.conf`: the `listen … ssl` port and `server_name`). Until B2 adds the setting, Part A reads `NETROLLOUT_PUBLIC_URL` directly; B2 switches it to the row.
4. **Two-step check**, in a background thread once Waitress answers (2 s timeouts; certificate not verified — this checks identity, not trust):
   - **Local proxy** — nginx on this machine returns our token → nginx is up *and* forwarding to this instance.
   - **Public URL** — the token comes back through the Public URL.
5. **Messages** (worded for both scenarios):
   - both pass → "NetRollout is available at `<Public URL>`";
   - only local passes → "nginx is up and forwarding here, but `<Public URL>` isn't reachable from this machine — if clients can reach it you're fine, otherwise check DNS/firewall";
   - neither → "reverse proxy not verified (`<reason>`)"; local fallback `http://localhost:<port>` — sign-in from this machine only, because the session cookie is HTTPS-only (`SESSION_COOKIE_SECURE`) — and **remote users can't sign in until the proxy works**.
6. **Open in the browser** — interactive runs only; never in a container or on an admin restart (the relaunch sets a marker); `NETROLLOUT_OPEN_BROWSER=0` turns it off. Opens whichever URL was verified.

## Part B — System Settings (this branch)

- **The table is the only runtime source** (revised 2026-09-30, replacing an earlier tiered design). Migration adds `system_settings(key PK, value json, updated_at, updated_by)`. **Every setting has a row**: at every startup `install()` seeds missing settings — with the install-time value (env) if the setting has one and it's valid, else the default — and never touches existing rows. So a fresh install is fully populated, an upgrade gains only new settings, and an install keeps its values across upgrades until an admin changes them (a release that must change an existing value does it with a deliberate migration). After seeding, the env is never consulted.
- **Registry** (`src/db/settings.py`) — defines each setting (type, range, page text, card, when a change applies, seed value) and the cross-setting rules; it is not a runtime source. Store API: `get` / `values` / `list_for_display` / `update` (all-or-nothing, returns the changes for the `settings.update` audit entry) / `reset` (writes the default into the row).
- **Rules enforced on both sides** — the cross-setting rules are declarative data (`left >= / <= right`, with a message): `update()` enforces them on the server and the page receives the same list (`rules_for_client()`) and checks it in the browser; per-field type and range also come from the registry, as input constraints on the page and `parse()` on the server. The server check is authoritative.
- **Robustness** — a stored value out of range (e.g. a range tightened in a release) is used as the nearest valid value and reported in the startup log; a missing row (seeding failed) reads as the default; the background paths (log prune, startup message) fall back to the default if the DB is unreachable.
- **Page markers** — "changed" = value differs from the default; "from install" = changed but never by an admin (seeded from env, `updated_by` empty).
- **Settings**

  | Card | Setting | Default | Applies |
  |---|---|---|---|
  | Retention | Job record (results + commands) | 30 d (≥ 1) | next nightly run |
  | Retention | Config snapshots | 7 d (≥ 1, ≤ job record) | next nightly run |
  | Retention | Audit log | 90 d (≥ 7) | next nightly run |
  | Retention | Log files | 60 d (≥ job record) | next daily prune |
  | Rollouts | Concurrent rollout jobs | 4 (1–32; seeded from `ORCHESTRATOR_WORKERS`) | after restart |
  | Rollouts | Devices in parallel per job | 10 (1–64) | next rollout |
  | Rollouts | Reachability cache | 60 s (10–3600) | immediately |
  | Access | Hostname (canonical) | seeded from `NETROLLOUT_PUBLIC_HOSTNAME`, else empty (→ auto-detect from nginx config) | next start; **Test** runs the two-step check live |
  | Access | HTTPS port | 443 (1–65535), seeded from `NETROLLOUT_HTTPS_PORT` | next start |
  | Access | Internal app port | read-only ("set at install") | — |

- **Wiring** — pg_cron jobs read values at run time (`COALESCE(setting, default)`; `install()` already reschedules them each start); `prune_logs` reads its setting each daily run; the Results page reads the snapshot setting; the orchestrator reads the worker count at start; rollouts read device parallelism; the reachability checker reads its TTL live.
- **Validation** — log retention ≥ job retention (Download Log needs the file while the job exists; today's unit test becomes this rule); snapshots ≤ job record; per-setting minimums.
- **Restart pending** — the admin Restart button's orange dot shows on every admin page while a restart-only setting differs from what the running process uses.
- **Page** — admin panel → System → **System Settings**, generated from the registry, in the app's style. Until Part C, changing the Public URL's port shows a note that nginx must be updated to match.

## Part C — the Public URL moves nginx (**built in 4.1**, specified now)

- The app renders the nginx config from a template (hostname, public port, internal app port as upstream) into a shared config volume.
- A reload watcher in the nginx container validates (`nginx -t`) and reloads only if valid, reporting success/failure back.
- The VM deployment uses **host networking**, so nginx's listen port *is* the host port — no port mapping to change. The laptop (Docker Desktop) applies port changes through the launcher / `install.py`.
- **Lockout safeguard** — the new port runs alongside the old one and reverts in ~2 min unless confirmed from the new address.
- Hostname changes are flagged: the TLS certificate must match the new name.
- **One canonical hostname** (decided 2026-09-30): the generated nginx config redirects every other name that reaches the machine (its IP, DNS aliases) to the canonical hostname, so users always land on the name the certificate matches and links stay consistent.

Today's nginx is a standalone container (`docker run`, bridge network, host 80/443 mapped, `docs/nginx/nginx.conf` mounted read-only) — a port change there means recreating the container, which is why Part C waits for the compose layout.

## For the Phase 4 planning session

- **Expose only nginx; never publish the app port.** Otherwise anyone reaching the app port directly can spoof `X-Forwarded-For` (`ProxyFix` trusts it) — falsifying audit-log IPs and bypassing the login rate limit. Not changed on this branch: today's nginx container reaches the app on the host via `host.docker.internal`.
- `install.py` sets the internal app port (checking for a clash on 8080) and the initial Public URL.
- Part C's requirements above.
- Certificates for organisations without internal DNS: they set the hostname to the VM's IP, so the certificate must be issued for that IP.

## Tests and verification

- **Unit** — nginx-config parser (ssl / non-ssl, custom port, `server_name _`, missing file); two-step decision logic with faked responses (every message variant); browser guards; registry validation and cross-rules; precedence DB > env > default.
- **Integration** — settings read/write/reset, admin-only, audit entry; retention statements honour changed settings (extending `test_db_layer`); device parallelism reaches the engine; the instance route.
- **Migration** — up and down on `rollout_scratch` before the live DB.
- **Real runs** — startup against the running nginx (expects `https://localhost`) and with nginx stopped (expects the fallback + reason); headless screenshots of the settings page.

## Delivery

Branch `system-settings` off `master`. Commits: **A** (startup check + announcement) → **B1** (table, registry, retention) → **B2** (rollouts + access cards, page, restart-pending). Every backend file touched is named in the report; `master` is fast-forwarded afterwards.
