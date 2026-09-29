# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
python -m src.cli -d <devices.csv> -c <commands.txt> [-vy] [-vb]
```
- `-vy` / `--verify`: verify config was applied after push (NAPALM); `-vb` / `--verbose`: print logs to the console (always written to `logs/`)
- Missing paths (and verify) are prompted for. Exit code: 0 all devices succeeded, 1 mixed, 2 nothing applied, 130 Ctrl+C

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
The app applies migrations itself at startup; the alembic CLI resolves `DATABASE_URL`, else `PG_*`.

### Install dependencies
```bash
pip install -r requirements.txt
```

### Configuration
- `config.env` (repo root, loaded at startup): `DATABASE_URL` or `PG_HOST/PG_PORT/PG_NAME/PG_USER/PG_PASSWORD/PG_SCHEMA`; `REDIS_URL` or `REDIS_HOST/REDIS_PORT/REDIS_DB/REDIS_PASSWORD`. Server Management (admin) writes these.
- Other env: `SECRET_KEY`, `ORCHESTRATOR_WORKERS` (concurrent jobs, default 4), `PORT`.
- Encryption key: `NETROLLOUT_ENCRYPTION_KEY`, else `~/.netrollout/encryption.key`. The app refuses to start on a malformed, missing-with-data, or mismatched key (fail-fast).
- Dev DB: `postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`, in Docker: `docker exec -it NetRollout-DB psql -U dbadmin -d rollout_db`

## Architecture

Full architecture in `docs/architecture.md`; plan and current status in `docs/workplan.md` (status table under "Remaining work").

### Module map (`src/`)
- `core.py` — `RolloutOptions` (verify, verbose, webapp, max_workers), `Device` (ip, port, label, device_type, credentials, `var_map_subs`, `extra`; `from_inventory(row, user_id)` applies only that user's mappings; `endpoint` = ip:port), `RolloutEngine(param, devices, commands).run(cancel, logger) → list[DeviceResultDict]` (push, optional verify, per-device status success/partial/failed/cancelled, summary counted from statuses), `mapping_resolvable()` (shared eligibility rule), `SubstitutionError` (device skipped before SSH)
- `orchestration.py` — `RolloutOrchestrator(backend, max_concurrent)`: jobs queued in Redis (`netrollout:job_queue`, BLPOP dispatcher), `submit/cancel/get_job`; `RolloutJob` runs the engine in a thread
- `logging_utils.py` — `RolloutLogger(webapp, verbose, prefix, job_id, redis_client)`: writes `logs/{prefix}_{ts}[_{job_id}].log` (UTF-8); in the web app also Redis pub/sub + history for SSE. `prune_logs()` (60-day retention), `utf8_console()`
- `input_parser.py` — `InputParser`: `prepare_devices(rows, require_credentials, check_reachable)` (shared by CLI and web), `csv_to_inventory(...) → ImportReport`, `parse_commands(path)` (utf-8-sig, blank lines dropped)
- `validation.py` — `Validator`: ip/port/platform checks, `test_tcp_port`, mapping token/property/index validators
- `reachability.py` — `ReachabilityChecker`: one TCP connect per `ip:port`, cached 60s in Redis (`reach:{ip}:{port}`); exposed as `app.web.reachability`
- `encryption.py` — Fernet `encrypt/decrypt`, `init_encryption` (fail-fast key checks)
- `ldap_auth.py` — search-then-bind via the service account (constructed DN fallback), group membership, directory browsing
- `cli.py` — headless entry point
- `db/` — `tables.py` (ORM), `backend.py` (`BackendServices`: Postgres + Redis connections, health, hot reload), `postgres_db.py`, `redis_db.py`, `db_install.py` (migrations, admin seed, retention jobs), `alembic/`
- `webapp/` — `__init__.py` (`create_app()`), `__main__.py` (entry: UTF-8 console, log pruning, Waitress), `setup.py` (config, Redis sessions, metrics, orchestrator), `extensions.py` (CSRF, login manager, error handlers), `utils.py` (`ok()/err()`, `@require_admin`, `@with_json/@with_form`, `WebServices`: `audit()`, `act_on_db_obj()`, `get_property_defs()`; device visibility helpers), `blueprints/` — auth, inventory, security, mappings, properties, rollout, jobs, analytics, admin_users, admin_observability, admin_servers

### Tables (`db/tables.py`)
- `User` — local or LDAP account, role, approval, OTP
- `Inventory` — device (ip, port, label, device_type, `var_maps` JSON attributes, `is_global`); FK to User and SecurityProfile (nullable). Global devices are admin-owned and visible to everyone; only admins edit them
- `SecurityProfile` — label (nullable), username, Fernet-encrypted password / enable secret; FK to User
- `VariableMapping` — `$$TOKEN$$` → property + optional list index; many-to-many with Inventory; each user binds their own mappings (also on global devices)
- `PropertyDefinition` — user-defined attributes (name, label, icon, is_list) next to the built-in system properties
- `DeviceResult` — one row per device per job (ip, port, status, counts, config snapshot)
- `JobMetadata` — the job's commands + note
- `AuditLog` — append-only (actor, dot-namespaced action, object, success, ip, detail)
- `LDAPServer`, `LDAPGroup` — directory config and group → role mappings

Retention: job record (results + metadata) 30 days, config snapshots 7 days, audit log 90 days (pg_cron, constants in `db_install.py`); log files 60 days (app-side).

### Webapp real-time logging
Server-Sent Events at `/rollout/stream/<job_id>`: history from Redis, then live messages via Redis pub/sub (`job:{id}:logs`), a heartbeat every 0.5s. `X-Accel-Buffering: no` disables nginx buffering.

### Supported platforms (Netmiko device types)
`cisco_ios`, `cisco_nxos`, `cisco_xe`, `cisco_xr`, `juniper_junos`, `arista_eos`, `fortinet`, `paloalto_panos`, `aruba_aoscx`, `checkpoint_gaia`, `hp_procurve`, `hp_comware`

NAPALM verification not supported for `checkpoint_gaia` and `hp_comware`.

### Device CSV format (shared by CLI and web import)
Required: `ip`, `device_type`, `port`. Optional: `label`; credentials `username`, `password`, `secret`; attribute columns named after a property (by name or label).
- **CLI**: credentials required; attribute columns unused; unreachable devices are dropped before the push.
- **Web import**: attribute columns saved as `var_maps`; credentials become security profiles (checkbox, default on — exact match reuses a profile, otherwise a new uniquely labelled one); unknown columns reported; no reachability check.

## Frontend

Always-dark enterprise aesthetic — permanently dark, no toggle. Key design elements:
- **Fonts:** Inter (body) + JetBrains Mono (monospace/badges)
- **Accent color:** `#00bcd4` cyan
- **Custom classes:** `.nr-card`, `.nr-card-accent`, `.nr-card-body`, `.nr-badge`, `.nr-label`, `.nr-back-btn`
- **Dot-grid background:** `body::before` at `z-index: -1` (NOT 0 — traps modals)
- **`.container` must NOT have z-index** — breaks Bootstrap modal stacking
- All Bootstrap components overridden in `base.html` to match dark theme
- All operator pages extend `operator_base.html`. Topbar and footer are automatic.
- **Admin pages extend `admin.html`** — standalone template (does not extend `base.html` or `operator_base.html`). Own topbar with ← Home button, own collapsible sidebar (Access / Observability / System sections), own footer, restart button. Sub-pages: `admin_users.html`, `admin_audit.html`, `admin_analytics.html`, `server_management.html`.
- **nginx** sits in front of Waitress. Config at `docs/nginx/nginx.conf`, bind-mounted to `/etc/nginx/nginx.conf`. Flask uses `ProxyFix(x_for=1, x_proto=1, x_host=1)`. SSE (`/rollout/stream/<job_id>`) streams through nginx because the app sends `X-Accel-Buffering: no`; the `location /rollout_stream` block in `nginx.conf` is dead config, to be cleaned up with the 4.1 compose rewrite.
- **Vendor logos**: `VENDOR_LOGOS` dict in `webapp.py` maps Netmiko device_type → Simple Icons CDN URL. Registered as Jinja2 global — available in all templates as `VENDOR_LOGOS`.
- **NrSelect widget**: custom FortiGate-style dropdown in `inventory.html` — search box, scrollable list, shield icon, cyan checkmark. Init with `initNrSelect(containerId)`, returns `{getValue, setValue, reset}`.
- **Inventory cards**: thin horizontal rectangles — vendor badge (CDN SVG + BI router fallback) + label + IP. Hover tooltip (FortiGate-style fixed panel). Click → edit modal.
- **Assign board** (Security Profiles + Variable Mappings devices modals): shared `nrAssignBoard()` in `operator_base.html` <head>. Two columns, Assigned | Available/Eligible; drag either way or click/Enter a card to move it. Moves are staged; Save shows `(+N / −M)`. Security saves via `/inventory/bulk_assign` (`profile_id: null` unassigns) and warns when devices lose their profile; mappings save via `/mappings/bulk_assign` with `device_ids` + `remove_ids`.

