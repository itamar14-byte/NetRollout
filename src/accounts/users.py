"""NetRollout's users: who the data layer's rules are about (Viewer), the
accounts (Accounts: local ones - one set of checks for Request access and an
admin's Add user - and directory ones, the name lookups), the one password
rule (registration, a change, an
admin reset's temporary password; the pages mirror it in
templates/_password_rule_script.html), and who is signed in and for how
long - the sessions in Redis (SessionStore), their idle and absolute limits,
signing a user out everywhere, and the clean start (everyone signed out).
No request here: the web app's side is in webapp/hooks.py."""
import re
import secrets
import string
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import redis
from sqlalchemy import func
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from src.db.connections import REDIS_UNAVAILABLE
from src.db.tables import User, AuthType, Role


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


@dataclass(frozen=True)
class Viewer:
	"""Who the data layer's rules are about: a user's id and whether they're
	an admin. The web app builds the signed-in user's once per request
	(webapp/http.viewer(); nothing here knows of a request). A rule about
	another user - a job owner's devices, an admin's look at someone's
	numbers - gets that user's: visibility doesn't depend on is_admin."""
	id: uuid.UUID
	is_admin: bool = False

	@classmethod
	def of(cls, user: User) -> "Viewer":
		""":returns: the user's viewer (their id, admin or not)"""
		return cls(user.id, user.role == Role.ADMIN)


ROLES = (Role.OPERATOR, Role.ADMIN)
# the columns' sizes (src/db/tables.py) - longer input is refused in words,
# not by a database error
LIMITS = {"username": 64, "email": 120, "full_name": 120, "position": 64}
# a local account's name: shown and logged everywhere, so plain characters only
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class AccountError(ValueError):
	"""Why the account isn't made - in words for the person."""


# Grafana's own administrator (compose.yaml GF_SECURITY_ADMIN_USER; the
# grafana-setup service signs in as it). Grafana trusts the username nginx hands
# it, so a NetRollout account with this name would be signed in to Grafana as
# its administrator: no account may carry it, in any capitals.
GRAFANA_ADMIN_USERNAME = "netrollout-grafana-admin"
RESERVED_USERNAME = "That username is reserved for NetRollout's own use - choose another."


def is_reserved(username: str) -> bool:
	""":returns: whether no NetRollout account may have this name"""
	return username.strip().lower() == GRAFANA_ADMIN_USERNAME


