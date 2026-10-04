# CLAUDE.md

## Overview

Network bulk configuration tool that pushes configuration snippets to multiple network devices simultaneously. Two interfaces:
- **CLI** (`src/cli.py`): headless one-time rollout from a devices CSV + commands file — no database
- **Web app** (`src/webapp/` package): Flask app served via Waitress on port 8080, behind nginx; Postgres + Redis

## Commands

Everything runs **from the repo root**: `src` is a package and all imports are `src.*`.

### Run the web app (dev)
The services run in the **dev stack** (`compose.dev.yaml`: the product's Postgres, Redis, nginx and monitoring, project `netrollout-dev`); the app runs on the host:
```bash
docker compose up -d          # the dev stack (.env lists all four compose files)
python -m src.webapp          # the app, from the repo root (PyCharm or a terminal)
docker compose ps             # health; `docker compose down` stops it (data kept in volumes)
```
Open `https://localhost` (nginx → the host app on 8080; certificate in `certs/`, self-signed for localhost/127.0.0.1); Grafana at `https://localhost/grafana/` (signed-in admins only; the dev app shows the sidebar link because `config/runtime.env` sets `NETROLLOUT_MONITORING=monitoring`). After changing an image's source (`deploy/…`, the Dockerfile): `docker compose up -d --build`. The repo's `.env` (gitignored) holds the stack's generated passwords; `config/runtime.env` points the host app at it (`127.0.0.1`, not `localhost`: the stack publishes IPv4 only and Windows waits ~2 s on every refused `::1` attempt). Every start runs the Alembic migrations, seeds the factory `admin`/`admin` account if missing, schedules the pg_cron retention jobs (skipped if pg_cron is unavailable), and prunes old log files. There is no separate DB-init step.

### Run the CLI
```bash
python -m src.cli -d <devices.csv> -c <commands.txt> [-vf] [-v]
```
- `-vf` / `--verify`: after the push, read each device's config and check every command; `-v` / `--verbose`: print logs to the console (always written to `logs/`)
- Missing paths are prompted for (asked again until the file exists); a prompted run also asks verify and confirms ("About to push N commands to M devices") before the push — full-flag runs never ask. The verify choice is always printed. Exit code: 0 all devices succeeded, 1 mixed, 2 nothing applied (including a stop before the push: missing file, no devices or commands left), 130 Ctrl+C. Every exit but Ctrl+C waits for Enter when run from a terminal, so a double-clicked window stays readable. `--version` prints the version.
- **Standalone `netrollout-cli.exe`** (no Python needed; same flags): `pyinstaller --clean --noconfirm netrollout-cli.spec` from the repo root in the dev venv → `dist/netrollout-cli.exe` (one file, ~15 MB, ~2 s startup; logs go to `logs/` next to it). The CLI must not import the web stack — `tests/unit/test_cli.py` guards it, and the spec excludes it. The exe is unsigned: Windows Defender / SmartScreen may flag it (code-signing is post-v1.0).

### Build the app image
```bash
docker build -t netrollout .                              # dev: 1.0.0.dev0
docker build --build-arg VERSION=1.0.0 -t netrollout .    # a release (stamps src/runtime.py)
```
`python:3.12-slim-bookworm`, non-root `netrollout` (uid 10001), `NETROLLOUT_DEPLOYMENT=docker`, `NETROLLOUT_HOME=/data`; health check on `/_netrollout/health`. Context = the `.dockerignore` whitelist. The footer (`templates/_footer.html`) shows the version and links the source of that version (AGPL §13).
- Postgres image (`deploy/postgres/`, `docker build -t netrollout-postgres deploy/postgres`): `postgres:17-bookworm` + pg_cron, started by `netrollout-postgres.sh` with `cron.use_background_workers=on` and `cron.timezone`/`timezone` from `TZ` (retention at 03:00 local). First start only (empty volume): roles `netrollout` (the app: owns the `netrollout` DB and `public`, not a superuser, may use `cron`) and `grafana_reader`; env `POSTGRES_PASSWORD`, `NETROLLOUT_DB_PASSWORD`, `GRAFANA_DB_PASSWORD`. At every start `install()` gives `grafana_reader` SELECT on exactly `GRAFANA_TABLES` (`device_results`, `job_metadata`, `audit_log`) when the role exists.
- nginx image (`deploy/nginx/`, `docker build -t netrollout-nginx deploy/nginx`): `nginx:1.30-alpine`; one site template (`site.conf.template`) rendered from values — env `NETROLLOUT_HOSTNAME`, `NETROLLOUT_HTTPS_PORT`, `APP_UPSTREAM`, overridden by `shared/site.env` (the app, stage 8; values are validated, never nginx syntax). Other names redirect to the hostname (IPs and localhost are served); port 80 → `https://<host>:<port>`; `/rollout/stream/` unbuffered; `/metrics` 404; the app looked up per request (Docker DNS, 10 s). `netrollout-watcher.sh`: waits for the certificate at boot; every 3 s (`NETROLLOUT_WATCH_INTERVAL`) a change of `site.env` or the certificate is rendered + `nginx -t`-tested in staging, then swapped in and reloaded, else the last good site keeps serving; the outcome in `shared/status.json` (`applied`/`rejected` + nginx's message).
- **Grafana** (monitoring profile): served by nginx at `/grafana/` to **signed-in NetRollout admins only** — nginx's `auth_request` asks the app (`/_netrollout/grafana-auth`: 204 + `X-NetRollout-User`, 401 → sign-in, 403 → "admins only"), Grafana runs in proxy-auth mode trusting `X-WEBAUTH-USER` (always set by nginx from that answer; a browser's own is replaced, `Authorization` dropped); every Grafana user is an Editor, no login form. Admins only because free Grafana lets anyone signed in query every datasource through its API (operators: post-v1, a separate organization — workplan). `grafana-setup` (app image, `deploy/grafana/setup.py`) keeps the layout: `NetRollout` → `Operations`/`Jobs`/`Security` (the shipped dashboards, dashboard v2 files in `deploy/grafana/dashboards/<subfolder>/`, re-imported, **view-only**) and `Custom` (the admins': create/edit/nest; never touched — backups must include Grafana's volume). It re-applies every 5 min and stays running (`up --wait` counts an exited container as a failure). The admin sidebar's Grafana glyph shows when `NETROLLOUT_MONITORING` contains `monitoring` (compose passes `COMPOSE_PROFILES`).
- The app → nginx (`src/webapp/proxy_config.py`): `write_site()` writes `site.env` in `config/nginx/` (shared with nginx) — the System Settings hostname plus the **applied** HTTPS port (`NETROLLOUT_HTTPS_PORT` from compose, not the setting, which waits for `netrollout apply`) — atomically and only when it changes; `read_status()` / `wait_for_status()` read the watcher's verdict (`None` = no nginx reports there). Every app start syncs it (`sync_at_start`, never blocks the start); the dev stack's nginx shares the repo's `config/nginx`, so dev follows too. **Saving the hostname** (System Settings, `admin_settings._save`) is all or nothing: `settings.plan()` validates without writing; `proxy_config.change_hostname()` reissues a self-signed certificate for the new name (keeping its IPs, and the previous names for a 7-day transition — deadlines in `certs/.old-names.json`, dropped by `drop_expired_names()` at start and hourly, `start_certificate_upkeep`; an organisation's certificate is never reissued) or refuses when the organisation's doesn't cover it, then writes `site.env` (any failure → `ProxyError`, files restored); the setting is saved; nginx's verdict for that hostname is awaited — rejected → setting, `site.env` and certificate put back. The page shows applied / not managed (own proxy) / no answer, or the reason under the field. The Access card always shows `proxy_config.overview()` — nginx's last verdict (applied / rejected + nginx's message, when) and the certificate in use checked by `certs.validate` against the saved hostname (names, expiry, self-signed or not, problems/warnings, old names in transition); it comes with the page, every save, and (in Docker) the Test button, which can't probe the published port from inside. **Server Management → TLS Certificate** (`admin_servers`, the same status via `templates/_access_status.html`): upload an organisation's certificate + key (`proxy_config.install_certificate`: `certs.validate` against the saved hostname first — any problem refuses it; key written first; the self-signed marker and old names removed) or **Generate self-signed** (`generate_selfsigned`: the saved hostname, else the current certificate's name, keeping its IPs); both wait for nginx's verdict (`proxy_config.verdict`) and put the previous files back on a rejection; audited (`server.certificate_uploaded` / `_generated`, never the key).
- The HTTPS port → the **port helper** (`src/webapp/port_apply.py`, the app's side of the contract in `docs/plans/phase-4.md` stage 9; the helper itself — host-side, recreates nginx — comes in stage 9): the setting is the **desired** port; saving (or Reset) writes `config/desired.env` (port + a new request id) before the setting, all or nothing. The helper opens the new port next to the old one (`apply-status.json`: `trying`), the page served on the new port confirms (`/admin/settings/port/confirm` → `apply-confirm`; checked against the Host header's port and the deadline), else it rolls back. Everything that shows or redirects to an address uses `serving_port()` (the helper's status, else `NETROLLOUT_HTTPS_PORT`). Page states: manual (no helper: `netrollout apply`) / waiting (bounded, `WAIT_SECONDS`) / trying / confirming / rolled back + Try again; its polls are background requests.
- TLS certificates (`src/certs.py`, inside the image so the host needs no OpenSSL): `python -m src.certs selfsigned --host <name> [--ip <addr>]...` writes `fullchain.pem` + `privkey.pem` + a `.selfsigned` marker (ECDSA P-256, 825 days, the IPs as SANs) to the certs folder; `validate --cert --key [--host]` checks key match, encrypted key, dates, SAN/wildcard coverage, chain order.

### Run tests
```bash
pytest                    # all
pytest tests/unit         # hermetic, no services
```
Integration tests use a real Postgres (`rollout_test` DB, created on the dev stack's Postgres as its superuser — password from `.env`; `TEST_PG_ADMIN_URL` overrides) and Redis (db 15, the server in `config/runtime.env`) and are skipped with a reason when a service is unhealthy — if ~150 skip right after `docker` starts, Postgres wasn't ready; rerun. LDAP tests start and remove an ephemeral OpenLDAP container. The pg_cron test is opt-in (`TEST_PG_CRON_URL`). Known bugs are recorded as strict xfails.

### Migrations
```bash
cd src/db && alembic revision --autogenerate -m "..."   # new migration
```
The app applies migrations itself at startup; the alembic CLI resolves `DATABASE_URL`, else `PG_*`. History starts at the `v1_0_0_baseline` revision (the development migrations were squashed); from v1.0.0 on, released migrations are never edited or squashed — every schema change is a new revision.

### Dependencies
Python 3.12 (dev `.venv` and the image). `requirements.txt` (runtime, `~=` ranges) → `requirements.lock` (exact pins, installed by the image); `requirements-dev.txt` adds pytest + mypy. After changing `requirements.txt`, regenerate the lock on Linux (a Windows freeze can pick up Windows-only packages):
```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W)/requirements.txt:/req/requirements.txt:ro" python:3.12-slim \
  sh -c "pip install -q --root-user-action=ignore -r /req/requirements.txt >/dev/null && pip freeze" > requirements.lock
```

### Configuration
- Folders (`src/runtime.py`, resolved per call): `NETROLLOUT_HOME` (image: `/data`), else the exe's folder when frozen, else the repo root → `logs/`, `config/`, `certs/`.
- Connection settings: `DATABASE_URL` or `PG_HOST/PG_PORT/PG_NAME/PG_USER/PG_PASSWORD/PG_SCHEMA`; `REDIS_URL` or `REDIS_HOST/REDIS_PORT/REDIS_DB/REDIS_PASSWORD`. Precedence: `config/runtime.env` (app-owned, written only by a Server Management switch, loaded with override) > the environment (in Docker: the installer's compose `.env`) > defaults. A switch writes every key of that service, blank when unused (URL, password, schema), so nothing inherited can override it. Dev keeps its settings in `config/runtime.env`.
- Other env: `SECRET_KEY`, `PORT` (internal app port, set at install), `NETROLLOUT_THREADS` (Waitress worker threads, default 32, 4–256: an open live rollout log holds one for the whole rollout, and with as many open logs as threads every other request waits — measured 7 s with Waitress's default of 4).
- **System Settings** (admin panel → System; `src/db/settings.py`): retention periods, concurrent rollout jobs, devices per job, reachability cache, canonical hostname + HTTPS port. The `system_settings` table is the only runtime source: `install()` seeds every missing setting at each start (from `ORCHESTRATOR_WORKERS`, `NETROLLOUT_PUBLIC_HOSTNAME`, `NETROLLOUT_HTTPS_PORT` if set, else the default) and never overwrites; after that env vars are ignored. Cross-setting rules are declarative and enforced on the server and in the page.
- Deployment (`src/runtime.py`): `NETROLLOUT_DEPLOYMENT=docker` (set by the image) → `SECRET_KEY` and the encryption key are required (never defaulted or generated), Restart exits and the restart policy brings it back, no startup proxy probe (one log line with the expected URL instead). Dev: a missing `SECRET_KEY` gets a random per-run key.
- Startup in dev (`src/webapp/startup.py`): verifies nginx forwards to this instance (per-run token at `/_netrollout/instance`) and prints the address to use — the System Settings hostname + serving port, else `https://localhost`; opens it in the browser on desktop launches (`NETROLLOUT_OPEN_BROWSER=0` to disable).
- Stop / Restart drain (`src/webapp/lifecycle.py`, `RolloutOrchestrator.drain`): on SIGTERM or the admin Restart, new rollouts are refused (banner on every page), queued ones are recorded as cancelled, running ones finish within `NETROLLOUT_DRAIN_SECONDS` (default 600) and are cancelled after. Restart with rollouts running asks: when finished / now.
- Health: `/_netrollout/health` (public) — Postgres/Redis up, running/queued rollouts, draining, version; 200 or 503. Version string in `src/runtime.py`.
- Session lifetime (`src/webapp/extensions.py`, `enforce_session_lifetime`): a session ends after `session_idle_minutes` (System Settings → Sessions; default 15, 5–480) without user activity and after 12 hours however active (`ABSOLUTE_SESSION_HOURS`); then a page gets the sign-in with `?next=`, an XHR 401 JSON, Grafana's auth check a bare 401; audit `auth.session_expired`. Activity = what a person does; background requests (header `X-NR-Background: 1`, `?_bg=1` on Active Jobs' auto-reload, the live log stream) are checked for expiry but don't extend. `templates/_idle_timeout.html` (in all three skeletons) warns a minute ahead ("Stay signed in"), asks `/account/session` before leaving (another active tab keeps it) and every 30 s in the background, so a session ended elsewhere (Terminate Session, a reset) leaves the page within 30 s. **Every app start signs everyone out** (`setup.clear_sessions`) — deliberately: a restart, update or reboot starts clean; the Restart button says so and asks.
- Passwords (`src/passwords.py`, local accounts): one rule — 8+ characters, at least 2 of letters / digits / special, ASCII, not containing the username — for registration, `/account/password` and admin resets (a temporary password shown once). `users.must_change_password` (the seeded `admin`, after a reset) gates every page to the change page; that forced change doesn't ask for the current password (the sign-in just proved it) but still refuses it as the new one — only a voluntary change asks for it. On every password field, a rule problem shows once the field is left; only what more typing can't fix (non-ASCII, the username) shows at once (`_password_rule_script.html`). A reset or Terminate Session signs the user out everywhere, a change signs out the user's other sessions (`utils.end_user_sessions`).
- Encryption key: `NETROLLOUT_ENCRYPTION_KEY`, else (dev only) `~/.netrollout/encryption.key`. The app refuses to start on a malformed, missing-with-data, or mismatched key (fail-fast).
- Dev DB: the dev stack's `netrollout` database, owned by the `netrollout` role (not a superuser) — `docker compose exec postgres psql -U postgres -d netrollout`. Moved from the hand-made `NetRollout-DB` container on 2026-10-04 (backup in `backups/`).

## Architecture

Full architecture in `docs/architecture.md`; plan and current status in `docs/workplan.md` (status table under "Remaining work"). Phase 4 (packaging v1.0.0) is planned in `docs/plans/phase-4.md`.

Retention (defaults; System Settings): job record (results + metadata) 30 days, config snapshots 7 days, audit log 90 days (pg_cron statements read the setting's row at run time); log files 60 days (app-side, daily).

### Rollout job state
`src/job_store.py` (`JobStore`) owns the Redis job keys (`job:{id}:meta`, `user_jobs:{uid}`, the queue, the pending/active counters): the orchestrator writes through it, the pages and the Prometheus collector read through it — no other code spells a key. Every start clears leftover job state (`setup.clear_stale_jobs`: jobs live only in their process, so anything there came from a crash). A finished job (`RolloutOrchestrator._finalize`): results to Postgres (retried; still failing → `logs/unsaved-results-<job>.json` + an ACTION NEEDED console line), then the Redis cleanup and the live log's end — each step runs even if another failed.

### Webapp real-time logging
Server-Sent Events at `/rollout/stream/<job_id>`: history from Redis, then live messages via Redis pub/sub (`job:{id}:logs`), a heartbeat every 0.5s. `X-Accel-Buffering: no` disables nginx buffering.

### Supported platforms (Netmiko device types)
The list is `SUPPORTED_PLATFORMS` (`src/validation.py`, derived from `PLATFORMS`; the pure input checks are module functions there, `Validator` only checks the CLI/import files); how each one finishes a push (save / commit / a command / nothing) and prints its config is the `PLATFORMS` dict in `src/platforms.py` (platform knowledge only, no I/O: also `rejection()` and `verify_commands()`; the SSH is the engine's, `src/core.py`, whose status rules are `classify()`) — adding a vendor is one row there plus a fixture in `tests/unit/test_platforms.py`. Cases only a person can resolve are logged as `ACTION NEEDED — <ip:port>: …` and counted in the rollout summary. Verify (`verify_commands`) places each typed command in its config section and checks presence / absence (`no`/`undo`/`delete`/`unset`); all 12 platforms are covered.

### Device CSV format (shared by CLI and web import)
Required: `ip`, `device_type`, `port`. Optional: `label`; credentials `username`, `password`, `secret`; attribute columns named after a property (by name or label).
- **CLI**: credentials required; attribute columns unused; unreachable devices are dropped before the push.
- **Web import**: attribute columns saved as `var_maps`; credentials become security profiles (checkbox, default on — exact match reuses a profile, otherwise a new uniquely labelled one); unknown columns reported; no reachability check.

## Frontend

Design rules, template gotchas and widget notes live in `templates/CLAUDE.md` (loaded when working under `templates/`).

## Working style
- The developer writes the code; Claude reviews, advises, and discusses design
- Exception, granted per feature: the developer may approve Claude writing backend code for a specific feature — plan first, stick to the approved plan, and verify on a scratch DB (never the live one) before touching real data
- Always read actual source before suggesting changes
- Frame architecture feedback in terms of encapsulation, minimal API, abstraction, information hiding
- Developer has real networking domain knowledge (3+ years, Netmiko/NAPALM fluency) — no need to explain networking basics
- Distinguish critical issues from design improvements from minor polish when reviewing
- For frontend work, Claude writes the templates/HTML directly (exception to the "developer writes" rule)
- **Changing an existing test needs a proper reason, stated in the commit** — a behavior change the developer approved, a wrong fixture/expectation proven against the source or the vendor's documentation, or a rename. A failing test is a finding first: fix the code, or report it. Never edit an expectation to match what the code now does (retrofitting) without the developer's approval, never loosen an assertion (exact → "somewhere", value → truthy) to make it pass, and never add `skip`/`xfail` to get green. A new test must fail without the change it covers.
