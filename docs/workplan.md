# Development Workplan
_Last updated: 2026-04-07 — Architecture session complete_

---

## Phase 1 — User Auth Pipeline ✅ COMPLETE

### 1.1 Flask-Login setup ✅
- `flask-login` installed, added to `requirements.txt`
- `UserMixin` added to `User` model in `tables.py`
- `LoginManager` initialized in `webapp.py`, login view set to `"home"`
- `user_loader` callback wired to DB via `get_session()`, with `expunge()` to avoid DetachedInstanceError
- `@login_required` applied to all protected routes

### 1.2 Frontend ✅
- `templates/index.html` — replaced Get Started with login card + flash messages + register link
- `templates/base.html` — user widget dropdown (username, My Account, Admin Panel for admins, Logout) shown when authenticated
- Full dark mode implemented across all Bootstrap components (cards, inputs, buttons, accordion, tables, alerts, dropdowns, modals)

### 1.3 Webapp infrastructure ✅
- `app = Flask(__name__, template_folder='../templates')` — templates resolved from project root
- `app.config["SECRET_KEY"] = "dev"` — required for flash/session (swap for env var in Phase 4)
- `flash` imported — ready for auth feedback messages
- `DATABASE_URL` must use `postgresql+psycopg2://` dialect (psycopg2-binary is installed, not psycopg3)

### 1.4 Auth routes — backend ✅
- `POST /login` — full decision tree: credentials → is_approved → is_active → admin bypass → OTP flow
- `GET /register` → render form, `POST /register` → hash password, flush, pending approval flash
- `GET /logout` → `logout_user()`, redirect home
- `GET /account` → render `account.html` with `current_user`
- Password hashing via `werkzeug.security` pbkdf2:sha256 — server-side only, DB stores hash never plaintext
- All DB queries use `uuid.UUID(user_id)` cast consistently
- `expunge()` before `login_user()` at all call sites

### 1.5 Frontend — remaining ✅
- `templates/register.html` — live client-side validation: ASCII-only password, min 8 chars, 2-of-3 groups (letters/numbers/special), cannot contain username, password match, email regex, role dropdown, submit greyed until valid, red asterisk on required fields
- `templates/account.html` — username, full name, email, role badge, position, member since with live ticking age counter (years/months/days/hours/minutes/seconds)

### 1.6 TOTP (2FA) ✅
- Mandatory for all non-admin users — no toggle, product-level requirement
- Factory admin (`username="admin"`) is exempt from OTP
- Flow: first login after approval → `otp_secret` is null → forced enrollment → on success save secret → future logins go to verify
- Pre-auth session guard: `session["pre_auth_user_id"]` set at login, checked at OTP routes — prevents navigating directly to OTP routes without credentials
- `GET /otp_enroll` — generate secret (reuse existing from session if failed attempt), build provisioning URI, render QR as base64 PNG
- `POST /otp_enroll` — `pyotp.TOTP.verify(valid_window=2)`, `flush()` then `expunge()` to persist secret, `login_user()`
- `GET/POST /otp_verify` — load user from session guard, verify code, `login_user()`
- `otp_enroll.html` / `otp_verify.html` — 6 individual digit boxes (auto-advance, backspace, paste), circular SVG countdown timer (green→amber≤20s→red≤10s, synced to real 30s TOTP window), shake animation on wrong code
- Dependencies: `pyotp==2.9.0`, `qrcode==8.2`, `pillow==11.0.0`

### 1.7 Admin Panel ✅
- Collapsible sidebar: icon-only (56px) ↔ icon+label (200px), toggled by hamburger button, state persisted in localStorage
- Sidebar sections: User Management (active), Audit Logs (Phase 2 stub), Query (Phase 2 stub)
- `GET /admin` → guard + redirect to `/admin/users`
- `GET /admin/users` → query all users ordered by `created_at`, `expunge_all()`, render table
- `POST /admin/users/<user_id>/<action>` → UUID cast, apply action within session, commit on exit
- Actions: `approve` (is_approved=True, is_active=True), `enable` (is_active=True), `disable` (is_active=False), `promote` (role="admin"), `demote` (role="user")
- `admin_users.html` — live search, sortable columns (client-side), status filter buttons (all/pending/active/inactive)
- Status is ternary: pending (not approved), active (approved + active), inactive (approved + not active)
- Per-row single action button (pill-shaped, color-coded): Approve (green) / Enable (cyan) / Disable (orange) — only relevant button shown, others absent
- Promote/demote always present except factory user row

### 1.8 User Model (final schema) ✅
```
User
  id            UUID PK (uuid.uuid4, non-sequential)
  username      String(64), unique, indexed, not null
  password_hash String(255), not null
  email         String(120), unique, not null
  full_name     String(120), not null
  role          String(40), default "user", not null
  position      String(64), nullable
  is_active     Boolean, default False — overrides UserMixin.is_active
  is_approved   Boolean, default False — distinguishes pending from inactive
  otp_secret    String(32), nullable — null means unenrolled
  created_at    DateTime, default datetime.now
```

