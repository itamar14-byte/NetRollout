# NetRollout — Architecture Document
_Written: 2026-04-07 — Updated: 2026-09-30 (checked against the code: System Settings, Redis orchestration, blueprints, startup check)_

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

The **CLI** (`src/cli.py`) uses the same `RolloutEngine` directly, from a devices CSV and a commands file. It has no database, no Redis and no orchestrator.

**Configuration** comes from env vars. `config.env` at the repo root is loaded with override by `BackendServices` at startup, and Server Management writes it.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` or `PG_HOST` / `PG_PORT` / `PG_NAME` / `PG_USER` / `PG_PASSWORD` / `PG_SCHEMA` | Postgres. The URL form takes precedence |
| `REDIS_URL` or `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis. The URL form takes precedence |
| `SECRET_KEY` | Flask session key (default `dev`; Phase 4 makes it mandatory in a container) |
| `PORT` | Internal app port Waitress listens on (default 8080). nginx forwards to it |
| `NETROLLOUT_ENCRYPTION_KEY` | Fernet key; else `~/.netrollout/encryption.key` (see below) |
| `ORCHESTRATOR_WORKERS`, `NETROLLOUT_PUBLIC_HOSTNAME`, `NETROLLOUT_HTTPS_PORT` | **Install-time seeds only** for the matching System Settings (§6). They are read when the setting's row doesn't exist yet and ignored after that |
| `NETROLLOUT_OPEN_BROWSER` | `0` disables opening the browser on a desktop launch |

**Encryption key.** Fernet protects security-profile passwords and enable secrets, LDAP bind passwords and OTP secrets.
- **When no key exists:** one is generated only on a fresh install, meaning the DB is reachable and holds no encrypted data.
- **Startup check:** the key is test-decrypted against one stored value.
- **When the app refuses to start** (`EncryptionStartupError`, a readable message and no traceback):
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
| `fetch_config` | `(logger) -> str \| None` | Opens a NAPALM connection and returns the running config. **Replaced in Phase 4 (stage 4b)** by per-platform profiles over Netmiko. NAPALM is dropped because the current mapping breaks verify on several platforms |

### `DeviceResultDict`
TypedDict returned per device by `RolloutEngine.run()`. Fields: `device_ip`, `device_port`, `device_type`, `commands_sent`, `commands_verified`, `fetched_config`, `status`.

---

## 3. ORM Models (`src/db/tables.py`)

All models use UUID primary keys except `SystemSetting`, whose key is the setting name. The schema is managed by Alembic (`src/db/alembic/versions`, one `v1_0_0_baseline` revision; from v1.0.0 on, schema changes are new revisions on top of it); the app applies migrations itself at every start.

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
| `auth_type` | `str(20)` | `"local"` or `"ldap"` |
| `ldap_server_id` | `UUID` | FK → `LDAPServer`, ON DELETE SET NULL, nullable |
| `created_at` | `DateTime` | Set at creation |

**Relationships:** `inventory`, `security_profiles`, `variable_mappings`, `property_definitions`, `results`, `job_metadata` (all cascade-delete with the user), `ldap_server`

The factory account `admin`/`admin` is seeded at startup if missing (`db_install.py`). Phase 4 adds a forced password change at first login.

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
| `commands_verified` | `int \| None` | Null if verify was not run |
| `status` | `str` | `success` / `partial` / `failed` / `cancelled` |
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
   - it pushes over Netmiko (`send_config_set`, then `save_config()`);
   - it honours the cancel flag between devices.
2. **Verify:** if on, it fetches each pushed device's config and counts the commands that appear in it.
3. **Summary:** it logs one summary line for the rollout.

Phase 4 stage 4b replaces the generic finish (`save_config()`) with per-platform profiles:
- `commit()` for Junos, PAN-OS and IOS-XR, whose changes today are not committed;
- the right save for the other platforms.
- a per-platform config fetch and matcher for verify.

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
    → job.start(_cleanup);  meta status=active, started_at;  pending−1, active+1

job thread finishes → _cleanup(job_id)
  → _finalize: DeviceResult rows to Postgres; DEL meta, SREM user_jobs, active−1; log keys cleaned
  → release the slot (always, in finally)
```

`cancel(job_id)` sets the job's cancel flag and meta `status=cancelling`. The Active Jobs page and the admin views read job state from the Redis `job:*:meta` hashes. The Prometheus collector reads `netrollout:active_count` and `netrollout:pending_count`.

---

## 6. DB Layer (`src/db/`)

### `PostgresConfig` / `RedisConfig`
Frozen dataclasses built from env vars (the URL form, or the individual `PG_*` / `REDIS_*` vars). `get_url()` returns the connection string, and `to_env_dict()` returns what to write to `config.env`. A new config object is created for each hot-reload.

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
#   load config.env (override) → PostgresConnection() → install() → RedisConnection()
# app.backend.postgres   →  PostgresConnection
# app.backend.redis      →  RedisConnection
# app.backend.settings   →  SettingsStore (System Settings)
```

