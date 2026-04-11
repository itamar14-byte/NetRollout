# NetRollout — Architecture Document
_Written: 2026-04-07 — Updated: 2026-04-11 (Inventory UI + schema updates)_

---

## 1. Overview

NetRollout is structured around five layers:

1. **Data classes** — pure runtime objects, no DB coupling
2. **ORM models** — DB schema, all anchored to `User` as the master table
3. **Service classes** — business logic, validation, parsing, logging
4. **Job execution classes** — orchestration, pipeline, concurrency
5. **Webapp layer** — Flask routes, thin delegation to orchestrator

The central design principle is that `User` is the master table. All persistent entities — devices, credentials, variable mappings, active jobs, and result history — are owned by a user via foreign key relationships.

At runtime, `RolloutOrchestrator` is the concurrency manager — it owns the active jobs dict, coordinates multithreading, and keeps the DB and in-memory state in sync. `RolloutJob` is the lifecycle owner for a single job — it owns the thread, cancel event, logger, and engine. All execution context flows as arguments at call time — no hanging state on `RolloutEngine`.

**Configuration** (`db_install.py` install script asks for these at setup, falls back to documented defaults if skipped):
- `NETROLLOUT_ENCRYPTION_KEY` — Fernet key for credential encryption. Default: auto-generated, written to `~/.netrollout/encryption.key`
- `MAX_CONCURRENT_JOBS` — orchestrator thread concurrency limit. Default: `4`
- `DATABASE_URL` — PostgreSQL connection string. Default: `postgresql+psycopg2://dbadmin:Pass123@localhost:5432/rollout_db`
- `SECRET_KEY` — Flask session key. Default: auto-generated via `secrets.token_urlsafe(32)`

---

## 2. Data Classes

Data classes are pure Python objects with no SQLAlchemy coupling. They exist at runtime only.

---

### `RolloutOptions`
Configuration flags for a rollout run. Pure data, no behavior.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `verify` | `bool` | Run post-push verification via NAPALM |
| `verbose` | `bool` | Print progress to console (CLI mode) |
| `webapp` | `bool` | Enqueue log messages for SSE stream |

---

### `Device`
Represents a single network device at runtime. Constructed from an `Inventory` row via `from_inventory()`.

**Public attributes:**
| Name | Type | Description |
|---|---|---|
| `ip` | `str` | Device IPv4 address |
| `username` | `str` | SSH username (decrypted at construction) |
| `password` | `str` | SSH password (decrypted at construction) |
| `device_type` | `str` | Netmiko platform string |
| `secret` | `str` | Enable secret (decrypted at construction) |
| `port` | `int` | SSH port |
| `label` | `str` | Friendly name from inventory |
| `extra` | `dict` | Optional per-device attributes for `$$TOKEN$$` substitution. Populated from `Inventory.var_maps`. Defaults to `{}` if not set. |

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `from_inventory` | `cls(row: Inventory) -> Device` | Factory. Constructs Device from Inventory row, decrypting credentials from the assigned SecurityProfile |
| `fetch_config` | `(logger: RolloutLogger) -> str \| None` | Opens NAPALM connection, returns running config string. Used by `RolloutEngine._verify()` |

**Public methods (kept public):**
| Method | Signature | Description |
|---|---|---|
| `netmiko_connector` | `() -> dict` | Builds Netmiko `ConnectHandler` params dict. Called by `RolloutEngine._push_config()`. Kept public — private would be bad practice when called from a different class |

---

## 3. ORM Models

All ORM models live in `src/tables.py`. All use UUID primary keys. `User` is the master table — every other table has a foreign key back to it.

---

### `User`
Master table. Anchor of the entire data model. Owns all persistent entities.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key, non-sequential |
| `username` | `str(64)` | Unique, indexed |
| `password_hash` | `str(255)` | pbkdf2:sha256 hash |
| `email` | `str(120)` | Unique |
| `full_name` | `str(120)` | |
| `role` | `str(40)` | `"user"` or `"admin"` |
| `position` | `str(64)` | Nullable |
| `is_active` | `bool` | False by default |
| `is_approved` | `bool` | False by default |
| `otp_secret` | `str(32)` | Nullable — null means unenrolled |
| `created_at` | `DateTime` | Set at creation |

