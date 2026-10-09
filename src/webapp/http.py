"""What many web pages share: JSON answers, the admin / JSON / form
decorators, which devices a user sees, the query builder's filters, the
dashboard's KPIs, signing a user out everywhere - and WebServices
(current_app.web): the audit log and the generic load-check-act on a row."""
import functools
import uuid
from collections.abc import Callable
from enum import Enum
from typing import Any

from flask import Response, flash, jsonify, redirect, request, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user
from sqlalchemy.orm import Session

from src.accounts.users import is_background
from src.audit import Actor, AuditAction
from src.db.connections import BackendServices
from src.db.tables import Base, PropertyDefinition, SecurityProfile, Role
from src.encryption import encrypt
from src.inventory import SYSTEM_PROPERTIES, ReachabilityChecker
from src.webapp.app import current_app


# A view function, and one that receives the request's data as `data`
View = Callable[..., ResponseReturnValue]


class Caller(Enum):
	"""Which signs make a request one from a page's script - answered JSON,
	not a page or a redirect (wants_json). Each situation has its own signs:
	(a JSON body, the XHR header, a background request (users.is_background),
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
		if current_user.role != Role.ADMIN:
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


def flash_redirect(msg: str, endpoint: str,
                   category: str = "success") -> ResponseReturnValue:
	""":returns: a redirect to the endpoint, with the message flashed"""
	flash(msg, category)
	return redirect(url_for(endpoint))


class WebServices:
	"""The web app's services on top of the backend (current_app.web)."""

	def __init__(self, backend: BackendServices) -> None:
		self.backend = backend
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
		if current_app.maintenance.writes_blocked:
			# a database move copies the data: a row now would be lost at
			# the switch (the gate lets only views that don't write through)
			print(f"[NetRollout] not audited during maintenance: {action} by "
			      f"{username}", flush=True)
			return
		if actor_id is None:
			actor_id = current_user.id if current_user.is_authenticated else None
		self.backend.audit_trail.record(
			Actor(actor_id, username, request.remote_addr), action,
			object_type=object_type, object_id=object_id, object_label=object_label,
			detail=detail, success=success)

	def act_on_db_obj(self, model: type[Base], obj_id: uuid.UUID | str | None,
	                  func: Callable[[Any, Session], ResponseReturnValue],
	                  user_id: uuid.UUID | None = None, many: bool = False,
	                  on_missing: Callable[[], ResponseReturnValue] | None = None,
	                  can_access: Callable[[Any], bool] | None = None,
	                  **extra_filters: Any) -> ResponseReturnValue:
		"""Load a row (or rows) and act on it in one session - committed when
		func returns.

		:param model: the table's class
		:param obj_id: the row's id; None: by the other filters only
		:param func: (the row - or the list, with many - , the session) → the
		 view's answer
		:param user_id: only a row of this owner
		:param many: every matching row (never "not found")
		:param on_missing: the answer when there's no such row; 404 JSON when None
		:param can_access: access rules filter_by can't express (e.g. owner
		 OR admin-on-global); a denied row gets the same answer as a missing
		 one, so its existence isn't leaked
		:param extra_filters: more column = value filters"""
		with self.backend.postgres.get_session() as db_session:
			filters: dict[str, Any] = {"id": obj_id} if obj_id is not None else {}
			if user_id is not None:
				filters["user_id"] = user_id
			filters.update(extra_filters)
			query = db_session.query(model).filter_by(**filters)
			if many:
				return func(query.all(), db_session)
			else:
				obj = query.first()
				if not obj or (can_access and not can_access(obj)):
					return on_missing() if on_missing else err("Not found", 404)
				return func(obj, db_session)

	# CRUD factories
	@staticmethod
	def get_label(obj: Any) -> str:
		""":returns: how the audit names a row: its label, name or token, else
		 its id"""
		return (getattr(obj, 'label', None) or
		        getattr(obj, 'name', None) or
		        getattr(obj, 'token', None) or
		        str(obj.id))

	def update_op(self, fields: dict[str, Any], audit_action: AuditAction,
	              label_func: Callable[[Any], str] | None = None,
	              skip_none: bool = False,
	              on_success: Callable[[str], ResponseReturnValue] | None = None
	              ) -> Callable[[Any, Session], ResponseReturnValue]:
		"""An act_on_db_obj func that sets fields on the row and audits it.

		:param fields: column → new value
		:param label_func: the row's name in the audit; get_label when None
		:param skip_none: a None value leaves its column as it is
		:param on_success: the answer, given the label; ok() when None"""
		def func(obj: Any, _: Session) -> ResponseReturnValue:
			for k, v in fields.items():
				if skip_none and v is None:
					continue
				setattr(obj, k, v)
			label = label_func(obj) if label_func else self.get_label(obj)
			self.audit(audit_action, object_type=type(obj).__name__,
			           object_id=obj.id,
			           object_label=label)
			return on_success(label) if on_success else ok()

		return func

	def delete_op(self, audit_action: AuditAction,
	              data_filter: Callable[[Any], ResponseReturnValue | None] | None = None,
	              label_func: Callable[[Any], str] | None = None,
	              on_success: Callable[[str], ResponseReturnValue] | None = None
	              ) -> Callable[[Any, Session], ResponseReturnValue]:
		"""An act_on_db_obj func that deletes the row and audits it.

		:param data_filter: may refuse: its answer is returned instead (None:
		 go ahead)
		:param label_func: the row's name in the audit; get_label when None
		:param on_success: the answer, given the label; ok() when None"""
		def func(obj: Any, db_session: Session) -> ResponseReturnValue:
			if data_filter:
				res = data_filter(obj)
				if res is not None:
					return res
			label = label_func(obj) if label_func else self.get_label(obj)
			db_session.delete(obj)
			self.audit(audit_action, object_type=type(obj).__name__,
			           object_id=obj.id,
			           object_label=label)
			return on_success(label) if on_success else ok()

		return func

	def build_security_profile(self, label: str | None, username: str, password: str,
	                           enable_secret: str | None,
	                           user_id: uuid.UUID) -> str:
		"""Save a security profile (its secrets encrypted) and audit it.

		:param label: its name; None: shown by its username
		:returns: its id"""
		profile = SecurityProfile(
			label=label,
			username=username,
			password_secret=encrypt(password),
			enable_secret=encrypt(
				enable_secret) if enable_secret else None,
			user_id=user_id
		)
		with self.backend.postgres.get_session() as db_session:
			db_session.add(profile)
			db_session.flush()
			profile_id = str(profile.id)
		self.audit(AuditAction.SECURITY_PROFILE_CREATE, object_type="SecurityProfile",
		           object_label=label or username)
		return profile_id

	def get_property_defs(self, user_id: uuid.UUID) -> tuple[
			list[dict[str, Any]], list[dict[str, Any]]]:
		""":returns: (the system properties, the user's own) - {name, label,
		 icon, is_list, and the user's: id}"""
		with self.backend.postgres.get_session() as db_session:
			user_props = db_session.query(PropertyDefinition).filter_by(
				user_id=user_id).order_by(PropertyDefinition.name).all()
			user_defs = [{"name": p.name, "label": p.label, "icon": p.icon,
			              "is_list": p.is_list, "id": str(p.id)}
			             for p in user_props]
		return SYSTEM_PROPERTIES, user_defs
