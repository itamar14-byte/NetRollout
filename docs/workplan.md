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
_Started: 2026-04-07 — In progress as of 2026-04-08_

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

### 2.3 `RolloutLogger` class — `logging_utils.py` (class complete, cleanup pending)
Refactor module-level globals (`LOG_QUEUE`, `LOGFILE`, `BASEDIR`) into a `RolloutLogger` class.
Owns `queue` and `logfile`. Methods: `log()`, `notify()`, `get()`.

**Done:** Class written, tests rewritten (TestMsg/TestLog/TestBaseNotify use RolloutLogger), engine/device test classes disabled (_DISABLED suffix, TODO Step 2.5).

**Closing 2.3:** Happens naturally as part of 2.5 — logger injection into engine/parser/validator removes all `base_notify` imports automatically. No separate cleanup step needed.

### 2.4 `RolloutJob` class — `core.py` or new `job.py`
New class owning job lifecycle: `id`, `thread`, `cancel_event`, `engine`, `logger`.
Methods: `start()`, `cancel()`.
`start()` launches thread and runs `engine.run(self.cancel_event, self.logger)`.
`cancel()` sets `cancel_event` only — DB writes handled by orchestrator.

### 2.4a `RolloutOrchestrator` class — new `orchestrator.py`
Singleton instantiated at app startup. Owns `jobs: dict[UUID, RolloutJob]` and `max_concurrent: int`.
Public: `submit(job)`, `cancel(job_id)`, `get(job_id)`.
Private: `_dispatch()` — fills available slots up to `max_concurrent` by calling `job.start()`.
Private: `_cleanup(job_id)` — called on job completion, deletes `RolloutSession`, writes `DeviceResult` rows, calls `_dispatch()`.
Webapp routes become thin delegators — no job logic in route handlers.

### 2.4b Install script — `db_install.py`
Extend to interactively ask for config values at setup time, write to `.env`:
- `NETROLLOUT_ENCRYPTION_KEY` — default: auto-generate, write to `~/.netrollout/encryption.key`
- `MAX_CONCURRENT_JOBS` — default: `4`
- `DATABASE_URL` — default: `postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`
- `SECRET_KEY` — default: auto-generate via `secrets.token_urlsafe(32)`

### 2.5 `RolloutEngine` refactor — `core.py`
- Remove `cancel_event` from constructor — passed as argument to `run()`, `_push_config()`, `_verify()`
- Replace `notify()` with injected `RolloutLogger` passed at call time
- Rename `push_config()` → `_push_config()`, `verify()` → `_verify()`
- Strip `base_notify` from `validation.py` and inject logger into `parser.py` — closes 2.3
- **After 2.5 complete:** review and update `CLAUDE.md` to reflect new class structure

### 2.6 `Device` updates — `core.py`
- Add `label` field
- Rename `netmiko_connector()` → `_netmiko_connector()`
- Add `from_inventory(cls, row: Inventory) -> Device` factory
- `fetch_config()` receives `logger: RolloutLogger` as argument instead of calling `base_notify()` directly

### 2.7 `Validator` class — `validation.py` ✅ (implemented before 2.3–2.6 due to no dependencies)
Wrap existing standalone functions as `@staticmethod` methods on a `Validator` class.

### 2.8 `InputParser` class — new `parser.py` ✅ (implemented before 2.3–2.6 due to no dependencies)
Inventory is the single rollout path. CSV and form are import mechanisms that populate `Inventory` table.
Constructor takes `Validator` instance.
- `csv_to_inventory(device_path, user_id, db_session)` — validates CSV, writes to `Inventory` table
- `form_to_inventory(devices_json, user_id, db_session)` — validates form/JSON, writes to `Inventory` table
- `import_from_inventory(inventory, commands)` — single rollout path, produces `(list[Device], list[str])`
- `_prepare_devices(raw_devices)` — private, shared by import methods
- `parse_commands(commands_path)` — reads command file, returns list of strings
`parse_files()` and `prepare_devices()` in `core.py` removed. `webapp_input()` in `webapp.py` removed.

**Key insight (2026-04-08):** `Inventory` is the decoupling boundary between parsing and rollout. Import (CSV/form) and rollout are independent operations — import populates the table once, rollout reads from it any number of times. This also enables per-job `DeviceResult` history against a stable inventory row.

### 2.9 Inventory management UI
- Account page gains inventory panel — add/edit/remove devices
- Account page gains security profiles panel — add/edit/remove profiles
- Upload page updated to support launching rollout from inventory (calls `InputParser.from_inventory()`)

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
