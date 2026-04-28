# Development Workplan
_Last updated: 2026-04-28 — 4.9b LDAP integration complete; Live Sessions page complete_

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

## Phase 2 — Architecture Refactor & DB Integration ✅ COMPLETE
_Started: 2026-04-07 — Completed: 2026-04-11_

### 2.1 DB schema — `tables.py` ✅
Add new ORM models:
- `Inventory` — per-user device topology store, FK to `User` and `SecurityProfile`
- `SecurityProfile` — encrypted credentials (Fernet), FK to `User`, loaded as `user.security_profiles`
- `VariableMapping` — `$$TOKEN$$` (free text) → `property_name` + optional `index` (nullable int), FK to `User`, loaded as `user.variable_mappings`. `index=None` = simple string attribute; `index=N` = positional element of a list attribute (e.g. `vrfs[1]`). Validator checks list length at rollout time.
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

### 2.9 Inventory management UI ✅

**Done (2026-04-10):**
- Operator zone restructure: `operator_base.html` with collapsible sidebar, dashboard, account, inventory stub, results stub
- `DeviceResultDict` TypedDict in `core.py` — typed return from `run()`, consumed by `_cleanup()`
- Orchestrator DB writes: `RolloutSession` written on `submit()`, promoted to "active" in `_dispatch()`, `DeviceResult` rows written + session deleted in `_cleanup()`
- `tables.py` fully fixed: ForeignKeys, `back_populates` pairs, `commands_verified: Mapped[int | None]`, `Inventory.security_profile` singular
- Dashboard route: groupby logic, active job detection, last 5 jobs table, system summary stats
- Account route + page: total rollouts, devices configured, commands pushed, success rate (color-coded), top platform, 2FA status, live tenure counter

**Done (2026-04-11):**
- Security Profiles UI: full CRUD (`/security`, `/security/create`, `/security/<id>/edit`, `/security/<id>/delete`, `/security/<id>/test`)
- Card grid layout — label or username fallback, 10-dot masked password/enable secret, attached devices modal, test connection modal
- Test connection: full Netmiko connect via `Device` + `netmiko_connector()`, TCP check first via `Validator.test_tcp_port()`, AJAX with spinner, inline pass/fail result
- Delete blocked with flash if profile has assigned inventory devices (FK safety)
- Eager load of `profile.inventory` inside session before `expunge_all()` — prevents DetachedInstanceError
- AGPL v3 license added to repo; footer license notice in `operator_base.html`
- Inventory UI frontend: thin card grid, vendor badge (Simple Icons CDN via `VENDOR_LOGOS` Jinja2 global), FortiGate hover tooltip, NrSelect custom dropdown, edit modal with variable attributes expand section (hostname, loopback_ip, asn, mgmt_vrf, mgmt_interface, site, domain, timezone, vrfs)
- TCP Test Connection button on both Add and Edit device modals — same three-state flow: grey Test → green Confirm (submit) / red Save Anyway (submit); status pill on left; resets on IP/port change and modal close
- Security profiles drag-assign: split-view modal, draggable device cards, dashed drop zone, cardLand animation, AJAX to `/inventory/bulk_assign`
- `Inventory.var_maps` JSON column, `Device.extra` dict field, `VariableMapping.index` nullable int
- Inventory backend: `create`, `edit`, `delete`, `bulk_assign` all implemented and ownership-guarded
- `edit` rebuilds `var_maps` from `attr_*` form fields; vrfs split to list; empty keys omitted
- Form validation: `novalidate` + `.field-error` + shake animation on all forms site-wide; CSS `:has()` rule handles password inputs inside `.pw-group` wrappers; validation CSS/JS added to `base.html` so login page is also covered
- `nr-submitted` class stamps invalid fields on submit so empty required fields also show red border + error text (independent of the "bad input while typing" path via `:placeholder-shown`)
- `nr-touched` class on selects so empty state doesn't alert on page load — only on submit
- Validation state fully cleared on modal close (`nr-submitted` stripped, `.field-error` inline styles reset)
- Platform (device type) selector replaced with NrSelect widget showing vendor logos in both add and edit modals
- Test connection device dropdown replaced with NrSelect widget showing vendor logo + IP
- NrSelect CSS moved to `operator_base.html` — available site-wide
- Double flash bug fixed in `inventory.html` (removed duplicate `get_flashed_messages`)
- OTP shake was silently broken (double `get_flashed_messages` drained queue) — fixed with `{% set flash_messages %}`
- Variable attributes expand toggle color matches `nr-label` (`#777`, weight 500)
- Tooltip label keys (IP/TYPE/PORT/PROFILE) color matches `nr-label`

---

## Phase 3 — Functionality, Logic & Testing

### 3.1 Variable mapping builder ✅ COMPLETE (2026-04-11)
- `variable_mappings.html` — card grid, add/edit/delete modals, split-view drag-assign
- `$$`...`$$` token input group, NrSelect attribute picker, index field for vrfs only
- Drag cards show resolved attribute value per device
- `var_mapping_to_devices` join table, many-to-many relationships, cascade delete
- `UniqueConstraint('token', 'user_id')` on `VariableMapping`
- `Validator` extended with 3 static methods returning `(bool, str|None)`
- Routes: GET/POST create/edit/delete/bulk_assign — ownership + eligibility guards
- UUID converter on all ID routes app-wide
- DB synced: new columns, constraints, join table

