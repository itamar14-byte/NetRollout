# Development Workplan
_Last updated: 2026-09-30 — Phase 4 planned and approved (`docs/plans/phase-4.md`, branch `phase-4-packaging`); see the status table under "Remaining work"_

> **How to read this file.** Phase entries are a dated log of what was built at the time. Later work changed some of it; those places are marked *Superseded* with a pointer, and small details (file names, signatures) have been corrected in place (checked against the code on 2026-09-30). The **current** design is in `docs/architecture.md`; open work is in the status table under "Remaining work" and in `docs/plans/`.

---

## Phase 1 — User Auth Pipeline ✅ COMPLETE

### 1.1 Flask-Login setup ✅
- `flask-login` installed, added to `requirements.txt`
- `UserMixin` added to `User` model in `tables.py`
- `LoginManager` initialized (today in `src/webapp/extensions.py`), login view `"auth.home"`
- `user_loader` callback wired to DB via `get_session()`, with `expunge()` to avoid DetachedInstanceError
- `@login_required` applied to all protected routes

### 1.2 Frontend ✅
- `templates/index.html` — replaced Get Started with login card + flash messages + register link
- `templates/base.html` — user widget dropdown (username, My Account, Admin Panel for admins, Logout) shown when authenticated
- Full dark mode implemented across all Bootstrap components (cards, inputs, buttons, accordion, tables, alerts, dropdowns, modals)

### 1.3 Webapp infrastructure ✅
- Flask app with templates resolved from the project root (today `template_folder='../../templates'`, `static_folder='../static'` in `src/webapp/setup.py`)
- `SECRET_KEY` — required for flash/session; now read from the env (default `dev`; Phase 4 makes it mandatory in a container)
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
- Mandatory for all local users — no toggle, product-level requirement (admin-role users included; LDAP users skip it since 4.9b)
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
- Sidebar sections at the time: User Management, Audit Logs (stub), Query (stub) — today: Access (User Management, Live Sessions), Observability (Audit Logs, Analytics), System (Server Management, System Settings); see 4.8c
- `GET /admin` → guard + redirect to `/admin/users`
- `GET /admin/users` → query all users ordered by `created_at`, `expunge_all()`, render table
- `POST /admin/users/<user_id>/<action>` → UUID cast, apply action within session, commit on exit
- Actions: `approve` (is_approved=True, is_active=True), `enable` (is_active=True), `disable` (is_active=False), `promote` (role="admin"; since 3.3b also approves + activates), `demote` (role="user"). Added later: `delete`, `terminate_session`, `reset_2fa`, and bulk `/admin/users/bulk/<action>`
- `admin_users.html` — live search, sortable columns (client-side), status filter buttons (all/pending/active/inactive)
- Status is ternary: pending (not approved), active (approved + active), inactive (approved + not active)
- Per-row single action button (pill-shaped, color-coded): Approve (green) / Enable (cyan) / Disable (orange) — only relevant button shown, others absent
- Promote/demote always present except factory user row

### 1.8 User Model (Phase 1 schema) ✅
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
Since then: `password_hash`, `email`, `full_name` nullable (LDAP users); `otp_secret` is `String(255)` and Fernet-encrypted; `auth_type` + `ldap_server_id` added (4.9b). Current schema: `docs/architecture.md` §3.

### 1.9 Security decisions ✅
- **Data minimization**: at the time, device credentials were never stored. ⚠ *Superseded:* since Phase 2 they are stored, Fernet-encrypted only, in `SecurityProfile` (key handling: `docs/architecture.md` §1)
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
- `RolloutLogger` is purely I/O — owns `queue` and `logfile`, replaces `logging_utils.py` globals (⚠ *Superseded:* Redis list + pub/sub since 4.6b)
- `Validator` — all static methods, pure namespace
- `InputParser` — three entry points: `from_files()`, `from_web()`, `from_inventory()` (as built: see 2.8)
- `Device.from_inventory()` factory — single boundary where decryption happens
- `SecurityProfile` — separate table, owned by `User`; assigned via `Inventory.sec_profile_id` (the FK is on the inventory side). Encrypted with Fernet. Key from `NETROLLOUT_ENCRYPTION_KEY` env var, fallback to `~/.netrollout/encryption.key`
- `RolloutSession` — "RAM" table, ephemeral, deleted on job completion (⚠ *Superseded:* dropped in 4.6b; Redis holds job state)
- `RolloutOrchestrator` — singleton at app startup, owns `{job_id: RolloutJob}` dict, coordinates multithreading via `_dispatch()`, syncs DB and in-memory state. Webapp routes are thin delegators. (⚠ *Superseded:* Redis BLPOP dispatcher + semaphore since 4.6b)
- ~~Config env vars asked interactively in `db_install.py` at install time~~ — ⚠ *Superseded:* never built that way. `db_install.py` doesn't prompt; config comes from env / `config.env`, `MAX_CONCURRENT_JOBS` became the *Concurrent rollout jobs* System Setting (seeded from `ORCHESTRATOR_WORKERS`), and install-time questions are the Phase 4 installer (`docs/plans/phase-4.md`, decision 4)
- `DeviceResult` — "MEMORY" table, one row per device per job, soft `job_id` ref, used for analytics and audit
- `VariableMapping` — Phase 3, hook points designed but not implemented yet
- `User` owns five relationships: `inventory`, `security_profiles`, `variable_mappings`, `sessions`, `results` (today: `sessions` gone; `job_metadata`, `property_definitions`, `ldap_server` added)

---

## Phase 2 — Architecture Refactor & DB Integration ✅ COMPLETE
_Started: 2026-04-07 — Completed: 2026-04-11_

### 2.1 DB schema — `tables.py` ✅
Add new ORM models:
- `Inventory` — per-user device topology store, FK to `User` and `SecurityProfile`
- `SecurityProfile` — encrypted credentials (Fernet), FK to `User`, loaded as `user.security_profiles`
- `VariableMapping` — `$$TOKEN$$` (free text) → `property_name` + optional `index` (nullable int), FK to `User`, loaded as `user.variable_mappings`. `index=None` = simple string attribute; `index=N` = positional element of a list attribute (e.g. `vrfs[1]`). Validator checks list length at rollout time.
- `RolloutSession` — ephemeral active jobs table ("RAM"), FK to `User`, loaded as `user.sessions` (dropped in 4.6b)
- `DeviceResult` — permanent archive ("MEMORY"), FK to `User`, soft `job_id` ref, loaded as `user.results`

Add relationships to `User`: `inventory`, `security_profiles`, `variable_mappings`, `sessions`, `results`

### 2.2 Encryption layer ✅
- Fernet encryption/decryption helpers for `SecurityProfile` fields
- Key resolution: `NETROLLOUT_ENCRYPTION_KEY` env var → `~/.netrollout/encryption.key`; generated only on a fresh install and checked against stored data at startup (fail-fast, Step 1b)

### 2.3 `RolloutLogger` class — `logging_utils.py` ✅
Refactored module-level globals into `RolloutLogger(webapp, verbose, logfile=None)`. Owns `queue` and `logfile`. Methods: `log()`, `notify()`, `get()`. All `base_notify` imports removed from entire codebase.
⚠ *Superseded:* today `RolloutLogger(webapp, verbose, prefix="rollout", job_id=None, redis_client=None)` with `notify`, `get_history`, `subscribe`, `redis_cleanup` (3.4c, 4.6b).

