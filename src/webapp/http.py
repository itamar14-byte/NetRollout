"""What many web pages share: JSON answers, the admin / JSON / form
decorators, the signed-in user as the data layer's rules see them (viewer),
loading a row the request may act on (load_owned) and the refusals a route
answers after its session block - and WebServices (current_app.web): the
audit log and the reachability checks."""
import functools
import uuid
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar, cast

from flask import Response, flash, g, jsonify, redirect, request, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user
from sqlalchemy.orm import Session

from src.accounts.users import Viewer
from src.audit import Actor, AuditAction, AuditTrail
from src.db.connections import BackendServices
from src.db.tables import Base, User
from src.inventory import ReachabilityChecker
if TYPE_CHECKING:   # annotations only: lifecycle imports this module
	from src.webapp.lifecycle import Maintenance


# A view function, and one that receives the request's data as `data`
View = Callable[..., ResponseReturnValue]
# what a page does by itself, not a person (is_background)
_PASSIVE_PATHS = ("/rollout/stream/",)


def is_background() -> bool:
	""":returns: whether the request is the page's own (a poll, an automatic
	 reload, the live log), not something a person did - marked by the header
	 X-NR-Background: 1 or ?_bg=1, or the live log's stream"""
	return (request.headers.get("X-NR-Background") == "1"
	        or request.args.get("_bg") == "1"
	        or request.path.startswith(_PASSIVE_PATHS))


class Caller(Enum):
	"""Which signs make a request one from a page's script - answered JSON,
	not a page or a redirect (wants_json). Each situation has its own signs:
	(a JSON body, the XHR header, a background request (is_background),
	any method but GET, the live log's stream)."""
	#             JSON body, XHR,  background, non-GET, live log stream
	# a page's script (fetch with a JSON body or the XHR header)
	SCRIPT = (True, True, False, False, False)
	# a request that can't follow a redirect to a page either: background
	# ones and form posts too (a session that ended, maintenance)
	SESSION_CHECK = (True, True, True, True, False)
	# only a JSON body counts (a stale CSRF token, a key that doesn't decrypt)
	JSON_BODY = (True, False, False, False, False)
	# a JSON body, or the live log's stream (a service that's down)
	STREAM_AWARE = (True, False, False, False, True)

	def wants_json(self) -> bool:
		""":returns: whether the current request shows one of this situation's
		 signs"""
		json_body, xhr, background, non_get, stream = self.value
		return bool((non_get and request.method != "GET")
		            or (json_body and request.is_json)
		            or (xhr and request.headers.get("X-Requested-With") == "XMLHttpRequest")
		            or (background and is_background())
		            or (stream and request.path.startswith("/rollout/stream")))


def picked_username(users: list[User], selected: str) -> str:
	""":param users: the accounts an admin picks from (Accounts.for_admin_picker)
	:param selected: the page's ?user= scope - "me" or an id
	:returns: what the user picker shows: "me", the picked user's name, else
	 the id as given"""
	if selected == "me":
		return "me"
	return next((u.username for u in users if str(u.id) == selected), selected)


def viewer() -> Viewer:
	""":returns: the signed-in user as the data layer's rules see them (built
	 once per request)"""
	if "viewer" not in g:
		g.viewer = Viewer.of(current_user)
	return cast(Viewer, g.viewer)


def ok(message: str | None = None, /, **extra: Any) -> Response:
	""":returns: {"status": "ok", "message"?, **extra} as JSON (the message is
	 positional, so no extra field can stand in for it)"""
	body: dict[str, Any] = {"status": "ok"}
	if message is not None:
		body["message"] = message
	body.update(extra)
	return jsonify(body)


def err(message: str, code: int = 400, **extra: Any) -> tuple[Response, int]:
	""":returns: {"status": "error", "message", **extra} as JSON, with the code"""
	return jsonify({"status": "error", "message": message, **extra}), code


def require_admin(f: View) -> View:
	"""Admins only: anyone else gets 403 (JSON / XHR) or is sent back to the
	page they came from."""
	@functools.wraps(f)
	def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
		if not viewer().is_admin:
			if Caller.SCRIPT.wants_json():
				return err("Forbidden", 403)
			return redirect(request.referrer or url_for("jobs.dashboard"))
		return f(*args, **kwargs)

	return decorated


