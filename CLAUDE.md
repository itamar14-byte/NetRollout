# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Network bulk configuration tool that pushes configuration snippets to multiple network devices simultaneously. It has two interfaces:
- **CLI** (`src/cli.py`): headless tool invoked directly
- **Web app** (`src/webapp.py`): Flask app served via Waitress on port 8080

## Commands

### Run the CLI
```bash
cd src
python cli.py -d <devices.csv> -c <commands.txt> [-vy] [-vb]
```
- `-vy` / `--verify`: verify config was applied after push (uses NAPALM)
- `-vb` / `--verbose`: print logs to console (always written to timestamped `.log` file)

### Run the web app
```bash
cd src
python webapp.py
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
> Note: `tests/test.py` is a stub — tests need to be written.

### Install dependencies
```bash
pip install -r requirements.txt
```

## Architecture

All source code lives in `src/`. Scripts import each other directly (no package `__init__.py`), so they must be run from the `src/` directory.

### Data flow

1. **Input parsing** — `core.py:parse_files()` reads a CSV of devices and a `.txt` of commands. For the webapp, `webapp.py:webapp_input()` does equivalent parsing from uploaded files or JSON form data.
2. **Device preparation** — `core.py:prepare_devices()` validates each device row (via `validation.py`) and tests TCP reachability before constructing `Device` objects.
3. **Config push** — `RolloutEngine.push_config()` iterates devices and uses **Netmiko** (`ConnectHandler`) to SSH in, enter config mode, and send each command.
4. **Verification** (optional) — `RolloutEngine.verify()` re-connects via **NAPALM** (`get_config()`) and checks each command appears in the running config string.
5. **Cancellation** — A `threading.Event` (`cancel_event`) is checked at each device iteration; the webapp sets it via `POST /cancel_rollout`.

### Key files

| File | Responsibility |
|---|---|
| `src/core.py` | `Device` dataclass, `RolloutEngine`, `parse_files`, `prepare_devices` |
| `src/cli.py` | Argument parsing, CLI entry point |
| `src/webapp.py` | Flask routes, SSE stream (`/rollout_stream`), background thread management |
| `src/validation.py` | IP/port/platform validation, TCP reachability probe (`test_tcp_port`) |
| `src/logging_utils.py` | ANSI/HTML color formatting, `log()` to timestamped file, `LOG_QUEUE` for SSE |
| `src/db.py` | SQLAlchemy engine/session from `DATABASE_URL` env var |
| `src/tables.py` | `User` ORM model |
| `src/db_install.py` | Creates all tables (`Base.metadata.create_all`) |

### Webapp real-time logging

The webapp uses **Server-Sent Events** (SSE). `LOG_QUEUE` (a `queue.Queue`) in `logging_utils.py` is the shared channel — `base_notify(..., webapp=True)` enqueues HTML-colored messages, and `/rollout_stream` streams them to the browser.

### Supported platforms (Netmiko device types)

`cisco_ios`, `cisco_nxos`, `cisco_xe`, `cisco_xr`, `juniper_junos`, `arista_eos`, `fortinet`, `paloalto_panos`, `aruba_aoscx`, `checkpoint_gaia`, `hp_procurve`, `hp_comware`

NAPALM verification is not supported for `checkpoint_gaia` and `hp_comware`.

### Device CSV format

Required columns: `ip`, `username`, `password`, `device_type`, `secret`, `port`

## Active development (as of 2026-04-06)

### Phase 1 — User Auth Pipeline (in progress)
Full plan in `docs/workplan.md`. Current state:

**Done:**
- `tables.py` — `User` model has `UserMixin` from Flask-Login ✓
- `db.py` — engine, Base, `get_session()` context manager ✓
- `db_install.py` — `create_all` with error handling ✓, tables created in DB ✓
- `webapp.py` — `LoginManager` initialized, `user_loader` callback, `@login_required` on all protected routes ✓
- `webapp.py` — `template_folder='../templates'`, `SECRET_KEY`, `flash` import, dummy stubs for all auth routes ✓
- `templates/index.html` — login card with flash messages, register link ✓
- `templates/base.html` — user widget dropdown (account + logout) shown when authenticated ✓
- `requirements.txt` — trimmed to direct dependencies only ✓
- `requirements-dev.txt` — pytest/pytest-cov as dev deps ✓
- Landing page renders correctly ✓

**Still to do (next session — start here):**
1. Replace `/login` stub — fetch user by username, `check_password_hash`, `login_user()`, redirect to upload; flash "Invalid credentials" on failure
2. Replace `/register` stub — check username not taken, `generate_password_hash`, create `User`, commit, `login_user()`, redirect to upload; flash error on conflict
3. Replace `/logout` stub — `logout_user()`, redirect to home
4. Replace `/account` stub — `render_template("account.html")` with `current_user`
5. Write `templates/register.html` — form with username/password/confirm, flash messages, back to login link
6. Write `templates/account.html` — username, member since, stats placeholder

**DB env var:** `DATABASE_URL=postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db` (lowercase `postgresql`, `+psycopg2` suffix — psycopg2-binary is installed)

### 2. OOP restructuring (Phase 2 — after architecture session)
Known gaps:
1. `logging_utils.py` global state → `RolloutLogger` class injected into `RolloutEngine`
2. `push_config()`, `verify()`, `notify()` on `RolloutEngine` → prefix `_`
3. `netmiko_connector()` on `Device` → `_netmiko_connector()`
4. `validation.py` → `Validator` class
5. `parse_files()` / `prepare_devices()` → `InputParser` class

## Working style
- The developer writes the code; Claude reviews, advises, and discusses design
- Always read actual source before suggesting changes
- Frame architecture feedback in terms of encapsulation, minimal API, abstraction, and information hiding
- Developer has real networking domain knowledge (3+ years network engineering, Netmiko/NAPALM fluency) — no need to explain networking basics
- Distinguish critical issues from design improvements from minor polish when reviewing