### 3.1b CSV import to inventory ✅ COMPLETE (2026-04-11)
- "Import CSV" button on inventory page opens a modal (file input + optional label)
- `POST /inventory/import_csv` — saves upload to temp file, delegates to
  `InputParser.csv_to_inventory`, drains logger queue for per-device errors, flashes result
- Both temp files (CSV + log) cleaned up in `finally` block
- NOTE: TCP checks are sequential — Phase 3.6 concurrency will fix large-CSV blocking
- Phase 3 TODO: proper activity logging with operation-prefixed filenames

### 3.2 Rollout initiation from web UI ✅ COMPLETE (2026-04-12)

- `new_rollout.html` — device selection table with checkboxes, vendor logos, platform tags, green/red profile dots, disabled rows for devices with no profile
- Multi-platform detection: amber warning banner when multiple platforms selected; UI rebuilds one command block per platform with vendor logo in header
- Per-platform command blocks: paste/file toggle, text preserved on rebuild
- Verify `?` tooltip (best-effort text match warning), verbose toggle, optional rollout note (audit comment)
- Single-platform submits natively; multi-platform JS packages `platform_commands` JSON hidden field
- `new_start_rollout` route: multi-platform detection via `platform_commands` field, groupby per device_type, one `orchestrator.submit()` per group, redirects to `active_jobs?new=<job_id>`
- `active_jobs.html` — stats bar (Running/Queued/Devices in flight/live clock), job table with pulsing status dot, elapsed timer, 3 action buttons per row
- Log button: toggles inline SSE terminal, replays `RolloutLogger._buffer` (history) then tails live queue
- Cancel button: POST to `cancel_rollout`, updates `RolloutSession.status` to "cancelling"
- Rollback button: modal with compensatory commands textarea, verify/verbose toggles with `?` tooltip, device attributes warning note. On confirm, `fetch('/rollback/<job_id>')`, redirects to `active_jobs?new=<job_id>` with same glow animation
- `job-new` CSS glow animation on new job row; auto-refresh strips `?new=` so glow fires once only
- `RolloutLogger` dual-write: `_queue` for live SSE delivery, `_buffer` for full history replay
- `important=True` flag on key engine messages (rollout start, verify start, per-device summary, completion)
- `JobMetadata` table: soft `job_id` ref, JSON `commands` (pre-substitution), nullable `comment`, `user_id` FK — written in same DB session as `RolloutSession` on submit
- pg_cron installed on PostgreSQL 17-bookworm container; `cron.database_name = 'rollout_db'` set via `ALTER SYSTEM`
- Two cron jobs: `job_metadata_retention` (7 days) + `device_result_retention` (30 days), idempotent via DO block unschedule-then-schedule pattern
- Old routes (`/start_rollout`, `/upload`, old `sse_stream`) retained but superseded — retirement deferred to Phase 4

**Pending / loose threads:**
- Rollback jobs have no audit comment in `job_metadata`
- pg_cron installation is manual (apt-get exec) — not baked into Docker image yet (Phase 4)
- Old routes (`/start_rollout`, `/upload`, old `sse_stream`) retired and deleted this session

### 3.3 Results page ✅ COMPLETE (2026-04-13)
- Job history grouped by `job_id`, sorted by `completed_at` desc
- Filter bar: All / Success / Partial / Failed / Cancelled
- Expandable rows: device sub-table (IP, platform logo, sent/verified, status pill)
- Per-job action buttons: **See Commands** (modal with full pre-substitution command list), **Download Log** (only shown if log file exists)
- **Diff feature**: Compare button enters selection mode, checkboxes on rows, second selection dims others; modal shows side-by-side LCS diff — red `−` removed, green `+` added, yellow `~` changed; header labels include job ID, timestamp, comment
- Duration calculated from `started_at` / `completed_at`, comment shown as cyan italic tag
- Empty state for users with no completed jobs

### 3.3b UI polish ✅ COMPLETE (2026-04-13)
- Dark custom checkboxes (`appearance:none`, cyan fill on check) + indeterminate state
- Verify/verbose options replaced with sliding toggle switches
- `🙈` monkey easter egg replaces `bi-eye-slash` on password reveal toggle (security.html, register.html, index.html)
- Disabled device rows in rollout: styled tooltip on hover + footer warning with link to Inventory
- Select-all excludes disabled (no-profile) devices
- Paste/file toggle bug fixed (stale `.remove()` call was deleting DOM elements)
- Sidebar: Variable Mappings moved above Launch Rollout; Admin → Admin Panel; Active Jobs tab added
- All Launch Rollout links updated to `new_rollout` route
- Promote pending user implicitly approves + activates
- Variable mapping chips in inventory tooltip (token/property stacked, cyan + muted)
- Mapping multi-select in device edit modal (searchable checkboxes, pre-populated, updates many-to-many)
- Dashboard system summary includes variable mapping count
- Double flash fixed in `variable_mappings.html`

### 3.4 Audit trail ✅ COMPLETE (2026-04-13)

**Audit log table:**
- `AuditLog` ORM model: `id`, `timestamp` (indexed), `actor_id` (FK → users, ON DELETE SET NULL), `actor_username` (denormalized — survives user deletion), `action` (dot-namespaced e.g. `inventory.delete`), `object_type`, `object_id` (soft ref), `object_label` (denormalized), `success`, `ip_address`
- `audit()` helper in `webapp.py` — opens own session, commits independently of calling route's transaction
- 21 routes instrumented: auth (login with failure reasons, register, logout), user management (all single + bulk actions), inventory CRUD + import + bulk_assign, security profile CRUD, variable mapping CRUD + bulk_assign, rollout start/cancel/rollback
- Login failures record reason: `invalid_credentials`, `account_disabled`, `pending_approval`
- pg_cron `audit_log_retention` job: 90-day retention, daily at 3AM

