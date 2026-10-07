"""The password rule (src/accounts/users.py) — the server side of it; the pages
mirror it in templates/_password_rule_script.html."""
import pytest

from src.accounts.users import password_problem, temporary_password


@pytest.mark.parametrize("password", ["Abcdefg1", "network2026", "P4ss word!",
                                      "letters-only", "1234-5678"])   # 2 of 3 groups
def test_acceptable(password):
	"""8+ ASCII characters from at least 2 of letters / digits / special pass
	the rule (no problem returned)."""
	assert password_problem(password) is None


@pytest.mark.parametrize("password, fragment", [
	("Abc1234", "at least 8"),               # 7 characters
	("abcdefgh", "at least 2 of"),           # letters only
	("12345678", "at least 2 of"),           # digits only
	("!!!!!!!!", "at least 2 of"),           # special only
	("pässword12", "ASCII"),
	("admin", "at least 8"),                 # the factory password
])
def test_refused(password, fragment):
	"""Each weak password is refused with the matching reason: fewer than 8
	characters (the factory `admin` included), only one group, or non-ASCII."""
	assert fragment in password_problem(password)


def test_username_inside_is_refused_case_insensitively():
	"""A password containing the username in any case is refused; another
	user's name inside it doesn't matter."""
	assert "username" in password_problem("xxBOBxx123", username="bob")
	assert password_problem("xxBOBxx123", username="alice") is None


def test_a_change_must_differ():
	"""A new password equal to the current one is refused ("differ"); a
	different one passes."""
	assert "differ" in password_problem("Same-pass-1", current="Same-pass-1")
	assert password_problem("Same-pass-2", current="Same-pass-1") is None


def test_temporary_passwords_follow_the_rule_and_differ():
	"""50 generated temporary passwords are all different and all pass the
	rule for their username."""
	generated = {temporary_password("tempuser") for _ in range(50)}
	assert len(generated) == 50
	assert all(password_problem(p, "tempuser") is None for p in generated)