def with_json(*required_fields: str,
              on_invalid: Callable[[], object] | None = None) -> Callable[[View], View]:
	"""The view gets the request's JSON body as `data`; no body, or a required
	field missing or blank, is answered 400 without calling it.

	:param on_invalid: called before answering a request without a body"""
	def decorator(f: View) -> View:
		@functools.wraps(f)
		def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
			data = request.get_json(silent=True)
			if not data:
				if on_invalid:
					on_invalid()
				return err("Invalid request")
			for field in required_fields:
				if field not in data or not str(data[field] or "").strip():
					return err(f"Missing field: {field}")
			return f(*args, data=data, **kwargs)

		return decorated

	return decorator


def with_form(*required_fields: str) -> Callable[[View], View]:
	"""The view gets the request's form as `data`; on a POST, a required field
	missing or blank is answered without calling it - 400 JSON for XHR, else
	a flash message and back to the page it came from."""
	def decorator(f: View) -> View:
		@functools.wraps(f)
		def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
			if request.method not in ("GET", "HEAD", "OPTIONS"):
				for field in required_fields:
					if not request.form.get(field, "").strip():
						if Caller.SCRIPT.wants_json():
							return err(f"Missing field: {field}")
						flash(f"{field.replace('_', ' ').title()} is required.",
						      "danger")
						return redirect(request.referrer or url_for("auth.home"))
			return f(*args, data=request.form, **kwargs)

		return decorated

	return decorator


class NotFound(LookupError):
	"""No such row - or one the request may not touch (the same answer, so
	its existence isn't revealed). Raised by load_owned; the route answers it
	after its session block."""


class Refused(Exception):
	"""The request is refused - the reason in words for the person. Raised by
	the pages' helpers; the route answers it after its session block, so
	nothing of a half-done change is committed (get_session rolls back on an
	exception, and commits on a normal exit)."""


Row = TypeVar("Row", bound=Base)


def load_owned(session: Session, model: type[Row], obj_id: uuid.UUID,
               can_access: Callable[[Row], bool] | None = None) -> Row:
	"""A row the request may act on.

	:param can_access: the access rule (e.g. its owner, or an admin on a
	 global one); None: the signed-in user owns it (its user_id)
	:raises NotFound: there's none, or the rule refuses it"""
	obj = session.get(model, obj_id)
	allowed = can_access or (lambda row: getattr(row, "user_id", None) == viewer().id)
	if obj is None or not allowed(obj):
		raise NotFound(f"no {model.__name__} {obj_id}")
	return obj


def flash_redirect(msg: str, endpoint: str,
                   category: str = "success") -> ResponseReturnValue:
	""":returns: a redirect to the endpoint, with the message flashed"""
	flash(msg, category)
	return redirect(url_for(endpoint))


class WebServices:
	"""The web app's services on top of the backend (current_app.web): the
	audit log and the reachability checks."""

	def __init__(self, backend: BackendServices, maintenance: "Maintenance") -> None:
		""":param maintenance: while it's locked, audit rows are printed, not written"""
		self.backend = backend
		self._maintenance = maintenance
		# the connection looked up per row: a database move is followed
		self.audit_trail = AuditTrail(lambda: backend.postgres)
		# resolved per use: the Redis connection can be hot-swapped
		self.reachability = ReachabilityChecker(
			lambda: backend.redis.client,
			ttl=lambda: backend.settings.get("reachability_cache_seconds"))

	def audit(self, action: AuditAction, *, object_type: str | None = None,
	          object_id: uuid.UUID | str | None = None,
	          object_label: str | None = None, detail: dict[str, Any] | None = None,
	          success: bool = True, username: str | None = None,
	          actor_id: uuid.UUID | None = None) -> None:
		"""The request's audit row (AuditTrail.record: its own session, so it
		commits independently of the calling route's transaction), by the
		signed-in user from the request's address. During a database move's
		maintenance it's printed instead (it would be lost).

		:param action: what happened (its value is the row's action)
		:param username: who did it; the signed-in user (or "anonymous") when None
		:param actor_id: their id; the signed-in user's when None"""
		if username is None:
			username = current_user.username if current_user.is_authenticated else "anonymous"
		# a failed sign-in records the name as typed - at most the column's length
		username = username[:64]
		if self._maintenance.writes_blocked:
			# a database move copies the data: a row now would be lost at
			# the switch (the gate lets only views that don't write through)
			print(f"[NetRollout] not audited during maintenance: {action} by "
			      f"{username}", flush=True)
			return
		if actor_id is None:
			actor_id = current_user.id if current_user.is_authenticated else None
		self.audit_trail.record(
			Actor(actor_id, username, request.remote_addr), action,
			object_type=object_type, object_id=object_id, object_label=object_label,
			detail=detail, success=success)



