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
python cli.py -d <_devices.csv> -c <_commands.txt> [-vy] [-vb]
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

### Target class structure (Phase 2 — not yet implemented)

**Data classes:**
- `RolloutOptions` — flags: verify, verbose, webapp
- `Device` — ip, username, password, device_type, secret, port, label. Factory: `from_inventory()`. Private: `_netmiko_connector()`. Public: `fetch_config(logger)`

**ORM models (`tables.py`):**
- `User` — master table. Relationships: inventory, security_profiles, variable_mappings, sessions, results
- `Inventory` — device topology, FK to User + SecurityProfile
- `SecurityProfile` — encrypted credentials (Fernet), FK to User
- `VariableMapping` — $$TOKEN$$ → device property name, FK to User
- `RolloutSession` — ephemeral active jobs ("RAM" table), FK to User
- `DeviceResult` — permanent archive ("MEMORY" table), one row per device per job, FK to User

**Service classes:**
- `Validator` — all @staticmethod validation methods
- `InputParser(validator)` — from_files(), from_web(), from_inventory()
- `RolloutLogger` — owns queue + logfile. log(), notify(), get()

**Job execution:**
- `RolloutEngine(param, devices, commands)` — run(cancel_event, logger), _push_config(), _verify()
- `RolloutJob(id, engine, logger)` — owns thread + cancel_event. start(), cancel()
- `RolloutOrchestrator(max_concurrent)` — singleton. Owns {job_id: RolloutJob}. submit(), cancel(), get(), _dispatch(), _cleanup()

### Current class structure (pre-Phase 2, what exists now)
- `RolloutOptions` — dataclass, flags only
- `Device` — dataclass, netmiko_connector() (should be private), fetch_config()
- `RolloutEngine` — public: run(). Should-be-private: push_config(), verify(), notify()
- `User` — ORM model, auth only, no relationships yet
- `logging_utils.py` — module-level globals (OOP gap)
- `validation.py` — standalone functions (OOP gap)
- `cancel_event` — module-level singleton in webapp.py (concurrency bug)

### Webapp real-time logging
Server-Sent Events (SSE). `LOG_QUEUE` in `logging_utils.py` is the shared channel — `base_notify(..., webapp=True)` enqueues HTML-colored messages, `/rollout_stream` streams them to the browser. In Phase 2 this moves to per-job `RolloutLogger`.

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
- All pages extend `base.html`. Topbar and footer are automatic.

## Phase status
- **Phase 1 — Auth pipeline ✅ COMPLETE (2026-04-06)**
- **Frontend redesign ✅ COMPLETE (2026-04-07)**
- **Architecture session ✅ COMPLETE (2026-04-07)** — see `docs/architecture.md`
- **Phase 2 — Architecture refactor + DB integration (next)**
- **Phase 3 — Testing**
- **Phase 4 — Packaging**

## Working style
- The developer writes the code; Claude reviews, advises, and discusses design
- Always read actual source before suggesting changes
- Frame architecture feedback in terms of encapsulation, minimal API, abstraction, information hiding
- Developer has real networking domain knowledge (3+ years, Netmiko/NAPALM fluency) — no need to explain networking basics
- Distinguish critical issues from design improvements from minor polish when reviewing
- For frontend work, Claude writes the templates/HTML directly (exception to the "developer writes" rule)
