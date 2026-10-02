# NetRollout — Architecture Document
_Written: 2026-04-07 — Updated: 2026-10-02 (Phase 4 stages 3–4b: container runtime, drain, health; passwords; per-platform push + verify)_

Deployment and packaging (Docker images, compose, installer, platform profiles) are planned in `docs/plans/phase-4.md`. Where that plan will change something described here, the section says so.

---

## 1. Overview

NetRollout is structured around six layers:

1. **Data classes** — pure runtime objects, no DB coupling
2. **ORM models** — DB schema; per-user data anchored to `User`
3. **Service classes** — business logic, validation, parsing, logging, reachability
4. **Job execution classes** — orchestration, pipeline, concurrency
5. **DB layer** — connection management, session lifecycle, hot-reload, install/seed, System Settings
6. **Webapp layer** — Flask app factory, extensions, blueprints, shared helpers, startup check

**Ownership.**
- Per-user data belongs to a `User` through a foreign key: devices, security profiles, variable mappings, property definitions, results and job metadata.
- Global devices (`Inventory.is_global`) are the exception. All users can see them and roll out to them, but only admins can edit or delete them.
- Org-level data belongs to no user: LDAP servers and groups, and System Settings. The audit log keeps its entries when a user is deleted (`actor_id` becomes NULL and the username stays, denormalized).

At runtime, `RolloutOrchestrator` is the concurrency manager.
- **The web app:** it owns the in-memory jobs dict and a permanent dispatcher thread. Redis holds the queue and the ephemeral job state.
- **`RolloutJob`:** the lifecycle owner of a single job. It owns the thread, the cancel event, the logger and the engine.
- **`RolloutEngine`:** execution context flows into it as arguments at call time, so it holds no hanging state.

The **CLI** (`src/cli.py`) uses the same `RolloutEngine` directly, from a devices CSV and a commands file. It has no database, no Redis and no orchestrator — and doesn't even import them: the DB models and Redis are imported for type checking only (`core.py`, `logging_utils.py`) or inside the web-only CSV import (`input_parser.py`), guarded by an import-isolation test. It ships as a standalone `netrollout-cli.exe` (PyInstaller, `netrollout-cli.spec`, the web stack excluded; logs next to the exe via `runtime.logs_dir()`).

