# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Network bulk configuration tool that pushes configuration snippets to multiple network devices simultaneously. Two interfaces:
- **CLI** (`src/cli.py`): headless tool invoked directly
- **Web app** (`src/webapp.py`): Flask app served via Waitress on port 8080

## Commands

### Run the CLI
```bash
cd src
python cli.py -d <devices.csv> -c <_commands.txt> [-vy] [-vb]
```
- `-vy` / `--verify`: verify config was applied after push (uses NAPALM)
- `-vb` / `--verbose`: print logs to console (always written to timestamped `.log` file)

### Run the web app
```bash
cd src
python _webapp.py
```
App available at `http://localhost:8080`.

### Initialize the database
Requires `DATABASE_URL` environment variable (PostgreSQL URL via psycopg).
```bash
cd src
python db_install.py
```

### Run tests
```bash
python -m pytest tests/
```

### Install dependencies
```bash
pip install -r requirements.txt
```

## Architecture

Full architecture documented in `docs/architecture.md`. Workplan in `docs/workplan.md`.

All source code lives in `src/`. Scripts import each other directly (no package `__init__.py`), so they must be run from the `src/` directory.

### Current class structure (Phase 2 — implemented)

**Data classes (`core.py`):**
- `RolloutOptions` — flags: verify, verbose, webapp
- `Device` — ip, username, password, device_type, secret, port, label, extra (dict, default `{}`). Factory: `from_inventory()`. Public: `netmiko_connector()`, `fetch_config(logger)`
- `DeviceResultDict` — TypedDict for `run()` return value

**ORM models (`tables.py`):**
- `User` — master table. Relationships: inventory, security_profiles, variable_mappings, sessions, results
- `Inventory` — device topology, FK to User + SecurityProfile (nullable). `var_maps` JSON column stores optional per-device substitution attributes (hostname, loopback_ip, asn, mgmt_vrf, mgmt_interface, site, domain, timezone, vrfs[])
- `SecurityProfile` — label (nullable), username (plaintext), password_secret (Fernet-encrypted), enable_secret (Fernet-encrypted, nullable). FK to User
- `VariableMapping` — free-text `$$TOKEN$$` → `property_name` + nullable `index` (int). `index=None` = string substitution; `index=N` = `device.extra[property_name][N]`. FK to User
- `RolloutSession` — ephemeral active jobs ("RAM" table), FK to User. Deleted on completion
- `DeviceResult` — permanent archive ("MEMORY" table), one row per device per job, FK to User

**Service classes:**
- `Validator(logger)` — instance methods: validate_device_data(), validate_file_extension(). Static: validate_ip(), validate_port(), validate_platform(), test_tcp_port()
- `InputParser(validator, logger)` — csv_to_inventory(), form_to_inventory(), parse_commands(), _prepare_devices(). Static: import_from_inventory()
- `RolloutLogger(webapp, verbose, logfile)` — log(), notify(), get()

**Job execution:**
- `RolloutEngine(param, devices, commands)` — run(cancel_event, logger) → list[DeviceResultDict], _push_config(), _verify()
- `RolloutJob(job_id, user_id, engine, options)` — owns thread + cancel_flag + logger. start(on_complete), cancel(), is_alive(), is_pending()
- `RolloutOrchestrator(max_concurrent=4)` — singleton. submit(), cancel(), get(), _dispatch(), _cleanup()

### Webapp real-time logging
Server-Sent Events (SSE). Per-job `RolloutLogger` owns the queue. `/rollout_stream` calls `job.get_log()` which blocks on the queue. Messages are HTML-colored strings enqueued by `logger.notify()`.

### Supported platforms (Netmiko device types)
`cisco_ios`, `cisco_nxos`, `cisco_xe`, `cisco_xr`, `juniper_junos`, `arista_eos`, `fortinet`, `paloalto_panos`, `aruba_aoscx`, `checkpoint_gaia`, `hp_procurve`, `hp_comware`

NAPALM verification not supported for `checkpoint_gaia` and `hp_comware`.

### Device CSV format
Required columns: `ip`, `username`, `password`, `device_type`, `secret`, `port`

### DB env var
`DATABASE_URL=postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`
DB runs in Docker: `docker exec -it NetRollout-DB psql -U dbadmin -d rollout_db`

## Frontend

Always-dark enterprise aesthetic — permanently dark, no toggle. Key design elements:
- **Fonts:** Inter (body) + JetBrains Mono (monospace/badges)
- **Accent color:** `#00bcd4` cyan
- **Custom classes:** `.nr-card`, `.nr-card-accent`, `.nr-card-body`, `.nr-badge`, `.nr-label`, `.nr-back-btn`
- **Dot-grid background:** `body::before` at `z-index: -1` (NOT 0 — traps modals)
- **`.container` must NOT have z-index** — breaks Bootstrap modal stacking
- All Bootstrap components overridden in `base.html` to match dark theme
- All operator pages extend `operator_base.html`. Topbar and footer are automatic.
- **Vendor logos**: `VENDOR_LOGOS` dict in `webapp.py` maps Netmiko device_type → Simple Icons CDN URL. Registered as Jinja2 global — available in all templates as `VENDOR_LOGOS`.
- **NrSelect widget**: custom FortiGate-style dropdown in `inventory.html` — search box, scrollable list, shield icon, cyan checkmark. Init with `initNrSelect(containerId)`, returns `{getValue, setValue, reset}`.
- **Inventory cards**: thin horizontal rectangles — vendor badge (CDN SVG + BI router fallback) + label + IP. Hover tooltip (FortiGate-style fixed panel). Click → edit modal.
- **Security profiles drag-assign**: devices modal has "+" button that widens modal to split view. Right panel: draggable unassigned device cards. Left panel: assigned list + dashed drop zone. Drop triggers `cardLand` animation. Save via AJAX to `/inventory/bulk_assign`.

## Phase status
- **Phase 1 — Auth pipeline ✅ COMPLETE (2026-04-06)**
- **Frontend redesign ✅ COMPLETE (2026-04-07)**
- **Architecture session ✅ COMPLETE (2026-04-07)** — see `docs/architecture.md`
- **Phase 2 — Architecture refactor + DB integration (IN PROGRESS)** — all core refactoring + DB wiring done. Security Profiles UI complete. Inventory UI frontend complete. Remaining: inventory backend routes (create/edit/delete/bulk_assign)
- **Phase 3 — Testing**
- **Phase 4 — Packaging**

## Working style
- The developer writes the code; Claude reviews, advises, and discusses design
- Always read actual source before suggesting changes
- Frame architecture feedback in terms of encapsulation, minimal API, abstraction, information hiding
- Developer has real networking domain knowledge (3+ years, Netmiko/NAPALM fluency) — no need to explain networking basics
- Distinguish critical issues from design improvements from minor polish when reviewing
- For frontend work, Claude writes the templates/HTML directly (exception to the "developer writes" rule)