### 2.4 `RolloutJob` + `RolloutOrchestrator` — `orchestration.py` ✅
Both classes in one file. `RolloutJob(id, engine, options)` — constructs own logger, owns thread + cancel_flag. `start(on_complete)` uses closure + callback pattern. `RolloutOrchestrator(max_concurrent=4)` — singleton, builds engine+job internally in `submit()`, `_dispatch()` uses `is_alive()`/`is_pending()` for slot management. DB writes (RolloutSession, DeviceResult) stubbed as TODO — pending 2.9.
⚠ *Superseded:* today `RolloutJob(job_id, user_id, engine, options, redis_client)` and `RolloutOrchestrator(backend, max_concurrent)` with a Redis BLPOP `_dispatcher` + semaphore (4.6b; `docs/architecture.md` §5).

### 2.4b Install script — moved to Phase 4 (now the native installer, `docs/plans/phase-4.md` stage 9).

### 2.5 `RolloutEngine` refactor — `core.py` ✅
- `cancel_event` removed from constructor — passed as argument to `run(cancel_event, logger)`, `_push_config()`, `_verify()`
- `notify()` method deleted — replaced by injected `RolloutLogger` at all callsites
- `push_config()` → `_push_config()`, `verify()` → `_verify()`
- `webapp`/`verbose` flags removed from engine — live in logger now, engine only reads `_verify_flag`
- Logfile path surfaced via `os.path.abspath(logger.logfile)`