**Relationships:**
| Name | Target | Description |
|---|---|---|
| `inventory` | `[Inventory]` | User's saved devices |
| `security_profiles` | `[SecurityProfile]` | User's credential profiles |
| `variable_mappings` | `[VariableMapping]` | User's `$$VAR$$` token definitions |
| `sessions` | `[RolloutSession]` | Active/pending rollout jobs |
| `results` | `[DeviceResult]` | Completed rollout archive |

---

### `Inventory`
Per-user device store. Stores device topology only — no credentials. Credentials are assigned via `SecurityProfile`.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `sec_profile_id` | `UUID` | FK → `SecurityProfile`, nullable |
| `ip` | `str` | Device IPv4 address |
| `device_type` | `str` | Netmiko platform string |
| `port` | `int` | SSH port |
| `label` | `str` | Friendly name, nullable |
| `var_maps` | `JSON` | Nullable. Per-device optional attributes dict. v1.0 keys: `hostname`, `loopback_ip`, `asn`, `mgmt_vrf`, `mgmt_interface`, `site`, `domain`, `timezone` (strings), `vrfs` (list of strings — positional substitution via `VariableMapping.index`). |

**Relationships:**
| Name | Target | Description |
|---|---|---|
| `security_profile` | `SecurityProfile` | Assigned credential profile |

---

### `SecurityProfile`
Per-user encrypted credential store. One profile can be assigned to many inventory devices.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `label` | `str(64)` | Friendly name, nullable — falls back to username in UI |
| `username` | `str(64)` | Plaintext — not a secret |
| `password_secret` | `str(255)` | Fernet-encrypted password |
| `enable_secret` | `str(255)` | Fernet-encrypted enable secret, nullable |

**Encryption:** Fernet (AES-128-CBC) applied to `password_secret` and `enable_secret` only. `username` is stored plaintext. Key loaded from `NETROLLOUT_ENCRYPTION_KEY` environment variable. If absent, a key is generated at startup and written to `~/.netrollout/encryption.key`. The user is responsible for securing this file.

**Delete safety:** deletion is blocked at the route level if any `Inventory` rows reference this profile (`profile.inventory` non-empty). User must reassign or delete those devices first.

---

### `VariableMapping`
Per-user store of `$$TOKEN$$` → Device property name mappings. Used by the variable substitution system (Phase 3).

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `token` | `str` | Free-text token string, e.g. `$$HOSTNAME$$`. Name is user's choice — no system meaning |
| `property_name` | `str` | Key in `device.extra` (or core Device field name) |
| `index` | `int \| None` | Nullable. `None` = simple string substitution. `N` = positional element of a list attribute (`device.extra[property_name][N]`). Validator checks `len >= index + 1` at rollout time. |

---

### `RolloutSession`
The "RAM" table. Tracks active and pending rollout jobs. The webapp job manager treats this as its work docket. Rows are deleted on job completion or cancellation.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key — maps to `RolloutJob.id` in memory |
| `user_id` | `UUID` | FK → `User` |
| `status` | `str` | `pending` / `active` / `cancelling` |
| `created_at` | `DateTime` | |

---

### `DeviceResult`
The "MEMORY" table. Long-term archive of completed rollout outcomes, one row per device per job. No FK to `RolloutSession` — the session row is deleted by the time results are written. `job_id` is a soft reference for grouping.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `job_id` | `UUID` | Soft reference to the originating session |
| `started_at` | `DateTime` | When the job started |
| `completed_at` | `DateTime` | When the job finished |
| `device_ip` | `str` | |
| `device_type` | `str` | |
| `commands_sent` | `int` | |
| `commands_verified` | `int \| None` | Null if verify was not run |
| `status` | `str` | `success` / `partial` / `failed` / `cancelled` |

---

## 4. Service Classes

---

### `Validator`
Wraps all input validation logic. Takes a `RolloutLogger` instance — methods that produce user-facing error messages are instance methods; pure computation methods remain static.

**Constructor:**
```
Validator(logger: RolloutLogger)
```

**Instance methods:**
| Method | Signature | Description |
|---|---|---|
| `validate_device_data` | `(device: dict) -> bool` | Runs ip + port + platform checks, logs failures |
| `validate_file_extension` | `(path: str, ext: str) -> bool` | File exists and has correct extension, logs failures |

