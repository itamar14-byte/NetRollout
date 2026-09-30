"""The one password rule for local accounts. Registration, a password change
and the temporary passwords of an admin reset all go through it; the pages
mirror it in templates/_password_rule_script.html for instant feedback."""
import secrets
import string

MIN_LENGTH = 8
# Also rules out the seeded admin's "admin" (too short, one group)
RULE = (f"at least {MIN_LENGTH} characters with at least 2 of: letters, "
        f"digits, special characters (ASCII only), not containing your "
        f"username")
_TEMP_ALPHABET = string.ascii_letters + string.digits
_TEMP_LENGTH = 14


def password_problem(new: str, username: str | None = None,
                     current: str | None = None) -> str | None:
	"""Why `new` isn't acceptable, or None. `current`: the password it
	replaces (a change must pick a different one)."""
	if any(not " " <= c <= "~" for c in new):
		return "The password must contain only ASCII characters."
	if len(new) < MIN_LENGTH:
		return f"The password must be at least {MIN_LENGTH} characters."
	groups = (any(c.isalpha() for c in new), any(c.isdigit() for c in new),
	          any(not c.isalnum() for c in new))
	if sum(groups) < 2:
		return ("The password must contain at least 2 of: letters, digits, "
		        "special characters.")
	if username and username.lower() in new.lower():
		return "The password can't contain your username."
	if current is not None and new == current:
		return "The new password must differ from the current one."
	return None


def temporary_password(username: str | None = None) -> str:
	"""A random password that satisfies the rule, for an admin reset."""
	while True:
		candidate = "".join(secrets.choice(_TEMP_ALPHABET)
		                    for _ in range(_TEMP_LENGTH))
		if password_problem(candidate, username) is None:
			return candidate
