"""Stop / Restart (src/webapp/lifecycle.py): the command a restart relaunches."""
import sys

import pytest

from src.webapp.lifecycle import relaunch_command


# ── Admin restart relaunch ───────────────────────────────────────────────────

@pytest.mark.parametrize("orig_argv,expected_tail", [
	(["python", "-m", "src.webapp"], ["-m", "src.webapp"]),   # module mode
	(["python", "run.py", "--x"], ["run.py", "--x"]),          # script mode
])
def test_restart_relaunches_the_original_invocation(monkeypatch, orig_argv,
                                                    expected_tail):
	"""The restart command is this Python with the original arguments
	(sys.orig_argv), in module mode (-m src.webapp) and script mode alike.

	Under -m, sys.argv[0] is the __main__.py path - relaunching that ran it as
	a script, where `src` isn't importable."""
	monkeypatch.setattr(sys, "argv", [r"C:\repo\src\webapp\__main__.py"])
	monkeypatch.setattr(sys, "orig_argv", orig_argv)
	assert relaunch_command() == [sys.executable, *expected_tail]