**Static methods:**
| Method | Signature | Description |
|---|---|---|
| `validate_ip` | `(ip: str) -> bool` | Valid IPv4 address |
| `validate_port` | `(port: str) -> bool` | Integer in 1–65535 range |
| `validate_platform` | `(platform: str) -> bool` | In supported platforms set |
| `test_tcp_port` | `(ip: str, port: int) -> bool` | TCP reachability probe, 3 attempts |

_Note: original design had all methods static. Logger injection required promoting two methods to instance methods._

---

### `InputParser`
Inventory is the single source of truth for rollout. CSV upload and web form are import mechanisms that populate the `Inventory` table — rollout always runs from inventory. Injected with a `Validator` and `RolloutLogger` instance.

**Constructor:**
```
InputParser(validator: Validator, logger: RolloutLogger)
```

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `csv_to_inventory` | `(device_path: str, user_id: UUID, db_session: Session) -> list[Device]` | Validates CSV rows, writes to `Inventory` table |
| `form_to_inventory` | `(devices_json: str, user_id: UUID, db_session: Session) -> list[Device]` | Validates web form/JSON devices, writes to `Inventory` table |
| `import_from_inventory` | `(inventory: list[Inventory]) -> list[Device]` | Static. Single rollout path. Constructs Device objects from user's saved inventory rows via `Device.from_inventory()` |
| `parse_commands` | `(commands_path: str) -> list[str]` | Reads command file, returns list of strings |

**Private methods:**
| Method | Signature | Description |
|---|---|---|
| `_prepare_devices` | `(raw_devices: list[dict]) -> list[Device]` | Validates and constructs Device objects. Shared by import methods |

_Note: original design had `import_from_csv`, `import_from_form`, `from_inventory` as names, and no logger in constructor. Names updated to better reflect intent. Logger added for import-path notifications._

---

### `RolloutLogger`
Owns all logging I/O for a single rollout job. One instance per `RolloutJob`. Replaces `logging_utils.py` module-level globals.

**Constructor:**
```
RolloutLogger(webapp: bool, verbose: bool, logfile: str = None)
```

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `queue` | `Queue` | SSE message queue, consumed by `sse_stream` |
| `logfile` | `str` | Timestamped log file path |
| `webapp` | `bool` | Enqueue messages for SSE instead of printing |
| `verbose` | `bool` | Surface non-error messages |

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `log` | `(string: str) -> None` | Writes timestamped message to log file |
| `notify` | `(string: str, color: str) -> None` | Routes message to queue (webapp) or console (CLI). Red always surfaces; others only if verbose |
| `get` | `() -> str` | Blocking get from queue, consumed by `sse_stream` |

_Note: original design had `RolloutLogger(logfile: str)` only. `webapp` and `verbose` moved here from `RolloutOptions` — logger owns output routing, engine only needs `verify` flag._

---

## 5. Job Execution Classes

---

### `RolloutOrchestrator`
Concurrency manager and single source of truth for active jobs. Singleton instantiated at app startup. Owns the in-memory jobs dict and keeps it in sync with `RolloutSession` in the DB. The webapp delegates all job lifecycle operations to it — routes are thin.

**Constructor:**
```
RolloutOrchestrator(max_concurrent: int = MAX_CONCURRENT_JOBS)
```

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `_jobs` | `dict[UUID, RolloutJob]` | Private. In-memory registry of all active and pending jobs |
| `max_concurrent` | `int` | Maximum simultaneously executing jobs |
| `_lock` | `Lock` | Protects `_jobs` from concurrent access |

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `submit` | `(devices, commands, params: RolloutOptions, user_id: UUID) -> UUID` | Builds engine + job internally, adds to `_jobs`, writes `RolloutSession(status="pending")`, calls `_dispatch()`, returns `job_id` |
| `cancel` | `(job_id: UUID) -> None` | Looks up job, calls `job.cancel()`, sets `RolloutSession.status = "cancelling"` |
| `get` | `(job_id: UUID) -> RolloutJob \| None` | Returns job from dict — used by SSE stream and status endpoint |

