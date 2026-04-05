# Bug Report
_Last updated: 2026-04-05_

Status key: 🔴 Open · ✅ Fixed

---

## Medium

### 🔴 `webapp.py:18` — `cancel_event` is a module-level singleton
`cancel_event` is created once at import time and shared across all requests.
`cancel_event.clear()` is called at the start of each rollout, which handles the common case.
However, if the SSE generator from a previous cancelled rollout is still running (it breaks on
cancel but may not have been garbage collected), a new rollout's stream could behave
unpredictably. The event and the SSE stream lifetime are not tied together.

---

### 🔴 `webapp.py:206-221` — SSE stream never signals completion
The `sse_stream()` generator loops forever, only breaking when `cancel_event` is set.
On normal rollout completion the stream continues sending empty heartbeats indefinitely.
The frontend must poll `/rollout_status` separately to detect completion rather than
receiving a done sentinel from the stream itself.

---

### 🔴 `core.py:263` — `enable()` silent failure with empty secret
If a device's `secret` field is empty and `enable` mode is required,
Netmiko will either hang waiting for a password prompt or raise an exception.
The exception is caught by the broad `except Exception` on line 295 but the resulting
error message gives no indication that the secret was missing.

---

### 🔴 `core.py:221` — redundant `cancel_event` ternary
```python
# current — assigns None either way
self.cancel_event = cancel_event if cancel_event else None

# fix
self.cancel_event = cancel_event
```

---

## Minor

### 🔴 `cli.py:27-33` — `store_const` / `default=None` on `--verify` is unnecessary
`action="store_const", const=True, default=None` creates a three-state flag
(`True` / `None` / not-passed) that the downstream `if/elif` in `main()` has to untangle.
`action="store_true"` is simpler and produces the same effective behaviour.

---

### 🔴 `core.py:401-402` — dead `except ValueError` in `run()`
The `try` block only evaluates a boolean condition (`if self.devices and self.commands`).
No code path inside it raises `ValueError`. The except clause is unreachable dead code.

---

### 🔴 `logging_utils.py` — redundant double-uppercasing in `msg()`
`COLORS` and `ANSI_TO_HTML` dict keys are already uppercase.
`msg()` calls `.upper()` on the caller-supplied color string before the lookup.
No functional impact, just noise.

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

### ✅ `validation.py:124` — socket reused after failed `connect()` on Windows
A single socket was created outside the retry loop. After a failed `connect()`, the socket
is in an error state and re-calling `connect()` raises `WinError 10056` on Windows,
making retries 2 and 3 immediately fail without attempting a connection.
Fixed: socket creation moved inside the loop so each attempt gets a fresh socket.
