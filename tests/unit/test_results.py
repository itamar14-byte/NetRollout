"""Finished jobs' history (src/results.py): who may see a job, and whose
numbers a page shows (the admin's ?user= choice)."""
import uuid
from unittest.mock import MagicMock

import pytest

from src.accounts.users import Viewer
from src.results import JobResults

ME, OTHER = uuid.uuid4(), uuid.uuid4()


def history(is_admin=False):
	"""JobResults for ME (the rules need no session)."""
	return JobResults(MagicMock(), Viewer(ME, is_admin))


def test_a_job_is_seen_by_its_owner_and_any_admin():
	"""may_see: the owner sees their job, another operator doesn't, an admin
	sees anyone's."""
	assert history().may_see(ME)
	assert not history().may_see(OTHER)
	assert history(is_admin=True).may_see(OTHER)


@pytest.mark.parametrize("raw", [None, "me", " me ", "not-a-uuid", ""])
def test_an_admins_own_numbers_unless_a_user_is_picked(raw):
	"""scope_user: no choice, "me" or something that isn't an id gives the
	admin's own numbers, shown as "me"."""
	assert history(is_admin=True).scope_user(raw) == (ME, "me")


def test_an_admin_may_pick_any_user():
	"""scope_user: an admin's ?user=<id> (spaces around it ignored) is that
	user's numbers, shown by the id as given."""
	assert history(is_admin=True).scope_user(f" {OTHER} ") == (OTHER, str(OTHER))


def test_an_operator_always_sees_their_own():
	"""scope_user: an operator's ?user= is ignored."""
	assert history().scope_user(str(OTHER)) == (ME, "me")
