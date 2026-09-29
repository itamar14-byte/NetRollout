import io
import sys

import pytest

from src.logging_utils import RolloutLogger, utf8_console


def cp1252_stream():
	"""What stdout looks like when output is redirected on Windows."""
	return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_non_utf8_console_used_to_fail_notify(monkeypatch):
	monkeypatch.setattr(sys, "stdout", cp1252_stream())
	logger = RolloutLogger(webapp=False, verbose=False)
	with pytest.raises(UnicodeEncodeError):
		logger.notify("Bulk assign started: 2 devices → profile p",
		              important=True)


def test_utf8_console_makes_notify_safe(monkeypatch):
	out, err = cp1252_stream(), cp1252_stream()
	monkeypatch.setattr(sys, "stdout", out)
	monkeypatch.setattr(sys, "stderr", err)
	utf8_console()
	RolloutLogger(webapp=False, verbose=False).notify(
		"Bulk assign started: 2 devices → profile p", important=True)
	print("Startup aborted — key problem", file=sys.stderr)
	out.flush(), err.flush()
	assert "→".encode() in out.buffer.getvalue()
	assert "—".encode() in err.buffer.getvalue()
