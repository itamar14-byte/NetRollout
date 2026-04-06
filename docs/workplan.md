# Development Workplan
_Last updated: 2026-04-06 — Phase 1 complete_

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

## Architecture Session (next — before any Phase 2 code)

Before writing Phase 2 code, design the full extended OOP architecture.
No code before the session.

Topics to cover:
- `RolloutJob` interface and lifecycle — replaces module-level `cancel_event` + `LOG_QUEUE` singletons (documented in bug_report.md). Each job owns its own event and queue, stored in webapp dict keyed by job ID.
- `RolloutLogger` class design and injection points — one instance per `RolloutJob`, injected into `RolloutEngine`
- `RolloutSession` and `DeviceResult` DB schema — one session per rollout run, one result row per device
- `Inventory` table design — per-user device store, foreign key to `User`, loaded as `user.inventory` via SQLAlchemy relationship. This is a "query as a field" — one flat table indexed by user UUID, each user sees only their rows.
- Encrypted credential storage decision — tradeoff: user convenience vs attack surface + key management complexity. To be decided in session.
- `InputParser` and `Validator` class interfaces
- Private method boundaries across all classes
- Concurrency model — `RolloutJob` per rollout, webapp stores dict of active jobs

---

## Phase 2 — Architecture Refactor & DB Integration

### 2.1 DB schema
Add to `tables.py`:
- `RolloutSession` — one row per rollout run (timestamp, status, initiated_by FK to User)
- `DeviceResult` — one row per device per session (ip, device_type, commands_sent, commands_verified, status, FK to RolloutSession)
- `Inventory` — per-user device store (FK to User), encrypted credentials (design TBD in architecture session)

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
- `README.md` — project overview, quick start, CLI usage, CSV format reference, security posture section (data minimization rationale)
- Inline docs review — docstrings consistent across all public APIs
- Deployment guide — Docker setup, environment variables, first-run DB init

---

## Product positioning
NetRollout is **push-based config distribution** with a human-friendly web interface.
This is the opposite of BackBox (pull-based config backup). Closer to Ansible Tower/AWX but purpose-built for network engineers who don't want to write YAML playbooks.
Tagline: **"Ansible for network engineers who don't want to write Ansible."**

## Order rationale
Phase 1 complete. Architecture session gates Phase 2 — no structural code without a design.
Phase 2 and 3 are coupled — tests follow code changes.
Phase 4 last — packaging assumes a stable, complete codebase.