**Admin UI (`/admin/audit`):**
- Filterable table: actor username (contains search), action (dropdown of distinct values), success/fail toggle
- Sticky floating header (FortiGate-style, `position: sticky` within scroll container)
- Per-row FortiGate gear (⚙): View Detail (modal with pretty-printed JSON + metadata), Copy Row (clipboard), Filter by Actor
- 500-row cap per query

**Log file infrastructure:**
- `LOGS_DIR` defined in `logging_utils.py` as `src/../logs/` (project root)
- `RolloutLogger.__init__` takes `job_id` + `timestamp`, constructs path `rollout_{timestamp}_{job_id}.log`, calls `os.makedirs(LOGS_DIR, exist_ok=True)` — all filesystem setup in one place
- Naming: timestamp = submission time (matches `job_metadata.created_at`), job_id makes glob lookup deterministic from results page
- `started_at` in results page gives actual execution time — intentional drift from filename timestamp shows queue wait time
- `/results/download_log/<job_id>` — ownership-verified via `DeviceResult`, globs `rollout_*_{job_id}.log`, serves with `send_file`
- Infrastructure is generic — any future activity can get a named logfile by instantiating `RolloutLogger` with an id + timestamp

### 3.4b Analytics ✅ COMPLETE (2026-04-15)

Two separate surfaces, different scopes. Data sourced entirely from `DeviceResult` and `AuditLog` — no new tables.

**Operator dashboard KPI strip** (`dashboard.html` / `dashboard()` route):
- 4 cards: Success Rate (color-coded green/yellow/red), Devices Reached, Commands Pushed, Top Failing Device (red accent, label+IP+count)
- Always scoped to `current_user` for operators
- Admin-only scope selector above the strip — `?user=<uuid>` loads KPI data for any operator while dashboard content (active job, recent jobs, system summary) stays as current user's own view

**Operator analytics page** (`analytics.html` / `/analytics` + `/analytics/query`):
- Badge: `ROLLOUT INTELLIGENCE` · icon: `bi-bar-chart-line-fill` · accent: cyan
- 5-card CSS grid KPI strip: Success Rate, Devices Reached, Commands Pushed, Top Failing Device, Top Platforms (ranked top 3)
- Admin-only scope selector in page header; operators see only their own data
- Device Results query engine (`bi-database-check`, cyan): jQuery QueryBuilder compound filter (AND/OR, field/operator/value trees), AJAX POST `{rules}` → `/analytics/query`, dynamic table, CSV export
- QueryBuilder fields: `started_at` (date), `device_type` (select), `status` (select), `commands_sent` (integer), `device_ip` (string)
- `/analytics/query` always scoped to `current_user.id` (operators) or `?user=` param (admin); audit log never exposed here
- Analytics link added to operator sidebar under Observability section

**Admin analytics page** (`admin_analytics.html` / `/admin/analytics` + `/admin/analytics/query`):
- Extends `admin.html` properly (was duplicating sidebar inline — fixed)
- 4 org-level KPI cards (always all-users, no scope selector): Active Users (X of Y registered), Org Success Rate, Total Jobs, Total Device Operations
- Most Active Users table (top 10 by job count, 30d)
- Most Failed Devices table (top 10 by failure count, 30d, best-effort label from Inventory)
- Audit Investigation query engine (`bi-shield-lock-fill`, amber `#ffe082`): jQuery QueryBuilder compound filter, badge `AUDIT INVESTIGATION`, AJAX POST `{rules}` → `/admin/analytics/query`, dynamic table, CSV export
- QueryBuilder fields: `timestamp` (date), `actor_username` (select from all users), `action` (string), `object_type` (select), `success` (boolean select), `ip_address` (string)
- `/admin/analytics/query` hits `AuditLog` only — device results query never exposed here
- Admin sidebar icons modernised: emojis → `bi-people-fill`, `bi-shield-check`, `bi-bar-chart-line`
- Muted text corrected from near-invisible `#333` → `#555` throughout

**Card identity split principle:** cards that answer org-level questions (active users, most active, most failed org-wide) live in admin only. Cards that answer user-level questions (success rate, devices reached, commands pushed, top failing, top platforms) live in operator surfaces — with admin scope selector available to admins on those surfaces.

**Deferred to Grafana/Prometheus:** time-series charts, per-platform breakdown over time — better served as live dashboard panels with PostgreSQL datasource than hardcoded Chart.js.

### 3.4c Activity Logging ✅ COMPLETE (2026-04-25)
Extended `RolloutLogger` to cover sequential administrative workflows with full log files.

- `RolloutLogger` constructor refactored: `timestamp` removed (calculated internally), `prefix` parameter added (default `"rollout"`), `os.makedirs` hoisted above branch — all log files now land in `LOGS_DIR` including CLI runs
- Three workflows instrumented:
  - `csv_import` — per-device success/failure + summary with `important=True`; notifies already existed in `input_parser.py`, uncommented and wired to new logger instance
  - `bulk_sec_assign` — start, per-device assigned/not-found, summary; failure paths covered
  - `bulk_map_assign` — start, per-device assigned/ineligible/already-assigned/not-found, summary; most granular — logs reason for each skip
