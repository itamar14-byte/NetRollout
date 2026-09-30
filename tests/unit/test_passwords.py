"""The password rule (src/passwords.py) — the server side of it; the pages
mirror it in templates/_password_rule_script.html."""
import pytest

from src.passwords import password_problem, temporary_password


@pytest.mark.parametrize("password", ["Abcdefg1", "network2026", "P4ss word!"])
def test_acceptable(password):
	assert password_problem(password) is None


@pytest.mark.parametrize("password, fragment", [
	("Abc1234", "at least 8"),               # 7 characters
	("abcdefgh", "letters and digits"),      # no digit
	("12345678", "letters and digits"),      # no letter
	("!!!!!!!!", "letters and digits"),
	("pässword12", "ASCII"),
	("admin", "at least 8"),                 # the factory password
])
def test_refused(password, fragment):
	assert fragment in password_problem(password)


def test_username_inside_is_refused_case_insensitively():
	assert "username" in password_problem("xxBOBxx123", username="bob")
	assert password_problem("xxBOBxx123", username="alice") is None


def test_a_change_must_differ():
	assert "differ" in password_problem("Same-pass-1", current="Same-pass-1")
	assert password_problem("Same-pass-2", current="Same-pass-1") is None


def test_temporary_passwords_follow_the_rule_and_differ():
	generated = {temporary_password("tempuser") for _ in range(50)}
	assert len(generated) == 50
	assert all(password_problem(p, "tempuser") is None for p in generated)
