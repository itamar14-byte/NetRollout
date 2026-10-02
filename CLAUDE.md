# CLAUDE.md

## Overview

Network bulk configuration tool that pushes configuration snippets to multiple network devices simultaneously. Two interfaces:
- **CLI** (`src/cli.py`): headless one-time rollout from a devices CSV + commands file — no database
- **Web app** (`src/webapp/` package): Flask app served via Waitress on port 8080, behind nginx; Postgres + Redis

## Commands

Everything runs **from the repo root**: `src` is a package and all imports are `src.*`.

### Run the web app
```bash
python -m src.webapp
```
App at `http://localhost:8080` (nginx in front on 80/443). Every start runs the Alembic migrations, seeds the factory `admin`/`admin` account if missing, schedules the pg_cron retention jobs (skipped if pg_cron is unavailable), and prunes old log files. There is no separate DB-init step.

### Run the CLI
```bash
python -m src.cli -d <devices.csv> -c <commands.txt> [-vf] [-v]
```
- `-vf` / `--verify`: after the push, read each device's config and check every command; `-v` / `--verbose`: print logs to the console (always written to `logs/`)
- Missing paths are prompted for (asked again until the file exists); a prompted run also asks verify and confirms ("About to push N commands to M devices") before the push — full-flag runs never ask. The verify choice is always printed. Exit code: 0 all devices succeeded, 1 mixed, 2 nothing applied (including a stop before the push: missing file, no devices or commands left), 130 Ctrl+C. Every exit but Ctrl+C waits for Enter when run from a terminal, so a double-clicked window stays readable. `--version` prints the version.
- **Standalone `netrollout-cli.exe`** (no Python needed; same flags): `pyinstaller --clean --noconfirm netrollout-cli.spec` from the repo root in the dev venv → `dist/netrollout-cli.exe` (one file, ~15 MB, ~2 s startup; logs go to `logs/` next to it). The CLI must not import the web stack — `tests/unit/test_cli.py` guards it, and the spec excludes it. The exe is unsigned: Windows Defender / SmartScreen may flag it (code-signing is post-v1.0).

### Run tests
```bash
pytest                    # all
pytest tests/unit         # hermetic, no services
```
Integration tests use a real Postgres (`rollout_test` DB) and Redis (db 15) and are skipped with a reason when a service is unhealthy — if ~150 skip right after `docker` starts, Postgres wasn't ready; rerun. LDAP tests start and remove an ephemeral OpenLDAP container. The pg_cron test is opt-in (`TEST_PG_CRON_URL`). Known bugs are recorded as strict xfails.

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
- Other env: `SECRET_KEY`, `PORT` (internal app port, set at install).
- **System Settings** (admin panel → System; `src/db/settings.py`): retention periods, concurrent rollout jobs, devices per job, reachability cache, canonical hostname + HTTPS port. The `system_settings` table is the only runtime source: `install()` seeds every missing setting at each start (from `ORCHESTRATOR_WORKERS`, `NETROLLOUT_PUBLIC_HOSTNAME`, `NETROLLOUT_HTTPS_PORT` if set, else the default) and never overwrites; after that env vars are ignored. Cross-setting rules are declarative and enforced on the server and in the page.
- Deployment (`src/runtime.py`): `NETROLLOUT_DEPLOYMENT=docker` (set by the image) → `SECRET_KEY` and the encryption key are required (never defaulted or generated), Restart exits and the restart policy brings it back, no startup proxy probe (one log line with the expected URL instead). Dev: a missing `SECRET_KEY` gets a random per-run key.
- Startup in dev (`src/webapp/startup.py`): verifies nginx forwards to this instance (per-run token at `/_netrollout/instance`) and prints the address to use; opens it in the browser on desktop launches (`NETROLLOUT_OPEN_BROWSER=0` to disable).
- Stop / Restart drain (`src/webapp/lifecycle.py`, `RolloutOrchestrator.drain`): on SIGTERM or the admin Restart, new rollouts are refused (banner on every page), queued ones are recorded as cancelled, running ones finish within `NETROLLOUT_DRAIN_SECONDS` (default 600) and are cancelled after. Restart with rollouts running asks: when finished / now.
- Health: `/_netrollout/health` (public) — Postgres/Redis up, running/queued rollouts, draining, version; 200 or 503. Version string in `src/runtime.py`.
- Passwords (`src/passwords.py`, local accounts): one rule — 8+ characters, at least 2 of letters / digits / special, ASCII, not containing the username — for registration, `/account/password` and admin resets (a temporary password shown once). `users.must_change_password` (the seeded `admin`, after a reset) gates every page to the change page. A reset or Terminate Session signs the user out everywhere, a change signs out the user's other sessions (`utils.end_user_sessions`).
- Encryption key: `NETROLLOUT_ENCRYPTION_KEY`, else (dev only) `~/.netrollout/encryption.key`. The app refuses to start on a malformed, missing-with-data, or mismatched key (fail-fast).
- Dev DB: `postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`, in Docker: `docker exec -it NetRollout-DB psql -U dbadmin -d rollout_db`

## Architecture

Full architecture in `docs/architecture.md`; plan and current status in `docs/workplan.md` (status table under "Remaining work"). Phase 4 (packaging v1.0.0) is planned in `docs/plans/phase-4.md`.

Retention (defaults; System Settings): job record (results + metadata) 30 days, config snapshots 7 days, audit log 90 days (pg_cron statements read the setting's row at run time); log files 60 days (app-side, daily).

### Webapp real-time logging
Server-Sent Events at `/rollout/stream/<job_id>`: history from Redis, then live messages via Redis pub/sub (`job:{id}:logs`), a heartbeat every 0.5s. `X-Accel-Buffering: no` disables nginx buffering.

### Supported platforms (Netmiko device types)
The list is `Validator.SUPPORTED_PLATFORMS` (`src/validation.py`); how each one finishes a push (save / commit / a command / nothing) and prints its config is the `PLATFORMS` dict in `src/core.py` — adding a vendor is one row there plus a fixture in `tests/unit/test_platforms.py`. Cases only a person can resolve are logged as `ACTION NEEDED — <ip:port>: …` and counted in the rollout summary. Verify (`verify_commands`) places each typed command in its config section and checks presence / absence (`no`/`undo`/`delete`/`unset`); all 12 platforms are covered.

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