- `RolloutJob` updated to pass `prefix="rollout"` explicitly
- Security profile test connection excluded — atomic single-device action, AJAX response is sufficient

### 3.5 Test suite ✅ PARTIAL — core layer complete (2026-04-13)

**Done:**
- All disabled test classes re-enabled and adapted to current architecture
- `TestLog` / `TestBaseNotify` fixed (`logfile=` param removed from constructor)
- `TestDeviceFetchConfig` — `fetch_config(logger)` API
- `TestRolloutEnginePushConfig` — `_push_config` returns `(cancel_signal, push_results)` tuple
- `TestRolloutEngineVerify` — `_verify(logger)` only, cancel removed (uses ThreadPoolExecutor internally)
- `TestRolloutEngineRun` — `run()` returns `list[DeviceResultDict]` not int
- `TestFullRolloutAndVerifyPipeline` — full mock pipeline, all 4 scenarios
- `_server_reachable` bug fixed — was returning True on ConnectionRefusedError
- **82 passing, 1 skipped** (rate limit integration — requires live server)

**Next session — webapp backend + CLI tests:**
- Flask test client: auth routes (login, register, OTP flow), inventory CRUD, security profile CRUD, variable mapping CRUD, rollout submission, SSE stream, audit log
- CLI unit tests: argument parsing, file input, headless rollout flow
- Mocked DB (SQLite in-memory or mock session) for route tests

**EVE-NG live testing (between Phase 3 and Phase 4):**
- EVE-NG deployed on GCP with WireGuard VPN to dev machine (2026-04-13) — cannot run locally (conflicts with Docker/VMware Workstation virtualization)
- First rollout test complete (2026-04-14): Cisco IOL IOS, hostname push + verify, ReadTimeout edge case hit and fixed
- Next: multi-device test, FortiOS node, verify pass/partial/fail paths, rollback flow

### 3.6 Per-job device concurrency ✅ COMPLETE (2026-04-13)

**Two-layer concurrency model:**
- Layer 1 — job-level: `RolloutOrchestrator` runs up to `max_concurrent=4` jobs simultaneously, each in its own `threading.Thread`
- Layer 2 — device-level: `RolloutEngine` uses `ThreadPoolExecutor(max_workers=10)` per job — up to 10 simultaneous SSH sessions per job, up to 40 total across 4 concurrent jobs

**Engine changes (`core.py`):**
- `max_workers: int = 10` added to `RolloutOptions`
- `_push_device(device, cancel_event, logger) -> tuple[str, bool | None]` extracted from `_push_config` (all original comments and docstrings preserved)
- `_verify_device(device, logger) -> tuple[str, int]` extracted from `_verify`
- Both `_push_config` and `_verify` rewritten to use `ThreadPoolExecutor` + `as_completed`

**Thread safety (`logging_utils.py`):**
- `_buffer` made private; only accessible via `get_buffer_snapshot()` which acquires `_buffer_lock` and returns a copy — prevents `RuntimeError: list changed size during iteration` on SSE replay
- `_buffer_lock: threading.Lock` — guards `_buffer.append()` in `notify()` and the copy in `get_buffer_snapshot()`
- `_log_lock: threading.Lock` — serializes file writes in `_log()` across concurrent worker threads
- `queue.Queue` is inherently thread-safe — no change needed
- `orchestration.py`: `get_log_history()` updated to call `get_buffer_snapshot()` instead of accessing `_buffer` directly

### 3.6b Admin all-users view ✅ COMPLETE (2026-04-13)

**Active Jobs + Results pages — admin toggle:**
- "All Users" button in page header (admin-only, hidden from operators)
- Toggles between flat view (default, own jobs only) and split view (two collapsible sections: My Jobs / Other Users)
- Each section has its own column headers and filter bar
- Other Users section shows owner badge (username) on each job row
- Both sections use the same expand/collapse, See Commands, Download Log, and Diff features

**Admin power over all jobs:**
- `cancel_rollout` — ownership check bypassed for admin
- `rollout_stream` — SSE stream accessible by admin for any job
- `download_log` — ownership check bypassed for admin

**Backend:**
- `active_jobs` route: if admin, queries all `RolloutSession` rows + username map; splits into `my_jobs` / `other_jobs`
- `results` route: if admin, queries all `DeviceResult` + `JobMetadata`; groups other users' rows by `user_id` to attach owner username; passes `other_jobs` with `owner` field
- Non-admin path unchanged — `other_jobs=[]`, `is_admin=False`

### 3.7 User-managed property definitions ✅ COMPLETE (2026-04-15)

- `PropertyDefinition` table: `id`, `name` (snake_case, unique per user), `label`, `icon` (Bootstrap Icons class), `is_list`, `user_id` FK
- System defaults (9 built-ins) hardcoded as `SYSTEM_PROPERTIES` constant — read-only, no DB seeding needed
- `get_property_defs(user_id)` returns `(sys_props, user_props)` tuple
- Routes: `GET /properties`, `POST /properties/create`, `POST /properties/quick_create`, `POST /properties/<id>/edit`, `POST /properties/<id>/delete` — all audited; shadowing system names blocked
- `properties.html`: system/custom visual separation; edit/delete on custom only
- `inventory.html`: var attrs section is a CSS grid loop over all props with system/custom separator; JS population uses global `PROP_DEFS`; quick-create property inline modal with icon picker
- `variable_mappings.html`: `ATTR_DEFS` set replaced with server-injected `sys_props`/`user_props`; `LIST_PROPS` JS set replaces hardcoded `vrfs` checks; "New property…" option at bottom of both pickers
- `operator_base.html`: shared `initIconPicker` (searchable 150-icon grid, click or type) + `autoSlug` (label → snake_case name auto-generation) — available site-wide
- Label-first UX: user types label, name auto-generates; name field editable but secondary

