# Bug Report
_Last updated: 2026-04-10_

Status key: 🔴 Open · ✅ Fixed

---

## Medium

### ✅ `webapp.py:18` — `cancel_event` is a module-level singleton
Fixed in Phase 2. `cancel_event` is now owned per-job by `RolloutJob` and passed as an
argument at call time. SSE stream lifetime is tied to the job via `orchestrator.get(job_id)`.

---

### 🔴 `webapp.py:206-221` — SSE stream never signals completion
The `sse_stream()` generator loops forever, only breaking when `cancel_event` is set.
On normal rollout completion the stream continues sending empty heartbeats indefinitely.
The frontend must poll `/rollout_status` separately to detect completion rather than
receiving a done sentinel from the stream itself.

---


### ✅ `register.html:49-53` + `webapp.py:210` — user can self-assign admin role
The registration form includes an "Admin" option in the role dropdown. The backend accepts
whatever role value is submitted with no validation. A user can register with `role="admin"`
and gain admin privileges (subject to approval, but approval itself is an admin action —
a bootstrapped attacker who registers as admin and gets approved by a naive admin is now admin).
Fix: remove "Admin" from the register form dropdown. Role assignment is admin's job only.

---

### ✅ `webapp.py:285` — `user.otp_secret` not checked for None in `otp_verify`
If a user somehow reaches `/otp_verify` with `pre_auth_user_id` set but `otp_secret` is null
(e.g. enrollment was interrupted), `pyotp.TOTP(None)` raises a TypeError.
Fix: add a null check on `user.otp_secret` before calling verify, redirect to enroll if null.

---

### ✅ `webapp.py:236,255,278` — `uuid.UUID(user_id)` not wrapped in try/except
If `pre_auth_user_id` in the Flask session is somehow malformed (corrupted cookie, manual
tampering), `uuid.UUID(user_id)` raises `ValueError` which is unhandled, returning a 500.
Fix: wrap in try/except ValueError and redirect to home on failure.

---

### ✅ `webapp.py:195` — `pre_auth_user_id` not cleared on failed login
On credential failure, `pre_auth_user_id` is not popped from session. If an attacker
partially completes a login (sets the session var) then fails, the session var persists.
Combined with session fixation, this is a minor risk.
Fix: `session.pop("pre_auth_user_id", None)` at the top of the login route before any checks.

---

### ✅ `validation.py` — port validation allows port 0
`int(port) < 0` passes port=0 as valid. Port 0 is not a valid SSH target.
Fix: change to `int(port) < 1`.

---

### 🔴 `rollout.html` — SSE log output inserted as `innerHTML` (potential XSS)
Log messages from `base_notify()` are inserted via `innerHTML` in the frontend.
If a device hostname, IP, or command output contains `<script>` or other HTML, it will
be rendered as markup. The content originates from network devices so risk is low but present.
Fix: sanitize output in `logging_utils.py` or use `textContent` for plain parts and only
`innerHTML` for the intentional HTML color wrappers.

---

### ✅ `webapp.py:260,285` — `valid_window=2` is too permissive
`valid_window=2` accepts codes from 5 consecutive 30-second windows (150 seconds total).
Standard practice is `valid_window=1` (90 seconds) which is sufficient for clock drift.
Fix: reduce to `valid_window=1`.

---

## Security Vulnerabilities

### ✅ `webapp.py:26` — Hardcoded `SECRET_KEY = "dev"`
Fixed in Phase 2. Now generated at startup via `secrets.token_urlsafe(32)`.
Phase 4 will move this to an env var so it persists across restarts.

---

### ✅ All POST routes — No CSRF protection
No CSRF tokens on any form. An attacker can craft a malicious page that silently submits
POST requests to NetRollout on behalf of a logged-in user — approving accounts, promoting
users to admin, triggering rollouts, disabling accounts.
**Fix:** add Flask-WTF and include `{{ form.hidden_tag() }}` or manually validate
`X-CSRFToken` headers. All state-changing POST routes are affected.

---

### ✅ `upload.html:304-305` — Device credentials stored in DOM dataset attributes
Resolved by Phase 2 architecture. Credentials now live in `SecurityProfile` (Fernet-encrypted
in DB). The rollout flow reads from inventory via `Device.from_inventory()` — credentials
never touch the DOM or browser history.

---

### ✅ No rate limiting on `/login`
Fixed. Flask-Limiter installed and `@conn_limit.limit("10 per minute")` applied to the
login route.

---

### ✅ `otp_secret` stored in plaintext in DB
If the database is compromised, all TOTP secrets are exposed. An attacker with DB access
can generate valid OTP codes for any user indefinitely.
**Tradeoff:** encrypting TOTP secrets requires key management (same problem as encrypted
inventory). For portfolio scope, document as a known limitation. In production, use
application-level encryption with a KMS-managed key.

---

## Fixed

### ✅ `db.py:22` — `except exception()` swallowed all DB errors
`exception` was imported from `logging`, so `except exception():` never matched anything.
Rollback never ran, session was never properly closed on error.
Fixed: bad import removed, changed to `except Exception:`.

### ✅ `logging_utils.py` — webapp error messages silently dropped
`base_notify()` only enqueued to `LOG_QUEUE` when `verbose=True`.
Error messages (red) were never sent to the SSE stream in the webapp.
Fixed: condition changed to `if verbose or color == "red":` in both CLI and webapp branches.

### ✅ `logging_utils.py` — `msg()` crashed on unknown or None color (both paths)
CLI path: `COLORS.get("UNKNOWN")` returned `None`, then `None + string + END` raised `TypeError`.
Webapp path: same issue with `ANSI_TO_HTML`, plus `None.upper()` crash when `color=None` passed from `base_notify`.
Fixed: CLI path guards with `if color:` after lookup; webapp path uses `ANSI_TO_HTML.get(color.upper()) if color else None` then guards before concatenation.
Covered by `TestMsg::test_unknown_color_returns_plain`.

### ✅ `cli.py:27-33` — `store_const` / `default=None` on `--verify` was unnecessary
`action="store_const", const=True, default=None` created a three-state flag requiring `is True` checks downstream.
Fixed: changed to `action="store_true"` and simplified `if args.verify is True` → `if args.verify`.

### ✅ `core.py` — dead `except ValueError` in `run()`
`run()` wrapped a boolean condition in a `try/except ValueError` that could never trigger.
Fixed: removed the `try/except` wrapper, leaving the `if/else` logic unchanged.

### ✅ `core.py:221` — redundant `cancel_event` ternary
`self.cancel_event = cancel_event if cancel_event else None` assigned `None` either way.
Fixed: simplified to `self.cancel_event = cancel_event`.

### ✅ `validation.py:124` — socket reused after failed `connect()` on Windows
A single socket was created outside the retry loop. After a failed `connect()`, the socket
is in an error state and re-calling `connect()` raises `WinError 10056` on Windows,
making retries 2 and 3 immediately fail without attempting a connection.
Fixed: socket creation moved inside the loop so each attempt gets a fresh socket.
