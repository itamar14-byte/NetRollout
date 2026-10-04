"""core.classify: a device's status from its push and verify — the rules the
Results page, the summary and the dashboards count by."""
import pytest

from src.core import PushResult, VerifyResult, classify

PUSHED = PushResult(applied=True, rejected=0)


@pytest.mark.parametrize("push, verify, expected", [
	# never started / not applied: nothing counted
	(None, None, ("cancelled", 0, None)),
	(PushResult(applied=False, rejected=0), None, ("failed", 0, None)),
	(PushResult(applied=False, rejected=0), VerifyResult(5, 5, None),
	 ("failed", 0, None)),
	# verified: all confirmed, nothing refused
	(PUSHED, VerifyResult(5, 5, None), ("success", 5, 5)),
	# ... all confirmed but the device refused one → partial
	(PushResult(True, 1), VerifyResult(5, 5, None), ("partial", 5, 5)),
	# none confirmed → failed; some → partial
	(PUSHED, VerifyResult(0, 5, None), ("failed", 5, 0)),
	(PUSHED, VerifyResult(3, 5, None), ("partial", 5, 3)),
	# commands that can't be checked count as accounted for
	(PUSHED, VerifyResult(2, 3, None), ("partial", 5, 4)),
	(PUSHED, VerifyResult(3, 3, None), ("success", 5, 5)),
	# nothing checkable at all: success unless something was refused
	(PUSHED, VerifyResult(0, 0, None), ("success", 5, 5)),
	(PushResult(True, 1), VerifyResult(0, 0, None), ("partial", 5, 5)),
	# not verified: what the device said while the commands were sent
	(PUSHED, None, ("success", 5, None)),
	(PushResult(True, 2), None, ("partial", 5, None)),
	(PushResult(True, 4), None, ("failed", 5, None)),      # every configuring one
	(PushResult(True, 5), None, ("failed", 5, None)),
])
def test_status_rules(push, verify, expected):
	# 5 commands, 4 of them configure something (one is navigation)
	assert classify(push, verify, total=5, configuring=4) == expected
