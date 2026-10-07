"""Local accounts, made the two ways: a person's access request (Request
access: waits for an admin's approval) and an admin's Add user (approved at
once, with a temporary password changed at the first sign-in). One set of
checks for both, so they can't drift apart."""
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from src.db.tables import User
from src.passwords import password_problem

ROLES = ("operator", "admin")
# the columns' sizes (src/db/tables.py) - longer input is refused in words,
# not by a database error
LIMITS = {"username": 64, "email": 120, "full_name": 120, "position": 64}


class AccountError(ValueError):
	"""Why the account isn't made - in words for the person."""


def check_new_user(db_session: Session, username: str, email: str, full_name: str,
                   position: str | None, password: str | None) -> None:
	""":raises AccountError: a missing or too long field, an email without
	@, the password rule (when a password is given), a username or email
	in use"""
	fields = {"username": username, "email": email, "full_name": full_name}
	labels = {"username": "Username", "email": "Email", "full_name": "Full name",
	          "position": "Position"}
	for key, value in fields.items():
		if not value:
			raise AccountError(f"{labels[key]} is required.")
	for key, value in {**fields, "position": position or ""}.items():
		if len(value) > LIMITS[key]:
			raise AccountError(f"{labels[key]} is too long (at most {LIMITS[key]} characters).")
	if "@" not in email:
		raise AccountError("That email address isn't valid.")
	if password is not None and (problem := password_problem(password, username)):
		raise AccountError(problem)
	if db_session.query(User.id).filter(User.username == username).first():
		raise AccountError("That username is taken.")
	if db_session.query(User.id).filter(User.email == email).first():
		raise AccountError("That email address is already in use.")


def new_local_user(db_session: Session, *, username: str, email: str, full_name: str,
                   position: str | None, password: str, role: str = "operator",
                   approved: bool = False, must_change_password: bool = False) -> User:
	"""The account, added to the session (checked first; the caller commits).

	:param password: the person's own, or (must_change_password) a temporary
	 one - not held to the password rule, the person replaces it at once
	:param approved: approved and active at once (an admin's Add user);
	 False: an access request
	:raises AccountError: refused, and why"""
	username, email, full_name = username.strip(), email.strip(), full_name.strip()
	position = (position or "").strip() or None
	if role not in ROLES:
		raise AccountError("The role is operator or admin.")
	check_new_user(db_session, username, email, full_name, position,
	               None if must_change_password else password)
	user = User(username=username, email=email, full_name=full_name, position=position,
	            password_hash=generate_password_hash(password), role=role,
	            is_approved=approved, is_active=approved,
	            must_change_password=must_change_password, auth_type="local")
	db_session.add(user)
	db_session.flush()
	return user


def pending_requests(db_session: Session) -> int:
	"""Access requests waiting for an admin (the sidebar's count)."""
	return db_session.query(User.id).filter(User.is_approved.is_(False)).count()