---

## Phase 4 — Packaging & Deployment

---
## Remaining work — path to v1.0
_Feature set is complete as of 2026-04-28. Remaining work is cleanup, packaging, and documentation._

### Step 1 — 4.9c Codebase cleanup ← NEXT
Do this before freezing into a Docker image. Code quality is easier to fix before packaging than after.

**Response shape standardization:**
- Unify all route responses to `{"status": "ok"/"error", "message": "..."}`. Currently the Postgres server management routes (`/admin/server/postgres/*`) return `{"success": True/False, "error": "..."}` — inconsistent with every other route. Update those routes and their frontend JS consumers.

**DRY — LDAP route boilerplate:**
- Every LDAP route opens a DB session, queries `LDAPServer` by id, and 404s if missing. Extract to a small helper so the pattern isn't repeated across 10 routes.

**`webapp.py` size:**
- At ~2850 lines it's readable but long. Blueprint split (step 2) is the real fix, but a quick pass to remove any dead code, leftover comments, or stale imports before the split reduces noise.

---

### Step 2 — 4.0 Flask Blueprints + frontend asset splitting
Split `webapp.py` into logical Blueprint modules. Do after cleanup so the split starts from clean code.

**Blueprint modules:**
- `auth.py` — login, register, OTP enroll/verify, logout
- `inventory.py` — inventory CRUD, bulk assign, CSV import
- `security.py` — security profile CRUD, test connection
- `mappings.py` — variable mapping CRUD, bulk assign
- `rollout.py` — new_rollout, new_start_rollout, active_jobs, rollout stream, cancel, rollback
- `admin.py` — admin panel, user actions, bulk actions, audit, sessions, server management
- `webapp.py` — thin entry point: app factory, blueprint registration, Waitress serve
- `extensions.py` — shared singletons: `orchestrator`, `csrf`, `login_mng`, `redis_client`, `VENDOR_LOGOS`

All `url_for` calls need blueprint prefix (e.g. `url_for('rollout.active_jobs')`). This is the main mechanical cost of the split.

**Frontend asset splitting:**
Extract per-page inline `<style>` and `<script>` blocks into `static/css/<page>.css` and `static/js/<page>.js`. Templates become thin layout files. Makes JS/CSS independently cacheable and reviewable. Do alongside or after Blueprints.

---

### Step 3 — 4.0b BYO Infrastructure ✅ PARTIAL (2026-04-28)
Allow users to connect their own Postgres and Redis instead of the bundled Docker services.

**PostgreSQL BYO ✅ COMPLETE** — server management UI card, `POST /admin/server/postgres/test` + `/save`, writes individual `DB_*` vars to `config.env`, merge-safe (does not overwrite Redis config).

**Redis BYO ✅ COMPLETE (2026-04-28)** — server management UI "Database Services" card with two large service selector buttons (Postgres / Redis). Redis form: host, port, db number, optional password. `POST /admin/server/redis/test` (ping) + `/save` (writes `REDIS_*` vars to `config.env`, merge-safe). `redis_db.py` reads individual vars as fallback when `REDIS_URL` not set. Warning: switching Redis drops all active sessions and orphans running jobs — documented in UI.

**Grafana BYO — deferred post-v1.0.** Grafana is observability-only; users can point it at any datasource manually. Not blocking release.

---

### Step 4 — 4.1 Docker image
Build and publish to Docker Hub as `itamar14/netrollout:latest` and `itamar14/netrollout:v1.0`.

`docker-compose.yml` services:
- `app` — Waitress + Flask, pulls from Docker Hub
- `db` — PostgreSQL
- `redis` — Redis
- `nginx` — reverse proxy, TLS termination

Grafana/Prometheus/Loki remain on a separate `docker-compose.obs.yml` — optional observability stack, not bundled in the base image.

**Pending Grafana wiring (carry over from 4.9):**
- docker-compose volume mounts for `docs/grafana/provisioning/` and `docs/grafana/dashboard_config/`
- `GRAFANA_DB_PASSWORD` env var in docker-compose

---

### Step 5 — 4.2 Install script — `install.py`
Single script, zero manual steps. `python install.py` → fully running stack.

Interactively asks (with defaults):
- `SECRET_KEY` — auto-generate via `secrets.token_urlsafe(32)`
- `NETROLLOUT_ENCRYPTION_KEY` — auto-generate, write to `~/.netrollout/encryption.key`
- `MAX_CONCURRENT_JOBS` — default `4`
- DB credentials — default bundled Postgres

Then:
1. Writes `config.env`
2. `docker compose pull`
3. `docker compose up -d`
4. `alembic upgrade head`
5. Seeds factory admin (`admin`/`admin`)
6. Prints `NetRollout is running at https://localhost`

Re-running pulls `:latest` and restarts — doubles as update mechanism.

---

### Step 6 — 4.10 Documentation
- `README.md` — project overview, quick start (`python install.py`), CLI usage, CSV format reference, security posture (data minimization, encryption key management), update instructions
- Inline docstrings pass on public APIs in `ldap_auth.py`, `orchestration.py`, `core.py`

---

### Step 7 — 4.4 Release
Tag v1.0 on GitHub, push `v1.0` and `latest` to Docker Hub.

