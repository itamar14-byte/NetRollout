# Development Workplan
_Last updated: 2026-04-06_

---

## Phase 1 — User Auth Pipeline

Self-contained deliverable: a working register/login/logout flow wired into the existing webapp.
No dependency on the architecture decisions coming in Phase 2.
Using **Flask-Login** (not manual sessions) for session management.

### 1.1 Flask-Login setup ✅
- `flask-login` installed, added to `requirements.txt`
- `UserMixin` added to `User` model in `tables.py`
- `LoginManager` initialized in `webapp.py`, login view set to `"home"`
- `user_loader` callback wired to DB via `get_session()`
- `@login_required` applied to all protected routes

### 1.2 Frontend ✅
- `templates/index.html` — replaced Get Started with login card + flash messages + register link
- `templates/base.html` — user widget dropdown (username, My Account, Logout) shown when authenticated

### 1.3 Auth routes — backend (in progress)
- `POST /login` — fetch user from DB, `check_password_hash`, `login_user()`
- `POST /register` — `generate_password_hash`, create `User`, commit to DB
- `GET /logout` — `logout_user()`, redirect to home
- `GET /account` — user stats placeholder (full stats wired in Phase 2)
- Password hashing via `werkzeug.security` in the register route (server-side only, DB never sees plaintext)

### 1.4 Frontend — remaining
- `templates/register.html` — registration form
- `templates/account.html` — account page (username, member since, stats placeholder)

---

## Architecture Session (between Phase 1 and Phase 2)

Before writing Phase 2 code, design the full extended OOP architecture:
- `RolloutJob` interface and lifecycle
- `RolloutLogger` class design and injection points
- `RolloutSession` and `DeviceResult` DB schema
- How the DB session ties into job start/end/per-device results
- `InputParser` and `Validator` class interfaces
- Private method boundaries across all classes

Phase 2 implementation follows from this design — no code before the session.

---

## Phase 2 — Architecture Refactor & DB Integration

### 2.1 DB schema
Add to `tables.py`:
- `RolloutSession` — one row per rollout run (timestamp, status, initiated_by)
- `DeviceResult` — one row per device per session (ip, device_type, commands_sent, commands_verified, status)

### 2.2 `RolloutJob` object
Replace module-level `cancel_event` and `LOG_QUEUE` globals in `webapp.py` with a per-job object:
```
RolloutJob
  id: str                  # maps to RolloutSession.id in DB
  cancel_event: Event
  log_queue: Queue
  engine: RolloutEngine
  thread: Thread
```
Webapp stores active jobs in a dict keyed by job ID.
SSE stream consumes from `job.log_queue` — stream terminates on done sentinel (fixes medium bug).
Cancel hits `job.cancel_event` only (fixes cancel_event singleton medium bug).

### 2.3 `RolloutLogger` class
Refactor `logging_utils.py` module-level globals into a `RolloutLogger` class owning a queue and log file.
One instance per `RolloutJob`, injected into `RolloutEngine`.

### 2.4 Wire `RolloutEngine.run()` into DB
Open a `RolloutSession` at rollout start, write `DeviceResult` rows per device, close session on completion.

### 2.5 Remaining OOP gaps
- `push_config()`, `verify()`, `notify()` on `RolloutEngine` → prefix `_`
- `netmiko_connector()` on `Device` → `_netmiko_connector()`
- `validation.py` → `Validator` class
- `parse_files()` / `prepare_devices()` → `InputParser` class

---

## Phase 3 — Testing

- Auth route tests (Flask test client)
- Tests for `RolloutJob`, `RolloutLogger`, refactored OOP classes
- Update existing tests to target new class instances instead of module-level globals

---

## Phase 4 — Packaging & Deployment

### 4.1 Module packaging
Structure the project as a proper Python package with `pyproject.toml` / `setup.py`.

### 4.2 Executable
Build a standalone `.exe` using PyInstaller for client distribution.
Bundles the CLI tool — no Python install required on client machines.

### 4.3 Docker stack
`docker-compose.yml` with three services:
- `app` — Waitress serving the Flask webapp
- `db` — PostgreSQL
- `nginx` — reverse proxy in front of `app`, handles TLS termination

Environment-based config (`DATABASE_URL`, `SECRET_KEY`) via `.env`.

### 4.4 Documentation
- `README.md` — project overview, quick start, CLI usage, CSV format reference
- Inline docs review — docstrings consistent across all public APIs
- Deployment guide — Docker setup, environment variables, first-run DB init

---

## Order rationale
Phase 1 is independent and delivers visible user-facing value immediately.
Architecture session gates Phase 2 — no structural code without a design.
Phase 2 and 3 are coupled — tests follow code changes.
Phase 4 last — packaging assumes a stable, complete codebase.
