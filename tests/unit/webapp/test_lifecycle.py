"""Stop / Restart (src/webapp/lifecycle.py): the command a restart relaunches."""
import sys
from types import SimpleNamespace

import pytest

import src.webapp.lifecycle as lifecycle
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


# ── A deliberate stop ────────────────────────────────────────────────────────

@pytest.fixture
def exits(monkeypatch):
	"""Records what Shutdown._run would do instead of doing it."""
	calls = SimpleNamespace(drained=[], relaunched=[], exited=[], slept=[])
	monkeypatch.setattr(lifecycle.time, "sleep", calls.slept.append)
	monkeypatch.setattr(lifecycle.os, "_exit", calls.exited.append)
	monkeypatch.setattr(lifecycle.subprocess, "Popen",
	                    lambda cmd, env: calls.relaunched.append(env))
	orchestrator = SimpleNamespace(
		drain=lambda deadline, report: calls.drained.append(deadline))
	calls.shutdown = lifecycle.Shutdown(orchestrator)
	return calls


def test_restart_in_dev_drains_relaunches_and_exits(dev, exits):
	"""A dev restart drains with the given deadline, waits the exit delay, relaunches
	with the relaunch marker set and exits 0."""
	exits.shutdown._run(30, restart=True)
	assert exits.drained == [30] and exits.exited == [0]
	assert exits.slept == [lifecycle._EXIT_DELAY]   # the response goes out
	assert exits.relaunched[0][lifecycle.RELAUNCH_ENV] == "1"


def test_restart_in_a_container_leaves_it_to_the_restart_policy(container,
                                                                exits):
	"""A restart in a container exits 0 without relaunching: the restart policy
	brings it back."""
	exits.shutdown._run(30, restart=True)
	assert exits.exited == [0] and exits.relaunched == []


def test_stop_never_relaunches_and_exits_without_delay(dev, exits):
	"""A stop exits 0 at once, with no relaunch and no sleep (docker stop only waits
	so long: stop_grace_period / StopTimeout)."""
	exits.shutdown._run(30, restart=False)
	assert exits.exited == [0] and exits.relaunched == []
	assert exits.slept == []


def test_exit_happens_even_if_the_drain_fails(dev, exits):
	"""When the drain raises, the error propagates but the process still exits 0."""
	def broken(deadline, report):
		raise RuntimeError("redis gone")
	exits.shutdown._orchestrator = SimpleNamespace(drain=broken)
	with pytest.raises(RuntimeError):
		exits.shutdown._run(30, restart=False)
	assert exits.exited == [0]


def test_a_second_stop_request_is_ignored(monkeypatch):
	"""The first begin() starts the shutdown thread and returns True; a second one
	(a restart) returns False and changes nothing: one thread, still a stop."""
	started = []
	monkeypatch.setattr(lifecycle.threading, "Thread",
	                    lambda **kw: SimpleNamespace(
		                    start=lambda: started.append(kw["args"])))
	shutdown = lifecycle.Shutdown(orchestrator=None)
	assert shutdown.begin(600, restart=False) is True
	assert shutdown.begin(0, restart=True) is False
	assert started == [(600, False)]
	assert not shutdown.restarting