---

### Post-v1.0 (deferred)
- **4.3 Update mechanism** — in-app version check widget hitting Docker Hub API
- **4.5 CLI `.exe`** — PyInstaller standalone for `cli.py`
- **3.5 Test suite expansion** — webapp route tests, CLI tests (core layer already at 82/83 passing)
- **4.0b Grafana BYO** — server management card for external Grafana instance

### 4.6 Server-side sessions (Flask-Session) ✅ COMPLETE (2026-04-23)
Flask-Session backed by Redis (`SESSION_TYPE=redis`). On startup, all `session:*` keys flushed from Redis — FortiGate-style invalidation, no SECRET_KEY rotation needed. SECRET_KEY is now a fixed env var (`SECRET_KEY=dev` default), session lifecycle managed by Redis flush instead.

### 4.6b Redis integration (v1.1) ✅ COMPLETE (2026-04-25)
Redis as a fourth Docker service, enabling three features under one infrastructure dependency:

**1. Job queue (distributed orchestration) ✅ COMPLETE (2026-04-25)**
`RolloutSession` Postgres table replaced entirely by Redis. `RolloutOrchestrator` now uses a persistent `_dispatcher` thread blocking on `BLPOP "netrollout:job_queue"`. `submit()` pushes job_id onto the queue via `RPUSH`; `_slots` semaphore (`threading.Semaphore(max_concurrent)`) enforces concurrency limit — acquired before dispatch, released in `_cleanup()`. Job state tracked via Redis hashes (`job:<id>:meta`), user→jobs index via Redis sets (`user_jobs:<user_id>`), live counts via Redis counters (`netrollout:active_count`, `netrollout:pending_count`). `RolloutSessionCollector` reads counters directly. `active_jobs` route and `admin_active_job_count` route both read from Redis. Alembic migration generated and applied to drop `rollout_sessions` table.

**2. Pub/sub log streaming ✅ COMPLETE (2026-04-24)**
Replace the in-process `queue.Queue` + `_buffer` in `RolloutLogger` with a Redis pub/sub channel per job (`job:<job_id>:logs`). Workers publish log lines; SSE endpoint subscribes by job ID — no shared in-process state, no buffer locks. History replay handled via a parallel Redis list (`job:<job_id>:history`) — on SSE connect, `LRANGE` for history then subscribe for live tail.
- `RolloutLogger` rewritten: keys only created when `job_id` + `timestamp` provided; `notify()` guards Redis writes with `if self._channel_key`; `get_history()` via `LRANGE`; `subscribe()` returns `PubSub`; `redis_cleanup()` publishes `__done__` sentinel then deletes both keys
- `RolloutJob` adds `get_log_queue()`, `get_log_history()`, `log_cleanup()` — encapsulates logger access
- SSE route replays history snapshot then tails live pub/sub channel; exits on `__done__` sentinel or dead thread
- CSV import drain loop removed; `prepare_devices()` now returns `(list[Device], list[str])` — errors surfaced to caller directly

**3. Session store + revocation ✅ COMPLETE (2026-04-23)**
Flask-Session backed by Redis. Two keys per logged-in user:
- `session:<sid>` — Flask-Session owns this, actual session data
- `user_session:<user_id>` — our mapping, allows admin to locate and delete a user's session

On login: `user_session:<user_id>` written with `session.sid`. On logout: `session.clear()` triggers Flask-Session to delete `session:<sid>`; we delete `user_session:<user_id>`. On terminate: we delete both manually (no Flask request context for target user). Admin "Terminate Session" button added to User Management toolbar.

- `redis-py`, `Flask-Session==0.8.0` added as dependencies
- `src/db/redis_db.py` — dedicated Redis module (`redis_client` singleton)
- Redis added as a service in `docker-compose.yml`, reachable by Flask and workers on the Docker network

### 4.7 Alembic migrations ✅ COMPLETE (2026-04-17)
DB layer refactored into `src/db/` package. `create_all` replaced with Alembic. Initial migration generated and applied. `db_install.py` calls `alembic upgrade head` programmatically. Migration files ship in the Docker image — fresh installs and schema upgrades both handled via `alembic upgrade head`.

### 4.8 Server Management ✅ COMPLETE (2026-04-17)
External DB configuration UI in admin panel. `db.py` refactored: `construct_url()` builds URL from individual env vars (`DB_HOST/PORT/NAME/USER/PASSWORD/SCHEMA`), `build_engine()` prefers `DATABASE_URL` then falls back to individual vars, `search_path` injected via `connect_args` if `DB_SCHEMA` set. `db_install.py` imports fixed to fully-qualified `db.db`/`db.tables` paths. `webapp.py`: `load_dotenv(config.env)` runs before DB imports; `_DB_HOST/_DB_PORT` read from `engine.url`; `pending_db_init.flag` checked on startup → runs `install()` → deletes flag. Three new routes: `GET /admin/server`, `POST /admin/server/db/test` (live connection test, blocks same-DB target), `POST /admin/server/db/save` (writes `config.env` + flag). `POST /admin/server/restart` uses `os.execv` for hot process restart (picks up new config + code changes). `server_management.html`: DB config card (status strip, migration warning, test/save/restart flow) + locked LDAP stub.

