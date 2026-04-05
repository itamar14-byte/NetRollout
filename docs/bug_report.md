# Bug Report
_Last updated: 2026-04-05_

Status key: 🔴 Open · ✅ Fixed

---

## Critical

### 🔴 `db.py:22` — `except exception()` swallows nothing
`exception` on line 3 is imported from `logging` (`from logging import exception`).
Calling `except exception():` invokes `logging.exception()` rather than catching `Exception`.
Any database error during a session will skip the rollback and leave the session open.
```python
# current (broken)
except exception():

# fix
except Exception:
```

---

### 🔴 `logging_utils.py` — webapp error messages silently dropped
`base_notify()` only enqueues to `LOG_QUEUE` when `verbose=True`.
Error messages (e.g. auth failure, device unreachable) are passed with no `verbose` flag,
so in the webapp they are logged to file but never sent to the SSE stream.
The user sees nothing when a device fails.
Confirmed by test: `TestRolloutEngineNotify::test_verbose_webapp_enqueues` passes only because
it explicitly sets `verbose=True` — non-verbose error paths are invisible.
```python
# current (broken) — errors silenced unless verbose
if verbose:
    LOG_QUEUE.put(msg(string, color, webapp=True))

# fix — always enqueue errors
if verbose or color == "red":
    LOG_QUEUE.put(msg(string, color, webapp=True))
```

---

## Medium

### 🔴 `validation.py:124-137` — socket reused after failed `connect()` on Windows
`test_tcp_port()` creates one socket and retries `connect()` on it.
On Windows, calling `connect()` on a socket that already failed raises
`OSError: [WinError 10056] A connect request was already made`.
A new socket must be created per attempt.
```python
# fix — move socket creation inside the loop
for attempt in range(TCP_RETRIES):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.settimeout(TCP_TIMEOUT)
        try:
            conn.connect((ip, port))
            return True
        except OSError:
            if attempt < TCP_RETRIES - 1:
                time.sleep(TCP_RETRY_DELAY)
return False
```

---

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
`COLORS` dict keys are already uppercase (`"RED"`, `"GREEN"`, `"YELLOW"`).
`msg()` calls `.upper()` on the caller-supplied color string before the lookup.
No functional impact, just noise.

---

## Fixed

### ✅ `logging_utils.py` — unknown color string caused `TypeError`
`COLORS.get("UNKNOWN")` returned `None`, then `None + string + END` raised `TypeError`.
Fixed by guarding: `if color: return color + string + END`.
Covered by `TestMsg::test_unknown_color_returns_plain`.
