"""The table models: what Flask-Login asks of a User, without the models
importing Flask (the setup core, the backup tool and the accounts load them)."""
import subprocess
import sys
import uuid
from pathlib import Path

from src.db.tables import User


ROOT = Path(__file__).resolve().parents[3]


def test_the_models_and_their_users_load_no_flask():
	"""Importing the setup core, the backup tool and the accounts (all of which
	import the models) in a fresh interpreter loads neither flask nor
	flask_login."""
	code = ("import sys, src.setup.__main__, src.backup.__main__, src.accounts.users; "
	        "print(sorted(m for m in ('flask', 'flask_login') if m in sys.modules))")
	out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
	                     text=True, check=True).stdout.strip()
	assert out == "[]"


def test_a_user_is_what_flask_login_expects():
	"""Signed in only while active (is_authenticated is the is_active column),
	never anonymous, its id as text for the session, and two User objects
	with the same id are equal (and different ids not) - as flask_login's
	UserMixin had it."""
	uid = uuid.uuid4()
	active = User(id=uid, username="a", is_active=True)
	inactive = User(id=uuid.uuid4(), username="b", is_active=False)
	assert active.is_authenticated is True and inactive.is_authenticated is False
	assert active.is_anonymous is False
	assert active.get_id() == str(uid)
	same = User(id=uid, username="a-again", is_active=False)
	assert active == same and not (active != same)
	assert active != inactive
	assert active.__eq__("x") is NotImplemented
	assert hash(active) == object.__hash__(active)