### 4.8b nginx integration ✅ COMPLETE (2026-04-18)
nginx reverse proxy running in Docker, terminating TLS and forwarding to Waitress on port 8080.
- Self-signed cert for local dev (`fullchain.pem` / `privkey.pem` mounted at `/etc/nginx/certs/`)
- HTTP → HTTPS 301 redirect
- `proxy_set_header Host/X-Real-IP/X-Forwarded-For/X-Forwarded-Proto`
- `ProxyFix(x_for=1, x_proto=1, x_host=1)` in Flask — trusts exactly 1 proxy hop
- `SESSION_COOKIE_SECURE=True`, `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE=Lax`
- `ProxyFix(x_for=1, x_proto=1, x_host=1)` + secure cookie config added to `webapp.py`
- Origin check on `@csrf.exempt` login route compares hostnames only via `urlparse` (scheme/port vary under proxy — full URL comparison was rejecting legitimate logins)
- SSL hardening: `TLSv1.2 TLSv1.3` only, `HIGH:!aNULL:!MD5` ciphers
- Security headers: `Strict-Transport-Security`, `X-Frame-Options SAMEORIGIN`, `X-Content-Type-Options nosniff`
- `client_max_body_size 10M` for CSV uploads
- `/rollout_stream` location block: `proxy_buffering off`, `proxy_cache off`, `proxy_http_version 1.1`, `Connection ''` — required for SSE log streaming
- Config lives at `docs/nginx/nginx.conf`, bind-mounted to `/etc/nginx/nginx.conf` in container

### 4.8c Admin panel redesign ✅ COMPLETE (2026-04-18)
Admin panel is now a fully standalone page — own layout, own topbar, own sidebar, not embedded in operator chrome.
- Standalone `admin.html` (does not extend `base.html` or `operator_base.html`)
- Topbar: ← Home button back to dashboard + "ADMINISTRATION" monospace label + user dropdown
- Left sidebar: collapsed icon-only (52px) ↔ expanded with labels (210px), localStorage state
- Sidebar sections with labels: **Access** (User Management), **Observability** (Audit Logs, Analytics), **System** (Server Management)
- Restart button in sidebar footer — same modal + countdown + active-job warning as operator sidebar
- Matching footer style (JetBrains Mono, same copy as operator pages)
- All admin sub-pages (`admin_users`, `admin_audit`, `admin_analytics`, `server_management`) extend `admin.html` unchanged

### 4.9 Grafana analytics ✅ COMPLETE (2026-04-23)

**3-datasource observability stack** running on `netrollout-obs` Docker network:
- **PostgreSQL** (`NetRollout-DB:5432`, read-only `grafana_reader` user) — historical business metrics
- **Prometheus** (`http://prometheus:9090`) — live/ephemeral job state and Flask request metrics
- **Loki** (`http://loki:3100`) — log stream search per job_id

**4 dashboards built and exported to `docs/grafana/dashbaord_config/`:**
- `operations_overview.json` — Active Jobs, Pending Jobs, p99 latency, request rate by endpoint, rollouts per day, job status breakdown pie
- `job_analytics.json` — Total Jobs, Success Rate, Avg Duration, Commands Sent vs Verified, Platform Success Rate bar gauge, Activity Heatmap (hour-of-day × day)
- `job_detail.json` — drill-down by `$job_id` template variable: Job Status stat, Device Results table, Commands table, Loki log stream panel
- `audit_security.json` — Total Events, Failed Actions, Unique Actors, Failure Rate stats, Audit Events Over Time, Failed Actions Over Time, Action Breakdown donut, Top Actors bar gauge, Failed Actions Log table

**Provisioning files written (`docs/grafana/provisioning/`):**
- `datasources/netrollout.yml` — all 3 datasources with fixed UIDs, grafana_reader credentials via `$GRAFANA_DB_PASSWORD` env var
- `dashboards/netrollout.yml` — file provider pointing to `/var/lib/grafana/dashboards`, `allowUiUpdates: true`

**Grafana config (set in container `grafana.ini`):**
- `allow_embedding = true` — enables iframe embed in webapp
- `[auth.anonymous] enabled = true, org_role = Viewer` — no-login access for embedded panels

**Prometheus metrics:**
- `prometheus_flask_exporter` with `group_by='url_rule'` — per-endpoint request metrics
- Custom `RolloutSessionCollector` in `webapp.py` — exposes `netrollout_active_jobs` + `netrollout_pending_jobs` gauges

**Loki + Promtail:**
- Promtail watches `logs/*.log`, extracts `job_id` label from filename pattern `rollout_{ts}_{uuid}.log`
- `reject_old_samples: false` in loki-config.yml — allows ingesting historical log files

**Pending (packaging session):**
- docker-compose volume mounts for `docs/grafana/provisioning/` and `docs/grafana/dashbaord_config/`
- `GRAFANA_DB_PASSWORD` env var wired in docker-compose
- Optional webapp iframe embed in `/admin/analytics`

### 4.9b LDAP Integration ✅ COMPLETE (2026-04-28)

**Goal:** Allow admins to configure an org-level LDAP/LDAPS server and import users or groups. Imported remote users authenticate directly with their LDAP credentials — no local password needed.

---

**New DB table — `LDAPServer`:**
- `id` (UUID PK), `name` (display name), `host`, `port` (int, default 389/636), `base_dn`, `cn_identifier` (e.g. `SAMAccountName`, `cn`, `uid`), `bind_type` (enum: `anonymous`, `simple`, `regular`), `bind_dn` (nullable), `bind_password` (Fernet-encrypted, nullable), `use_ssl` (bool)
- Org-level — one row, shared across all users

