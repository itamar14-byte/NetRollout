# NetRollout — Architecture Document
_Written: 2026-04-07 — Updated: 2026-10-10 (the OOP redesign: the classes as built, §11 the object model)_

Deployment and packaging (Docker images, compose, the installers, releases) are described in §8–9 and CLAUDE.md; what's still ahead (stage 10, CI and the release) is planned in `docs/plans/phase-4.md`.

---

## 1. Overview

NetRollout is structured around six layers:

1. **Data classes** — pure runtime objects, no DB coupling
2. **ORM models** — DB schema; per-user data anchored to `User`
3. **Service classes** — business logic, validation, parsing, logging, reachability
4. **Job execution classes** — orchestration, pipeline, concurrency
5. **DB layer** — connection management, session lifecycle, hot-reload, install/seed, System Settings
6. **Webapp layer** — Flask app factory, extensions, blueprints, shared helpers, startup check

**Where the code lives** — (one module = one whole concern; the classes and why they are classes: §11)
- `src/rollout/` — the engine, shared by the CLI and the web app and free of the web stack: `engine.py` (`Device`, `RolloutEngine`, `classify`), `session.py` (`NetmikoSession`: one device's SSH conversation; `RunReport`), `platforms.py` (`PLATFORMS`, the `Finish` family, verify), `inputs.py` (the input checks; the devices CSV / commands file: `InputParser`, `Validator`), `log.py` (`RolloutLogger`, its `Echo`: `Console` / `LiveLog`; the log pruner).
- `src/jobs.py` — a web rollout's life: `RolloutOrchestrator`, `RolloutJob`, `JobStore` (its Redis keys), `ResultRecorder`, job_status, build_kpi, clear_stale_jobs. `src/results.py` — finished jobs as the viewer may see them (`JobResults`). `src/inventory.py` — the device inventory's rules (`InventoryView`, `SecurityProfiles`), reachability, the CSV import (`import_csv`). `src/audit.py` — `AuditAction`, `AuditTrail`, `Actor`.
- `src/db/` — `connections.py` (`PostgresConnection` / `RedisConnection` over the `ServiceConnection` ABC, `RuntimeEnv`, `BackendServices`), `tables.py`, `install.py` (migrations, grants, seeds, retention SQL), `retention.py` (the nightly run), `settings.py` (the `Setting` kinds, `SettingsStore`), `move.py`.
- `src/accounts/` — `users.py` (`Viewer`, `Accounts`, `SessionStore`, the password rule), `ldap.py` (`Directory`).
- `src/access/` — how NetRollout is reached: `site_env.py` (config/nginx/site.env), `nginx.py` (`Nginx`: its verdicts and the changes it must accept), `port.py` (the HTTPS port request), `certs.py` (`CertificateStore`, the `Certificate` kinds), `service.py` (`Access`, the facade the pages use).
- `src/backup/` — `archive.py` (the zip, `BackupFolder`), `schedule.py` (`BackupScheduler`), `__main__.py` (`python -m src.backup`).
- `src/setup/` — the setup core the scripts call (`python -m src.setup`): `install.py`, `env.py` (.env), `update.py` (releases, upgrade), `manage.py`, `port.py`.
- `src/webapp/` — `build.py` (`launch_app`, create_app), `app.py` (`NetRolloutApp`), `hooks.py`, `http.py`, `lifecycle.py` (`Shutdown`, `Maintenance`), `db_move.py` (`DatabaseMove`, the Redis switch), `startup.py`; `blueprints/` one file per page (a merged page keeps its own Blueprint: `admin_settings.py` + backups, `analytics.py` + the admins' analytics, `mappings.py` + properties).
- `src/runtime.py` (folders, version, read_json / write_json / `write_atomic`, `PeriodicTask`), `src/encryption.py`, `src/cli.py`. Tests mirror it under `tests/unit/`; integration tests go by page.

**Ownership.**
- Per-user data belongs to a `User` through a foreign key: devices, security profiles, variable mappings, property definitions, results and job metadata.
- Global devices (`Inventory.is_global`) are the exception. All users can see them and roll out to them, but only admins can edit or delete them.
- Org-level data belongs to no user: LDAP servers and groups, and System Settings. The audit log keeps its entries when a user is deleted (`actor_id` becomes NULL and the username stays, denormalized).

At runtime, `RolloutOrchestrator` is the concurrency manager.
- **The web app:** it owns the in-memory jobs dict and a permanent dispatcher thread. Redis holds the queue and the ephemeral job state.
- **`RolloutJob`:** the lifecycle owner of a single job. It owns the thread, the cancel event, the logger and the engine.
- **`RolloutEngine`:** execution context flows into it as arguments at call time, so it holds no hanging state.

The **CLI** (`src/cli.py`) uses the same `RolloutEngine` directly, from a devices CSV and a commands file. It has no database, no Redis and no orchestrator — and doesn't even import them: the DB models and Redis are imported for type checking only (`src/rollout/`); the web-only CSV import lives in `src/inventory.py`. An import-isolation test guards it. It ships as a standalone `netrollout-cli.exe` (PyInstaller, `netrollout-cli.spec`, the web stack excluded; logs next to the exe via `runtime.logs_dir()`).

**Configuration** comes from env vars. `config/runtime.env` (under the NetRollout home, `src/runtime.py`; `RuntimeEnv`, `src/db/connections.py`) is loaded with override by `BackendServices` at startup; it holds only what a Server Management switch wrote, so it wins over the container environment (the installer's `.env`), which wins over the defaults.

| Variable | Purpose |
|---|---|
| `DATABASE_URL` or `PG_HOST` / `PG_PORT` / `PG_NAME` / `PG_USER` / `PG_PASSWORD` / `PG_SCHEMA` | Postgres. The URL form takes precedence |
| `REDIS_URL` or `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_PASSWORD` | Redis. The URL form takes precedence |
| `SECRET_KEY` | Flask session key. Required in a container (startup refuses without it); in development a missing key becomes a random per-run key with a warning |
| `PORT` | Internal app port Waitress listens on (default 8080). nginx forwards to it |
| `NETROLLOUT_ENCRYPTION_KEY` | Fernet key; else `~/.netrollout/encryption.key` (see below) |
| `ORCHESTRATOR_WORKERS`, `NETROLLOUT_PUBLIC_HOSTNAME`, `NETROLLOUT_HTTPS_PORT` | **Install-time seeds only** for the matching System Settings (§6), read when the setting's row doesn't exist yet. In Docker installs compose passes only `NETROLLOUT_HTTPS_PORT` (the published port); the hostname comes from `config/nginx/site.env`, written by the installer (stage 9.1) |
| `NETROLLOUT_SERVER_IPS` | The server's addresses (installer), included in every generated self-signed certificate |
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

## 2. Data Classes (`src/rollout/engine.py`)

Data classes are pure Python objects with no SQLAlchemy coupling. They exist at runtime only.

### `RolloutOptions`
Configuration flags for a rollout run. Pure data, no behavior.

| Name | Type | Description |
|---|---|---|
| `verify` | `bool` | Run post-push verification |
| `verbose` | `bool` | Print progress to console (CLI mode) |
| `webapp` | `bool` | Log for the page (HTML, the live log in Redis) rather than a console |
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
| `extra` | `dict` | Per-device attribute values for substitution, as the rolling-out user sees them (`InventoryView.attributes`) |

`endpoint` (property) — `ip:port`. This is what identifies a target: with NAT or port forwarding, several devices share one IP.

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `from_inventory` | `cls(row: Inventory, user_id, attributes=None) -> Device` | Factory. Decrypts the assigned SecurityProfile's credentials; only `user_id`'s mappings apply. It raises `ValueError` if the device has no profile |
| `unresolved` | `(commands) -> list[str]` | Why the commands can't be filled in on this device: one line per `$$TOKEN$$` without a mapping here or whose value is missing — checked by the launch and rollback routes before queueing, and again at the push |
| `commands_for` | `(commands) -> list[str]` | The commands with the device's tokens replaced; `SubstitutionError` when `unresolved()` finds anything |

The device's SSH is not the `Device`'s: `NetmikoSession` (`src/rollout/session.py`) holds the conversation — `connect(device)` (a context manager, also the connection tests'), `push(commands)`, `fetch_config()`.

### `DeviceResultDict`
TypedDict returned per device by `RolloutEngine.run()`. Fields: `device_ip`, `device_port`, `device_type`, `commands_sent`, `commands_verified`, `fetched_config`, `status` (a `DeviceStatus`), `action_needed`.

`VerifyResult` (NamedTuple: how verify went on one device) and `PushResult` (`session.py`: how the push went) feed `classify(push, verify, total, configuring)`, the status rules.

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
| `role` | `str(40)` | `"operator"` or `"admin"` (the app only checks for `"admin"`; stored as `"user"` before the `role_user_to_operator` migration) |
| `position` | `str(64)` | Nullable |
| `is_active` | `bool` | Default False |
| `is_approved` | `bool` | Default False |
| `otp_secret` | `str(255)` | Fernet-encrypted TOTP secret. Null = not enrolled |
| `must_change_password` | `bool` | Default False. Set for the seeded admin and by an admin password reset: every page redirects to `/account/password` until the user picks a password |
| `auth_type` | `str(20)` | `"local"` or `"ldap"` |
| `ldap_server_id` | `UUID` | FK → `LDAPServer`, ON DELETE SET NULL, nullable |
| `created_at` | `DateTime` | Set at creation |

**Relationships:** `inventory`, `security_profiles`, `variable_mappings`, `property_definitions`, `results`, `job_metadata` (all cascade-delete with the user), `ldap_server`

The factory account `admin`/`admin` is seeded at startup if missing (`src/db/install.py`), with `must_change_password` set: its first sign-in forces a new password.

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
| `var_maps` | `JSON` | The device's system property values, keyed by property name (`SYSTEM_PROPERTIES` in `src/inventory.py` — hostname, loopback_ip, asn, mgmt_vrf, mgmt_interface, site, domain, timezone, vrfs), shared by everyone who sees the device and set by who may edit it. List properties hold lists. Custom values are `DeviceAttribute` rows |

**Relationships:** `var_mappings` (many-to-many via `var_mapping_to_devices`), `security_profile`, `user`, `custom_attributes`

What a user's mappings and rollouts substitute is `InventoryView(session, viewer).attributes(devices)` (`src/inventory.py`): the system values plus that viewer's own custom values.

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
A user's own device attribute definitions (custom properties); their values are that user's `DeviceAttribute` rows. Unique `(name, user_id)`; deleting one deletes the user's values of it.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `name` | `str(64)` | Internal key name |
| `label` | `str(64)` | Display label |
| `icon` | `str(64)` | Bootstrap Icons class |
| `is_list` | `bool` | Whether the value is a list (enables index-based substitution) |

### `DeviceAttribute`
One user's value of one of their custom properties on a device (`device_attributes`). Unique `(device_id, user_id, name)`; both foreign keys `ON DELETE CASCADE` (a device's or a user's deletion removes the rows). Anyone who sees a device sets their own (an operator on a global device: `POST /inventory/<id>/attributes`); nobody else's values are read or written.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `device_id` | `UUID` | FK → `Inventory` |
| `user_id` | `UUID` | FK → `User` |
| `name` | `str(64)` | The property's name |
| `value` | `JSON` | A text, or a list of texts |

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
| `role` | `str(40)` | Role given to auto-provisioned users (`"operator"` or `"admin"`) |
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

### Input checks and `Validator` (`src/rollout/inputs.py`)
The pure checks are module functions, shared by the CLI, the import and the pages: `validate_ip` / `normalize_ip` (IPv4 and IPv6), `validate_port`, `validate_platform`, `tcp_reachable`, and the mapping checks `token_problem`, `property_name_problem`, `index_problem`. `Validator(logger)` checks only the CLI / import files (`validate_file_extension`, `validate_device_data`), reporting each problem through the logger. `SUPPORTED_PLATFORMS` is the list of supported Netmiko device types (derived from `PLATFORMS`):
- `cisco_ios`, `cisco_xe`, `cisco_nxos`, `cisco_xr`;
- `juniper_junos`, `arista_eos`, `fortinet`, `paloalto_panos`;
- `aruba_aoscx`, `checkpoint_gaia`, `hp_procurve`, `hp_comware`.

### `InputParser` (`src/rollout/inputs.py`)
One CSV format is shared by the CLI and web import. Required columns are `ip`, `device_type` and `port`. Optional columns are `label`, the credentials (`username`, `password`, `secret`), and attribute columns named after a property (by name or label).

**Methods:**
- `prepare_devices(raw_devices, require_credentials=True, check_reachable=True)` → `(devices, errors)` — CLI path; credentials required.
- `parse_commands(path)`.
- Static `import_from_inventory(rows, user_id, attributes)` → `Device`s (`attributes`: each device's values as that user sees them).

The web import is `import_csv(parser, path, user_id, …)` → `ImportReport` (`src/inventory.py`; it reads the rows with the parser's `prepare_devices`):
- it saves the attribute columns: system properties in `var_maps`, custom ones as the importer's `DeviceAttribute` rows;
- it turns credentials into security profiles (optional, on by default);
- it reports unknown columns;
- it does no reachability check.

`ImportReport` carries `errors` and `notices`.

### `RolloutLogger` (`src/rollout/log.py`)
Owns the logging I/O for one rollout job. It is constructed as `RolloutLogger(webapp, verbose, prefix="rollout", job_id=None, redis_client=None)`.
- **Log file:** it always writes one, `logs/{prefix}_{timestamp}_{job_id}.log` (threads take turns).
- **Where notable messages are shown** — errors, `important` ones, all of them when verbose: one `Echo` strategy, chosen at construction — `Console` (the CLI: ANSI colours) or, for a web job with a Redis client, `LiveLog` (HTML-escaped, appended to the Redis list `job:{id}:history` and published on `job:{id}:logs`; best-effort: a Redis error never fails the rollout). A web logger without a Redis client shows nothing.
- **`notify(message, color, important)`** — `color` is a `Tone` (ERROR, WARNING, SUCCESS, INFO). The engine and the sessions see it only as the `Notifier` Protocol (`session.py`).
- **`live_log`** — the `LiveLog`, which the job reads: `history()`, `follow(over)` (the page's stream), `close()` (readers get "done", the keys go). Redis is typed by what is used of it, the `KeyValueStore` Protocol, so this module never imports redis (the CLI `.exe`).

Log files are pruned by `LogPruner` (a `PeriodicTask`, started by the web app's entry point: at start, then every 24 h), using the *Log files* retention (default 60 days); the CLI prunes once per run (`prune_logs`).

### `ReachabilityChecker` (`src/inventory.py`)
Probes TCP reachability of `ip:port` targets in parallel and caches results in Redis for the *Reachability cache* period (a callable TTL, so a settings change applies immediately). Inventory uses it for the live status dots, and New Rollout uses it to flag unreachable devices.

### LDAP (`src/accounts/ldap.py`)
`Directory(server)` — one configured directory server (an `LDAPServer` row):
- `authenticate` / `user_bind` bind as the user, using a DN constructed from `cn_identifier` or one found by a service-account search (`find_dn`; with the "regular" bind type — "simple" binds only as the user signing in);
- `check_group_membership`, `user_details`, `fetch_base_dn` and `walk_tree` (for the explorer UI), `test_connection`, `test_user`.

Error handling:
- bad credentials (`LDAPBindError`, `LDAPInvalidCredentialsResult`) are a normal "no";
- any other LDAP exception becomes `LdapUnavailable`, so the login page can tell "wrong password" apart from "directory down".

---

## 5. Job Execution Classes (`src/rollout/engine.py`, `session.py`, `platforms.py`, `src/jobs.py`)

`src/rollout/platforms.py` holds what NetRollout knows per platform, with no I/O of its own (`PLATFORMS`: each `Platform` row names its `Finish`; `rejection()`, the config parser, `verify_commands()`); `src/rollout/session.py` holds one device's SSH conversation (`NetmikoSession`) and the rollout's `RunReport`; `src/rollout/engine.py` runs the devices in parallel and holds `classify(push, verify, …)` — the status rules; `src/jobs.py` owns the Redis job keys.

### `RolloutEngine`
Pure pipeline object: `RolloutEngine(param: RolloutOptions, devices: list[Device], commands: list[str])`. `run(cancel_flag, logger) -> list[DeviceResultDict]` (`logger`: any `Notifier`, wrapped in a `RunReport` that also collects what only a person can resolve, per device):
1. **Per device, in parallel** (`ThreadPoolExecutor(max_workers)`):
   - it substitutes `$$TOKEN$$`s (`Device.commands_for`);
   - `NetmikoSession(device, report).push(commands)` pushes over Netmiko, one `send_config_set(…, enter_config_mode=False)` per command after entering config mode once — exactly as typed (Netmiko's default re-checks config mode per call, and Aruba CX's driver only recognises `(config)#`, so inside a section it failed); a typed `end` followed by more config commands is refused as on the device; a refused command (`rejection()`: one list of vendor error strings, the command's own echo skipped) is logged with the device's reply and the rest are still sent;
   - it **finishes the way the platform needs** — the platform's `Finish` (`platforms.py`), acting on the session it is handed (`before_leave` / `after_leave` / `in_new_session`): `SaveConfig` — `save_config()` after leaving config mode (Cisco IOS/IOS-XE/NX-OS, Arista, Aruba CX, HP ProCurve/Comware); `Commit` — `commit()` *before* leaving it (Junos, PAN-OS, IOS-XR — leaving discards uncommitted changes; up to `COMMIT_TIMEOUT` = 300 s; a failed commit is a failure, Junos then `rollback 0`, PAN-OS keeps the candidate and the log says so); `RunCommand("save config")` (Check Point Gaia); `NoFinish` (FortiOS, after closing any open config block with `end`);
   - per-platform details, checked against the vendor documentation and Netmiko 4.6.0's source (2026-10-02): Junos configures with **`configure private`** (our commit can't take another admin's pending shared edits along, and our `rollback 0` can't wipe them; Junos refuses private mode while someone has uncommitted shared edits — that device then fails with the reason); PAN-OS discards a failed commit with `revert config`, else `load config from running-config.xml` (only if both are refused does the log ask to discard on the device); FortiOS checks `cfg-save` after the push and runs `execute cfg save` when it is manual or revert (else the change is lost at reboot / undone after the revert timeout); Check Point Gaia switches an account that lands in expert (bash) to clish (`clish`) for both the push and the config fetch — bash would silently swallow every `set …` — and refuses only if that fails; Aruba CX sends `end` before leaving config mode (Netmiko only recognises `(config)#`, so from `(config-if)#` its exit did nothing);
   - a prompt that changes mid-push (e.g. a new hostname) ends that session: the save then runs from a fresh one ("applied, and saved from a new session"), or the log says it wasn't saved;
   - the session is always closed; the cancel flag is honoured between devices.
   - **Only a person can resolve** (another admin's work, or the device's state is unknown): Junos refusing `configure private` while someone has uncommitted shared edits; a save refused (e.g. a Gaia config lock held by another session); a commit still running after `COMMIT_TIMEOUT`; PAN-OS changes left in the candidate when both discards are refused; Gaia stuck in expert even after `clish`; FortiOS `execute cfg save` refused. With Verify on, a device whose config couldn't be read keeps the status of its push (couldn't verify ≠ not configured) but is flagged — the change was applied and nobody checked it. Each is logged as one red `ACTION NEEDED — <ip:port>: <what to do>` line (live log, log file, CLI console), and the rollout summary ends with `ACTION NEEDED on N devices (…)`. The web app also stores it per device (`device_results.action_needed`): when a live log ends, Active Jobs shows a completion card (job note + `[id]`, status, counts, the action-needed instructions, *View in Results*); the Results page shows a badge on the job and the instructions in it; the Dashboard's Recent Jobs mark it — so it doesn't depend on anyone reading the log; the CLI prints it on the console.
2. **Verify** (if on, only on devices the push applied to): `NetmikoSession.fetch_config()`, then `verify_commands(device_type, config, commands)` — one verdict per command: *verified*, *not configured*, *still configured* (a removal that didn't take), *not verifiable* (navigation like `exit`/`end`/`next`, operational like `write memory`/`commit`), *variable* (an unresolved `$$TOKEN$$`, only on the Verify Diff page).
   - **Indented configs** (Cisco-style, FortiOS): a plain indentation parser; typed commands are flat (the device tracks the mode), so each is placed in its section from the config's own structure — a typed line that is a section there opens it, `exit`/`next`/`exit-…` close one (`end` too on FortiOS, all on Cisco), a command found only at an outer level leaves the section, one found nowhere stays put. A command must exist at its place; `no`/`undo`/`unset`/`delete X` passes when `X` is gone.
   - **Flat configs** (Junos `display set`, PAN-OS set format, Gaia): one line per setting; `edit`/`up`/`top` move the prefix relative `set`/`delete` extend; `delete X` passes when no `set X…`/`add X…` line remains.
   - A config that can't be fetched means "couldn't verify", not "failed".
3. **Status:** *cancelled* (never connected), *failed* (nothing applied: no connection, a failed commit, or every configuring command refused), otherwise from verify (all checkable verified → success; none → failed; else partial) or, without verify, from the refusals (none → success, some → partial).
4. **Summary:** it logs one summary line for the rollout.

Known limits (Phase 5b: Deep Diff with hier_config + netutils): typed abbreviations (`int gi1`), values the device rewrites (hashed secrets, normalised values like Junos `area 0` → `0.0.0.0`, Comware VLAN ranges `101 to 102`, hidden defaults — FortiOS `show` omits values equal to their default), "already there before the rollout", multi-line banners; PAN-OS prints one setting per line, so a compound typed `set … from x to y action allow` never matches (type one setting per line); Junos `up` is taken as leaving the whole last `edit` (it really goes up one statement level). The Verify Diff route (`/results/config_diff`) returns the same verdicts, so the page can't disagree with the rollout.

### `RolloutJob`
Lifecycle owner, constructed as `RolloutJob(job_id, user_id, engine, options, redis_client)`. It owns the thread, cancel flag, engine, logger and its live log.
- **Methods:** `claim()` (a queued job is claimed once, under the orchestrator's lock: by the dispatcher to start it, or by a cancel / the drain to record it), `start(on_complete)`, `cancel()`, `cancel_before_start(reason)` and `is_over()`, plus `follow_log(over)` (the live log for the stream) and `log_cleanup()`.
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
  → _finalize: DeviceResult rows to Postgres (ResultRecorder: retried, else logs/unsaved-results-<job>.json);
              DEL meta, SREM user_jobs, active−1; log keys cleaned
  → release the slot (always, in finally)
```

`cancel(job_id)` sets the job's cancel flag and meta `status=cancelling`.

**Drain** (`drain(deadline)`, run by `lifecycle.Shutdown` on SIGTERM or the admin Restart): `submit()` raises `Draining` from then on (the routes show `DRAINING_MESSAGE`); queued jobs are recorded as cancelled, device by device, with the reason in their log (a job's definition lives only in this process, so it could never run after the restart); running jobs may finish for `deadline` seconds, then are cancelled and given up to 60 s to record — a job still running after that is cut off by the exit and leaves no result record (its log file remains). `counts()` returns running/queued from memory (the health endpoint, the Restart choice). **Pause** (`pause()` / `idle()` / `resume()`, a database move): `submit()` raises `Paused` (a `Draining`), nothing is cancelled; `idle()` is true once no job is queued, running or still being recorded (`ResultRecorder.busy()`). A crash or `SIGKILL` still loses queued jobs silently.
 The Active Jobs page and the admin views read job state from the Redis `job:*:meta` hashes. The Prometheus collector reads `netrollout:active_count` and `netrollout:pending_count`.

---

## 6. DB Layer (`src/db/`)

### `PostgresConfig` / `RedisConfig`
Frozen dataclasses built from env vars (`unload_env()`: the URL form, or the individual `PG_*` / `REDIS_*` vars). `get_url()` returns the connection string, `place()` where the data is whatever the login (Postgres: host, port, database, schema; Redis: host, port, db), `describe()` the same for people (no password), and `to_env_dict()` returns what to write to `config/runtime.env` (every key of the service, blank when unused — URL, password, schema — so nothing inherited from the container environment can override the switch). A new config object is created for each hot-reload.

### `ServiceConnection` → `PostgresConnection`, `RedisConnection`
`ServiceConnection` (an ABC, a template method) is the rule both services share: `mode(env)` — bundled or an organisation's (`ServiceMode`): by host (the compose names, localhost) until a switch remembered the bundled one's address, then by the whole place; `bundled_config(env)`; `switch_to(config, env)` — `swap()` the live connection, then write `config/runtime.env` (every key of the service, blank when unused; leaving the bundled service its URL, `NETROLLOUT_BUNDLED_DATABASE_URL` / `_REDIS_URL`), `undo()` on a failure after the swap. The subclasses supply the hooks: `config_from_url`, `live_host`, `own_url`, `swap`, `after_swap`, `undo`.

`PostgresConnection` wraps a SQLAlchemy engine (`build_engine(config)`, `pool_pre_ping=True`; a schema becomes the connection's `search_path`).
- **`get_session()`:** a context manager that commits on a clean exit and rolls back on an exception.
- **`reload_db(config, install_flag=True)`:** atomically swaps the engine, and raises `RuntimeError` if the new server is unreachable (nothing changed).

`RedisConnection` wraps a `redis.Redis` client, with the same `reload_db(config)` pattern. `REDIS_UNAVAILABLE` (connection errors plus timeouts) is the exception tuple that callers catch. There is no module singleton: the client is always reached through `app.backend.redis.client`, so a hot swap is picked up.

### `BackendServices` (`src/db/connections.py`)
The composition root for infrastructure. It is constructed once in `launch_app()` and attached to `app.backend`.

```python
BackendServices(env=None)   # env: the RuntimeEnv (tests pass their own); None: config/runtime.env
#   load it (override) → PostgresConnection() → install() → RedisConnection()
# app.backend.postgres   →  PostgresConnection
# app.backend.redis      →  RedisConnection
# app.backend.settings   →  SettingsStore (System Settings; built on first use)
```

**`health()`:** returns `{"POSTGRES": bool, "REDIS": bool}`. Each service is checked independently, so one failure doesn't mask the other.

**`move_postgres(config)`:** the database move's last step (`src/webapp/db_move.py`, after the data was copied): `switch_to` the copy — reconnect, then `install_extras` (Grafana's read grants; **never** `install()`, which would seed the factory admin into the copy) — anything failing after the reconnect puts the connection back on the old database. **`reload_redis(config)`:** the live Redis switch (`db_move.switch_redis`). `bundled_postgres()` / `bundled_redis()`: where the way back goes. Both write `config/runtime.env` atomically, owner-only.

**`connection_modes()`:** `bundled` or `external` per service (`ServiceConnection.mode`).

**`encrypted_sample()`:** returns one stored Fernet token, for the startup key check.

### `install()` (`src/db/install.py`)
Runs at every start (not after a database move: the copy already holds the data and its seeds). It is idempotent.
1. **Migrations:** `alembic upgrade head` on the app's own connection.
2. **Factory admin:** seeds `admin`/`admin` if missing.
3. **Settings:** seeds any missing System Setting rows.
4. **Grafana's read access** (`install_extras`, also run alone by a database move).

The nightly clean-up (`src/db/retention.py`, in the app — no pg_cron since 9.9a), daily at 03:00 server time; its last outcome is shown on System Settings → Retention. Each statement reads its period from `system_settings` when it runs, so a change applies at the next run without a restart:

| Job | Action | Setting (default) |
|---|---|---|
| `device_result_retention` | delete `device_results` rows | *Job records* (30 d) |
| `job_metadata_retention` | delete `job_metadata` rows whose job has no results left | *Job records* (30 d) |
| `device_result_config_retention` | set `fetched_config = NULL`, keep the row | *Config snapshots* (7 d) |
| `audit_log_retention` | delete `audit_log` rows | *Audit log* (90 d) |

### System Settings (`src/db/settings.py`)
Admin-editable runtime settings. The `system_settings` table is the **only runtime source**.

**The registry `SETTINGS`:** each setting is one of the `Setting` kinds (an ABC: `IntSetting` — a range, and `sql_value()` for the retention statements —, `TextSetting` — trimmed, optionally a format and a length —, `ChoiceSetting` — one of a fixed list, a dropdown on the page: `BackupSchedule`, `Weekday`), with a key, label, help text, card and default; `parse()` checks a typed value, `coerce()` a stored one. It also has an `applies` value, which says when a change takes effect. It can optionally name an install-time env var, and it marks whether the retention statements read it via SQL.

| Setting | Card | Default | Range | A change applies |
|---|---|---|---|---|
| Job records (`job_retention_days`) | Retention | 30 | 1–3650 | next nightly clean-up |
| Config snapshots (`config_snapshot_retention_days`) | Retention | 7 | 1–3650 | next nightly clean-up |
| Audit log (`audit_retention_days`) | Retention | 90 | 7–3650 | next nightly clean-up |
| Log files (`log_retention_days`) | Retention | 60 | 1–3650 | next daily log clean-up |
| Concurrent rollout jobs (`orchestrator_workers`) | Rollouts | 4 | 1–32 | after restart (seed: `ORCHESTRATOR_WORKERS`) |
| Devices in parallel per job (`device_parallelism`) | Rollouts | 10 | 1–64 | next rollout |
| Reachability cache (`reachability_cache_seconds`) | Rollouts | 60 | 10–3600 | immediately |
| Hostname (`public_hostname`) | Access | "" (no canonical name) | — | immediately (seed: `NETROLLOUT_PUBLIC_HOSTNAME`) |
| HTTPS port (`https_port`) | Access | 443 | 1–65535 | once confirmed on the new port (seed: `NETROLLOUT_HTTPS_PORT`) |
| Sign out after inactivity (`session_idle_minutes`) | Sessions | 15 | 5–480 | within 30 s |
| Scheduled backup (`backup_schedule`) | Backups | daily | off / daily / weekly | next scheduled time |
| At (`backup_time`) | Backups | 02:00 | HH:MM | next scheduled time |
| On (`backup_weekday`) | Backups | Sunday | a weekday | next scheduled time |
| Keep (`backup_keep`) | Backups | 14 | 1–365 | next scheduled backup |

- **Seeding:** `install()` inserts a row for every missing setting. The value is the env seed if it is set and valid, else the default. Existing rows are **never overwritten**, and env vars are ignored after that.
- **Rules:** cross-setting rules are declarative `RULES`, sent to the page as data (`rules_for_client()`) and enforced on both server and client:
  - log files are kept at least as long as job records;
  - config snapshots are kept no longer than their job record.
- **`SettingsStore`:**
  - `get(key)` coerces a bad stored value to the nearest valid one and never raises;
  - `values()` and `list_for_display()`;
  - `plan(values)` validates without writing (the changes it would make); `update(values, user_id)` is all-or-nothing, runs range and rule checks, and returns the changes (`Change`), which are audited — a failure raises `SettingsError` ({key: message});
  - `reset(key)` writes the default;
  - `restart_only_values()` / `restart_pending()` drive the "restart pending" marker.
- **`sql_value(key)`** (`IntSetting.sql_value`): gives the retention statements a `COALESCE((SELECT value …), default)` expression.
- **The hostname and HTTPS port reach nginx** (Phase 4 stage 8, `src/access/`; the pages save them through `Access.save`, `service.py`): a saved hostname applies live — the app writes values (`config/nginx/site.env`), never nginx syntax; nginx's watcher validates, renders and reloads; a refused change is rolled back. A new HTTPS port goes to the host-side port helper (stage 9) as a confirm-or-roll-back trial; without it, `netrollout apply`.

---

## 7. Webapp Layer (`src/webapp/`)

### `__main__.py` — entry point (`python -m src.webapp`)
1. **App:** `create_app()`. A `StartupError` (missing secret in a container, encryption key problem) exits with a readable message.
2. **SIGTERM:** `app.shutdown.begin(drain_seconds(), restart=False)` — drain, then exit (`docker stop`, `netrollout stop` / `update`).
3. **Background** (each a `runtime.PeriodicTask`): `LogPruner` (daily), `CertificateUpkeep` (hourly: previous hostnames leave the self-signed certificate), `BackupScheduler` and `NightlyCleanUp` — the last two wait (`hold`) while a database move runs.
4. **Announcer:** in development, the startup announcer below; in a container, one line with the expected URL (`container_announcement`) — the host-side check is the installer and `netrollout status` calling `/_netrollout/health`.
5. **Serve:** Waitress `serve()` on `0.0.0.0:$PORT` (default 8080).

### `startup.py` — reverse-proxy check (development)
In a container nothing is probed: its console isn't watched and the published port isn't reliably reachable from inside, so a probe would report working setups as broken (the System Settings *Test* button says so too). Each run creates a random instance token, served at `/_netrollout/instance`. Once Waitress answers, a background check runs in two steps:
1. **Locally:** it connects to the local nginx, presenting the public hostname (SNI/Host).
2. **Publicly:** it tries the public URL.

The public URL comes from the *Hostname* / *HTTPS port* settings; with no hostname set, it is `https://localhost` (the old auto-detect from `docs/nginx/nginx.conf` was retired in stage 8 with that file). The check confirms that nginx forwards to *this* process, then prints the address people should use, with a message that fits the case (proxy missing, wrong upstream, DNS, …). On a desktop launch it opens that address in the browser (`NETROLLOUT_OPEN_BROWSER=0` disables this).

### `build.py` — app factory
`launch_app(backend=None)` is the composition root (tests pass their own `BackendServices`).
1. **Secrets:** resolves `SECRET_KEY` first and requires the encryption key in a container (fail fast); seeds the hostname from `site.env` (the installer's) before the settings are seeded.
2. **Backend and encryption:** builds `BackendServices` (unless given), then runs the encryption key check.
3. **Settings:** reads the restart-only settings; these become `app.settings_started_with`.
4. **Services:** the orchestrator, `Maintenance`, `WebServices`, then the Flask app (`NetRolloutApp`, `template_folder='../../templates'`, `static_folder='../static'`), with `Shutdown`, `Access` and `DatabaseMove`.
5. **Configuration:** the instance token, the config, the maintenance gate (the first request hook), extensions and handlers, the Prometheus collector.
6. **Sessions:** `app.sessions` (`SessionStore`) clears every `redis_session:*`, so every restart logs everyone out; leftover job state is cleared (`clear_stale_jobs`); nginx gets the saved hostname (`sync_at_start`).

Blueprints are registered in `create_app()`.

```python
app.backend       →  BackendServices
app.web           →  WebServices (the audit, the reachability checks)
app.orchestrator  →  RolloutOrchestrator
app.shutdown      →  lifecycle.Shutdown (drain, then exit; relaunch in dev)
app.maintenance   →  lifecycle.Maintenance (a database move's pause and lock)
app.db_move       →  db_move.DatabaseMove
app.access        →  access.service.Access (hostname, HTTPS port, certificate)
app.sessions      →  accounts.users.SessionStore
app.instance_token, app.settings_started_with, app.app_port   (typed on NetRolloutApp)
```

`src/webapp/app.py` types these: `NetRolloutApp(Flask)` declares them, and the pages import its `current_app` (the same proxy, typed), so `current_app.backend.postgres` is checked like any other attribute.

### `hooks.py` — module-level Flask extensions and the request hooks
Extensions are created at module level so blueprints can import them at definition time.

```python
login_mng = LoginManager()         # login_view = "auth.home"
conn_limit = Limiter(...)          # rate limiting
csrf = CSRFProtect()
```

- **`register_extensions(app)`:** calls `init_app()` on each and initializes `PrometheusMetrics` (`/metrics`).
- **`register_auth(app)`:** registers the user loader and the session checks (`enforce_session_lifetime`, the forced password change; the limits: `src/accounts/users.py`).
- **The request's side of the sessions** (the store is `app.sessions`): `session_seconds_left`, `mark_signed_in`, `signed_in_user(db_session)`, `end_user_sessions` (→ `SessionStore.end_for`), `signed_in_users` (→ `SessionStore.signed_in`).
- **`register_handlers(app, backend)`:**
  - CSRF error handler;
  - service-unavailable (503) handler for Postgres `OperationalError` and Redis connection/timeout errors, which renders a page saying which service is down;
  - invalid-encryption-key handler, which renders `key_error.html` explaining what to do.

### `http.py` — shared web helpers
**`WebServices(backend, maintenance)`**, attached to `app.web`:
- `audit(action, *, object_type, object_id, object_label, detail, success, username, actor_id)` — the request's audit row through `AuditTrail.record` (`src/audit.py`: its own session, an `AuditAction`, an `Actor`); printed instead while maintenance blocks writes
- `audit_trail` — the `AuditTrail` (also for the database move and the scheduler, which have no request)
- `reachability` — the shared `ReachabilityChecker`

**Module functions and types:**
- responses and decorators: `ok()`, `err()`, `require_admin`, `with_json`, `with_form`, `flash_redirect`;
- `is_background()` (a page's own request: `X-NR-Background: 1`, `?_bg=1`, the live log) and `Caller` — an enum whose members are the signs that make a request a script's in each situation (`SCRIPT`, `SESSION_CHECK`, `JSON_BODY`, `STREAM_AWARE`), asked with `wants_json()`;
- `viewer()` — the signed-in user as the data layer's rules see them (`Viewer`, built once per request);
- `load_owned(session, model, obj_id, can_access=None)` — a row the request may act on, else `NotFound` (none and not yours are the same answer); `Refused` — a refusal in words; both raised in the session block and caught by the route outside it.

The domain rules live in the services, not here: device visibility, edit rights and shared endpoints in `InventoryView` (`src/inventory.py`), the jobs' owner-or-admin rule in `JobResults` (`src/results.py`); `compile_query_rules(node, allowed_fields)` (jQuery QueryBuilder → SQLAlchemy expression) with `QUERY_OPS` in the analytics blueprint; `build_kpi(results_30d, label_map)` and `job_status` in `src/jobs.py`.

**Constants:** `SYSTEM_PROPERTIES` (`src/inventory.py`). The analytics field and column lists live in `analytics.py`, `validate_mapping_fields` in `mappings.py`.

### `blueprints/`
Each blueprint owns its routes and route-specific helpers. Blueprints reach `app.web`, `app.backend` and `app.orchestrator` through `current_app` inside routes, never at module level. Every route except the public ones requires login, and every `/admin` route requires the admin role. This is enforced for all routes by `tests/integration/test_route_matrix.py`.

| Blueprint (file) | Prefix | Routes |
|---|---|---|
| `auth` | — | `/`, `/login`, `/register`, `/otp_enroll`, `/otp_verify`, `/logout`, `/account`, `/account/password` (forced or voluntary change; local users) |
| `jobs` | — | `/dashboard`, `/active_jobs`, `/results` (`?job=<id>` opens that job), `/results/summary/<job_id>` (a finished job in a few lines: the completion card), `/results/config_diff/<job_id>/<ip>`, `/results/download_log/<job_id>` |
| `rollout` | `/rollout` | `/new`, `/start`, `/cancel`, `/stream/<job_id>` (SSE), `/rollback/<job_id>` |
| `inventory` | `/inventory` | list, `/create`, `/test_connection`, `/reachability`, `/<id>/edit`, `/<id>/mappings`, `/<id>/delete`, `/import_csv`, `/bulk_assign` |
| `security` | `/security` | list, `/create`, `/quick_create`, `/<id>/edit`, `/<id>/delete`, `/<id>/test` |
| `mappings` | `/mappings` | list, `/create`, `/quick_create`, `/<id>/edit`, `/<id>/delete`, `/bulk_assign` |
| `properties` (in `mappings.py`) | `/properties` | list, `/create` + `/quick_create`, `/<id>/edit`, `/<id>/delete` |
| `analytics` | `/analytics` | KPI page, `/query` (POST, own results) |
| `admin_users` | `/admin` | admin home, `/users`, `/users/<id>/<action>` (approve, enable, disable, promote, demote, delete, reset_2fa, terminate_session), `/users/<id>/reset_password` (JSON: a temporary password, shown once), `/users/bulk/<action>`, `/sessions`, `/sessions/<id>/kick` |
| `admin_servers` | `/admin/server` | Server Management page; `/database/{sql,prepare,check,move,move-back,move/status,move/cancel}`; `/redis/{test,save,back}`; `/certificate` (upload), `/certificate/selfsigned`; `/rollouts`; `/restart` (with rollouts running or queued: 409 unless `mode` is `when_finished` (drain) or `now` (cancel)) |
| `admin_ldap` | `/admin/server` | the 12 LDAP routes: `/ldap`, new, save, delete, test, test_user, fetch_dn, explore, import, groups list/toggle/delete |
| `admin_observability` (in `analytics.py`) | `/admin` | `/analytics`, `/analytics/query` (the audit log's query builder), `/active_job_count` |
| `admin_audit` | `/admin` | `/audit` (the audit log) |
| `admin_settings` | `/admin/settings` | page, save (POST), `/<key>/reset`, `/port` (status), `/port/confirm`, `/port/retry`, `/test` (public URL check) |
| `admin_backups` (in `admin_settings.py`) | `/admin/backups` | list, POST = back up now, `/<name>` download, `/<name>/delete` |
| `system` | — | `/_netrollout/instance` (public; no session written), `/_netrollout/health` (public; Postgres/Redis up, rollout counts, draining, version; 200 or 503) |

Plus `/metrics` (Prometheus; `404` at nginx, scraped from the app directly).

**Auth flow:**
```
POST /login
  local user  →  check_password_hash → approval/active gates → start_otp_flow()
                 (the factory "admin" account skips 2FA)
  ldap user   →  Directory.user_bind() → approval/active gates → complete_login()   (no 2FA)
  unknown     →  login_ldap_group() → Directory.check_group_membership() → Accounts.new_ldap() → complete_login()

start_otp_flow()
  → session["pre_auth_user_id"] = user.id
  → otp_secret present? → /otp_verify
  → no secret?          → /otp_enroll (first-time setup)

complete_login()
  → login_user(user) → audit → redirect jobs.dashboard
```

Admins can reset a user's 2FA; the user re-enrols at the next login.

**Passwords** (local accounts): one rule, `src/accounts/users.py` — at least 8 characters with at least 2 of letters / digits / special characters, ASCII only, not containing the username; a change must differ from the current one. Registration, `/account/password` and generated temporary passwords all use it; `templates/_password_rule_script.html` mirrors it in the pages. While `must_change_password` is set, a `before_request` gate (`hooks.py`, allowlist `PASSWORD_CHANGE_ALLOWED`: the change page, logout, static, instance/health) redirects pages to `/account/password` and answers fetch calls with 403. A change (rate-limited like login) clears the flag, rotates the session id, **signs the user out of every other session** and is audited (`auth.password_change`). An admin *Reset password* on another local user (not LDAP, not themselves, not the factory admin) replaces the stored password with a random temporary one shown once, sets the flag and **signs the user out everywhere** (`user.reset_password`; the password is never logged). Admin *Terminate Session* signs out everywhere too.

**Signing a user out everywhere** (`src/accounts/users.py`, `SessionStore.end_for`; the pages call `webapp/hooks.end_user_sessions`): every `redis_session:*` is decoded with flask-session's serializer and the user's (`_user_id`) are deleted — complete and cheap at this scale; a per-user index would be faster but miss existing sessions and need expiry cleanup (the earlier `user_session:<id>` pointer, latest sign-in only, was dropped in the 2026-10 clean-up: nothing read it). Live Sessions and the Users page read the sessions the same way (`SessionStore.signed_in`).

**Real-time logs:** `/rollout/stream/<job_id>` is Server-Sent Events (`RolloutJob.follow_log` → `LiveLog.follow`). It subscribes to the pub/sub channel `job:<id>:logs`, then replays `job:<id>:history` (LRANGE) and tails the channel, skipping a numbered message (`<n>	<line>`) the history already held; a heartbeat comment every 0.5 s; a queued job's stream first says it's queued. The response sets `X-Accel-Buffering: no` so nginx doesn't buffer.

---

## 8. Observability Stack

Optional sidecar services. The Flask app runs independently and is unaffected when they are down. Configs live under `deploy/{grafana,prometheus,loki,alloy}` (an optional compose profile; Grafana served through nginx at `/grafana`).

| Service | Role |
|---|---|
| PostgreSQL | Historical business metrics. A direct Grafana datasource through the `grafana_reader` read-only user, which can read exactly `device_results`, `job_metadata` and `audit_log` (`GRAFANA_TABLES`, granted by `install()`) |
| Prometheus | Live metrics: active and pending jobs, Flask request rates and latencies (scrapes the app's `/metrics` directly at `app:8080`; nginx answers `/metrics` with 404) |
| Loki + Grafana Alloy | Log stream: the log files shipped by Alloy (promtail is end-of-life) with labels `prefix` and `job_id` from the file name, each entry at its own timestamp (local time, `TZ`), continuation lines joined to their entry; Loki keeps 60 days. Entries with old timestamps (e.g. files written while Loki was down) become searchable once Loki writes them to storage, not immediately |

**Custom Prometheus collector** (`RolloutSessionCollector`, `src/webapp/build.py`): reads `netrollout:active_count` and `netrollout:pending_count` from Redis and exposes the `netrollout_active_jobs` and `netrollout_pending_jobs` gauges.

**Access:** Grafana is served by nginx at `/grafana/` to signed-in NetRollout **admins only**: for every request nginx asks the app (`auth_request` → `/_netrollout/grafana-auth`: 204 + the username / 401 / 403) and passes the username in `X-WEBAUTH-USER`, which Grafana trusts (proxy auth; the browser's own header is replaced). Admins are Grafana Editors; there is no Grafana login form. Not operators: free Grafana lets anyone signed in query every datasource through its API (per-datasource permissions are Enterprise), which would bypass NetRollout's own-jobs-only rule — a separate organization for operators is a post-v1 item.

**Layout** (kept by the `grafana-setup` service, `deploy/grafana/setup.py` — baked into the app image with the dashboards — re-applied every 5 min):

```
NetRollout          the shipped dashboards — view-only, re-imported on every update
├── Operations      Operations Overview, Job Analytics
├── Jobs            Job Details
└── Security        Audit & Security
Custom              the admins' own (Save as, new dashboards, subfolders) — never touched by updates
```

**Four Grafana dashboards**, dashboard v2 files in `deploy/grafana/dashboards/<subfolder>/`, imported through Grafana's v2 API (`metadata.name` is the stable id); `tests/unit/packaging/test_monitoring_config.py` checks every datasource they reference is provisioned (by uid — keep the uids) and every subfolder is one the setup imports:

| Dashboard | File | Datasources | Purpose |
|---|---|---|---|
| Operations Overview | `operations_overview.json` | Prometheus | Live job state, request rates, p99 latency |
| Job Analytics | `job_analytics.json` | PostgreSQL | Historical outcomes, platform breakdown, heatmap |
| Job Details | `job_details.json` | PostgreSQL + Loki | Drill-down by `$job_id`: device results, log stream |
| Audit & Security | `audit&security.json` | PostgreSQL | Audit trail, failure rates, top actors |

---

## 9. Installation, the setup core and the port helper

**One decision-maker, two thin host scripts.** What installing and running NetRollout decides and writes lives once, in the **setup core** (`src/setup/`, run inside the app image with the install folder mounted: `python -m src.setup <command>`); the host scripts — `windows/manage.ps1` and `linux/netrollout.sh` — only do what needs the host (checks, Docker, owners and permissions, auto-start, shortcuts) and call it. The setup core's commands (`src/setup/__main__.py`, `COMMANDS`):

| Command | What it decides |
|---|---|
| `init` / `check` | the install's answers (hostname, HTTPS port, monitoring, the organisation's certificate, timezone) → `.env`, `config/nginx/site.env`, the folders; `check` validates only |
| `prepare-start`, `status` | before a start (busy ports, the server's IPs) / the `netrollout status` report |
| `restore-key` | after a restore: the backup's encryption key into `.env` |
| `check-update`, `upgrade`, `release` | an update's direction (only newer: the same version or an older one refused, run with the *installed* image; Setup checks it before its first page too), `.env`'s new keys after it, Linux's release download and unpack |
| `port-ready`, `port-next`, `port-open`, `port-trying`, `port-close` | the port helper's steps (below) |

**Windows** installs only through **NetRollout Setup** (`windows/installer/netrollout.iss`, Inno Setup): the wizard collects the answers, runs `manage.ps1 install`, updates in place over an existing install (`prepare-update` before any file is replaced, `update` after), and puts NetRollout Manager (`windows/manager/`, C#) in `bin\` — the people's front end: status, start / stop, backup / restore, updates from GitHub's releases. **Linux**: `linux/install.sh` + `netrollout.sh` (the same commands, a numbered menu, `update` from a release zip). The details — every command, Setup's pages, the update flow — are in CLAUDE.md (Install / manage).

**A release** (`tools/build_release.py`, stage 10's job runs it): `netrollout-<v>-linux.zip` (the install folder under `netrollout/`: `bin/`, the compose files, `deploy/`, `VERSION`, `LICENSE`, a README — the update contract is in `src/setup/update.py`), `NetRollout-Setup-<v>.exe`, `netrollout-cli-<v>.exe`, `SHA256SUMS`. `SHIPPED` in `build_release.py` is the one list of the files an install gets; the images come from Docker Hub.

**The port helper** (a new HTTPS port, System Settings): the port is Docker's mapping — nginx always listens on 443 inside — so a change needs nginx recreated, on the host, never by the app. The app writes the request into `config/nginx/site.env` (`src/access/port.py`, `site_env.py`) and reads the helper's answers from `config/apply-status.json`; the helper (`src/setup/port.py` decides, the scripts' `apply` does the Docker part) opens the new port next to the old one as a trial (`config/port-trial.yaml`, listed in `.env`'s `COMPOSE_FILE` while it lasts), keeps it once a browser confirms it from the new port, or rolls back after 120 s. It runs automatically — Windows: `NetRollout Manager.exe --helper` (headless, started at sign-in); Linux: a systemd path unit on `site.env` — else by hand (`netrollout apply`). The contract, step by step, is in those two modules' docstrings.

**Bring your own database / Redis** (Server Management): the database *move* (`src/db/move.py`, `src/webapp/db_move.py`, maintenance mode) and the Redis switch — see §6 and CLAUDE.md (Database move).

---

## 10. Key Design Decisions

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
| Runtime settings | `system_settings` table as the only runtime source; env only seeds it | One truth that admins can change in the UI, which the retention statements read in SQL, and which is never silently overridden by env |
| Settings rules | Declarative, shared with the page | The same rules enforced on the server and in the browser, with no duplicated logic |
| Flask extensions | Module-level with `init_app()` | Must be importable by blueprints at definition time, before an app context exists |
| `app.web` / `app.backend` | Set on the app object in `launch_app()` | Available through `current_app` in any request context; avoids circular imports |
| Blueprint `url_for` | Always prefixed (`"auth.home"`, `"jobs.dashboard"`) | Blueprint namespace prevents endpoint name collisions |
| `_SafeRedisSessionInterface` | The only session interface, set in `launch_app()`; no `Session(app)` | Flask-Session's `Session(app)` only builds an interface from `SESSION_*` config - ours replaces it, so it isn't called |
| LDAP auto-provisioning | A matched group rule creates a user on first login | Zero-touch onboarding; role assigned from the group mapping |
| `AuditLog.actor_username` | Denormalized | Audit records survive user deletion; no orphaned FK |
| `reload_db()` | Raises `RuntimeError` if the new server is unreachable | Silent failure would leave the app pointing at a broken connection |
| Startup proxy check | Per-run token fetched through nginx | Proves the proxy reaches *this* process (not a stale one) and tells the operator the right URL |
| Domain services | `Service(session, viewer)` per area; raise, the route catches outside its session block | One home per rule and query; a refused change rolls back instead of committing half (§11) |
| Multi-system changes | The `Access` facade (settings, certificate, `site.env`, the port request) | One all-or-nothing call instead of undo steps written by hand in each route |

---

## 11. The object model

Built in the OOP redesign before rc1 (2026-10-09/10; behaviour unchanged). The rules it follows:
- **A class only for a real thing with state and behaviour** — a rule without state stays a function (the input checks, `classify`, `job_status`, the file protocol of `site.env` / `status.json`).
- **An ABC** when we own the whole family and the base shares code: an incomplete variant fails when it's created, not in production. **A Protocol** when the implementer is foreign (Netmiko, redis-py, flask-session) or a test fake stands in: structural, checked by mypy, nothing shared. **A `Callable`** for a one-call contract (`hold`, `retention_days`, the per-call `postgres` / Redis lookups that follow a database move or a Redis switch).
- **An enum** when a closed set of values is spelled twice or crosses a boundary (Redis, a file, the database, JSON, a script's exit code); its values are what those places hold, so they never change — a new value is a new member.
- **`isinstance` only to validate external data** (JSON from a file, a request or an API, Redis bytes), never to choose behaviour.
- **One file = one concern**; a name says what it is, not how it's used.
- **Services raise, routes catch outside the session block.** A domain service works in the caller's session (the caller commits) and raises its refusal; the route catches it after the `with` block, so the session rolls back — a route that returned an error from inside its block used to commit half a change.

### Rollout core (`src/rollout/`)
| Class | Kind | Owns |
|---|---|---|
| `Device` | dataclass | where a target is, its credentials (decrypted), its mappings and the values they read: `unresolved()`, `commands_for()`, `from_inventory()` (the one decryption boundary) |
| `RolloutEngine` | class | one rollout's devices, commands and options; `run()` pushes in parallel, verifies, classifies |
| `NetmikoSession` | class | one device's SSH conversation: `connect()`, `push()` (into the CLI shell and config mode, line by line, then the platform's finish), `fetch_config()` |
| `RunReport` | class | what a rollout tells: the log lines (through its `Notifier`) and what only a person can resolve, per endpoint |
| `Finish` → `SaveConfig`, `Commit`, `RunCommand`, `NoFinish` | ABC (strategy) | how a pushed change takes effect and is kept: `before_leave`, `after_leave`, `in_new_session`; defaults shared in the base. A `Platform` row in `PLATFORMS` names one |
| `ConfigSession`, `Target`, `Report` | Protocols | what a finish needs: Netmiko's connection methods (the tests' mocks fit), a device's endpoint, where it reports |
| `Notifier` | Protocol | what the engine and sessions need from a logger (`notify`) |
| `RolloutLogger` | class | one rollout's log file and its one `Echo` |
| `Echo` → `Console`, `LiveLog` | Protocol (strategy) | where notable messages are shown and how they're dressed (ANSI / HTML); `LiveLog` also is the live log's history, channel, `follow()` and `close()` |
| `KeyValueStore` | Protocol | the Redis client as NetRollout uses it — so `src/rollout/` never imports redis (the CLI `.exe`) |
| `LogPruner` | `PeriodicTask` | the logs folder's daily clean-up |
| `RolloutOptions`, `VerifyResult`, `PushResult`, `DeviceResultDict` | values | how a rollout runs, how push and verify went, a device's row |
| `DeviceStatus`, `Tone` | enums | a device's outcome (the `device_results.status` values), a message's tone |

`InputParser` and `Validator` (`inputs.py`) read the CLI / import files; the checks themselves are functions.

### Jobs and results (`src/jobs.py`, `src/results.py`, `src/audit.py`)
| Class | Owns |
|---|---|
| `RolloutOrchestrator` | the web app's rollouts: submit, cancel, the dispatcher, drain (stop / restart), pause (a database move) |
| `RolloutJob` | one rollout: its engine, logger and live log, thread, cancel flag and results; `claim()` — claimed once, by the dispatcher to start it or by a cancel / the drain to record it |
| `JobStore` | the job keys in Redis — the one place that spells them (a `KeyValueStore` client) |
| `ResultRecorder` | finished jobs' results into Postgres — retried, else a JSON file in `logs/`; `busy()` while saving (a move and a stop wait for it) |
| `JobMeta`, `RolloutRow`, `JobStatus` | a job's hash typed, a waiting list's row (the JSON the scripts and the Manager parse), a job's status |
| `JobResults` | finished jobs as the viewer may see them: the one owner-or-admin rule (`may_see`; a job someone may not see is answered as one that doesn't exist), Results' pages (`JobPage`, `JobScope`), summaries, config snapshots (`ConfigSnapshot`), the admin `?user=` scope, the last 30 days |
| `AuditTrail`, `Actor`, `AuditAction` | the one writer of audit rows (its own session), who did it (a user or the server itself), every action a row can record |

### Inventory and accounts (`src/inventory.py`, `src/accounts/`)
| Class | Owns |
|---|---|
| `InventoryView(session, viewer)` | the inventory as one user sees it: visibility and edit rights, shared endpoints, label maps (`LabelScope` VISIBLE / ANYONE), property definitions and attribute values, mapping bindings, the device-saving rules (`save_device(DeviceFields)` → `SavedDevice`, `RuleRefused`) |
| `SecurityProfiles(session, viewer)` | the viewer's profiles — their secrets are encrypted here, in one place |
| `ReachabilityChecker` | TCP probes in parallel, cached in Redis |
| `Viewer` | who the rules are about: an id and whether an admin (built once per request; a rollback uses the job owner's) |
| `Accounts(session)` | one way to make a local account (Request access, Add user) or a directory one, the case-insensitive lookups, the access requests waiting |
| `SessionStore` | the signed-in sessions in Redis: whose, signing a user out everywhere, who is signed in, the clean start, the idle limit (cached); `SessionSerializer` (Protocol) is flask-session's — no Flask import in `users.py` |
| `Directory(server)` | one configured LDAP server: sign-in, groups, details, the admin page's tools — the bind type is its own business |

### Database, settings, backups (`src/db/`, `src/backup/`, `src/runtime.py`)
| Class | Kind | Owns |
|---|---|---|
| `BackendServices` | facade | Postgres, Redis and the settings, one of each per process; a move's and a switch's entry points; its `RuntimeEnv` injectable |
| `ServiceConnection` → `PostgresConnection`, `RedisConnection` | ABC (template method) | the bundled-or-yours rule and the live switch written once; each service supplies only its hooks |
| `RuntimeEnv` | class | `config/runtime.env`: loading it, merging a switch's keys, the remembered bundled addresses |
| `Setting` → `IntSetting`, `TextSetting`, `ChoiceSetting` | ABC | a setting's rules (`parse`, `coerce`), its page fields, its seed |
| `SettingsStore` | class | reading and changing the settings — always the table; `plan()` / `update()` all or nothing |
| `BackupFolder`, `BackupLock` | classes | the backups in the folder (`entries()`), the scheduled ones' retention (`prune`), one backup or restore at a time (`lock()`), the last scheduled outcome |
| `PeriodicTask` → `NightlyCleanUp`, `BackupScheduler`, `CertificateUpkeep`, `LogPruner` | ABC | a daemon loop: an optional first wait, `run_once()` every interval, a failure reported (`failed()`, may arrange a retry), `hold()` for "not now" |
| `ServiceMode`, `Role`, `AuthType`, `BindType`, `BackupKind`, `BackupSchedule`, `Weekday` | enums | values stored in the database, `runtime.env` or a backup's name |

### Access (`src/access/`)
| Class | Owns |
|---|---|
| `Access` | facade (`app.access`): saving the hostname and port with nginx and the port helper following them, the certificate upload / self-signed — every multi-system change all or nothing (`SaveResult`) |
| `Nginx` | nginx from the app's side: whether one reports here (`managed`), its verdicts (`Verdict`, `VerdictState`), `apply(change)` — a change it must accept, undone when rejected; `change_hostname`, `overview` |
| `CertificateStore` | the certs folder: the pair nginx serves, the self-signed marker, the previous names' deadlines; every change under one lock from a snapshot, its undo putting back only what no later change replaced |
| `Certificate` → `SelfSigned`, `Organisation` | ABC (strategy, `CertificateStore.current()` the factory): what a new hostname (`rename_to`) and the upkeep (`drop_expired`) do to it — reissue, or refuse / nothing |
| `ApplyStatus`, `ApplyState`, `PortPageState` | the port helper's answer typed, its states, what the page shows |

The file protocols themselves (`site_env.py`, `nginx.write_site` / `read_status`, `port.request_port` / `confirm`) stay functions: no state of their own.

### Web app (`src/webapp/`)
| Class | Owns |
|---|---|
| `NetRolloutApp` | Flask with the typed services (`app.*`, §7) |
| `WebServices` | the request's audit row and the reachability checks |
| `Shutdown` | this process's stop or restart: at most one, begun once, the drain |
| `Maintenance` (+ `MaintenanceState`) | a database move's waiting and lock: the gate that answers 503, the requests under way counted, the writes held |
| `DatabaseMove` (+ `MoveStatus`, `MoveState`) | one database move at a time, in the background: wait, lock, copy, switch; cancel; its outcome for the page |
| `Caller` | an enum used as a light strategy: which signs make a request a script's in each situation |
| `NotFound`, `Refused` | a route's refusals, raised in its session block |

### Deliberately not built
- **`BaseConnector` ABC + `ConnectorFactory`** (the old Phase 5 plan): one backend (Netmiko); with one there is nothing to choose between. Both come the day a second backend is being written — one file then.
- **A template-method engine:** needs a second kind of rollout (a dry run, a staged rollout) to be worth it.
- **`Repository[T]` + unit of work:** SQLAlchemy's `Session` already is the unit of work and the mapped classes the data mapper; generic repositories would only re-wrap `session.get` / `add`, and their usual payoff (in-memory fakes) doesn't apply when the tests run on a real Postgres. The per-area services give every query and rule one home instead.
- **The State pattern** (jobs, maintenance, the move, the port helper): linear flows; an enum fits, and jobs must keep "claimed counts as running".
- **Observer for the finalize steps:** a fixed, ordered sequence with step-specific error handling; a subscriber list would hide the order.