### 1.9 Security decisions ✅
- **Data minimization**: device credentials never stored — reduces attack surface deliberately
- UUID PKs: non-sequential, non-enumerable in URLs
- Pre-auth session guard on OTP routes
- Server-side role guard on all `/admin/*` routes (UI hiding is UX only)
- `expunge()` before `login_user()` at all call sites

### DB env var
`DATABASE_URL=postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`

---

## Architecture Session ✅ COMPLETE (2026-04-07)

Full architecture documented in `docs/architecture.md`.

**Decisions made:**
- `RolloutJob` is the lifecycle owner — owns `thread`, `cancel_event`, `engine`, `logger`
- `cancel_event` passed as argument at call time to `RolloutEngine.run()`, `_push_config()`, `_verify()` — no hanging state on engine
- `RolloutLogger` is purely I/O — owns `queue` and `logfile`, replaces `logging_utils.py` globals
- `Validator` — all static methods, pure namespace
- `InputParser` — three entry points: `from_files()`, `from_web()`, `from_inventory()`
- `Device.from_inventory()` factory — single boundary where decryption happens
- `SecurityProfile` — separate table, FK to both `User` (ownership) and `Inventory` (assignment). Encrypted with Fernet. Key from `NETROLLOUT_ENCRYPTION_KEY` env var, fallback to `~/.netrollout/encryption.key`
- `RolloutSession` — "RAM" table, ephemeral, deleted on job completion
- `RolloutOrchestrator` — singleton at app startup, owns `{job_id: RolloutJob}` dict, coordinates multithreading via `_dispatch()`, syncs DB and in-memory state. Webapp routes are thin delegators.
- Config env vars (`NETROLLOUT_ENCRYPTION_KEY`, `MAX_CONCURRENT_JOBS`, `DATABASE_URL`, `SECRET_KEY`) asked interactively in `db_install.py` at install time, with sensible defaults
- `DeviceResult` — "MEMORY" table, one row per device per job, soft `job_id` ref, used for analytics and audit
- `VariableMapping` — Phase 3, hook points designed but not implemented yet
- `User` owns five relationships: `inventory`, `security_profiles`, `variable_mappings`, `sessions`, `results`

---

## Phase 2 — Architecture Refactor & DB Integration
_Started: 2026-04-07 — Updated: 2026-04-09_

### 2.1 DB schema — `tables.py` ✅
Add new ORM models:
- `Inventory` — per-user device topology store, FK to `User` and `SecurityProfile`
- `SecurityProfile` — encrypted credentials (Fernet), FK to `User`, loaded as `user.security_profiles`
- `VariableMapping` — `$$VAR$$` → device property name, FK to `User`, loaded as `user.variable_mappings`
- `RolloutSession` — ephemeral active jobs table ("RAM"), FK to `User`, loaded as `user.sessions`
- `DeviceResult` — permanent archive ("MEMORY"), FK to `User`, soft `job_id` ref, loaded as `user.results`

Add relationships to `User`: `inventory`, `security_profiles`, `variable_mappings`, `sessions`, `results`

### 2.2 Encryption layer ✅
- Fernet encryption/decryption helpers for `SecurityProfile` fields
- Key resolution: `NETROLLOUT_ENCRYPTION_KEY` env var → fallback generate + write to `~/.netrollout/encryption.key`

### 2.3 `RolloutLogger` class — `logging_utils.py` ✅
Refactored module-level globals into `RolloutLogger(webapp, verbose, logfile=None)`. Owns `queue` and `logfile`. Methods: `log()`, `notify()`, `get()`. All `base_notify` imports removed from entire codebase.

### 2.4 `RolloutJob` + `RolloutOrchestrator` — `orchestration.py` ✅
Both classes in one file. `RolloutJob(id, engine, options)` — constructs own logger, owns thread + cancel_flag. `start(on_complete)` uses closure + callback pattern. `RolloutOrchestrator(max_concurrent=4)` — singleton, builds engine+job internally in `submit()`, `_dispatch()` uses `is_alive()`/`is_pending()` for slot management. DB writes (RolloutSession, DeviceResult) stubbed as TODO — pending 2.9.

### 2.4b Install script — moved to Phase 4.

### 2.5 `RolloutEngine` refactor — `core.py` ✅
- `cancel_event` removed from constructor — passed as argument to `run(cancel_event, logger)`, `_push_config()`, `_verify()`
- `notify()` method deleted — replaced by injected `RolloutLogger` at all callsites
- `push_config()` → `_push_config()`, `verify()` → `_verify()`
- `webapp`/`verbose` flags removed from engine — live in logger now, engine only reads `_verify_flag`
- Logfile path surfaced via `os.path.abspath(logger.logfile)`

### 2.6 `Device` updates — `core.py` ✅
- `label` field added
- `netmiko_connector()` kept public (called from different class — private would be bad practice)
- `from_inventory(cls, row: Inventory) -> Device` factory implemented — decrypts credentials from linked `SecurityProfile` via `encryption.decrypt()`
- `fetch_config(logger: RolloutLogger)` — logger injected, `base_notify` removed