**New DB table — `LDAPGroup`:**
- `id`, `ldap_server_id` FK, `group_dn` (full DN of the AD group), `label` (display name)
- Group-level rules — any member of this AD group can authenticate, checked at login time
- No per-user pre-import needed for group rules

**`User` model changes:**
- Add `auth_type` field: `"local"` (default) or `"ldap"`
- Add `ldap_server_id` FK (nullable) — which server this user authenticates against
- `password_hash` nullable — null for LDAP users
- OTP skipped for LDAP users — LDAP server handles auth security

---

**Login flow changes:**
1. User submits credentials
2. Look up user by username — check `auth_type`
3. If `"local"` — existing flow (hash check → OTP)
4. If `"ldap"` — bind to LDAP server with user's credentials via `ldap3`; on success → `login_user()`, skip OTP
5. If no matching `User` row — check `LDAPGroup` rules: attempt LDAP bind, then verify group membership; on success → auto-create `User` row with `auth_type="ldap"` and `login_user()`

---

**Server Management UI — LDAP card (currently locked stub):**
- Fields: Name, Server IP/Name, Port (default 389), CN Identifier (default `SAMAccountName`), Distinguished Name (base DN), Bind Type toggle (Simple / Anonymous / Regular)
- Conditional fields: bind DN + password shown only for Simple/Regular
- LDAP/LDAPS toggle (Secure Connection) — switches default port 389↔636
- **Test** button — verifies connection and bind
- **Fetch DN** button — auto-populates base DN by querying the server
- Save button — encrypts bind password, writes to DB

**LDAP Explorer (modal launched from server management after server is saved):**
- AJAX endpoint connects to LDAP using saved bind credentials, walks tree under `base_dn`
- Returns browsable OU/group/user tree
- Admin can:
  - Select individual users → creates `User` rows with `auth_type="ldap"`
  - Select groups → creates `LDAPGroup` rows (group-level rules, no per-user import)
- Mix of individual users and group rules supported simultaneously

---

**User Management UI changes:**
- Two sections under the existing user table: **Local Users** and **Remote Users (LDAP)**
- Remote users show LDAP server name as a badge instead of role/OTP status
- Group rules appear as a separate row type with member count indicator
- Approve/enable/disable/promote actions apply to individual LDAP users; group rules have enable/disable only
- Admin cannot set password for LDAP users

**Live Sessions page (new admin panel tab):**
- Reads all `user_session:*` keys from Redis
- Resolves username + auth_type for each session
- Two sections: **Local Sessions** and **Remote Sessions (LDAP)**
- Shows: username, auth type badge, login time (if stored in session), IP address
- **Kick** button per row — deletes `session:<sid>` + `user_session:<user_id>` from Redis (same logic as existing terminate session)
- Replaces the per-user terminate button in user management (or keeps both)
- New sidebar entry under **Access** section in admin panel

---

**Python dependency:** `ldap3~=2.9.1` — pure Python, no C extensions, Docker-friendly. Added to `requirements.txt`.

**Alembic migrations:**
- `a19370a56122_add_ldap` — `ldap_servers`, `ldap_groups` tables; `auth_type` + `ldap_server_id` on `users`; `password_hash` made nullable
- `5c2b80c49fc9_nullable_user_email_fullname` — `email` + `full_name` nullable (LDAP users have no local registration)

**`src/ldap_auth.py`** — new module: `make_server`, `service_bind`, `user_bind`, `test_connection`, `test_user`, `check_group_membership`, `fetch_user_details`, `fetch_base_dn`, `walk_tree`. All network calls wrapped in `(LDAPBindError, LDAPSocketOpenError)` try/except. Consistent `{"status": "ok/error", ...}` response shape throughout.

**Login flow** — extended with two new branches:
- Existing LDAP user: `user_bind` → check `is_approved`/`is_active` → `login_user()` or flash
- Unknown user: `check_group_membership` against all active groups → auto-create `User(auth_type="ldap")` → `login_user()`

**LDAP routes in `webapp.py`** (10 routes under `/admin/server/ldap/`): GET list, new, save, delete, test, test_user, fetch_dn, explore (walk_tree), import (users + groups), groups list, group toggle, group delete.

**`templates/server_management.html`** — full LDAP card replacing locked stub: server list with inline expand/collapse edit panels, bind type selector (two styled option cards), LDAPS toggle (auto-flips 389↔636), Test/Test User/Fetch DN/Save/Delete per server. Explorer modal (modal-xl) with DOM-built tree browser, breadcrumb nav, selected panel, import with summary. All Explorer JS uses `createElement` + `addEventListener` + `data-dn` + `CSS.escape` — no inline onclick injection.

**`templates/admin_users.html`** — LDAP badge on LDAP users; Group Rules section at bottom (AJAX-loaded, toggle/delete per rule).

**`templates/live_sessions.html`** — new page under Access section: two cards (Local / Remote LDAP), session table with elapsed time (Jinja2 macro), kick/kick-all JS, updateCounts on kick.

**`templates/admin.html`** — Live Sessions sidebar link added under Access section (`bi-activity` icon, `active_section="sessions"`).

**Live Sessions routes:** `GET /admin/sessions` (scans `user_session:*` Redis keys, TTL→elapsed math, local/ldap split), `POST /admin/sessions/<uuid>/kick` (deletes `redis_session:<sid>` + `user_session:<uid>`, emits audit log).

### 4.9c Codebase Cleanup (post-LDAP)
See "Step 1" in the Remaining Work section above — detailed there.

### 4.10 Documentation
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
