# Development Workplan
_Last updated: 2026-04-05_

---

## Phase 1 — DB Foundation & Auth

### 1.1 Complete DB schema
`tables.py` has `User`. Need two more models:
- `RolloutSession` — one row per rollout run (timestamp, status, initiated_by)
- `DeviceResult` — one row per device per session (ip, device_type, commands_sent, commands_verified, status)

Wire `RolloutEngine.run()` to open a `RolloutSession` at start and write `DeviceResult` rows as devices complete.

### 1.2 Password hashing
`User.password_hash` exists but nothing hashes yet. Use `werkzeug.security` (`generate_password_hash` / `check_password_hash`) — already a Flask dependency, no new install needed.

### 1.3 Auth flow — backend
Add routes: `POST /register`, `POST /login`, `GET /logout`.
Use Flask sessions (`secret_key`) to track the logged-in user.
Protect `/upload`, `/start_rollout`, `/rollout`, `/rollout_stream` with a login required decorator.

### 1.4 Auth flow — frontend
Add login and register pages (templates).
Redirect unauthenticated users to login.
Show logged-in username in the nav.

---

## Phase 2 — Concurrency & OOP Refactor

These two are coupled — do them together.

### 2.1 `RolloutJob` object
Replace the module-level `cancel_event` and `LOG_QUEUE` globals in `webapp.py` with a per-job object:
```
RolloutJob
  id: str                  # maps to RolloutSession.id in DB
  cancel_event: Event
  log_queue: Queue
  engine: RolloutEngine
  thread: Thread
```
The webapp stores active jobs in a dict keyed by job ID.
SSE stream consumes from `job.log_queue`, not a global queue.
Cancel hits `job.cancel_event`.
When `run()` finishes it pushes a done sentinel to `job.log_queue` — SSE stream terminates cleanly (fixes medium bug).

### 2.2 `RolloutLogger` class
`logging_utils.py` currently uses module-level globals (`LOG_QUEUE`, `LOGFILE`, `BASEDIR`).
Refactor into a `RolloutLogger` class that owns a queue and a log file path.
One `RolloutLogger` instance per `RolloutJob`, injected into `RolloutEngine`.
Replaces the current `base_notify` / `log` / `notify` fragmentation.

### 2.3 Remaining OOP gaps
Once `RolloutLogger` is injected into `RolloutEngine`, clean up the rest:
- `push_config()`, `verify()`, `notify()` on `RolloutEngine` → prefix `_`
- `netmiko_connector()` on `Device` → `_netmiko_connector()`
- `validation.py` standalone functions → wrap in a `Validator` class
- `parse_files()` / `prepare_devices()` → move into an `InputParser` class

---

## Phase 3 — Polish & Testing

- Extend test suite to cover auth routes (Flask test client)
- Add tests for `RolloutJob`, `RolloutLogger`, and the refactored OOP classes
- Update tests that currently patch module-level globals — they'll need to target the new class instances
- Address remaining minor bugs from `docs/bug_report.md`

---

## Order rationale
Phase 1 first — auth and DB are user-visible features and are mostly independent of the refactor.
Phase 2 second — the concurrency/OOP refactor touches `RolloutEngine.run()` which Phase 1 also wires into, so do Phase 1 DB wiring first to avoid double-touching that method.
Phase 3 last — test updates follow the code changes naturally.