## Phase status
- **Phase 1 — Auth pipeline ✅ COMPLETE (2026-04-06)**
- **Frontend redesign ✅ COMPLETE (2026-04-07)**
- **Architecture session ✅ COMPLETE (2026-04-07)**
- **Phase 2 — Architecture refactor + DB integration ✅ COMPLETE (2026-04-11)**
- **Phase 3.1 — Variable mapping builder ✅ COMPLETE (2026-04-11)**
- **Phase 3.1b — CSV import ✅ COMPLETE (2026-04-11)**
- **Phase 3.2 — Rollout initiation from web UI ✅ COMPLETE (2026-04-12)**
- **Phase 3.3 — Results page ✅ COMPLETE (2026-04-13)** — expandable job rows, See Commands modal, Download Log, side-by-side LCS diff
- **Phase 3.4 — Audit trail ✅ COMPLETE (2026-04-13)** — AuditLog table, 21 instrumented routes, /admin/audit filterable UI, log file infrastructure
- **Phase 3.4b — Analytics + Query Builder ✅ COMPLETE (2026-04-17)** — KPI cards, jQuery QueryBuilder compound filters, `/analytics/query` + `/admin/analytics/query` POST routes, CSV export
- **Phase 3.5 — Test suite ✅ (2026-09-29)**, **3.6 — Per-job device concurrency ✅**
- **Phase 4 — Packaging:** 4.6/4.6b sessions + Redis ✅, 4.7 Alembic ✅, 4.8 Server Management ✅, 4.8b nginx ✅, 4.8c Admin panel ✅, 4.9 Grafana ✅, 4.9b LDAP ✅, 4.9c cleanup ✅, 4.0 Blueprints ✅, pre-4.1 cleanup ✅ (branch `pre-4.1-cleanup`). **Next:** EVE-NG round, then 4.1 Docker. Current status table: `docs/workplan.md` → "Remaining work"

## Frontend asset structure
Per-page CSS and JS live inline in `{% block extra_style %}` and `{% block extra_script %}` blocks — no build pipeline, one file per page. Shared widgets (reachability helpers, the assign board) live in `operator_base.html` `<head>` so page scripts can call them. Extracting to `static/css/` and `static/js/` is deferred post-v1.0.

## Working style
- The developer writes the code; Claude reviews, advises, and discusses design
- Exception, granted per feature: the developer may approve Claude writing backend code for a specific feature — plan first, stick to the approved plan, and verify on a scratch DB (never the live one) before touching real data
- Always read actual source before suggesting changes
- Frame architecture feedback in terms of encapsulation, minimal API, abstraction, information hiding
- Developer has real networking domain knowledge (3+ years, Netmiko/NAPALM fluency) — no need to explain networking basics
- Distinguish critical issues from design improvements from minor polish when reviewing
- For frontend work, Claude writes the templates/HTML directly (exception to the "developer writes" rule)