**Private methods:**
| Method | Signature | Description |
|---|---|---|
| `_dispatch` | `() -> None` | Snapshots state under lock, then starts pending jobs outside lock up to `max_concurrent`. Updates `RolloutSession.status = "active"`. Calls `job.start(self._cleanup)` |
| `_cleanup` | `(job_id: UUID) -> None` | Called on job completion via callback. Removes from `_jobs`, writes `DeviceResult` rows, deletes `RolloutSession`, calls `_dispatch()` |

_Note: original design had `submit(job: RolloutJob)`. Revised: orchestrator builds engine + job internally from raw inputs. `jobs` dict made private (`_jobs`)._

**Dispatch loop:**
```
submit(job)
  → write RolloutSession(status="pending")
  → _dispatch()
       → active_count < max_concurrent?
            yes → job.start(), RolloutSession(status="active")
            no  → job waits in dict

job completes
  → _cleanup(job_id)
       → delete RolloutSession
       → write DeviceResult rows
       → _dispatch()   # slot freed, start next pending job
```

---

### `RolloutEngine`
Owns the network pipeline — push and verify. Pure pipeline object. Receives execution context as arguments at call time, not at construction.

**Constructor:**
```
RolloutEngine(param: RolloutOptions, devices: list[Device], commands: list[str])
```

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `_verify_flag` | `bool` | Extracted from `param.verify`. Only flag engine needs — `webapp`/`verbose` live in logger |
| `devices` | `list[Device]` | Devices to configure |
| `commands` | `list[str]` | Commands to push |

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `run` | `(cancel_event: Event, logger: RolloutLogger) -> list[DeviceResultDict]` | Entry point. Calls `_push_config`, optionally `_verify`. Returns structured per-device results |

**Private methods:**
| Method | Signature | Description |
|---|---|---|
| `_push_config` | `(cancel_event: Event, logger: RolloutLogger) -> tuple[str \| None, dict[str, bool]]` | Iterates devices, opens Netmiko, sends commands. Returns cancel signal + per-device success map |
| `_verify` | `(logger: RolloutLogger) -> dict[str, int]` | Iterates devices, calls `device.fetch_config(logger)`, compares to commands. Runs to completion — not cancellable |

---

### `RolloutJob`
Lifecycle owner for a single rollout execution. Owns the thread, cancel flag, engine, and logger. Created by `RolloutOrchestrator.submit()` and stored in the active jobs registry.

**Constructor:**
```
RolloutJob(job_id: UUID, user_id: UUID, engine: RolloutEngine, options: RolloutOptions)
```
Constructs its own `RolloutLogger` from `options.webapp` and `options.verbose` internally.

**Attributes:**
| Name | Type | Description |
|---|---|---|
| `job_id` | `UUID` | Maps to `RolloutSession.id` in DB |
| `user_id` | `UUID` | FK owner — used by `_cleanup` to write `DeviceResult` rows |
| `started_at` | `datetime \| None` | Set in `start()`, always populated before `_cleanup` runs |
| `results` | `list[DeviceResultDict]` | Populated by `engine.run()`, consumed by `_cleanup` |
| `_thread` | `Thread` | Private. Background execution thread |
| `_cancel_flag` | `Event` | Private. Cancellation signal — owned here, passed to engine at call time |
| `_engine` | `RolloutEngine` | Private. The pipeline |
| `_logger` | `RolloutLogger` | Private. Constructed internally from options |

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `start` | `(on_complete: Callable[[UUID], None]) -> None` | Creates thread via closure, starts it. Thread calls `engine.run(cancel_flag, logger)` then fires `on_complete(self.id)` |
| `cancel` | `() -> None` | Sets `cancel_flag`. Engine polls this during iteration |
| `is_alive` | `() -> bool` | Returns whether thread is running |
| `is_pending` | `() -> bool` | Returns whether thread has not started yet |

_Note: original design had `RolloutJob(id, engine, logger)` and `start()` with no args. Revised: job takes `options` and constructs logger internally. `start()` takes `on_complete` callback to notify orchestrator on completion — avoids circular import. `_thread` privatized._

---

## 6. Relationship Map