**`health()`:** returns `{"POSTGRES": bool, "REDIS": bool}`. Each service is checked independently, so one failure doesn't mask the other.

**`reload_postgres(config)` / `reload_redis(config)`:** hot-reload without a restart, from the Server Management UI.
- Both write the new values to `config.env`.
- Switching Postgres also runs `install()` on the new database.
- The `pending_db_init.flag` file it still writes is unused and will be removed in Phase 4.

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
1. **App:** `create_app()`. An `EncryptionStartupError` exits with a readable message.
2. **Background:** starts the daily log pruning.
3. **Announcer:** starts the startup announcer.
4. **Serve:** Waitress `serve()` on `0.0.0.0:$PORT` (default 8080).

### `startup.py` — reverse-proxy check
Each run creates a random instance token, served at `/_netrollout/instance`. Once Waitress answers, a background check runs in two steps:
1. **Locally:** it connects to the local nginx, presenting the public hostname (SNI/Host).
2. **Publicly:** it tries the public URL.

The public URL comes from the *Hostname* / *HTTPS port* settings, or is auto-detected from the nginx config. The check confirms that nginx forwards to *this* process, then prints the address people should use, with a message that fits the case (proxy missing, wrong upstream, DNS, …). On a desktop launch it opens that address in the browser (`NETROLLOUT_OPEN_BROWSER=0` disables this).

### `setup.py` — app factory
`launch_app()` is the composition root.
1. **Backend and encryption:** builds `BackendServices`, then runs the encryption key check.
2. **Settings:** reads the restart-only settings; these become `SETTINGS_STARTED_WITH`.
3. **Services:** creates the orchestrator and `WebServices`, then the Flask app (`template_folder='../../templates'`, `static_folder='../static'`).
4. **Configuration:** sets the instance token, the config, extensions and handlers, and the Prometheus collector.
5. **Sessions:** clears the old `redis_session:*` sessions, so every restart logs everyone out.

Blueprints are registered in `__init__.py` (`create_app()`), not here.

```python
app.backend       →  BackendServices
app.web           →  WebServices
app.orchestrator  →  RolloutOrchestrator
```

- **Sessions:** server-side in Redis via Flask-Session, prefix `redis_session:`, not permanent. The cookie is `Secure`, `HttpOnly`, `SameSite=Lax`.
- **`_SafeRedisSessionInterface`:** catches `REDIS_UNAVAILABLE` on open and save and returns an empty session instead of crashing. It is registered **after** `configure_app()` (which calls `Session(app)`) so it isn't overwritten.
- **Proxy headers:** `ProxyFix(x_for=1, x_proto=1, x_host=1)` sits behind nginx.
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
| `auth` | — | `/`, `/login`, `/register`, `/otp_enroll`, `/otp_verify`, `/logout`, `/account` |
| `jobs` | — | `/dashboard`, `/active_jobs`, `/results`, `/results/config_diff/<job_id>/<ip>`, `/results/download_log/<job_id>` |
| `rollout` | `/rollout` | `/new`, `/start`, `/cancel`, `/stream/<job_id>` (SSE), `/rollback/<job_id>` |
| `inventory` | `/inventory` | list, `/create`, `/test_connection`, `/reachability`, `/<id>/edit`, `/<id>/mappings`, `/<id>/delete`, `/import_csv`, `/bulk_assign` |
| `security` | `/security` | list, `/create`, `/quick_create`, `/<id>/edit`, `/<id>/delete`, `/<id>/test` |
| `mappings` | `/mappings` | list, `/create`, `/quick_create`, `/<id>/edit`, `/<id>/delete`, `/bulk_assign` |
| `properties` | `/properties` | list, `/create` + `/quick_create`, `/<id>/edit`, `/<id>/delete` |
| `analytics` | `/analytics` | KPI page, `/query` (POST, own results) |
| `admin_users` | `/admin` | admin home, `/users`, `/users/<id>/<action>` (approve, enable, disable, promote, demote, delete, reset_2fa, terminate_session), `/users/bulk/<action>`, `/sessions`, `/sessions/<id>/kick` |
| `admin_servers` | `/admin/server` | Server Management page; `/postgres/{test,save}`, `/redis/{test,save}`; 12 LDAP routes (`/ldap`, new, save, delete, test, test_user, fetch_dn, explore, import, groups list/toggle/delete); `/restart` |
| `admin_observability` | `/admin` | `/audit`, `/analytics`, `/analytics/query`, `/active_job_count` |
| `admin_settings` | `/admin/settings` | page, save (POST), `/<key>/reset`, `/test` (public URL check) |
| `system` | — | `/_netrollout/instance` (public; no session written) |

Plus `/metrics` (Prometheus; blocked at nginx in Phase 4).

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