### 2.6 `Device` updates — `core.py` ✅
- `label` field added
- `netmiko_connector()` kept public (called from different class — private would be bad practice)
- `from_inventory(cls, row: Inventory) -> Device` factory implemented (today `from_inventory(row, user_id)` — applies only the rolling-out user's mappings) — decrypts credentials from linked `SecurityProfile` via `encryption.decrypt()`
- `fetch_config(logger: RolloutLogger)` — logger injected, `base_notify` removed

### 2.7 `Validator` class — `validation.py` ✅
Logger-injected instance class. `validate_device_data` and `validate_file_extension` are instance methods (need logger). `validate_ip`, `validate_port`, `validate_platform`, `test_tcp_port` remain static.

### 2.8 `InputParser` class — `input_parser.py` ✅ (renamed from parser.py)
Constructor takes `Validator` + `RolloutLogger`. Methods at the time: `csv_to_inventory`, `form_to_inventory`, `parse_commands`, `_prepare_devices`. Static: `import_from_inventory(inventory) -> list[Device]`. `parse_files()` removed from codebase.
Today: `prepare_devices(raw) -> (devices, errors)` (public, CLI path), `csv_to_inventory(...) -> ImportReport`, `parse_commands`, static `import_from_inventory(rows, user_id)`; `form_to_inventory` removed in the Step 1b dead-code sweep. `webapp_input()` and `background_rollout()` removed from webapp.

**Webapp rewire ✅** — routes are thin delegators. `cancel_event` global removed. `start_rollout` loads inventory from DB, calls `import_from_inventory`, submits to orchestrator, stores `job_id` in Flask session. SSE reads from `job.logger.queue`. (⚠ *Superseded:* since 3.2 the start route redirects to `active_jobs?new=<id>`; SSE replays Redis history then tails pub/sub since 4.6b)

**Tests: 83/83 passing (at the time).** All previously disabled test classes updated to new API and passing.

### 2.9 Inventory management UI ✅

**Done (2026-04-10):**
- Operator zone restructure: `operator_base.html` with collapsible sidebar, dashboard, account, inventory stub, results stub
- `DeviceResultDict` TypedDict in `core.py` — typed return from `run()`, consumed by `_cleanup()`
- Orchestrator DB writes: `RolloutSession` written on `submit()`, promoted to "active" in `_dispatch()`, `DeviceResult` rows written + session deleted in `_cleanup()` (⚠ *Superseded:* Redis `job:<id>:meta` hash + counters since 4.6b)
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
- TCP Test Connection button on both Add and Edit device modals. Add: grey Test → green Confirm (submit) / red Save Anyway (submit). Edit: a direct Save button, and Test only reports status. Status pill on the left; resets on IP/port change and modal close
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

## Phase 3 — Functionality, Logic & Testing ✅ COMPLETE

### 3.1 Variable mapping builder ✅ COMPLETE (2026-04-11)
- `variable_mappings.html` — card grid, add/edit/delete modals, drag-assign (since 2026-09: the shared two-column assign board)
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
- ⚠ *Superseded:* today errors and notices come from an `ImportReport`, and web import does no reachability check at all (Inventory shows reachability live). Credentials and attribute columns are saved (Step 1b)
- Phase 3 TODO: proper activity logging with operation-prefixed filenames

### 3.2 Rollout initiation from web UI ✅ COMPLETE (2026-04-12)

- `new_rollout.html` — device selection table with checkboxes, vendor logos, platform tags, green/red profile dots, disabled rows for devices with no profile
- Multi-platform detection: amber warning banner when multiple platforms selected; UI rebuilds one command block per platform with vendor logo in header
- Per-platform command blocks: paste/file toggle, text preserved on rebuild
- Verify `?` tooltip (best-effort text match warning), verbose toggle, optional rollout note (audit comment)
- Single-platform submits natively; multi-platform JS packages `platform_commands` JSON hidden field
- `new_start_rollout` route: multi-platform detection via `platform_commands` field, groupby per device_type, one `orchestrator.submit()` per group, redirects to `active_jobs?new=<job_id>`
- `active_jobs.html` — stats bar (Running/Queued/Devices in flight/live clock), job table with pulsing status dot, elapsed timer, 3 action buttons per row
- Log button: toggles inline SSE terminal, replays the job's log history then tails live messages (today: Redis `job:<id>:history` via LRANGE, then pub/sub `job:<id>:logs`)
- Cancel button: POST to `/rollout/cancel`, sets the job's status to "cancelling" (today `job:<id>:meta` in Redis)
- Rollback button: modal with compensatory commands textarea, verify/verbose toggles with `?` tooltip, device attributes warning note. On confirm, `fetch('/rollout/rollback/<job_id>')`, redirects to `active_jobs?new=<job_id>` with same glow animation
- `job-new` CSS glow animation on new job row; auto-refresh strips `?new=` so glow fires once only
- `RolloutLogger` dual-write: `_queue` for live SSE delivery, `_buffer` for full history replay (⚠ *Superseded:* Redis pub/sub + history list, 4.6b)
- `important=True` flag on key engine messages (rollout start, verify start, per-device summary, completion)
- `JobMetadata` table: soft `job_id` ref, JSON `commands` (pre-substitution), nullable `comment`, `user_id` FK — written on submit (today in its own session in `RolloutOrchestrator.submit()`)
- pg_cron installed on PostgreSQL 17-bookworm container; `cron.database_name = 'rollout_db'` set via `ALTER SYSTEM`
- Two cron jobs at the time: `job_metadata_retention` (7 days) + `device_result_retention` (30 days), idempotent via DO block unschedule-then-schedule pattern. ⚠ *Superseded:* today four jobs daily at 03:00 (`device_result_retention`, `job_metadata_retention`, `device_result_config_retention`, `audit_log_retention`), periods read from System Settings at run time, re-scheduled at every startup (see the retention table in Step 1b)
- Old routes (`/start_rollout`, `/upload`, old `sse_stream`) retired in the Blueprint split

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
- `audit()` helper (today `WebServices.audit` in `src/webapp/utils.py`) — opens own session, commits independently of calling route's transaction
- 21 routes instrumented: auth (login with failure reasons, register, logout), user management (all single + bulk actions), inventory CRUD + import + bulk_assign, security profile CRUD, variable mapping CRUD + bulk_assign, rollout start/cancel/rollback
- Login failures record reason: `invalid_credentials`, `account_disabled`, `pending_approval`
- pg_cron `audit_log_retention` job: daily at 3AM; 90 days by default, now the *Audit log* System Setting (7–3650)

**Admin UI (`/admin/audit`):**
- Filterable table: actor username (contains search), action (dropdown of distinct values), success/fail toggle
- Sticky floating header (FortiGate-style, `position: sticky` within scroll container)
- Per-row FortiGate gear (⚙): View Detail (modal with pretty-printed JSON + metadata), Copy Row (clipboard), Filter by Actor
- 500-row cap per query

**Log file infrastructure:**
- `LOGS_DIR` defined in `logging_utils.py` as `src/../logs/` (project root)
- `RolloutLogger.__init__` takes `job_id` (timestamp computed internally since 3.4c), constructs path `rollout_{timestamp}_{job_id}.log`, calls `os.makedirs(LOGS_DIR, exist_ok=True)` — all filesystem setup in one place
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
- QueryBuilder fields: `started_at` (date), `device_type` (select), `status` (select), `commands_sent` (integer), `device_ip` (string), `device_port` (integer, added with ip:port identity)
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

### 3.5 Test suite ✅ COMPLETE (2026-09-29)
`pytest` from the repo root. Last full run: **456 passed, 1 skipped (2026-09-30)**.
- `tests/unit/` — hermetic: engine, device, parser, validator, orchestration, reachability, encryption, LDAP auth logic, web helpers, Redis client, logging, settings registry and rules, the startup proxy check, and the CLI (`test_cli.py`: arguments, prompts, file input, error exits, Ctrl+C; engine and TCP probe mocked)
- `tests/integration/` — real Postgres (`rollout_test`) and Redis (db 15), skipped with a reason when a service is unhealthy: auth, admin, inventory, profiles/mappings/properties, rollouts and jobs, error handling, DB layer, System Settings (store, seeding, rules, admin page), the `/_netrollout/instance` endpoint, a route matrix, and LDAP against an ephemeral OpenLDAP container the suite starts and removes. The pg_cron test is opt-in (`TEST_PG_CRON_URL`)
- Known bugs are recorded as strict xfails: once fixed they fail as "unexpectedly passed", so the marker gets removed with the fix

**CLI bugs found by the new tests — fixed (2026-09-29):** blank lines in the commands file were pushed as commands; a UTF-8 BOM stuck to the first command (the file is now read as `utf-8-sig`, like the devices CSV, and a non-UTF-8 file is refused with a clear message); the final "Press Enter to exit..." raised `EOFError` without a terminal (cron, CI, pipes).

**EVE-NG live testing (between Phase 3 and Phase 4):**
- EVE-NG deployed on GCP with WireGuard VPN to dev machine (2026-04-13) — cannot run locally (conflicts with Docker/VMware Workstation virtualization)
- First rollout test complete (2026-04-14): Cisco IOL IOS, hostname push + verify, ReadTimeout edge case hit and fixed
- Next: multi-device test, FortiOS node, verify pass/partial/fail paths, rollback flow

### 3.6 Per-job device concurrency ✅ COMPLETE (2026-04-13)

**Two-layer concurrency model:**
- Layer 1 — job-level: `RolloutOrchestrator` runs up to `max_concurrent` jobs simultaneously (default 4), each in its own `threading.Thread`
- Layer 2 — device-level: `RolloutEngine` uses `ThreadPoolExecutor(max_workers)` per job (default 10) — up to 40 simultaneous SSH sessions with the defaults
- Both are System Settings since 2026-09-30: *Concurrent rollout jobs* (1–32, after restart) and *Devices in parallel per job* (1–64, next rollout)

**Engine changes (`core.py`):**
- `max_workers: int = 10` added to `RolloutOptions`
- `_push_device(device, cancel_event, logger) -> tuple[str, bool | None]` extracted from `_push_config` (all original comments and docstrings preserved)
- `_verify_device(device, logger) -> tuple[str, int]` extracted from `_verify`
- Both `_push_config` and `_verify` rewritten to use `ThreadPoolExecutor` + `as_completed`

**Thread safety (`logging_utils.py`):** ⚠ *Superseded:* `_buffer`, `_buffer_lock`, `get_buffer_snapshot()` and the `queue.Queue` were removed with the Redis rewrite (4.6b); only `_log_lock` remains.
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
- cancel (`/rollout/cancel`) — ownership check bypassed for admin
- SSE stream (`/rollout/stream/<job_id>`) — accessible by admin for any job
- `download_log` — ownership check bypassed for admin

**Backend:**
- `active_jobs` route: if admin, reads all jobs + username map; splits into `my_jobs` / `other_jobs` (today it scans the Redis `job:*:meta` hashes; `RolloutSession` was dropped in 4.6b)
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
The remaining work, and the Phase 4 plan it leads to, is below. Sections numbered 4.6–4.10 further down are completed earlier work from the original Phase 4 list, kept as a log.

---
## Remaining work — path to v1.0
_Feature set is complete as of 2026-04-28. Remaining work is cleanup, packaging, and documentation._

**Status (2026-09-30):**

| Step | Scope | Status |
|---|---|---|
| 1 — 4.9c | Codebase cleanup (route abstraction, audit table) | ✅ Done |
| 1b | Pre-4.1 cleanup (branch `pre-4.1-cleanup`) | ✅ Done — EVE-NG round postponed (not blocking) |
| 2 — 4.0 | Blueprint split | ✅ Done — frontend asset splitting deferred |
| 3 — 4.0b | BYO Postgres / Redis | ✅ Done — Grafana BYO post-v1.0 |
| 3b | System settings + startup reverse-proxy check (`docs/plans/system-settings.md`, branch `system-settings`) | ✅ Parts A + B done (2026-09-30) — Part C (nginx follows the Access settings) is Phase 4 stage 8 |
| 4 — Phase 4 | Packaging v1.0.0 — `docs/plans/phase-4.md` (approved 2026-09-30, branch `phase-4-packaging`): hygiene + migration squash, paths/config, container runtime, forced password change, **platform profiles (push commit + verify on all 12 platforms)**, CLI `.exe`, images, compose, Part C + certificate upload, installer + scripts, CI, docs | ⬜ Next — not started |
| 5 — release | v1.0.0 — release gates in `docs/plans/phase-4.md` (EVE-NG round against the built image, backup/restore, upgrade) | ⬜ |

### Step 1 — 4.9c Codebase cleanup ✅ COMPLETE
Do this before freezing into a Docker image. Code quality is easier to fix before packaging than after.

**Response shape standardization ✅ COMPLETE (2026-04-28)**
- All routes unified to `{"status": "ok"/"error", "message": "..."}`. All frontend consumers updated.

**Route abstraction — decorators + DB helpers + operation factories:**
The core insight: nearly every route in `webapp.py` (the single-file app at the time) is one of 5 archetypes — **list**, **create**, **edit**, **delete**, **action**. Each archetype has the same skeleton. The fix is a 3-layer mini-framework that absorbs all cross-cutting concerns so routes become pure declarative wiring.

*Layer 1 — Decorators (cross-cutting concerns):*
- `@require_admin` — role check, returns 403 automatically
- `@with_json(*required_fields)` — extracts + validates JSON body, injects as `data=` kwarg
- `@with_form(*required_fields)` — same for form-encoded routes

*Layer 2 — DB dispatcher:*
- `act_on_db_object(model, id, callback, user_id=None)` (implemented as `WebServices.act_on_db_obj`) — owns the session lifecycle, does the ownership-guarded lookup, returns 404 if missing, calls `callback(obj, db_session)` if found. Replaces the open-session / query / 404-check / expunge pattern repeated across ~30 routes.
- `ok(message=None, **extra)` → `jsonify({"status": "ok", "message": ..., **extra})`
- `err(message, code=200)` → `jsonify({"status": "error", "message": ...}, code)`

*Layer 3 — Operation factories (build callbacks for `act_on_db_object`):*
- `delete_op(audit_action, guard=None)` — returns a callback that checks the optional guard (e.g. "has assigned devices"), calls `db.delete(obj)`, calls `audit()`, returns `ok()`. Guard is a callable `(obj) -> err(...)` or `None`.
- `edit_op(fields, audit_action)` (implemented as `update_op`) — returns a callback that applies field updates from a dict, audits, returns `ok()`.
- Custom lambda or named function for "action" routes (test, assign, rollback) where the logic is genuinely unique.

*The full call chain:*
```
route
  → act_on_db_object(Model, id, callback, user_id)
      → finds object, checks ownership, 404 if missing
      → calls callback(obj, db_session)
          ← callback built by delete_op() / edit_op() / or custom lambda
              → does the work, audits, returns ok() / err()
```

*Result:* a CRUD route collapses to a declaration — which model, which operation, which audit action:
```python
@app.route("/security/<uuid:profile_id>/delete", methods=["POST"])
@login_required
def security_delete(profile_id):
    return act_on_db_object(
        SecurityProfile, profile_id,
        delete_op("security_profile.delete",
                  guard=lambda p: err(f"Cannot delete — {len(p.inventory)} device(s) assigned", 400)
                                  if p.inventory else None),
        user_id=current_user.id
    )
```
Routes with real business logic (rollback, bulk assign, test connection) get a custom callback. Everything else is configuration of a pattern.

**Audit complete (2026-04-28):** full read of all 2885 lines done. 22 concrete problems catalogued across 7 categories. The three highest-leverage moves: `@require_admin` + `@with_json` (kills 40+ boilerplate blocks), `act_on_db_object` + operation factories (kills ~20 ownership-lookup patterns), `ok()`/`err()` (enforces response shape everywhere).

**Full audit table (2026-04-29):**

| # | Problem | Impact | Fix | Status |
|---|---------|--------|-----|--------|
| 1 | Admin guard inline in 30+ routes | High | `@require_admin` decorator | ✅ implemented |
| 2 | JSON validation repeated ~15× | High | `@with_json(*fields)` decorator | ✅ implemented |
| 3 | Form extraction duplicated in create/edit pairs | Medium | `@with_form(*fields)` or shared helper | ✅ implemented |
| 4 | Ownership-guarded lookup copy-pasted ~12× | High | `act_on_db_object()` | ✅ implemented |
| 5 | Mapping validator cascade duplicated 100% | Medium | `_validate_mapping_fields()` helper | ✅ complete (2026-04-29) |
| 6 | LDAP server lookup repeated in 7+ routes | High | `act_on_db_object()` | ✅ implemented |
| 7 | `security_create` / `security_quick_create` ~70% dupe | Medium | `_build_security_profile()` helper | ✅ complete (2026-04-29) |
| 8 | KPI calculation duplicated in dashboard + analytics | Low | KPI helper function | ✅ complete (2026-04-29) |
| 9 | Redis session write in 3 auth routes | Low | `_record_session()` helper | ✅ complete (2026-04-29) |
| 10 | `properties_quick_create` is a pass-through stub | Low | merge URLs onto `properties_create` | ✅ complete (2026-04-29) |
| 11 | Login route has 5 levels of nesting | Medium | decompose to named functions | ✅ complete (2026-04-29) |
| 12 | `rollback` does DB query before validating JSON body | Low | reorder guard before query | ✅ complete (2026-04-29) |
| 13 | `download_log` bespoke ownership check | Low | `_user_owns_job()` helper | ✅ complete (2026-04-29) |
| 14 | `security_test` returns raw dicts | Medium | `ok()`/`err()` on all returns | ✅ complete (2026-04-29) |
| 15 | `security_test` error paths return 200 OK | Medium | correct HTTP codes (401/404/503/504) | ✅ complete (2026-04-29) |
| 16 | `"Invalid redis_session_request"` in unrelated routes | Low | replace with `"Invalid request"` | ✅ complete (2026-04-29) |
| 17 | `cancel_rollout` returns raw dicts | Medium | `ok()`/`err()` + HTTP codes | ✅ complete (2026-04-29) |
| 18 | LDAP group routes use 2-space indentation | Low | reformat to 4-space | ✅ already correct |
| 19 | `import subprocess` inside function body | Low | move to top-level imports | ✅ complete (2026-04-29) |
| 20 | `import redis` inside function body | Low | move to top-level imports | ✅ complete (2026-04-29) |
| 21 | ~80 routes in one 2885-line file | High | Blueprint split (Step 2) | ✅ Step 2 |
| 22 | No centralized response envelope | Medium | `ok()` / `err()` helpers | ✅ implemented |

**Dead code sweep:** moved to the deferred list (Step 1b).

---

### Step 1b — Pre-4.1 cleanup (2026-09, branch `pre-4.1-cleanup`) ✅ except the items under Remaining
Blocking and should-fix items found in a full codebase review after the Blueprint split. Work lands on `pre-4.1-cleanup`; `master` is fast-forwarded when done.

**Done:**
- Untracked `.idea/` and `__pycache__/`; `logs/` ignored
- Global devices (admin-published inventory shared with all users), incl. mapping delete-cascade fix and profile-ownership check
- Orchestrator slot leak on engine crash; UTF-8 log files
- Error handlers finished (`db_error.html`, encryption key, CSRF redirect)
- `require_admin` redirect target
- Redis resilience: dispatcher retries with backoff; app starts and serves with Redis unreachable (`REDIS_UNAVAILABLE`)
- Retention cron fixed and policy revised (below)
- Encryption key fail-fast: no import-time key load; refuses to start on malformed, missing-with-data, or mismatched key
- Test suite: `pytest` from repo root; unit (hermetic) + integration (real PG/Redis, skipped when unhealthy) — current scope and count in 3.5
- 8 bugs found by the test suite, fixed: rollout cancel 500 (`hset(field=)`), Active Jobs 500 after restart, LDAP group auto-provision FK violation, duplicate registration 500, profile label NOT NULL (now nullable, migration `713db4dd6251`), Server Management postgres test/save 500, anonymous `/logout` 500, Redis client timeouts (~20s -> ~6s when unreachable)
- Cancel race: devices finishing after a cancel were recorded as `cancelled` (rollback skipped them)
- Rollout targets identified by `ip:port`, not IP: engine results keyed per device, `device_results.device_port` (migration `c2f578d78dc4`), Results labels / Verify Diff / rollback match on ip:port; the same ip:port selected twice in one rollout is refused (same IP on different ports is allowed — NAT / port forwarding)
- LDAP hardened: search-then-bind via the service account (users nested in OUs — required for Active Directory; constructed DN only as a fallback), usernames escaped in DNs and filters, empty passwords never sent, directory outages reported as 'LDAP authentication service unavailable' (not 500), connect/receive timeouts, connections closed; tested against an ephemeral OpenLDAP container started and removed by the test suite (not part of the deployment)
- CSV import: each row validated independently (a bad row is reported, never aborts the import), blank cells accepted (e.g. empty enable secret), credential columns optional for inventory import, row `label` honoured (form label > row label > IP)
- Mapping eligibility: a mapping is bound only if the device can resolve it (attribute set; index in range) — shared `mapping_resolvable` rule for the device modal and drag-assign; the engine skips a device whose mappings can't resolve (no SSH, clear reason) instead of aborting the job
- User-facing "Invalid ldap_request" messages -> "Invalid request"
- Migrations run on the app's own connection (not only `DATABASE_URL`): correct DB after a Server Management switch, honours `PG_SCHEMA`, works with `PG_*`-only config (Docker). pg_cron is optional — its absence no longer blocks the schema. The alembic CLI resolves `DATABASE_URL`, else `PG_*`
- Admin restart relaunches the original command (`sys.orig_argv`): under `python -m src.webapp` it re-ran `__main__.py` as a script, which can't import `src`, so the app never came back
- Accessibility pass: readability tokens (`--nr-text*`, all tiers >= AA on every surface), 420 sub-AA text colors remapped, 12px text floor, visible keyboard focus, alt/aria labels, always-visible delete buttons
- Device reachability: live `ip:port` status (cached 60s) on New Rollout rows and Inventory cards; unreachable devices blocked for rollout and rollback
- Mappings on user-defined properties (validator used a hard-coded list of built-ins); drag panel uses the server's eligibility rule and explains an empty state
- Edit device: direct Save; Test Connection only reports status (Add still tests first)
- Assign board — one shared two-column board (`nrAssignBoard`) for the Security Profiles and Variable Mappings device modals: drag or click/Enter both ways, staged Save `(+N / −M)`, pending-change card edges with a key. Mappings unassign via `remove_ids` (only that mapping's bindings); unassigning a profile warns that the device is blocked in New Rollout; global devices keep their profile (enforced server-side too)
- Console output UTF-8 and never fatal (`utf8_console()` at the webapp and CLI entry points): a `→` in a log line used to fail requests on a non-UTF-8 stdout
- Global devices browser click-through (admin + normal user) — done by the developer
- CSV import — one format for the CLI and the web app: attribute columns (system or custom property, by name or label) saved as variable attributes; credential columns become security profiles (checkbox, default on): exact username/password/secret match reuses the user's profile, otherwise a new profile with a unique label (`admin · CSV import 29 Sep`), a warning when it shares a username with another profile, audited as `security_profile.create` (source csv_import); unknown columns reported; no TCP check on web import (the CLI keeps it)
- Log file retention: `*.log` files in `logs/` (web app and CLI) not modified for 60 days (`LOG_RETENTION_DAYS`; since 2026-09-30 the *Log files* System Setting for the web app, kept >= the 30-day job retention so Download Log never loses its file) are pruned at web-app startup, then daily, and on each CLI run; mtime-based, so a running job's file is never removed. Loki keeps its own copy
- `CLAUDE.md` refreshed from the code: commands (run from repo root, no DB-init step), configuration, module map, tables, retention, SSE, shared CSV format; per-feature backend exception recorded under Working style
- `key_error.html` rebuilt: standalone in the app's style, no external scripts (was Tailwind CDN + unpkg), no fake status footer. Admins get concrete steps (which env var / file this server reads, restore the original key, restart; if lost: re-enter profile / LDAP passwords, clear 2FA via SQL) and a warning not to generate a new key; others are told to contact an admin. The steps are also written to the server log, since a failure at 2FA sign-in can lock admins out
- Dead-code sweep and rename artifacts: find-and-replace leftovers fixed (incl. the Users page's "Factory user redis_session" text; the `redis_session:` Redis key prefix kept on purpose), unused `form_to_inventory` / `create_op` / imports removed, pyflakes clean, a latent `\,` escape in the test conftest fixed; `.coverage` untracked, stale `docs/TODO` and `docs/bug_report.md` removed
- Admin → Users → **Reset 2FA** (toolbar action, confirmation modal): clears the selected local users' 2FA secret so they re-enroll at next sign-in; enabled only when every selected user has 2FA (LDAP users and the factory admin don't use it); bulk audit entries now list the affected usernames. The encryption key error page points to it instead of SQL
- Same ip:port warning (never blocks): on device create, on edit when the endpoint changes, and on CSV import (also duplicates within the file), against devices the user can see — own and global; other users' private devices are never considered
- CLI unit tests (`tests/unit/test_cli.py`); the three CLI bugs they found are fixed (see 3.5)
- Rollout summary counts real outcomes ("1 success, 1 failed (of 2 devices)"; it used to call every attempted device configured). CLI exit code reflects the outcome: 0 all succeeded, 1 mixed, 2 nothing applied, 130 Ctrl+C

**Remaining (in order):**

- EVE-NG round: multi-device, FortiOS, verify pass/partial/fail, rollback — postponed by the developer (2026-09-29)

**Retention policy (decided 2026-09-29, supersedes the 7-day `job_metadata` rule):**

| Data | Retention | Mechanism |
|---|---|---|
| Job record — `job_metadata` + `device_results` rows | 30 days | Expire together: metadata is deleted only once its results are gone |
| Config snapshot — `device_results.fetched_config` | 7 days | Column cleared, row kept (status/analytics unaffected) |
| `audit_log` | 90 days | Unchanged |

Since 2026-09-30 these are **System Settings** (defaults above, plus log files 60 days): the pg_cron statements read the setting's row when they run, `install()` re-schedules them at every startup, and admins change them in admin panel → System → System Settings. Results page shows "Verify Diff expired" once a job's snapshots are cleared.

**Deferred past 4.1:**
- ~~nginx SSE location~~ — fixed in Phase 4's nginx image (`docs/plans/phase-4.md`, stage 6)
- Frontend asset splitting (Step 2)
- Active Directory: test against a real AD (Samba AD DC container, ephemeral like the OpenLDAP one) — `sAMAccountName` logins, UPN binds, and nested-group membership (today only direct members of a mapped group match). A v1.0.0 release gate only if v1.0 claims AD support (`docs/plans/phase-4.md`)
- ~~System settings page~~ — moved ahead of Phase 4: see Step 3b and `docs/plans/system-settings.md` (approved 2026-09-29)

**Factory admin:** decided in Phase 4 — `admin`/`admin` with a forced password change at first login (`docs/plans/phase-4.md`, decision 8).

---

### Step 2 — 4.0 Flask Blueprints ✅ COMPLETE · frontend asset splitting deferred
`webapp.py` is now the `src/webapp/` package, run with `python -m src.webapp`.

**Package:**
- `__init__.py` — `create_app()` factory, blueprint registration
- `__main__.py` — entry point (Waitress)
- `extensions.py` — shared singletons and error handlers; `setup.py` — app configuration; `utils.py` — `ok()`/`err()`, decorators, `act_on_db_obj`, access helpers

**Blueprints (`src/webapp/blueprints/`):**
- `auth.py` — login, register, OTP enroll/verify, logout, account
- `inventory.py` — `/inventory`: CRUD, test connection, reachability, per-user mappings, CSV import, bulk profile assign
- `security.py` — `/security`: profile CRUD, connection test
- `mappings.py` — `/mappings`: mapping CRUD, bulk assign/unassign
- `properties.py` — `/properties`: user-defined property CRUD
- `rollout.py` — `/rollout`: new, start, stream, cancel, rollback
- `jobs.py` — dashboard, active jobs, results, config diff, log download
- `analytics.py` — `/analytics`: KPI page + query
- `admin_users.py`, `admin_observability.py`, `admin_servers.py` — `/admin`: users and sessions; audit and analytics; server management (Postgres, Redis, LDAP, restart)
- Added 2026-09-30: `admin_settings.py` — `/admin/settings` (System Settings); `system.py` — `/_netrollout/instance` (startup proxy check)
- Full route table: `docs/architecture.md` §7

**Frontend asset splitting (deferred past 4.1):**
Extract per-page inline `<style>` and `<script>` blocks into `static/css/<page>.css` and `static/js/<page>.js`. Templates become thin layout files. Makes JS/CSS independently cacheable and reviewable. Do alongside or after Blueprints.

---

### Step 3 — 4.0b BYO Infrastructure ✅ COMPLETE for v1.0 (2026-04-28)
Allow users to connect their own Postgres and Redis instead of the bundled Docker services.

**PostgreSQL BYO ✅ COMPLETE** — server management UI card, `POST /admin/server/postgres/test` + `/save`, writes individual `PG_*` vars to `config.env`, merge-safe (does not overwrite Redis config).

**Redis BYO ✅ COMPLETE (2026-04-28)** — server management UI "Database Services" card with two large service selector buttons (Postgres / Redis). Redis form: host, port, db number, optional password. `POST /admin/server/redis/test` (ping) + `/save` (writes `REDIS_*` vars to `config.env`, merge-safe). `redis_db.py` reads individual vars as fallback when `REDIS_URL` not set. Warning: switching Redis drops all active sessions and orphans running jobs — documented in UI.

**Grafana BYO — deferred post-v1.0.** Grafana is observability-only; users can point it at any datasource manually. Not blocking release.

---

### Phase 4 — Packaging v1.0.0 → `docs/plans/phase-4.md`
Approved 2026-09-30, branch `phase-4-packaging`. Replaces the earlier Steps 4–7 (Docker image, Python `install.py`, docs, release), which predated the planning session. In short: a compose project on Docker Hub (`itamar14/netrollout`, `-postgres`, `-nginx`) delivered as a GitHub release zip; native installer (PowerShell + bash) with a licence notice and `[default]` questions; Docker Desktop on Windows 10/11 VMs as the main team-server case (nested virtualization, auto-logon), Linux supported; only nginx published, Grafana at `/grafana` (optional monitoring profile); management + update scripts; forced admin password change; certificates (self-signed / organisation / upload); Part C (hostname + certificate apply live, HTTPS port via `netrollout apply`); **platform profiles** fixing push commit (Junos / PAN-OS / IOS-XR) and verify on all 12 platforms, NAPALM dropped; CLI `.exe`; GitHub Actions release on a tag; migration history squashed. The plan holds the 15 decisions, 11 stages, verification and release gates.

### Post-v1.0 (deferred)
- **4.3 Update mechanism** — in-app "check for updates" button (the update *script* ships in v1.0)
- **Offline / isolated networks** — bundle the CDN assets (Bootstrap, jQuery, fonts, icons) into the image + offline image transfer (`docker save` / `load`)
- **Frontend asset splitting** (see Step 2)
- **4.0b Grafana BYO** — server management card for external Grafana instance
- **Local-AI "Explain this failure"** — optional small quantized model on the server, on demand per failed device, advisory only (never the pass/fail verdict), no cloud
- ~~4.5 CLI `.exe`~~ — moved into Phase 4

---

### Phase 5 — Core layer refactor (post-v1.0)

Critics are right that `Device` and `InputParser` mix concerns. Goal: each class has one reason to change.

**Class splits:**
- `Device` → `DeviceConfig` (pure value object, no I/O) + `DeviceConnector` (owns the Netmiko operations)
- `InputParser` → `CSVParser` + `FormParser` (each returns plain data) + `DeviceFactory` (constructs `DeviceConfig` from parsed data)

**OOP patterns with genuine use cases:**

*Polymorphism via ABC:*
- `BaseConnector(ABC)` — declares `push_config()` and `verify()` as `@abstractmethod`
- `NetmikoConnector(BaseConnector)` — Netmiko implementation, driven by the per-platform profiles introduced in Phase 4 (how to finish a push, how to fetch the config, which matcher)
- A future backend (e.g. an API-based vendor) is another `BaseConnector` — `RolloutEngine` receives a `BaseConnector`, so backends swap without touching engine logic
- (NAPALM was dropped in Phase 4 — it was only used to fetch configs for verify)
- Python duck typing means polymorphism works without ABC, but ABC makes the contract explicit and raises `TypeError` at instantiation if a subclass is incomplete

*Factory:*
- `ConnectorFactory.build(device_config)` — picks the connector and platform profile for a `device_type`

*Template Method:*
- `RolloutEngine` defines a fixed execution skeleton (`push → optionally verify → record result`)
- Steps are overridable if a future engine variant needs different behaviour

**Approach:** write tests for the current behaviour first, then refactor. The public interface of `RolloutEngine.run()` should not change — internals only.

---

### Phase 5b — Deep Diff (post-v1.0)

Upgrade the verify flow from best-effort substring matching to a true before/after config diff.

**What Phase 4's verify still can't tell (platform profiles fix everything else):**
- A line that was already in the config before the rollout still counts as verified (false positive)
- Typed abbreviations (`int gi0/1`) and values the device rewrites (hashed secrets, reformatting, hidden defaults) can still show as not configured

**Deep Diff feature:**
- New `RolloutOptions` flag: `deep_diff` (only valid when `verify=True`)
- `_verify_device()`: fetch config before push, push, fetch config after — diff the two
- `DeviceResult` schema: replace `fetched_config` (Text) with `config_before` + `config_after` (both Text, nullable) — Alembic migration required
- Pass/fail still derivable: commands that appear in the diff came from this push, no ambiguity about context
- **Comparison engine: `hier_config`** (hierarchy-aware intended-vs-running comparison, negation, per-vendor rules) with **`netutils`** normalization (e.g. interface abbreviations); Junos can use its own commit diff (`show | compare rollback 1`)
- Frontend diff view unchanged — feed `config_before` vs `config_after` instead of commands vs config

**Standard verify (Phase 4, unchanged by 5b):**
- Fetch the post-push config once over Netmiko in the vendor's typed syntax, per-command match (section-aware; removals as "must be absent")
- Fast, one connection per device
- Still shows commands-vs-result view for quick signal

**UI:**
- Rollout form: **Deep Diff** checkbox alongside Verify + Verbose — grayed out unless Verify is checked (JS dependency)
- Tooltip: *"Fetches running config before and after push. Eliminates false negatives but doubles verification time."*
- Results diff view: if `config_before` + `config_after` present → full LCS colored diff; if only shallow verify → commands vs config view

### 4.6 Server-side sessions (Flask-Session) ✅ COMPLETE (2026-04-23)
Flask-Session backed by Redis (`SESSION_TYPE=redis`). On startup, all `redis_session:*` keys flushed from Redis — FortiGate-style invalidation, no SECRET_KEY rotation needed. SECRET_KEY is now a fixed env var (`SECRET_KEY=dev` default), session lifecycle managed by Redis flush instead.

### 4.6b Redis integration (v1.1) ✅ COMPLETE (2026-04-25)
Redis as a fourth Docker service, enabling three features under one infrastructure dependency:

**1. Job queue (distributed orchestration) ✅ COMPLETE (2026-04-25)**
`RolloutSession` Postgres table replaced entirely by Redis. `RolloutOrchestrator` now uses a persistent `_dispatcher` thread blocking on `BLPOP "netrollout:job_queue"`. `submit()` pushes job_id onto the queue via `RPUSH`; `_slots` semaphore (`threading.Semaphore(max_concurrent)`) enforces concurrency limit — acquired before dispatch, released in `_cleanup()`. Job state tracked via Redis hashes (`job:<id>:meta`), user→jobs index via Redis sets (`user_jobs:<user_id>`), live counts via Redis counters (`netrollout:active_count`, `netrollout:pending_count`). `RolloutSessionCollector` reads counters directly. `active_jobs` route and `admin_active_job_count` route both read from Redis. Alembic migration generated and applied to drop `rollout_sessions` table.

**2. Pub/sub log streaming ✅ COMPLETE (2026-04-24)**
Replace the in-process `queue.Queue` + `_buffer` in `RolloutLogger` with a Redis pub/sub channel per job (`job:<job_id>:logs`). Workers publish log lines; SSE endpoint subscribes by job ID — no shared in-process state, no buffer locks. History replay handled via a parallel Redis list (`job:<job_id>:history`) — on SSE connect, `LRANGE` for history then subscribe for live tail.
- `RolloutLogger` rewritten: keys only created when `job_id` provided; `notify()` guards Redis writes with `if self._channel_key`; `get_history()` via `LRANGE`; `subscribe()` returns `PubSub`; `redis_cleanup()` publishes `__done__` sentinel then deletes both keys
- `RolloutJob` adds `get_log_queue()`, `get_log_history()`, `log_cleanup()` — encapsulates logger access
- SSE route replays history snapshot then tails live pub/sub channel; exits on `__done__` sentinel or dead thread
- CSV import drain loop removed; `prepare_devices()` now returns `(list[Device], list[str])` — errors surfaced to caller directly

**3. Session store + revocation ✅ COMPLETE (2026-04-23)**
Flask-Session backed by Redis. Two keys per logged-in user:
- `redis_session:<sid>` — Flask-Session owns this, actual session data
- `user_session:<user_id>` — our mapping, allows admin to locate and delete a user's session

On login: `user_session:<user_id>` written with `session.sid`. On logout: `session.clear()` triggers Flask-Session to delete `redis_session:<sid>`; we delete `user_session:<user_id>`. On terminate: we delete both manually (no Flask request context for target user). Admin "Terminate Session" button added to User Management toolbar.

- `redis-py`, `Flask-Session==0.8.0` added as dependencies
- `src/db/redis_db.py` — dedicated Redis module (today `RedisConfig` + `RedisConnection`, owned by `BackendServices` and reached as `app.backend.redis.client` — no module singleton, so a Server Management switch is picked up)
- Redis runs as a Docker container in development; the packaged compose file is Phase 4 stage 7

### 4.7 Alembic migrations ✅ COMPLETE (2026-04-17)
DB layer refactored into `src/db/` package. `create_all` replaced with Alembic. Initial migration generated and applied. `db_install.py` calls `alembic upgrade head` programmatically — today on the app's own connection at every start, followed by seeding the factory admin and System Settings and scheduling pg_cron (`docs/architecture.md` §6). Migration files ship in the Docker image — fresh installs and schema upgrades both handled via `alembic upgrade head`.

### 4.8 Server Management ✅ COMPLETE (2026-04-17)
⚠ *Superseded:* the module, variable and route names below were replaced: today `src/db/postgres_db.py` (`PostgresConfig.get_url`, `PostgresConnection`) with `PG_HOST/PG_PORT/PG_NAME/PG_USER/PG_PASSWORD/PG_SCHEMA`; `load_dotenv(config.env)` in `BackendServices`; `install()` runs at every start and after a switch (the `pending_db_init.flag` is still written but never read — removed in Phase 4 stage 2); routes `/admin/server/postgres/{test,save}` and `/admin/server/redis/{test,save}`; Restart relaunches `sys.orig_argv` via `subprocess.Popen` then `os._exit(0)` (Phase 4 stage 3 changes it for containers); the LDAP card is real since 4.9b.

As built on 2026-04-17: external DB configuration UI in admin panel. `db.py` refactored: `construct_url()` builds URL from individual env vars (`DB_HOST/PORT/NAME/USER/PASSWORD/SCHEMA`), `build_engine()` prefers `DATABASE_URL` then falls back to individual vars, `search_path` injected via `connect_args` if `DB_SCHEMA` set. `db_install.py` imports fixed to fully-qualified `db.db`/`db.tables` paths. `webapp.py`: `load_dotenv(config.env)` runs before DB imports; `_DB_HOST/_DB_PORT` read from `engine.url`; `pending_db_init.flag` checked on startup → runs `install()` → deletes flag. Three new routes: `GET /admin/server`, `POST /admin/server/db/test` (live connection test, blocks same-DB target), `POST /admin/server/db/save` (writes `config.env` + flag). `POST /admin/server/restart` uses `os.execv` for hot process restart (picks up new config + code changes). `server_management.html`: DB config card (status strip, migration warning, test/save/restart flow) + locked LDAP stub.

### 4.8b nginx integration ✅ COMPLETE (2026-04-18)
nginx reverse proxy running in Docker, terminating TLS and forwarding to Waitress on port 8080.
- Self-signed cert for local dev (`fullchain.pem` / `privkey.pem` mounted at `/etc/nginx/certs/`)
- HTTP → HTTPS 301 redirect
- `proxy_set_header Host/X-Real-IP/X-Forwarded-For/X-Forwarded-Proto`
- `ProxyFix(x_for=1, x_proto=1, x_host=1)` in Flask — trusts exactly 1 proxy hop
- `SESSION_COOKIE_SECURE=True`, `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE=Lax`
- `ProxyFix(x_for=1, x_proto=1, x_host=1)` + secure cookie config (today in `src/webapp/setup.py`)
- Origin check on `@csrf.exempt` login route compares hostnames only via `urlparse` (scheme/port vary under proxy — full URL comparison was rejecting legitimate logins)
- SSL hardening: `TLSv1.2 TLSv1.3` only, `HIGH:!aNULL:!MD5` ciphers
- Security headers: `Strict-Transport-Security`, `X-Frame-Options SAMEORIGIN`, `X-Content-Type-Options nosniff`
- `client_max_body_size 10M` for CSV uploads
- `/rollout_stream` location block: `proxy_buffering off`, `proxy_cache off`, `proxy_http_version 1.1`, `Connection ''` — required for SSE log streaming. **Known issue:** the SSE route moved to `/rollout/stream/<job_id>` in the Blueprint split, so this block no longer matches; streaming still works because the app sends `X-Accel-Buffering: no`. Fixed in Phase 4's nginx image (stage 6)
- Config lives at `docs/nginx/nginx.conf`, bind-mounted to `/etc/nginx/nginx.conf` in container

### 4.8c Admin panel redesign ✅ COMPLETE (2026-04-18)
Admin panel is now a fully standalone page — own layout, own topbar, own sidebar, not embedded in operator chrome.
- Standalone `admin.html` (does not extend `base.html` or `operator_base.html`)
- Topbar: ← Home button back to dashboard + "ADMINISTRATION" monospace label + user dropdown
- Left sidebar: collapsed icon-only (52px) ↔ expanded with labels (210px), localStorage state
- Sidebar sections with labels: **Access** (User Management, Live Sessions), **Observability** (Audit Logs, Analytics), **System** (Server Management, System Settings — gear icon, 2026-09-30)
- Restart button in sidebar footer — same modal + countdown + active-job warning as operator sidebar
- Matching footer style (JetBrains Mono, same copy as operator pages)
- All admin sub-pages (`admin_users`, `live_sessions`, `admin_audit`, `admin_analytics`, `server_management`, `admin_settings`) extend `admin.html`

### 4.9 Grafana analytics ✅ COMPLETE (2026-04-23)

**3-datasource observability stack** running on `netrollout-obs` Docker network:
- **PostgreSQL** (`NetRollout-DB:5432`, read-only `grafana_reader` user) — historical business metrics
- **Prometheus** (`http://prometheus:9090`) — live/ephemeral job state and Flask request metrics
- **Loki** (`http://loki:3100`) — log stream search per job_id

**4 dashboards built and exported to `docs/grafana/dashbaord_config/`:**
- `operations_overview.json` — Active Jobs, Pending Jobs, p99 latency, request rate by endpoint, rollouts per day, job status breakdown pie
- `job_analytics.json` — Total Jobs, Success Rate, Avg Duration, Commands Sent vs Verified, Platform Success Rate bar gauge, Activity Heatmap (hour-of-day × day)
- `job_details.json` — drill-down by `$job_id` template variable: Job Status stat, Device Results table, Commands table, Loki log stream panel
- `audit&security.json` — Total Events, Failed Actions, Unique Actors, Failure Rate stats, Audit Events Over Time, Failed Actions Over Time, Action Breakdown donut, Top Actors bar gauge, Failed Actions Log table

**Provisioning files written (`docs/grafana/provisioning/`):**
- `datasources/netrollout.yml` — all 3 datasources with fixed UIDs, grafana_reader credentials via `$GRAFANA_DB_PASSWORD` env var
- `dashboards/netrollout.yml` — file provider pointing to `/var/lib/grafana/dashboards`, `allowUiUpdates: true`

**Grafana config (set in container `grafana.ini`):**
- `allow_embedding = true` — enables iframe embed in webapp
- `[auth.anonymous] enabled = true, org_role = Viewer` — no-login access for embedded panels

**Prometheus metrics:**
- `prometheus_flask_exporter` with `group_by='url_rule'` — per-endpoint request metrics
- Custom `RolloutSessionCollector` (today in `src/webapp/setup.py`) — exposes `netrollout_active_jobs` + `netrollout_pending_jobs` gauges

**Loki + Promtail:**
- Promtail watches `logs/*.log`, extracts `job_id` label from filename pattern `rollout_{ts}_{uuid}.log`
- `reject_old_samples: false` in loki-config.yml — allows ingesting historical log files

**Pending → Phase 4 stage 7** (`docs/plans/phase-4.md`): configs move to `deploy/` (`dashbaord_config` → `dashboards`), compose mounts + generated `GRAFANA_DB_PASSWORD`, Grafana served through nginx at `/grafana` as an optional monitoring profile. The iframe embed in `/admin/analytics` is replaced by an admin sidebar "Monitoring" link.

### 4.9b LDAP Integration ✅ COMPLETE (2026-04-28)

**Goal:** Allow admins to configure an org-level LDAP/LDAPS server and import users or groups. Imported remote users authenticate directly with their LDAP credentials — no local password needed.

---

**New DB table — `LDAPServer`:**
- `id` (UUID PK), `name` (display name), `host`, `port` (int, default 389/636), `base_dn`, `cn_identifier` (e.g. `SAMAccountName`, `cn`, `uid`), `bind_type` (enum: `anonymous`, `simple`, `regular`), `bind_dn` (nullable), `bind_password` (Fernet-encrypted, nullable), `use_ssl` (bool), `is_active` (bool)
- Org-level — one row, shared across all users

**New DB table — `LDAPGroup`:**
- `id`, `ldap_server_id` FK (ON DELETE CASCADE), `group_dn` (full DN of the AD group), `label` (display name), `role` (given to auto-provisioned users), `is_active`
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
- Shows: username, auth type badge, role, time since login (from the session key's TTL). No IP address
- **Kick** button per row — deletes `redis_session:<sid>` + `user_session:<user_id>` from Redis (same logic as existing terminate session)
- Replaces the per-user terminate button in user management (or keeps both)
- New sidebar entry under **Access** section in admin panel

---

**Python dependency:** `ldap3~=2.9.1` — pure Python, no C extensions, Docker-friendly. Added to `requirements.txt`.

**Alembic migrations:**
- `a19370a56122_add_ldap` — `ldap_servers`, `ldap_groups` tables; `auth_type` + `ldap_server_id` on `users`; `password_hash` made nullable
- `5c2b80c49fc9_nullable_user_email_fullname` — `email` + `full_name` nullable (LDAP users have no local registration)

**`src/ldap_auth.py`** — new module: `make_server`, `service_bind`, `user_bind`, `test_connection`, `test_user`, `check_group_membership`, `fetch_user_details`, `fetch_base_dn`, `walk_tree`. Error handling (hardened in Step 1b): bad credentials (`LDAPBindError`, `LDAPInvalidCredentialsResult`) are a normal "no"; any other LDAP exception becomes `LdapUnavailable` ("LDAP authentication service unavailable"). Later helpers: `constructed_dn`, `user_filter`, `find_user_dn`, `authenticate` (search-then-bind). Consistent `{"status": "ok/error", ...}` response shape throughout.

**Login flow** — extended with two new branches:
- Existing LDAP user: `user_bind` → check `is_approved`/`is_active` → `login_user()` or flash
- Unknown user: `check_group_membership` against all active groups → auto-create `User(auth_type="ldap")` → `login_user()`

**LDAP routes** (today in `src/webapp/blueprints/admin_servers.py`; 12 routes under `/admin/server/ldap`): GET list, new, save, delete, test, test_user, fetch_dn, explore (walk_tree), import (users + groups), groups list, group toggle, group delete.

**`templates/server_management.html`** — full LDAP card replacing locked stub: server list with inline expand/collapse edit panels, bind type selector (two styled option cards), LDAPS toggle (auto-flips 389↔636), Test/Test User/Fetch DN/Save/Delete per server. Explorer modal (modal-xl) with DOM-built tree browser, breadcrumb nav, selected panel, import with summary. All Explorer JS uses `createElement` + `addEventListener` + `data-dn` + `CSS.escape` — no inline onclick injection.

**`templates/admin_users.html`** — LDAP badge on LDAP users; Group Rules section at bottom (AJAX-loaded, toggle/delete per rule).

**`templates/live_sessions.html`** — new page under Access section: two cards (Local / Remote LDAP), session table with elapsed time (Jinja2 macro), kick/kick-all JS, updateCounts on kick.

**`templates/admin.html`** — Live Sessions sidebar link added under Access section (`bi-activity` icon, `active_section="sessions"`).

**Live Sessions routes:** `GET /admin/sessions` (scans `user_session:*` Redis keys, TTL→elapsed math, local/ldap split), `POST /admin/sessions/<uuid>/kick` (deletes `redis_session:<sid>` + `user_session:<uid>`, emits audit log).

### 4.9c Codebase Cleanup (post-LDAP)
See "Step 1" in the Remaining Work section above — detailed there.

### 4.10 Documentation → Phase 4 stage 11
- `README.md` — project overview, quick start (the Phase 4 installer and release zip), CLI usage (incl. the `.exe`), CSV format reference, backup/restore, update, certificates, security posture section; `docs/deployment.md` for VM prerequisites
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