### 2.7 `Validator` class — `validation.py` ✅
Logger-injected instance class. `validate_device_data` and `validate_file_extension` are instance methods (need logger). `validate_ip`, `validate_port`, `validate_platform`, `test_tcp_port` remain static.

### 2.8 `InputParser` class — `input_parser.py` ✅ (renamed from parser.py)
Constructor takes `Validator` + `RolloutLogger`. Methods: `csv_to_inventory`, `form_to_inventory`, `parse_commands`, `_prepare_devices`. Static: `import_from_inventory(inventory) -> list[Device]`. `parse_files()` and `prepare_devices()` removed from codebase. `webapp_input()` and `background_rollout()` removed from webapp.

**Webapp rewire ✅** — routes are thin delegators. `cancel_event` global removed. `start_rollout` loads inventory from DB, calls `import_from_inventory`, submits to orchestrator, stores `job_id` in Flask session. SSE reads from `job.logger.queue`.

**Tests: 83/83 passing.** All previously disabled test classes updated to new API and passing.

### 2.9 Inventory management UI — pending
- Account page: inventory panel (add/edit/remove devices), security profiles panel
- Orchestrator DB writes (RolloutSession on submit, DeviceResult + delete session on cleanup)
- Import error surfacing via flash messages (no SSE at import time)

---

## Phase 3 — Testing

- Auth route tests (Flask test client)
- Tests for `RolloutJob`, `RolloutLogger`, refactored OOP classes
- Update existing tests to target new class instances instead of module-level globals

---

## Phase 4 — Packaging & Deployment

### 4.1 Docker image
Build and publish image to Docker Hub as `itamar14/netrollout:latest` and `itamar14/netrollout:v1.0`.
`docker-compose.yml` with three services:
- `app` — Waitress serving the Flask webapp, pulls from Docker Hub
- `db` — PostgreSQL
- `nginx` — reverse proxy, handles TLS termination

### 4.2 Install script — `install.py`
Single script, zero manual steps. User runs `python install.py` and gets a fully running stack.
Interactively asks for config values, falls back to defaults:
- `DATABASE_URL` — default: `postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`
- `MAX_CONCURRENT_JOBS` — default: `4`
- `NETROLLOUT_ENCRYPTION_KEY` — default: auto-generate, write to `~/.netrollout/encryption.key`
- `SECRET_KEY` — default: auto-generate via `secrets.token_urlsafe(32)`

Then automatically:
1. Writes `.env` with resolved values
2. Runs `docker compose pull` — pulls latest image from Docker Hub
3. Runs `docker compose up -d` — starts app + db + nginx
4. Initializes DB tables
5. Seeds factory admin user (admin/admin)
6. Prints `NetRollout is running at http://localhost:8080`

Re-running the script later pulls the latest image and restarts — doubles as the update mechanism.

### 4.3 Update mechanism
Three options (all documented in README, admin chooses based on comfort level):

**Option A — Re-run install script:**
```
python install.py
```
Pulls `:latest` from Docker Hub, restarts stack, re-applies config. Simplest, no extra tooling.

**Option B — In-container update script (`update.py`):**
```
docker exec netrollout-app python update.py
```
Hits Docker Hub API to compare current vs latest version, prompts user, pulls and restarts if confirmed.
Keeps webapp unprivileged — no Docker socket exposure.

**Option C — In-app update button (Admin Panel):**
Version check widget in admin panel hits Docker Hub API (`hub.docker.com/v2/repositories/itamar14/netrollout/tags/latest`).
Displays current vs latest version. On user confirmation, triggers pull and restart.
Most user-friendly but requires Docker socket mounted into container (`/var/run/docker.sock`) — root-equivalent host privilege. Must be documented clearly in security posture section.
Decision deferred to Phase 4.

### 4.4 Release
Tag v1.0 on GitHub, push `v1.0` and `latest` tags to Docker Hub.

### 4.5 Executable (CLI)
Build a standalone `.exe` using PyInstaller for the CLI tool.
Bundles `cli.py` — no Python install required on client machines.

### 4.6 Documentation
- `README.md` — project overview, quick start (install.py), CLI usage, CSV format reference, security posture section, update instructions
- Inline docs review — docstrings consistent across all public APIs
- Security posture section: data minimization rationale, encryption key management, Docker socket decision

---

## Product positioning
NetRollout is **push-based config distribution** with a human-friendly web interface.
This is the opposite of BackBox (pull-based config backup). Closer to Ansible Tower/AWX but purpose-built for network engineers who don't want to write YAML playbooks.
Tagline: **"Ansible for network engineers who don't want to write Ansible."**

## Order rationale
Phase 1 complete. Architecture session gates Phase 2 — no structural code without a design.
Phase 2 and 3 are coupled — tests follow code changes.
Phase 4 last — packaging assumes a stable, complete codebase.
