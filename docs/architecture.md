# NetRollout — Architecture Document
_Written: 2026-04-07 — Updated: 2026-05-04 (DB layer, LDAP, blueprints, observability)_

---

## 1. Overview

NetRollout is structured around six layers:

1. **Data classes** — pure runtime objects, no DB coupling
2. **ORM models** — DB schema, all anchored to `User` as the master table
3. **Service classes** — business logic, validation, parsing, logging
4. **Job execution classes** — orchestration, pipeline, concurrency
5. **DB layer** — connection management, session lifecycle, hot-reload
6. **Webapp layer** — Flask app factory, extensions, blueprints, shared helpers

The central design principle is that `User` is the master table. All persistent entities — devices, credentials, variable mappings, and result history — are owned by a user via foreign key relationships.

At runtime, `RolloutOrchestrator` is the concurrency manager. It owns the active jobs dict, coordinates multithreading, and keeps DB and in-memory state in sync. `RolloutJob` is the lifecycle owner for a single job — it owns the thread, cancel event, logger, and engine. All execution context flows as arguments at call time — no hanging state on `RolloutEngine`.

**Configuration** (set via `config.env`, loaded at startup):
- `NETROLLOUT_ENCRYPTION_KEY` — Fernet key for credential encryption. Default: auto-generated, written to `~/.netrollout/encryption.key`
- `MAX_CONCURRENT_JOBS` — orchestrator thread concurrency limit. Default: `4`
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` — Redis connection string
- `SECRET_KEY` — Flask session key

---

## 2. Data Classes

Data classes are pure Python objects with no SQLAlchemy coupling. They exist at runtime only.

---

### `RolloutOptions`
Configuration flags for a rollout run. Pure data, no behavior.

| Name | Type | Description |
|---|---|---|
| `verify` | `bool` | Run post-push verification via NAPALM |
| `verbose` | `bool` | Print progress to console (CLI mode) |
| `webapp` | `bool` | Enqueue log messages for SSE stream |

---

### `Device`
Represents a single network device at runtime. Constructed from an `Inventory` row via `from_inventory()`.

| Name | Type | Description |
|---|---|---|
| `ip` | `str` | Device IPv4 address |
| `username` | `str` | SSH username (decrypted at construction) |
| `password` | `str` | SSH password (decrypted at construction) |
| `device_type` | `str` | Netmiko platform string |
| `secret` | `str` | Enable secret (decrypted at construction) |
| `port` | `int` | SSH port |
| `label` | `str` | Friendly name from inventory |
| `extra` | `dict` | Per-device attributes for `$$TOKEN$$` substitution, from `Inventory.var_maps` |

**Public methods:**
| Method | Signature | Description |
|---|---|---|
| `from_inventory` | `cls(row: Inventory) -> Device` | Factory. Decrypts credentials from assigned SecurityProfile |
| `netmiko_connector` | `() -> dict` | Builds Netmiko `ConnectHandler` params dict |
| `fetch_config` | `(logger: RolloutLogger) -> str \| None` | Opens NAPALM connection, returns running config |

---

## 3. ORM Models

All ORM models live in `src/db/tables.py`. All use UUID primary keys. `User` is the master table.

---

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
| `otp_secret` | `str` | Fernet-encrypted. Null = unenrolled (LDAP users skip OTP) |
| `auth_type` | `str` | `"local"` or `"ldap"` |
| `ldap_server_id` | `UUID` | FK → `LDAPServer`, nullable |
| `created_at` | `DateTime` | Set at creation |

**Relationships:** `inventory`, `security_profiles`, `variable_mappings`, `results`, `ldap_server`

---

### `Inventory`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `sec_profile_id` | `UUID` | FK → `SecurityProfile`, nullable |
| `ip` | `str` | Device IPv4 address |
| `device_type` | `str` | Netmiko platform string |
| `port` | `int` | SSH port |
| `label` | `str` | Friendly name, nullable |
| `var_maps` | `JSON` | Per-device substitution attributes. Keys: `hostname`, `loopback_ip`, `asn`, `mgmt_vrf`, `mgmt_interface`, `site`, `domain`, `timezone` (strings), `vrfs` (list) |

---

### `SecurityProfile`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `label` | `str(64)` | Nullable |
| `username` | `str(64)` | Plaintext |
| `password_secret` | `str(255)` | Fernet-encrypted |
| `enable_secret` | `str(255)` | Fernet-encrypted, nullable |

Deletion blocked at route level if any `Inventory` rows reference this profile.

---

### `VariableMapping`

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `token` | `str` | Free-text token, e.g. `$$HOSTNAME$$` |
| `property_name` | `str` | Key in `device.extra` |
| `index` | `int \| None` | Null = string substitution; N = `device.extra[property_name][N]` |

---

### `PropertyDefinition`
User-managed device attribute definitions. Extend the set of keys available in `var_maps`.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `name` | `str` | Internal key name |
| `label` | `str` | Display label |
| `icon` | `str` | Bootstrap Icons class |
| `is_list` | `bool` | Whether the value is a list (enables index-based substitution) |

---

### `DeviceResult`
Permanent archive. One row per device per job. `job_id` is a soft reference — no FK to any session table.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `job_id` | `UUID` | Soft ref for grouping results by job |
| `started_at` | `DateTime` | |
| `completed_at` | `DateTime` | |
| `device_ip` | `str` | |
| `device_type` | `str` | |
| `commands_sent` | `int` | |
| `commands_verified` | `int \| None` | Null if verify not run |
| `status` | `str` | `success` / `partial` / `failed` / `cancelled` |
| `fetched_config` | `TEXT` | Nullable — post-push running config snapshot if verify was run |

---

### `JobMetadata`
Stores pre-substitution commands and optional comment per job. Soft `job_id` ref. 7-day pg_cron retention.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `user_id` | `UUID` | FK → `User` |
| `job_id` | `UUID` | Soft ref |
| `commands` | `JSON` | Raw command list before variable substitution |
| `comment` | `str` | Optional user comment |
| `created_at` | `DateTime` | |

---

### `AuditLog`
Append-only audit trail. 90-day pg_cron retention.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `actor_id` | `UUID` | FK → `User`, ON DELETE SET NULL — denormalized below so logs survive user deletion |
| `actor_username` | `str` | Denormalized username |
| `action` | `str` | Dot-namespaced action string, e.g. `auth.login`, `inventory.create` |
| `object_type` | `str` | Nullable — ORM class name of the affected object |
| `object_id` | `UUID` | Soft ref to affected object |
| `object_label` | `str` | Denormalized label — survives object deletion |
| `success` | `bool` | |
| `ip_address` | `str` | `request.remote_addr` |
| `detail` | `JSON` | Nullable — machine-readable reason dict |
| `timestamp` | `DateTime` | |

---

### `LDAPServer`
Org-level LDAP/LDAPS server configuration.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `host` | `str` | LDAP server hostname/IP |
| `port` | `int` | Default 389 (LDAP) / 636 (LDAPS) |
| `use_ssl` | `bool` | LDAPS |
| `bind_dn` | `str` | Service account DN |
| `bind_password` | `str` | Fernet-encrypted |
| `base_dn` | `str` | Search base DN |
| `is_active` | `bool` | |

**Relationships:** `groups` → `[LDAPGroup]`, `users` → `[User]`

---

### `LDAPGroup`
Group-level rule: members of this group are auto-provisioned as local users on first login.

| Name | Type | Description |
|---|---|---|
| `id` | `UUID` | Primary key |
| `ldap_server_id` | `UUID` | FK → `LDAPServer` |
| `group_dn` | `str` | LDAP group DN to match against |
| `role` | `str` | Role to assign to auto-created users (`"user"` or `"admin"`) |
| `is_active` | `bool` | |

> **Note:** `RolloutSession` table was **dropped** in Phase 4.6b. Redis is now the sole source of truth for ephemeral job state (active counts, job queue via BLPOP/RPUSH, log pub/sub).

---

## 4. Service Classes

### `Validator`
Wraps all input validation. Logger-injected for user-facing errors; pure computation methods are static.

### `InputParser`
Inventory is the single source of truth for rollout. CSV upload and web form populate `Inventory` — rollout always runs from inventory.

### `RolloutLogger`
Owns all logging I/O for a single rollout job. One instance per `RolloutJob`. Writes timestamped log file; routes SSE messages to Redis pub/sub channel.

---

## 5. Job Execution Classes

### `RolloutOrchestrator`
Concurrency manager. Singleton instantiated at app startup. Owns the in-memory `_jobs` dict. Reads/writes job state counters in Redis.

**Constructor:** `RolloutOrchestrator(backend: BackendServices, max_concurrent: int)`

**Dispatch loop:**
```
submit(job)
  → RPUSH job to Redis queue
  → write active/pending counts to Redis
  → _dispatch()
       → active_count < max_concurrent?
            yes → job.start(), increment active counter
            no  → job waits in dict