```
                        ┌─────────────────────────────────┐
                        │              User                │
                        │  id, username, role, otp_secret  │
                        └──────────────┬──────────────────┘
                                       │ owns (FK)
          ┌──────────────┬─────────────┼──────────────┬──────────────┐
          ▼              ▼             ▼              ▼              ▼
    ┌──────────┐  ┌─────────────┐  ┌────────┐  ┌──────────┐  ┌──────────────┐
    │Inventory │  │SecurityProf.│  │Variable│  │Rollout   │  │DeviceResult  │
    │          │  │             │  │Mapping │  │Session   │  │(archive)     │
    │ip        │  │label        │  │        │  │          │  │              │
    │device_typ│  │username     │  │token   │  │status    │  │job_id (soft) │
    │port      │  │password(enc)│  │prop_   │  │created_at│  │device_ip     │
    │label     │  │secret (enc) │  │name    │  │          │  │status        │
    │profile_id│  └─────────────┘  └────────┘  └──────────┘  │cmds_sent    │
    └────┬─────┘        ▲                                      │cmds_verified│
         │              │ assigns                              └──────────────┘
         └──────────────┘


                    RUNTIME (not persisted)
                    ───────────────────────

    ┌─────────────────────────────────────────────────────┐
    │                    RolloutJob                        │
    │  id, thread, cancel_event                           │
    │                                                     │
    │   ┌───────────────┐      ┌─────────────────────┐   │
    │   │ RolloutLogger │      │    RolloutEngine     │   │
    │   │               │      │                     │   │
    │   │ queue         │      │ param               │   │
    │   │ logfile       │      │ devices             │   │
    │   │               │      │ commands            │   │
    │   │ log()         │      │                     │   │
    │   │ notify()      │      │ run(cancel, logger) │   │
    │   │ get()         │      │ _push_config()      │   │
    │   └───────────────┘      │ _verify()           │   │
    │          ▲               └──────────┬──────────┘   │
    │          │ injected at call time    │ uses          │
    │          └──────────────────────────┘              │
    └─────────────────────────────────────────────────────┘
              │                        │
              ▼                        ▼
       sse_stream()              Device objects
       consumes queue            (built from Inventory
                                  via from_inventory())


    RolloutOrchestrator (singleton, app startup):
    ┌──────────────────────────────────────────┐
    │  jobs: { job_id: RolloutJob }            │
    │  max_concurrent: int                     │
    │                                          │
    │  submit() ──► write RolloutSession (DB)  │
    │               _dispatch()                │
    │                                          │
    │  _dispatch() ─► job.start() if slot free │
    │                 update RolloutSession     │
    │                                          │
    │  _cleanup() ─► delete RolloutSession     │
    │                write DeviceResult rows   │
    │                _dispatch() (next job)    │
    └──────────────────────────────────────────┘

    webapp.py routes (thin):
    POST /start_rollout  →  orchestrator.submit(job)
    POST /cancel_rollout →  orchestrator.cancel(job_id)
    GET  /rollout_stream →  orchestrator.get(job_id).logger.get()
    GET  /rollout_status →  orchestrator.get(job_id)


    InputParser ──uses──► Validator
    InputParser ──produces──► (list[Device], list[str])
                               └──► RolloutEngine constructor
```

---

## 7. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `cancel_event` ownership | `RolloutJob` | Lifecycle concern, not logging or pipeline |
| Execution context passing | Arguments at call time | No hanging state on `RolloutEngine` |
| Credential storage | `SecurityProfile` table, encrypted | Separates topology from credentials, one profile → many devices |
| Encryption key source | Env var → fallback to `~/.netrollout/encryption.key` | User owns their security posture |
| `Device` construction | `from_inventory()` factory | Single boundary where decryption happens |
| Results schema | One row per device per job | Enables per-device analytics and audit via SQL aggregation |
| `RolloutOrchestrator` | Singleton at app startup | Single owner of concurrency logic — webapp routes delegate to it |
| `RolloutOrchestrator._dispatch()` | Called on submit and cleanup | Fills available slots automatically, no polling needed |
| `RolloutSession` lifetime | Deleted on completion | Keeps the "RAM" table small; archive lives in `DeviceResult` |
| Config/env vars | Asked at install time via `db_install.py`, defaults provided | User controls their own environment; no hardcoded secrets in code |
| `Validator` design | Logger-injected instance class | `validate_device_data` and `validate_file_extension` need logger to surface errors; pure computation methods stay static |