class Accounts:
	"""NetRollout's accounts in a session (the caller commits): a new local
	account (Request access, an admin's Add user) or directory account (the
	first sign-in of a mapped group's member, the admin page's import), the
	one case-insensitive name lookup, the access requests waiting, and the
	users an admin picks from."""

	def __init__(self, session: Session) -> None:
		self.session = session

	def by_name(self, username: str) -> User | None:
		""":returns: the account with exactly this name; None: none"""
		return self.session.query(User).filter_by(username=username).first()

	def by_name_ci(self, username: str) -> list[User]:
		""":returns: the accounts with this name in any case - as a directory
		 matches names ("Alice" is "alice")"""
		return self.session.query(User).filter(
			func.lower(User.username) == username.lower()).all()

	def pending_count(self) -> int:
		"""Access requests waiting for an admin (the sidebar's count)."""
		return self.session.query(User.id).filter(User.is_approved.is_(False)).count()

	def for_admin_picker(self) -> list[User]:
		""":returns: every account, by name - the users an admin picks from"""
		return self.session.query(User).order_by(User.username).all()

	def usernames(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
		""":returns: user id → username for these ids, in one query (an id
		 without an account is left out)"""
		wanted = set(ids)
		return {row.id: row.username for row in self.session.query(User.id, User.username)
		        .filter(User.id.in_(wanted))} if wanted else {}

	def new_local(self, *, username: str, email: str, full_name: str,
	              position: str | None, password: str, role: str = Role.OPERATOR,
	              approved: bool = False, must_change_password: bool = False) -> User:
		"""A local account, added to the session (checked first).

		:param password: the person's own, or (must_change_password) a temporary
		 one - not held to the password rule, the person replaces it at once
		:param approved: approved and active at once (an admin's Add user);
		 False: an access request
		:raises AccountError: refused, and why"""
		username, email, full_name = username.strip(), email.strip(), full_name.strip()
		position = (position or "").strip() or None
		if role not in ROLES:
			raise AccountError("The role is operator or admin.")
		self._check_new(username, email, full_name, position,
		                None if must_change_password else password)
		user = User(username=username, email=email, full_name=full_name, position=position,
		            password_hash=generate_password_hash(password), role=role,
		            is_approved=approved, is_active=approved,
		            must_change_password=must_change_password, auth_type=AuthType.LOCAL)
		self.session.add(user)
		self.session.flush()
		return user

	def new_ldap(self, username: str, server_id: uuid.UUID, role: str | None = None,
	             details: Mapping[str, str | None] | None = None) -> User:
		"""A directory user's account, added to the session: approved and
		active, no password (the directory checks it).

		:param role: the mapped group's; None: the default (operator)
		:param details: {email, full_name} as the directory gave them; None:
		 none
		:raises AccountError: the name is reserved (the callers check first)"""
		if is_reserved(username):
			raise AccountError(RESERVED_USERNAME)
		extra: dict[str, Any] = {"role": role} if role is not None else {}
		user = User(username=username, auth_type=AuthType.LDAP, ldap_server_id=server_id,
		            is_approved=True, is_active=True, password_hash=None,
		            email=(details or {}).get("email"),
		            full_name=(details or {}).get("full_name"), **extra)
		self.session.add(user)
		self.session.flush()
		return user

	def _check_new(self, username: str, email: str, full_name: str,
	               position: str | None, password: str | None) -> None:
		""":raises AccountError: a missing or too long field, a username with
		other characters than letters, digits, . _ -, an email without @, the
		password rule (when a password is given), the reserved username, a
		username (in any case) or email in use"""
		fields = {"username": username, "email": email, "full_name": full_name}
		labels = {"username": "Username", "email": "Email", "full_name": "Full name",
		          "position": "Position"}
		for key, value in fields.items():
			if not value:
				raise AccountError(f"{labels[key]} is required.")
		for key, value in {**fields, "position": position or ""}.items():
			if len(value) > LIMITS[key]:
				raise AccountError(f"{labels[key]} is too long (at most {LIMITS[key]} characters).")
		if not USERNAME_RE.fullmatch(username):
			raise AccountError("A username may contain letters, digits, . _ and - "
			                   "(starting with a letter or digit).")
		if "@" not in email:
			raise AccountError("That email address isn't valid.")
		if password is not None and (problem := password_problem(password, username)):
			raise AccountError(problem)
		if is_reserved(username):      # its own words: not "taken"
			raise AccountError(RESERVED_USERNAME)
		# "Dana" next to "dana" would be two accounts for one name
		if self.by_name_ci(username):
			raise AccountError("That username is taken.")
		if self.session.query(User.id).filter(User.email == email).first():
			raise AccountError("That email address is already in use.")


SESSION_PREFIX = "redis_session:"


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
# the idle limit is re-read from System Settings at most this often (it's
# asked on every request, Grafana's included)
IDLE_LIMIT_CACHE_SECONDS = 30


def seconds_left(data: Mapping[str, Any], now: float, idle_limit: int) -> tuple[float, float]:
	"""(idle, absolute) seconds left for a session's data (a session without
	its clocks counts from now).

	:param now: the time (epoch seconds)
	:param idle_limit: the idle limit, seconds"""
	idle = idle_limit - (now - data.get(LAST_ACTIVE, now))
	absolute = ABSOLUTE_SESSION_HOURS * 3600 - (now - data.get(SIGNED_IN_AT, now))
	return idle, absolute


class SessionSerializer(Protocol):
	"""How the stored sessions are encoded (flask-session's serializer)."""
	def decode(self, serialized_data: bytes) -> Any: ...


class SessionStore:
	"""The signed-in sessions in Redis (redis_session:<sid>, written by the
	web app's session interface): whose they are, signing a user out
	everywhere, who is signed in, the clean start - and the idle limit, from
	System Settings, cached."""

	def __init__(self, client: Callable[[], redis.Redis], serializer: SessionSerializer,
	             idle_minutes: Callable[[], int]) -> None:
		""":param client: returns the Redis client in use now (a Redis switch
		 is followed)
		:param serializer: decodes a stored session
		:param idle_minutes: reads the session_idle_minutes setting"""
		self._client = client
		self._serializer = serializer
		self._idle_minutes = idle_minutes
		self._idle_at = 0.0
		self._idle_seconds: int | None = None

	def idle_seconds(self) -> int:
		"""The idle limit, re-read at most every IDLE_LIMIT_CACHE_SECONDS."""
		now = time.monotonic()
		if self._idle_seconds is None or now - self._idle_at > IDLE_LIMIT_CACHE_SECONDS:
			self._idle_seconds = self._idle_minutes() * 60
			self._idle_at = now
		return self._idle_seconds

	def forget_idle_limit(self) -> None:
		"""The next idle_seconds() reads the setting again - the tests' seam
		(a changed setting otherwise applies within IDLE_LIMIT_CACHE_SECONDS)."""
		self._idle_seconds = None

	def _stored(self, client: redis.Redis) -> Iterator[tuple[bytes, Any]]:
		""":returns: each stored session's (key, decoded data) - an empty or
		 unreadable one left out (not ours to judge)"""
		for key in client.scan_iter(f"{SESSION_PREFIX}*"):
			raw = cast(bytes | None, client.get(key))
			if not raw:
				continue
			try:
				data = self._serializer.decode(raw)
			except Exception:   # unreadable: not a session of ours
				continue
			yield key, data

	def end_for(self, user_id: uuid.UUID | str, keep_sid: str | None = None) -> int:
		"""Sign a user out everywhere (except `keep_sid`, the caller's own
		session after a password change): every stored session is decoded to
		find the user's — complete and cheap at this scale.

		:returns: how many sessions were ended"""
		client = self._client()
		ended = 0
		for key, data in self._stored(client):
			if keep_sid and key.decode() == f"{SESSION_PREFIX}{keep_sid}":
				continue
			if data.get("_user_id") == str(user_id):
				client.delete(key)
				ended += 1
		return ended

	def signed_in(self, now: float) -> dict[str, float]:
		"""Who is signed in now, for Live Sessions and the Users page. Every
		stored session is read: one that ended by inactivity stays stored until
		its browser comes back (that's when it's checked).

		:param now: the time (epoch seconds)
		:returns: user id → when the newest of their live sessions began"""
		users: dict[str, float] = {}
		for _, data in self._stored(self._client()):
			user_id = data.get("_user_id")
			if not user_id:
				continue
			idle, absolute = seconds_left(data, now, self.idle_seconds())
			if idle > 0 and absolute > 0:
				users[user_id] = max(users.get(user_id, 0.0), data.get(SIGNED_IN_AT, now))
		return users

	def clear_all(self) -> None:
		"""Everyone signed out. Every start does it — deliberately (2026-10-04):
		a privileged network-management console starts clean after a restart,
		update or reboot, like a firewall's management plane. Rollouts don't
		depend on sessions (the drain lets them finish). Within a run, sessions
		end after inactivity (session_idle_minutes) and after
		ABSOLUTE_SESSION_HOURS (above). Redis being down changes nothing."""
		client = self._client()
		try:
			for redis_key in client.scan_iter(f"{SESSION_PREFIX}*"):
				client.delete(redis_key)
		except REDIS_UNAVAILABLE:
			pass