job completes
  → _cleanup(job_id)
       → write DeviceResult rows to Postgres
       → decrement active counter in Redis
       → _dispatch()   # slot freed, start next pending job
```

### `RolloutEngine`
Pure pipeline object. Receives execution context as arguments — no hanging state.

**Constructor:** `RolloutEngine(param: RolloutOptions, devices: list[Device], commands: list[str])`

### `RolloutJob`
Lifecycle owner. Owns thread, cancel flag, engine, logger. Created by `RolloutOrchestrator.submit()`.

---

## 6. DB Layer

### `PostgresConfig` / `RedisConfig`
Frozen dataclasses. Load from env vars via `unload_env()`. `get_url()` returns the connection string. Immutable — a new config object is created for each hot-reload.

### `PostgresConnection`
Wraps a SQLAlchemy engine. `get_session()` is a context manager — commits on clean exit, rolls back on exception. `pool_pre_ping=True` — stale connections are tested before use. `reload_db(config)` atomically swaps the engine; raises `RuntimeError` if the new server is unreachable.

### `RedisConnection`
Wraps a `redis.Redis` client. Same `reload_db(config)` pattern as Postgres.

### `BackendServices`
Composition root for all infrastructure. Constructed once in `launch_app()`, attached to `app.backend`.

```python
BackendServices(pg: PostgresConnection, redis: RedisConnection)
# app.backend.postgres  →  PostgresConnection
# app.backend.redis     →  RedisConnection
```

`health()` returns `{"postgres": bool, "redis": bool}` — each service checked independently, exceptions caught separately so one failure doesn't mask the other.

`reload_postgres(config)` / `reload_redis(config)` — hot-reload without restart. Called from the Server Management UI.

---

## 7. Webapp Layer

### `setup.py` — App factory
`launch_app()` is the pure composition root. Creates backend, orchestrator, web services, Flask app object. Registers extensions and handlers. Does **not** register blueprints — that responsibility lives in `__init__.py`.

```python
app.backend   →  BackendServices
app.web       →  WebServices
app.orchestrator  →  RolloutOrchestrator
```

`_SafeRedisSessionInterface` — subclass of `RedisSessionInterface` that catches `RedisConnectionError` on open/save, returning an empty session rather than crashing. Registered **after** `configure_app()` (which calls `Session(app)` internally) so it isn't overwritten.

### `extensions.py` — Module-level Flask extensions
Extensions are created at module level (not inside functions) so they can be imported by blueprints at definition time without an app context.

```python
login_mng = LoginManager()         # login_view = "auth.home"
conn_limit = Limiter(...)          # rate limiting
csrf = CSRFProtect()
```

`register_extensions(app)` — calls `init_app()` on each, initializes `PrometheusMetrics`.
`register_auth(app)` — registers `@login_mng.user_loader`.
`register_handlers(app, backend)` — CSRF error handler, service-unavailable handler, hot-reload env capture.

### `utils.py` — Shared helpers

**`WebServices(backend)`** — attached to `app.web`. Contains helpers used by 2+ blueprints:
- `audit(action, ...)` — writes one `AuditLog` row in its own session
- `act_on_db_obj(model, obj_id, func, ...)` — generic DB dispatcher
- `create_op`, `update_op`, `delete_op` — CRUD operation factories
- `build_kpi(results_30d, label_map)` — KPI dict for analytics
- `job_status(rows)` — aggregate job status from device results
- `compile_query_rules(node, allowed_fields)` — jQuery QueryBuilder → SQLAlchemy expression
- `get_property_defs(user_id)` — system + user property definitions
- `build_security_profile(...)`, `user_owns_job(...)`

**Standalone functions:** `ok()`, `err()`, `require_admin`, `with_json`, `with_form`, `flash_redirect`, `validate_mapping_fields`

**Constants:** `SYSTEM_PROPERTIES`, `QUERY_DEVICE_RESULT_FIELDS`, `QUERY_AUDIT_LOG_FIELDS`, `QUERY_OPS`, `DEVICE_RESULT_COLUMNS`, `AUDIT_LOG_COLUMNS`

### `webapp/__init__.py` — Entry point
`create_app()` calls `launch_app()` then registers all blueprints. Serve call wired to container entrypoint.

### `blueprints/`
Each blueprint owns its routes and route-specific helpers. Shared helpers stay in `WebServices`. Blueprints access `app.web` and `app.backend` via `current_app` inside routes — never at module level.

| Blueprint | Status | Routes |
|---|---|---|
| `auth.py` | ✅ Complete | `/`, `/login`, `/register`, `/otp_enroll`, `/otp_verify`, `/logout`, `/account` |
| `rollout.py` | stub | `/dashboard`, `/start_rollout`, `/cancel_rollout`, `/rollout_stream`, `/rollout_status`, `/results`, `/active_jobs` |
| `inventory.py` | stub | `/inventory/*` |
| `security.py` | stub | `/security_profiles/*` |
| `mappings.py` | stub | `/mappings/*` |
| `properties.py` | stub | `/properties/*` |
| `analytics.py` | stub | `/analytics/*` |
| `admin.py` | stub | `/admin/*` |

**Auth flow:**
```
POST /login
  local user  →  check_password_hash → approval/active gates → start_otp_flow()
  ldap user   →  user_bind() → approval/active gates → complete_login()
  unknown     →  login_ldap_group() → check_group_membership() → auto-provision → complete_login()

start_otp_flow()
  → session["pre_auth_user_id"] = user.id
  → otp_secret present? → /otp_verify
  → no secret?          → /otp_enroll (first-time setup)

complete_login()
  → db_session.expunge(user) → login_user(user) → record_redis_session() → redirect dashboard
```

---

## 8. Observability Stack

Three sidecar services on the `netrollout-obs` Docker network. Flask app runs independently — unaffected if they are down.

| Service | Address | Role |
|---|---|---|
| PostgreSQL | `NetRollout-DB:5432` | Historical business metrics — direct Grafana datasource via `grafana_reader` read-only user |
| Prometheus | `http://prometheus:9090` | Live metrics — active job counts, Flask request rates and latencies |
| Loki | `http://loki:3100` | Log stream — per-job log files shipped by Promtail, searchable by `job_id` label |

**Custom Prometheus collector** (`RolloutSessionCollector`) reads `netrollout:active_count` and `netrollout:pending_count` from Redis (set by `RolloutOrchestrator`). Exposes `netrollout_active_jobs` and `netrollout_pending_jobs` gauges.

**Four Grafana dashboards** provisioned from `docs/grafana/dashboard_config/`:

| Dashboard | Datasources | Purpose |
|---|---|---|
| Operations Overview | Prometheus | Live job state, request rates, p99 latency |
| Job Analytics | PostgreSQL | Historical outcomes, platform breakdown, heatmap |
| Job Detail | PostgreSQL + Loki | Drill-down by `$job_id` — device results, log stream |
| Audit & Security | PostgreSQL | Audit trail, failure rates, top actors |

---

## 9. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `cancel_event` ownership | `RolloutJob` | Lifecycle concern, not logging or pipeline |
| Execution context passing | Arguments at call time | No hanging state on `RolloutEngine` |
| Credential storage | `SecurityProfile` table, Fernet-encrypted | Topology separated from credentials; one profile → many devices |
| `Device` construction | `from_inventory()` factory | Single boundary where decryption happens |
| Results schema | One row per device per job | Enables per-device analytics via SQL aggregation |
| `RolloutOrchestrator` | Singleton at app startup | Single owner of concurrency — routes delegate to it |
| Ephemeral job state | Redis only (`RolloutSession` table dropped) | Redis is faster and naturally ephemeral; Postgres unnecessary for RAM data |
| Flask extensions | Module-level with `init_app()` | Must be importable by blueprints at definition time, before app context exists |
| `app.web` / `app.backend` | Set on app object in `launch_app()` | Available via `current_app` in any request context; avoids circular imports |
| Blueprint `url_for` | Always prefixed (`"auth.home"`, `"rollout.dashboard"`) | Blueprint namespace prevents endpoint name collisions |
| `_SafeRedisSessionInterface` | Registered after `Session(app)` | `Session(app)` overwrites `session_interface`; must follow it |
| LDAP auto-provisioning | Group-rule matched → create local User on first login | Zero-touch onboarding; role assigned from group mapping |
| `AuditLog.actor_username` | Denormalized | Audit records survive user deletion; no orphaned FK |
| `reload_db()` | Raises `RuntimeError` if new server unreachable | Silent failure would leave app pointing at a broken connection |