**Configuration** comes from env vars. `config/runtime.env` (under the NetRollout home, `src/runtime.py`) is loaded with override by `BackendServices` at startup; it holds only what a Server Management switch wrote, so it wins over the container environment (the installer's `.env`), which wins over the defaults.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` or `PG_HOST` / `PG_PORT` / `PG_NAME` / `PG_USER` / `PG_PASSWORD` / `PG_SCHEMA` | Postgres. The URL form takes precedence |
| `REDIS_URL` or `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis. The URL form takes precedence |
| `SECRET_KEY` | Flask session key. Required in a container (startup refuses without it); in development a missing key becomes a random per-run key with a warning |
| `PORT` | Internal app port Waitress listens on (default 8080). nginx forwards to it |
| `NETROLLOUT_ENCRYPTION_KEY` | Fernet key; else `~/.netrollout/encryption.key` (see below) |
| `ORCHESTRATOR_WORKERS`, `NETROLLOUT_PUBLIC_HOSTNAME`, `NETROLLOUT_HTTPS_PORT` | **Install-time seeds only** for the matching System Settings (§6). They are read when the setting's row doesn't exist yet and ignored after that |
| `NETROLLOUT_OPEN_BROWSER` | `0` disables opening the browser on a desktop launch |
| `NETROLLOUT_DEPLOYMENT` | `docker` (set by the image) turns on container behaviour (`src/runtime.py`): secrets required, Restart via the restart policy, no startup proxy probe |
| `NETROLLOUT_DRAIN_SECONDS` | How long a stop / Restart waits for running rollouts before cancelling them (default 600; compose's `stop_grace_period` must be longer) |
| `NETROLLOUT_HOME` | Base folder for `logs/`, `config/`, `certs/` (`src/runtime.py`; the image uses `/data`) |

**Encryption key.** Fernet protects security-profile passwords and enable secrets, LDAP bind passwords and OTP secrets.
- **When no key exists:** one is generated only on a fresh install, meaning the DB is reachable and holds no encrypted data — and never in a container, where the key must come from `NETROLLOUT_ENCRYPTION_KEY` (a file inside the container would vanish with it at the next update).
- **Startup check:** the key is test-decrypted against one stored value.
- **When the app refuses to start** (`EncryptionStartupError`, a `StartupError`: a readable message and no traceback):
  - the key is malformed;
  - the key is missing while encrypted data exists;
  - the key doesn't match the stored data;
  - there is no key while the DB is unreachable.

---

## 2. Data Classes (`src/core.py`)

Data classes are pure Python objects with no SQLAlchemy coupling. They exist at runtime only.

### `RolloutOptions`
Configuration flags for a rollout run. Pure data, no behavior.

| Name | Type | Description |
|---|---|---|
| `verify` | `bool` | Run post-push verification |
| `verbose` | `bool` | Print progress to console (CLI mode) |
| `webapp` | `bool` | Publish log messages to Redis for the SSE stream |
| `max_workers` | `int` | Devices pushed in parallel within this job (default 10; the web app passes the *Devices in parallel per job* System Setting) |

### `Device`
Represents a single network device at runtime. The web app builds it from an `Inventory` row via `from_inventory()`; the CLI builds it from a CSV row.

| Name | Type | Description |
|---|---|---|
| `ip` | `str` | Device address |
| `label` | `str` | Friendly name |
| `username` | `str` | SSH username |
| `password` | `str` | SSH password (decrypted at construction, hidden from `repr`) |
| `device_type` | `str` | Netmiko platform string |
| `secret` | `str` | Enable secret (decrypted, hidden from `repr`) |
| `port` | `int` | SSH port |
| `var_map_subs` | `dict` | `$$TOKEN$$` → `(property_name, index)` — only the rolling-out user's mappings |
| `extra` | `dict` | Per-device attribute values for substitution, from `Inventory.var_maps` |

`endpoint` (property) — `ip:port`. This is what identifies a target: with NAT or port forwarding, several devices share one IP.

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `from_inventory` | `cls(row: Inventory, user_id) -> Device` | Factory. Decrypts the assigned SecurityProfile's credentials. It raises if the device has no profile |
| `netmiko_connector` | `() -> dict` | Builds the Netmiko `ConnectHandler` params |
| `fetch_config` | `(logger) -> str \| None` | The running config over Netmiko (the push's SSH: the device's port and credentials), printed by the platform's `show_config` command(s) in the syntax engineers type. None if it can't be fetched (logged) |

### `DeviceResultDict`
TypedDict returned per device by `RolloutEngine.run()`. Fields: `device_ip`, `device_port`, `device_type`, `commands_sent`, `commands_verified`, `fetched_config`, `status`.

---

## 3. ORM Models (`src/db/tables.py`)

All models use UUID primary keys except `SystemSetting`, whose key is the setting name. The schema is managed by Alembic (`src/db/alembic/versions`: the `v1_0_0_baseline` revision, then `must_change_password`, `device_results_action_needed`; from v1.0.0 on, schema changes are new revisions on top); the app applies migrations itself at every start.

### `User`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `username` | `str(64)` | Unique, indexed |
| `password_hash` | `str(255)` | Nullable — null for LDAP users |
| `email` | `str(120)` | Unique, nullable |
| `full_name` | `str(120)` | Nullable |
| `role` | `str(40)` | `"user"` or `"admin"` |
| `position` | `str(64)` | Nullable |
| `is_active` | `bool` | Default False |
| `is_approved` | `bool` | Default False |
| `otp_secret` | `str(255)` | Fernet-encrypted TOTP secret. Null = not enrolled |
| `must_change_password` | `bool` | Default False. Set for the seeded admin and by an admin password reset: every page redirects to `/account/password` until the user picks a password |
| `auth_type` | `str(20)` | `"local"` or `"ldap"` |
| `ldap_server_id` | `UUID` | FK → `LDAPServer`, ON DELETE SET NULL, nullable |
| `created_at` | `DateTime` | Set at creation |

**Relationships:** `inventory`, `security_profiles`, `variable_mappings`, `property_definitions`, `results`, `job_metadata` (all cascade-delete with the user), `ldap_server`

The factory account `admin`/`admin` is seeded at startup if missing (`db_install.py`), with `must_change_password` set: its first sign-in forces a new password.

### `Inventory`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` (owner) |
| `sec_profile_id` | `UUID` | FK → `SecurityProfile`, nullable. A device without a profile can't be rolled out |
| `ip` | `str(64)` | Device address |
| `device_type` | `str(64)` | Netmiko platform string |
| `port` | `int` | SSH port |
| `label` | `str(64)` | Friendly name, required |
| `is_global` | `bool` | Visible to and rollout-able by all users; only admins edit or delete it |
| `var_maps` | `JSON` | Per-device attribute values, keyed by property name: the system properties (`SYSTEM_PROPERTIES` in `src/webapp/utils.py` — hostname, loopback_ip, asn, mgmt_vrf, mgmt_interface, site, domain, timezone, vrfs) plus the owner's user-defined `PropertyDefinition`s. List properties hold lists |

**Relationships:** `var_mappings` (many-to-many via `var_mapping_to_devices`), `security_profile`, `user`

Several devices may share an `ip:port` (e.g. entries by different users). Inventory and New Rollout show a warning; nothing is blocked.

### `SecurityProfile`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `label` | `str(64)` | Nullable; the UI falls back to the username |
| `username` | `str(64)` | Plaintext |
| `password_secret` | `str(255)` | Fernet-encrypted |
| `enable_secret` | `str(255)` | Fernet-encrypted, nullable |

The routes block deleting a profile while any `Inventory` row references it. CSV import can create profiles from credential columns: an exact match reuses an existing profile, and anything else creates a new, uniquely labelled one.

### `VariableMapping`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `label` | `str(64)` | Nullable |
| `token` | `str(64)` | Free-text token, e.g. `$$HOSTNAME$$`. Unique per user |
| `property_name` | `str(64)` | Key in `device.extra` |
| `index` | `int \| None` | Null = string substitution; N = `device.extra[property_name][N]` |

**Relationships:** `devices` — many-to-many via `var_mapping_to_devices`. The join table is shared across users because of global devices, so `Device.from_inventory` applies only the rolling-out user's mappings. A mapping whose device lacks the property fails that device with a clear message (`SubstitutionError`).

### `PropertyDefinition`
User-managed device attribute definitions that extend the keys available in `var_maps`. Unique `(name, user_id)`.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `name` | `str(64)` | Internal key name |
| `label` | `str(64)` | Display label |
| `icon` | `str(64)` | Bootstrap Icons class |
| `is_list` | `bool` | Whether the value is a list (enables index-based substitution) |

### `DeviceResult`
Result archive, with one row per device per job. `job_id` is a soft reference: there is no job table.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `job_id` | `UUID` | Soft ref for grouping results by job |
| `started_at` / `completed_at` | `DateTime` | |
| `device_ip` | `str(64)` | |
| `device_port` | `int` | Server default 22 for rows from before the column existed |
| `device_type` | `str(64)` | |
| `commands_sent` | `int` | |
| `commands_verified` | `int \| None` | Commands confirmed, plus those that can't be checked (navigation, operational). Null if verify didn't run or the config couldn't be fetched |
| `status` | `str` | `success` / `partial` / `failed` / `cancelled` |
| `action_needed` | `TEXT` | Nullable. What only a person can resolve on the device after the rollout (the `ACTION NEEDED` lines), shown on the Results page: a badge on the job row, the instructions at the top of the expanded job, a marker on the device |
| `fetched_config` | `TEXT` | Running config captured by Verify. Stored only when some commands didn't verify, which feeds the Results page's side-by-side diff. Cleared after the *Config snapshots* retention (default 7 days); the Results page then shows "Verify Diff expired" |

Rows are deleted after the *Job records* retention (default 30 days).

### `JobMetadata`
The pre-substitution command list and the optional comment for each job, written at submit time. It is deleted together with its job's results: the *Job records* retention applies, and a row is removed only once its job's `device_results` are gone.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `job_id` | `UUID` | Soft ref |
| `commands` | `JSON` | Raw command list before variable substitution |
| `comment` | `str(255)` | Optional user comment |
| `created_at` | `DateTime` | |

### `AuditLog`
Append-only audit trail. Retention is the *Audit log* System Setting (default 90 days, minimum 7).

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `timestamp` | `DateTime` | Indexed |
| `actor_id` | `UUID` | FK → `User`, ON DELETE SET NULL |
| `actor_username` | `str` | Denormalized — survives user deletion |
| `action` | `str` | Dot-namespaced, indexed, e.g. `auth.login`, `inventory.create`, `settings.update` |
| `object_type` | `str` | Nullable — ORM class name of the affected object |
| `object_id` | `UUID` | Soft ref to the affected object |
| `object_label` | `str` | Denormalized label — survives object deletion |
| `success` | `bool` | |
| `ip_address` | `str` | Client address (real client IP via ProxyFix behind nginx) |
| `detail` | `JSON` | Nullable — machine-readable detail (reason, changes) |

### `LDAPServer`
Org-level LDAP/LDAPS server configuration.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `name` | `str(64)` | Display name |
| `host` | `str(255)` | Server hostname/IP |
| `port` | `int` | Default 389 (the UI suggests 636 for LDAPS) |
| `base_dn` | `str(255)` | Search base DN |
| `cn_identifier` | `str(64)` | Login attribute, default `sAMAccountName` (`uid` for OpenLDAP) |
| `bind_type` | `str(20)` | `anonymous` / `simple` / `regular` (service account) |
| `bind_dn` | `str(255)` | Service account DN, nullable |
| `bind_password` | `str(255)` | Fernet-encrypted, nullable |
| `use_ssl` | `bool` | LDAPS |
| `is_active` | `bool` | |

**Relationships:** `user_groups` → `[LDAPGroup]` (cascade delete), `users` → `[User]`

### `LDAPGroup`
Group rule: members of this group are auto-provisioned as users on their first login.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `ldap_server_id` | `UUID` | FK → `LDAPServer`, ON DELETE CASCADE |
| `group_dn` | `str(512)` | Group DN to match against |
| `label` | `str(128)` | Display name |
| `role` | `str(40)` | Role given to auto-provisioned users (`"user"` or `"admin"`) |
| `is_active` | `bool` | |

Only direct members of a mapped group match. Nested groups are not resolved (see the Active Directory note in the workplan).

### `SystemSetting`
The runtime value of each System Setting (§6). There is one row per registered setting.

| Name | Type | Description |
|---|---|---|
| `key` | `str(64)` | Primary key — the setting name |
| `value` | `JSON` | The value |
| `updated_at` | `DateTime` | |
| `updated_by` | `UUID` | FK → `User`, ON DELETE SET NULL; null = seeded, never changed by an admin |

> **Note:** the `RolloutSession` table was **dropped** in Phase 4.6b. Redis is the sole store for ephemeral job state (§5).

---

## 4. Service Classes

### `Validator` (`src/validation.py`)
Wraps input validation. It is logger-injected for user-facing errors, and the pure computation methods are static: IP, port, platform and file extension checks, plus a TCP port test. `SUPPORTED_PLATFORMS` is the list of supported Netmiko device types:
- `cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_xr`;
- `juniper_junos`, `arista_eos`, `fortinet`, `paloalto_panos`;
- `aruba_aoscx`, `checkpoint_gaia`, `hp_procurve`, `hp_comware`.

### `InputParser` (`src/input_parser.py`)
One CSV format is shared by the CLI and web import. Required columns are `ip`, `device_type` and `port`. Optional columns are `label`, the credentials (`username`, `password`, `secret`), and attribute columns named after a property (by name or label).

**Methods:**
- `prepare_devices(raw_devices)` → `(devices, errors)` — CLI path; credentials required.
- `csv_to_inventory(path, user_id, …)` → `ImportReport` — web import:
  - it saves the attribute columns as `var_maps`;
  - it turns credentials into security profiles (optional, on by default);
  - it reports unknown columns;
  - it does no reachability check.
- `parse_commands(path)`.
- Static `import_from_inventory(rows, user_id)` → `Device`s.

`ImportReport` carries `errors` and `notices`.

### `RolloutLogger` (`src/logging_utils.py`)
Owns the logging I/O for one rollout job. It is constructed as `RolloutLogger(webapp, verbose, prefix="rollout", job_id=None, redis_client=None)`.
- **Log file:** it always writes one, `logs/{prefix}_{timestamp}_{job_id}.log`.
- **With `webapp`:** it also appends each message to the Redis list `job:{id}:history` and publishes it on the channel `job:{id}:logs`.
- **Methods:** `notify(message, color, important)`, `get_history()`, `subscribe()` and `redis_cleanup()`.

Log files are pruned by a daily background task in the web app, using the *Log files* retention (default 60 days).

### `ReachabilityChecker` (`src/reachability.py`)
Probes TCP reachability of `ip:port` targets in parallel and caches results in Redis for the *Reachability cache* period (a callable TTL, so a settings change applies immediately). Inventory uses it for the live status dots, and New Rollout uses it to flag unreachable devices.

### LDAP (`src/ldap_auth.py`)
Module functions:
- `authenticate` binds as the user, using a DN constructed from `cn_identifier` or one found by a service-bind search;
- `check_group_membership`, `fetch_user_details`, `fetch_base_dn` and `walk_tree` (for the explorer UI), `test_connection`, `test_user`.

Error handling:
- bad credentials (`LDAPBindError`, `LDAPInvalidCredentialsResult`) are a normal "no";
- any other LDAP exception becomes `LdapUnavailable`, so the login page can tell "wrong password" apart from "directory down".

---

## 5. Job Execution Classes (`src/core.py`, `src/orchestration.py`)

### `RolloutEngine`
Pure pipeline object: `RolloutEngine(param: RolloutOptions, devices: list[Device], commands: list[str])`. `run(cancel_flag, logger) -> list[DeviceResultDict]`:
1. **Per device, in parallel** (`ThreadPoolExecutor(max_workers)`):
   - it substitutes `$$TOKEN$$`s;
   - it pushes over Netmiko, one `send_config_set(…, enter_config_mode=False)` per command after entering config mode once — exactly as typed (Netmiko's default re-checks config mode per call, and Aruba CX's driver only recognises `(config)#`, so inside a section it failed); a typed `end` followed by more config commands is refused as on the device; a refused command (`rejection()`: one list of vendor error strings, the command's own echo skipped) is logged with the device's reply and the rest are still sent;
   - it **finishes the way the platform needs** (`PLATFORMS` in `core.py`): `save_config()` after leaving config mode (Cisco IOS/IOS-XE/NX-OS, Arista, Aruba CX, HP ProCurve/Comware); `commit()` *before* leaving it (Junos, PAN-OS, IOS-XR — leaving discards uncommitted changes; up to `COMMIT_TIMEOUT` = 300 s; a failed commit is a failure, Junos then `rollback 0`, PAN-OS keeps the candidate and the log says so); `save config` (Check Point Gaia); nothing (FortiOS, after closing any open config block with `end`);
   - per-platform details, checked against the vendor documentation and Netmiko 4.6.0's source (2026-10-02): Junos configures with **`configure private`** (our commit can't take another admin's pending shared edits along, and our `rollback 0` can't wipe them; Junos refuses private mode while someone has uncommitted shared edits — that device then fails with the reason); PAN-OS discards a failed commit with `revert config`, else `load config from running-config.xml` (only if both are refused does the log ask to discard on the device); FortiOS checks `cfg-save` after the push and runs `execute cfg save` when it is manual or revert (else the change is lost at reboot / undone after the revert timeout); Check Point Gaia switches an account that lands in expert (bash) to clish (`clish`) for both the push and the config fetch — bash would silently swallow every `set …` — and refuses only if that fails; Aruba CX sends `end` before leaving config mode (Netmiko only recognises `(config)#`, so from `(config-if)#` its exit did nothing);
   - a prompt that changes mid-push (e.g. a new hostname) ends that session: the save then runs from a fresh one ("applied, and saved from a new session"), or the log says it wasn't saved;
   - the session is always closed; the cancel flag is honoured between devices.
   - **Only a person can resolve** (another admin's work, or the device's state is unknown): Junos refusing `configure private` while someone has uncommitted shared edits; a save refused (e.g. a Gaia config lock held by another session); a commit still running after `COMMIT_TIMEOUT`; PAN-OS changes left in the candidate when both discards are refused; Gaia stuck in expert even after `clish`; FortiOS `execute cfg save` refused. Each is logged as one red `ACTION NEEDED — <ip:port>: <what to do>` line (live log, log file, CLI console), and the rollout summary ends with `ACTION NEEDED on N devices (…)`. The web app also stores it per device (`device_results.action_needed`): when a live log ends, Active Jobs shows a completion card (job note + `[id]`, status, counts, the action-needed instructions, *View in Results*); the Results page shows a badge on the job and the instructions in it; the Dashboard's Recent Jobs mark it — so it doesn't depend on anyone reading the log; the CLI prints it on the console.
2. **Verify** (if on, only on devices the push applied to): `fetch_config()`, then `verify_commands(device_type, config, commands)` — one verdict per command: *verified*, *not configured*, *still configured* (a removal that didn't take), *not verifiable* (navigation like `exit`/`end`/`next`, operational like `write memory`/`commit`), *variable* (an unresolved `$$TOKEN$$`, only on the Verify Diff page).
   - **Indented configs** (Cisco-style, FortiOS): a plain indentation parser; typed commands are flat (the device tracks the mode), so each is placed in its section from the config's own structure — a typed line that is a section there opens it, `exit`/`next`/`exit-…` close one (`end` too on FortiOS, all on Cisco), a command found only at an outer level leaves the section, one found nowhere stays put. A command must exist at its place; `no`/`undo`/`unset`/`delete X` passes when `X` is gone.
   - **Flat configs** (Junos `display set`, PAN-OS set format, Gaia): one line per setting; `edit`/`up`/`top` move the prefix relative `set`/`delete` extend; `delete X` passes when no `set X…`/`add X…` line remains.
   - A config that can't be fetched means "couldn't verify", not "failed".
3. **Status:** *cancelled* (never connected), *failed* (nothing applied: no connection, a failed commit, or every configuring command refused), otherwise from verify (all checkable verified → success; none → failed; else partial) or, without verify, from the refusals (none → success, some → partial).
4. **Summary:** it logs one summary line for the rollout.

Known limits (Phase 5b: Deep Diff with hier_config + netutils): typed abbreviations (`int gi1`), values the device rewrites (hashed secrets, normalised values like Junos `area 0` → `0.0.0.0`, Comware VLAN ranges `101 to 102`, hidden defaults — FortiOS `show` omits values equal to their default), "already there before the rollout", multi-line banners; PAN-OS prints one setting per line, so a compound typed `set … from x to y action allow` never matches (type one setting per line); Junos `up` is taken as leaving the whole last `edit` (it really goes up one statement level). The Verify Diff route (`/results/config_diff`) returns the same verdicts, so the page can't disagree with the rollout.

### `RolloutJob`
Lifecycle owner, constructed as `RolloutJob(job_id, user_id, engine, options, redis_client)`. It owns the thread, cancel flag, engine and logger.
- **Methods:** `start(on_complete)`, `cancel()` and `is_alive()`, plus log accessors (`get_log_queue`, `get_log_history`) and `log_cleanup`.
- **Completion:** `on_complete` always fires, even if the engine raises, so an orchestrator slot is never leaked.

### `RolloutOrchestrator`
The concurrency manager: `RolloutOrchestrator(backend, max_concurrent)`. A single instance is created at app startup, and `max_concurrent` is the *Concurrent rollout jobs* setting read at startup (default 4; a change applies after a restart).

```
submit(devices, commands, params, user_id, comment)
  → build RolloutEngine + RolloutJob, store in _jobs
  → Redis: HSET job:<id>:meta {user_id, status=pending, device_count, created_at}
           SADD user_jobs:<uid> <id>;  INCR netrollout:pending_count
  → Postgres: JobMetadata row (raw commands + comment)
  → RPUSH netrollout:job_queue <id>

_dispatcher thread (permanent, started in __init__)
  loop: BLPOP netrollout:job_queue (5 s timeout — picks up a hot-swapped Redis client;
        Redis down → exponential backoff 1–30 s, the loop never dies)
    → acquire a semaphore slot (max_concurrent)
    → claim the job under the lock (started_at) unless draining, then job.start(_cleanup)
      outside the lock;  meta status=active, started_at;  pending−1, active+1

job thread finishes → _cleanup(job_id)
  → _finalize: DeviceResult rows to Postgres; DEL meta, SREM user_jobs, active−1; log keys cleaned
  → release the slot (always, in finally)
```

`cancel(job_id)` sets the job's cancel flag and meta `status=cancelling`.

**Drain** (`drain(deadline)`, run by `lifecycle.Shutdown` on SIGTERM or the admin Restart): `submit()` raises `Draining` from then on (the routes show `DRAINING_MESSAGE`); queued jobs are recorded as cancelled, device by device, with the reason in their log (a job's definition lives only in this process, so it could never run after the restart); running jobs may finish for `deadline` seconds, then are cancelled and given up to 60 s to record — a job still running after that is cut off by the exit and leaves no result record (its log file remains). `counts()` returns running/queued from memory (the health endpoint, the Restart choice). A crash or `SIGKILL` still loses queued jobs silently.
 The Active Jobs page and the admin views read job state from the Redis `job:*:meta` hashes. The Prometheus collector reads `netrollout:active_count` and `netrollout:pending_count`.

---

## 6. DB Layer (`src/db/`)

### `PostgresConfig` / `RedisConfig`
Frozen dataclasses built from env vars (the URL form, or the individual `PG_*` / `REDIS_*` vars). `get_url()` returns the connection string, and `to_env_dict()` returns what to write to `config/runtime.env` (every key of the service, blank when unused — URL, password, schema — so nothing inherited from the container environment can override the switch). A new config object is created for each hot-reload.

### `PostgresConnection`
Wraps a SQLAlchemy engine.
- **`get_session()`:** a context manager that commits on a clean exit and rolls back on an exception.
- **Pooling:** `pool_pre_ping=True`.
- **`reload_db(config)`:** atomically swaps the engine, and raises `RuntimeError` if the new server is unreachable.

### `RedisConnection`
Wraps a `redis.Redis` client, with the same `reload_db(config)` pattern. `REDIS_UNAVAILABLE` (connection errors plus timeouts) is the exception tuple that callers catch. There is no module singleton: the client is always reached through `app.backend.redis.client`, so a hot swap is picked up.

### `BackendServices` (`src/db/backend.py`)
The composition root for infrastructure. It is constructed once in `launch_app()` and attached to `app.backend`.

```python
BackendServices()        # no arguments:
#   load config/runtime.env (override) → PostgresConnection() → install() → RedisConnection()
# app.backend.postgres   →  PostgresConnection
# app.backend.redis      →  RedisConnection
# app.backend.settings   →  SettingsStore (System Settings)
```

**`health()`:** returns `{"POSTGRES": bool, "REDIS": bool}`. Each service is checked independently, so one failure doesn't mask the other.

**`reload_postgres(config)` / `reload_redis(config)`:** hot-reload without a restart, from the Server Management UI.
- Both write the new values to `config/runtime.env` (atomically, owner-only).
- Switching Postgres also runs `install()` on the new database.

**`connection_modes()`:** `bundled` or `external` per service, from the host of the live connection: `localhost`/`127.0.0.1` or the compose service name (`postgres`, `redis`) is bundled.

**`encrypted_sample()`:** returns one stored Fernet token, for the startup key check.

### `install()` (`src/db/db_install.py`)
Runs at every start, and again after a Postgres switch. It is idempotent.
1. **Migrations:** `alembic upgrade head` on the app's own connection.
2. **Factory admin:** seeds `admin`/`admin` if missing.
3. **Settings:** seeds any missing System Setting rows.
4. **pg_cron:** (re)schedules the retention jobs. This is skipped with a message if pg_cron is unavailable.

pg_cron jobs, daily at 03:00. Each statement reads its period from `system_settings` when it runs, so a change applies at the next run without a restart:

| Job | Action | Setting (default) |
|---|---|---|
| `device_result_retention` | delete `device_results` rows | *Job records* (30 d) |
| `job_metadata_retention` | delete `job_metadata` rows whose job has no results left | *Job records* (30 d) |
| `device_result_config_retention` | set `fetched_config = NULL`, keep the row | *Config snapshots* (7 d) |
| `audit_log_retention` | delete `audit_log` rows | *Audit log* (90 d) |

### System Settings (`src/db/settings.py`)
Admin-editable runtime settings. The `system_settings` table is the **only runtime source**.

**The registry `SETTINGS`:** each `Setting` has a key, label, help text, card, default, range and kind. It also has an `applies` value, which says when a change takes effect. It can optionally name an install-time env var, and it marks whether pg_cron reads it via SQL.

| Setting | Card | Default | Range | A change applies |
|---|---|---|---|---|
| Job records (`job_retention_days`) | Retention | 30 | 1–3650 | next nightly clean-up |
| Config snapshots (`config_snapshot_retention_days`) | Retention | 7 | 1–3650 | next nightly clean-up |
| Audit log (`audit_retention_days`) | Retention | 90 | 7–3650 | next nightly clean-up |
| Log files (`log_retention_days`) | Retention | 60 | 1–3650 | next daily log clean-up |
| Concurrent rollout jobs (`orchestrator_workers`) | Rollouts | 4 | 1–32 | after restart (seed: `ORCHESTRATOR_WORKERS`) |
| Devices in parallel per job (`device_parallelism`) | Rollouts | 10 | 1–64 | next rollout |
| Reachability cache (`reachability_cache_seconds`) | Rollouts | 60 | 10–3600 | immediately |
| Hostname (`public_hostname`) | Access | "" (auto-detect) | — | next start (seed: `NETROLLOUT_PUBLIC_HOSTNAME`) |
| HTTPS port (`https_port`) | Access | 443 | 1–65535 | next start (seed: `NETROLLOUT_HTTPS_PORT`) |

- **Seeding:** `install()` inserts a row for every missing setting. The value is the env seed if it is set and valid, else the default. Existing rows are **never overwritten**, and env vars are ignored after that.
- **Rules:** cross-setting rules are declarative `RULES`, sent to the page as data (`rules_for_client()`) and enforced on both server and client:
  - log files are kept at least as long as job records;
  - config snapshots are kept no longer than their job record.
- **`SettingsStore`:**
  - `get(key)` coerces a bad stored value to the nearest valid one and never raises;
  - `values()` and `list_for_display()`;
  - `update(values, user_id)` is all-or-nothing, runs range and rule checks, and returns the changes, which are audited;
  - `reset(key)` writes the default;
  - `restart_only_values()` / `restart_pending()` drive the "restart pending" marker.
- **`sql_value(key)`:** gives pg_cron a `COALESCE((SELECT value …), default)` expression.
- **Phase 4, stage 8 (Part C):** the hostname will apply live, via an nginx config the app renders. An HTTPS port change will need `netrollout apply`.

---

## 7. Webapp Layer (`src/webapp/`)

### `__main__.py` — entry point (`python -m src.webapp`)
1. **App:** `create_app()`. A `StartupError` (missing secret in a container, encryption key problem) exits with a readable message.
2. **SIGTERM:** `app.shutdown.begin(drain_seconds(), restart=False)` — drain, then exit (`docker stop`, `netrollout stop` / `update`).
3. **Background:** starts the daily log pruning.
4. **Announcer:** in development, the startup announcer below; in a container, one line with the expected URL (`container_announcement`) — the host-side check is the installer and `netrollout status` calling `/_netrollout/health`.
5. **Serve:** Waitress `serve()` on `0.0.0.0:$PORT` (default 8080).

### `startup.py` — reverse-proxy check (development)
In a container nothing is probed: its console isn't watched and the published port isn't reliably reachable from inside, so a probe would report working setups as broken (the System Settings *Test* button says so too). Each run creates a random instance token, served at `/_netrollout/instance`. Once Waitress answers, a background check runs in two steps:
1. **Locally:** it connects to the local nginx, presenting the public hostname (SNI/Host).
2. **Publicly:** it tries the public URL.

The public URL comes from the *Hostname* / *HTTPS port* settings, or is auto-detected from the nginx config. The check confirms that nginx forwards to *this* process, then prints the address people should use, with a message that fits the case (proxy missing, wrong upstream, DNS, …). On a desktop launch it opens that address in the browser (`NETROLLOUT_OPEN_BROWSER=0` disables this).

### `setup.py` — app factory
`launch_app()` is the composition root.
1. **Backend and encryption:** resolves `SECRET_KEY` first (fail fast), builds `BackendServices`, then runs the encryption key check.
2. **Settings:** reads the restart-only settings; these become `SETTINGS_STARTED_WITH`.
3. **Services:** creates the orchestrator and `WebServices`, then the Flask app (`template_folder='../../templates'`, `static_folder='../static'`).
4. **Configuration:** sets the instance token, the config, extensions and handlers, and the Prometheus collector.
5. **Sessions:** clears the old `redis_session:*` sessions, so every restart logs everyone out.

Blueprints are registered in `__init__.py` (`create_app()`), not here.

```python
app.backend       →  BackendServices
app.web           →  WebServices
app.orchestrator  →  RolloutOrchestrator
app.shutdown      →  lifecycle.Shutdown (drain, then exit; relaunch in dev)
```

- **Sessions:** server-side in Redis via Flask-Session, prefix `redis_session:`, not permanent. The cookie is `Secure`, `HttpOnly`, `SameSite=Lax`.
- **`_SafeRedisSessionInterface`:** catches `REDIS_UNAVAILABLE` on open and save and returns an empty session instead of crashing. Its `client` is looked up per request from `backend.redis`, so a Server Management Redis switch keeps sign-ins working. It is registered **after** `configure_app()` (which calls `Session(app)`) so it isn't overwritten.
- **Proxy headers:** `ProxyFix(x_for=1, x_proto=1, x_host=1)` sits behind nginx.
- **Drain banner:** a context processor gives every template `server_draining`; `_drain_banner.html` (in both base templates) says new rollouts are paused and reloads the page when a new instance answers. The Restart modal and script are shared includes too (`_restart_modal.html`, `_restart_script.html`).
- **Vendor logos:** `VENDOR_LOGOS` (device_type → Simple Icons CDN URL) is a Jinja global.

### `extensions.py` — module-level Flask extensions
Extensions are created at module level so blueprints can import them at definition time.

```python
login_mng = LoginManager()         # login_view = "auth.home"
conn_limit = Limiter(...)          # rate limiting
csrf = CSRFProtect()
```

- **`register_extensions(app)`:** calls `init_app()` on each and initializes `PrometheusMetrics` (`/metrics`).
- **`register_auth(app)`:** registers the user loader.
- **`register_handlers(app, backend)`:**
  - CSRF error handler;
  - service-unavailable (503) handler for Postgres `OperationalError` and Redis connection/timeout errors, which renders a page saying which service is down;
  - invalid-encryption-key handler, which renders `key_error.html` explaining what to do.

### `utils.py` — shared helpers
**`WebServices(backend)`**, attached to `app.web`. It has helpers used by two or more blueprints:
- `audit(action, *, object_type, object_id, object_label, success, detail)` — writes one `AuditLog` row in its own session
- `act_on_db_obj(model, obj_id, func, user_id, many, …)` — generic load-check-act dispatcher, with the ownership check
- `update_op(...)`, `delete_op(...)` — operation factories for `act_on_db_obj`; `get_label(obj)`
- `build_security_profile(...)` — encrypts and builds a profile
- `get_property_defs(user_id)` — system and user property definitions
- `reachability` — the shared `ReachabilityChecker`

**Module functions:**
- responses and decorators: `ok()`, `err()`, `require_admin`, `with_json`, `with_form`, `flash_redirect`;
- device visibility (own and global): `visible_devices_clause`, `query_visible_devices`, `can_edit_device`;
- duplicate endpoints: `same_endpoint_devices`, `same_endpoint_warning`;
- `partition_devices`;
- query and KPI builders: `compile_query_rules(node, allowed_fields)` (jQuery QueryBuilder → SQLAlchemy expression) and `build_kpi(results_30d, label_map)`.

**Constants:** `SYSTEM_PROPERTIES`, `QUERY_OPS`. The analytics field and column lists live in their blueprints (`analytics.py`, `admin_observability.py`). `validate_mapping_fields` lives in `mappings.py`, and `job_status` / `user_owns_job` in `jobs.py`.

### `blueprints/`
Each blueprint owns its routes and route-specific helpers. Blueprints reach `app.web`, `app.backend` and `app.orchestrator` through `current_app` inside routes, never at module level. Every route except the public ones requires login, and every `/admin` route requires the admin role. This is enforced for all routes by `tests/integration/test_route_matrix.py`.

| Blueprint | Prefix | Routes |
|---|---|---|
| `auth` | — | `/`, `/login`, `/register`, `/otp_enroll`, `/otp_verify`, `/logout`, `/account`, `/account/password` (forced or voluntary change; local users) |
| `jobs` | — | `/dashboard`, `/active_jobs`, `/results` (`?job=<id>` opens that job), `/results/summary/<job_id>` (a finished job in a few lines: the completion card), `/results/config_diff/<job_id>/<ip>`, `/results/download_log/<job_id>` |
| `rollout` | `/rollout` | `/new`, `/start`, `/cancel`, `/stream/<job_id>` (SSE), `/rollback/<job_id>` |
| `inventory` | `/inventory` | list, `/create`, `/test_connection`, `/reachability`, `/<id>/edit`, `/<id>/mappings`, `/<id>/delete`, `/import_csv`, `/bulk_assign` |
| `security` | `/security` | list, `/create`, `/quick_create`, `/<id>/edit`, `/<id>/delete`, `/<id>/test` |
| `mappings` | `/mappings` | list, `/create`, `/quick_create`, `/<id>/edit`, `/<id>/delete`, `/bulk_assign` |
| `properties` | `/properties` | list, `/create` + `/quick_create`, `/<id>/edit`, `/<id>/delete` |
| `analytics` | `/analytics` | KPI page, `/query` (POST, own results) |
| `admin_users` | `/admin` | admin home, `/users`, `/users/<id>/<action>` (approve, enable, disable, promote, demote, delete, reset_2fa, terminate_session), `/users/<id>/reset_password` (JSON: a temporary password, shown once), `/users/bulk/<action>`, `/sessions`, `/sessions/<id>/kick` |
| `admin_servers` | `/admin/server` | Server Management page; `/postgres/{test,save}`, `/redis/{test,save}`; 12 LDAP routes (`/ldap`, new, save, delete, test, test_user, fetch_dn, explore, import, groups list/toggle/delete); `/restart` (with rollouts running or queued: 409 unless `mode` is `when_finished` (drain) or `now` (cancel)) |
| `admin_observability` | `/admin` | `/audit`, `/analytics`, `/analytics/query`, `/active_job_count` |
| `admin_settings` | `/admin/settings` | page, save (POST), `/<key>/reset`, `/test` (public URL check) |
| `system` | — | `/_netrollout/instance` (public; no session written), `/_netrollout/health` (public; Postgres/Redis up, rollout counts, draining, version; 200 or 503) |

Plus `/metrics` (Prometheus; `404` at nginx, scraped from the app directly).

**Auth flow:**
```
POST /login
  local user  →  check_password_hash → approval/active gates → start_otp_flow()
                 (the factory "admin" account skips 2FA)
  ldap user   →  user_bind() → approval/active gates → complete_login()   (no 2FA)
  unknown     →  login_ldap_group() → check_group_membership() → auto-provision → complete_login()

start_otp_flow()
  → session["pre_auth_user_id"] = user.id
  → otp_secret present? → /otp_verify
  → no secret?          → /otp_enroll (first-time setup)

complete_login()
  → login_user(user) → record_redis_session() → audit → redirect jobs.dashboard
```

Admins can reset a user's 2FA; the user re-enrols at the next login.

**Passwords** (local accounts): one rule, `src/passwords.py` — at least 8 characters with at least 2 of letters / digits / special characters, ASCII only, not containing the username; a change must differ from the current one. Registration, `/account/password` and generated temporary passwords all use it; `templates/_password_rule_script.html` mirrors it in the pages. While `must_change_password` is set, a `before_request` gate (`extensions.py`, allowlist `PASSWORD_CHANGE_ALLOWED`: the change page, logout, static, instance/health) redirects pages to `/account/password` and answers fetch calls with 403. A change (rate-limited like login) clears the flag, rotates the session id, **signs the user out of every other session** and is audited (`auth.password_change`). An admin *Reset password* on another local user (not LDAP, not themselves, not the factory admin) replaces the stored password with a random temporary one shown once, sets the flag and **signs the user out everywhere** (`user.reset_password`; the password is never logged). Admin *Terminate Session* signs out everywhere too.

**Signing a user out everywhere** (`utils.end_user_sessions`): `user_session:<id>` points only at the latest sign-in, so every `redis_session:*` is decoded with flask-session's serializer and the user's (`_user_id`) are deleted — complete (sessions from before the change too) and cheap at this scale; a per-user index would be faster but miss existing sessions and need expiry cleanup.

**Real-time logs:** `/rollout/stream/<job_id>` is Server-Sent Events. It replays `job:<id>:history` (LRANGE), then tails the pub/sub channel `job:<id>:logs`, sending a heartbeat every 0.5 s. The response sets `X-Accel-Buffering: no` so nginx doesn't buffer.

---

## 8. Observability Stack

Optional sidecar services. The Flask app runs independently and is unaffected when they are down. Configs are currently under `docs/{grafana,prometheus,loki,promtail}`. Phase 4 moves them to `deploy/` as an optional compose profile, with Grafana served through nginx at `/grafana`.

| Service | Role |
|---|---|
| PostgreSQL | Historical business metrics. A direct Grafana datasource through the `grafana_reader` read-only user |
| Prometheus | Live metrics: active and pending jobs, Flask request rates and latencies (scrapes `/metrics`) |
| Loki + Promtail | Log stream: per-job log files shipped by Promtail, searchable by the `job_id` label |

**Custom Prometheus collector** (`RolloutSessionCollector`, `src/webapp/setup.py`): reads `netrollout:active_count` and `netrollout:pending_count` from Redis and exposes the `netrollout_active_jobs` and `netrollout_pending_jobs` gauges.

**Four Grafana dashboards**, provisioned from `docs/grafana/dashbaord_config/` (sic; renamed to `deploy/grafana/dashboards` in Phase 4):

| Dashboard | File | Datasources | Purpose |
|---|---|---|---|
| Operations Overview | `operations_overview.json` | Prometheus | Live job state, request rates, p99 latency |
| Job Analytics | `job_analytics.json` | PostgreSQL | Historical outcomes, platform breakdown, heatmap |
| Job Details | `job_details.json` | PostgreSQL + Loki | Drill-down by `$job_id`: device results, log stream |
| Audit & Security | `audit&security.json` | PostgreSQL | Audit trail, failure rates, top actors |

---

## 9. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `cancel_event` ownership | `RolloutJob` | Lifecycle concern, not logging or pipeline |
| Execution context passing | Arguments at call time | No hanging state on `RolloutEngine` |
| Credential storage | `SecurityProfile` table, Fernet-encrypted | Topology separated from credentials; one profile → many devices |
| Encryption key | Generated only on a fresh install; checked against stored data at startup | A wrong or lost key is caught at startup, not at a user's first rollout, and a new key never silently orphans existing secrets |
| `Device` construction | `from_inventory()` factory | Single boundary where decryption happens |
| Results schema | One row per device per job, `ip:port` identity | Per-device analytics via SQL; NAT/port-forwarded devices stay distinct |
| `RolloutOrchestrator` | Singleton at app startup; Redis queue + semaphore | Single owner of concurrency; routes delegate to it |
| Ephemeral job state | Redis only (`RolloutSession` table dropped) | Faster and naturally ephemeral; Postgres unnecessary for RAM data |
| Runtime settings | `system_settings` table as the only runtime source; env only seeds it | One truth that admins can change in the UI, which pg_cron can read, and which is never silently overridden by env |
| Settings rules | Declarative, shared with the page | The same rules enforced on the server and in the browser, with no duplicated logic |
| Flask extensions | Module-level with `init_app()` | Must be importable by blueprints at definition time, before an app context exists |
| `app.web` / `app.backend` | Set on the app object in `launch_app()` | Available through `current_app` in any request context; avoids circular imports |
| Blueprint `url_for` | Always prefixed (`"auth.home"`, `"jobs.dashboard"`) | Blueprint namespace prevents endpoint name collisions |
| `_SafeRedisSessionInterface` | Registered after `Session(app)` | `Session(app)` overwrites `session_interface`; must follow it |
| LDAP auto-provisioning | A matched group rule creates a user on first login | Zero-touch onboarding; role assigned from the group mapping |
| `AuditLog.actor_username` | Denormalized | Audit records survive user deletion; no orphaned FK |
| `reload_db()` | Raises `RuntimeError` if the new server is unreachable | Silent failure would leave the app pointing at a broken connection |
| Startup proxy check | Per-run token fetched through nginx | Proves the proxy reaches *this* process (not a stale one) and tells the operator the right URL |
