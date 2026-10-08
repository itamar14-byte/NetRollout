"""NetRollout's users: local accounts (one set of checks for Request access
and an admin's Add user), the one password rule (registration, a change, an
admin reset's temporary password; the pages mirror it in
templates/_password_rule_script.html), and who is signed in and for how
long - the sessions in Redis, their idle and absolute limits, signing a user
out everywhere, and the clean start (everyone signed out)."""
import secrets
import string
import time
import uuid
from collections.abc import Mapping
from typing import Any, cast

from flask import request, session
from flask_login import current_user
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from src.db.connections import REDIS_UNAVAILABLE, RedisConnection
from src.db.tables import User
from src.webapp.app import current_app


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


SESSION_PREFIX = "redis_session:"


def signed_in_user(db_session: Session) -> User:
	"""The signed-in user's row in this session (current_user is a detached
	copy, without its relationships).

	:raises LookupError: the account is gone (deleted while signed in)"""
	user = db_session.get(User, current_user.id)
	if user is None:
		raise LookupError("the signed-in account no longer exists")
	return user


def end_user_sessions(user_id: uuid.UUID | str, keep_sid: str | None = None) -> int:
	"""Sign a user out everywhere (except `keep_sid`, the caller's own
	session after a password change). user_session:<id> only points at the
	latest sign-in, so every stored session is decoded to find the user's —
	complete (older sessions too) and cheap at this scale.

	:returns: how many sessions were ended"""
	client = current_app.backend.redis.client
	serializer = current_app.session_interface.serializer
	ended = 0
	for key in client.scan_iter(f"{SESSION_PREFIX}*"):
		if keep_sid and key.decode() == f"{SESSION_PREFIX}{keep_sid}":
			continue
		raw = client.get(key)
		try:
			owner = serializer.decode(raw).get("_user_id") if raw else None
		except Exception:   # unreadable: not ours to judge — leave it
			continue
		if owner == str(user_id):
			client.delete(key)
			ended += 1
	if keep_sid is None:
		client.delete(f"user_session:{user_id}")
	return ended


# ── Session lifetime ──
# A session ends after `session_idle_minutes` (System Settings) without user
# activity, and after ABSOLUTE_SESSION_HOURS however active. Activity is what
# a person does: a page load, a form, a click that calls the server. What a
# page does by itself doesn't count — requests marked background (header
# X-NR-Background: 1, or ?_bg=1 on an automatic reload) and the live log
# stream — but expiry is checked on every request.
ABSOLUTE_SESSION_HOURS = 12
SIGNED_IN_AT = "nr_signed_in_at"
LAST_ACTIVE = "nr_last_active"
# no session is checked or written for these (the request hook in hooks.py)
NO_SESSION_PATHS = ("/static/", "/_netrollout/instance", "/_netrollout/health")
_PASSIVE_PATHS = ("/rollout/stream/",)
_IDLE_CACHE: dict[str, Any] = {"at": 0.0, "seconds": None}


def idle_seconds() -> int:
	"""The idle limit, re-read from System Settings at most every 30 s (this
	runs on every request, Grafana's included)."""
	now = time.monotonic()
	if _IDLE_CACHE["seconds"] is None or now - _IDLE_CACHE["at"] > 30:
		_IDLE_CACHE["seconds"] = \
			current_app.backend.settings.get("session_idle_minutes") * 60
		_IDLE_CACHE["at"] = now
	return _IDLE_CACHE["seconds"]


def is_background() -> bool:
	""":returns: whether the request is the page's own (a poll, an automatic
	 reload, the live log), not something a person did"""
	return (request.headers.get("X-NR-Background") == "1"
	        or request.args.get("_bg") == "1"
	        or request.path.startswith(_PASSIVE_PATHS))


def session_seconds_left(now: float | None = None) -> tuple[float, float]:
	"""(idle, absolute) seconds left for the current session.

	:param now: the time (epoch seconds); now when None"""
	return _seconds_left(session, now or time.time())


def _seconds_left(data: Mapping[str, Any], now: float) -> tuple[float, float]:
	""":returns: (idle, absolute) seconds left for a session's data (a session
	 without its clocks counts from now)"""
	idle = idle_seconds() - (now - data.get(LAST_ACTIVE, now))
	absolute = ABSOLUTE_SESSION_HOURS * 3600 - (now - data.get(SIGNED_IN_AT, now))
	return idle, absolute


def signed_in_users(now: float | None = None) -> dict[str, float]:
	"""Who is signed in now, for Live Sessions and the Users page. Every
	stored session is read: one that ended by inactivity stays stored until
	its browser comes back (that's when it's checked), and user_session:<id>
	knows only the latest sign-in.

	:param now: the time (epoch seconds); now when None
	:returns: user id → when the newest of their live sessions began"""
	client = current_app.backend.redis.client
	serializer = current_app.session_interface.serializer
	now = now or time.time()
	users: dict[str, float] = {}
	for key in client.scan_iter(f"{SESSION_PREFIX}*"):
		raw = cast(bytes | None, client.get(key))
		if not raw:
			continue
		try:
			data: dict[str, Any] = serializer.decode(raw)
		except Exception:   # unreadable: not a session of ours
			continue
		user_id = data.get("_user_id")
		if not user_id:
			continue
		idle, absolute = _seconds_left(data, now)
		if idle > 0 and absolute > 0:
			users[user_id] = max(users.get(user_id, 0.0), data.get(SIGNED_IN_AT, now))
	return users


def mark_signed_in() -> None:
	"""A sign-in just completed: both clocks start now."""
	now = time.time()
	session[SIGNED_IN_AT] = session[LAST_ACTIVE] = now


def record_redis_session(user_id: uuid.UUID) -> None:
	"""Point user_session:<id> at this session (Live Sessions, Kick)."""
	sid = getattr(session, "sid", None)
	if sid is None:
		return
	current_app.backend.redis.client.set(f"user_session:{user_id}",
	                              sid, ex=86400)


def clear_sessions(redis_conn: RedisConnection) -> None:
	"""Every start signs everyone out — deliberately (2026-10-04): a privileged
	network-management console starts clean after a restart, update or
	reboot, like a firewall's management plane. Rollouts don't depend on
	sessions (the drain lets them finish). Within a run, sessions end after
	inactivity (session_idle_minutes) and after ABSOLUTE_SESSION_HOURS (above)."""
	try:
		for redis_key in redis_conn.client.scan_iter(f"{SESSION_PREFIX}*"):
			redis_conn.client.delete(redis_key)
	except REDIS_UNAVAILABLE:
		pass
