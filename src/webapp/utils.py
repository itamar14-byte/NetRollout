"""What many web pages share: JSON answers, the admin / JSON / form
decorators, which devices a user sees, the query builder's filters, the
dashboard's KPIs, signing a user out everywhere - and WebServices
(current_app.web): the audit log and the generic load-check-act on a row."""
import functools
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from flask import Response, flash, jsonify, redirect, request, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user
from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.orm import Session

from src.core import endpoint
from src.db.backend import BackendServices
from src.db.tables import (AuditLog, Base, DeviceResult, Inventory,
                           PropertyDefinition, SecurityProfile, User)
from src.encryption import encrypt
from src.reachability import ReachabilityChecker
from src.webapp.flask_app import current_app

##########################Constants#######################################
# A view function, and one that receives the request's data as `data`
View = Callable[..., ResponseReturnValue]

SYSTEM_PROPERTIES: list[dict[str, Any]] = [
	{"name": "hostname", "label": "Hostname", "icon": "bi-type-h1",
	 "is_list": False},
	{"name": "loopback_ip", "label": "Loopback IP", "icon": "bi-hdd-network",
	 "is_list": False},
	{"name": "asn", "label": "ASN", "icon": "bi-diagram-3", "is_list": False},
	{"name": "mgmt_vrf", "label": "Management VRF", "icon": "bi-box",
	 "is_list": False},
	{"name": "mgmt_interface", "label": "Management Interface",
	 "icon": "bi-ethernet", "is_list": False},
	{"name": "site", "label": "Site", "icon": "bi-geo-alt", "is_list": False},
	{"name": "domain", "label": "Domain", "icon": "bi-globe2",
	 "is_list": False},
	{"name": "timezone", "label": "Timezone", "icon": "bi-clock",
	 "is_list": False},
	{"name": "vrfs", "label": "VRFs", "icon": "bi-layers", "is_list": True},
]


QUERY_OPS: dict[str, Callable[[Any, Any], ColumnElement[bool]]] = {
	"equal": lambda x, y: x == y,
	"not_equal": lambda x, y: x != y,
	"greater_or_equal": lambda x, y: x >= y,
	"less_or_equal": lambda x, y: x <= y,
	"contains": lambda x, y: x.ilike(f"%{y}%"),
	"begins_with": lambda x, y: x.ilike(f"{y}%"),
	"ends_with": lambda x, y: x.ilike(f"%{y}")
}


##########################Sessions#############################################

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


##########################Jsonify helpers#######################################

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


##########################Decorators###########################################
def require_admin(f: View) -> View:
	"""Admins only: anyone else gets 403 (JSON / XHR) or is sent back to the
	page they came from."""
	@functools.wraps(f)
	def decorated(*args: Any, **kwargs: Any) -> ResponseReturnValue:
		if current_user.role != "admin":
			if (request.is_json or request.headers.get("X-Requested-With")
					== "XMLHttpRequest"):
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
						if (request.is_json or request.headers.get(
								"X-Requested-With") == "XMLHttpRequest"):
							return err(f"Missing field: {field}")
						flash(f"{field.replace('_', ' ').title()} is required.",
						      "danger")
						return redirect(request.referrer or url_for("auth.home"))
			return f(*args, data=request.form, **kwargs)

		return decorated

	return decorator


#######################Route helpers###############################
def flash_redirect(msg: str, endpoint: str,
                   category: str = "success") -> ResponseReturnValue:
	""":returns: a redirect to the endpoint, with the message flashed"""
	flash(msg, category)
	return redirect(url_for(endpoint))

#######################Device visibility###############################
def visible_devices_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
	"""Devices a user may see and roll out to: their own plus all global ones."""
	return or_(Inventory.user_id == user_id, Inventory.is_global.is_(True))


def query_visible_devices(db_session: Session, user_id: uuid.UUID) -> list[Inventory]:
	"""Visible devices with the relationships templates and rollout need,
	preloaded so rows survive expunge."""
	devices = (db_session.query(Inventory)
	           .filter(visible_devices_clause(user_id))
	           .order_by(Inventory.label)
	           .all())
	_ = [d.security_profile for d in devices]
	_ = [d.var_mappings for d in devices]
	return devices


def can_edit_device(device: Inventory, user: User) -> bool:
	"""Owners edit their own devices; any admin may edit a global device."""
	return device.user_id == user.id or (
			device.is_global and user.role == "admin")


def same_endpoint_devices(db_session: Session, user_id: uuid.UUID, ip: str,
                          port: int | str,
                          exclude_id: uuid.UUID | None = None) -> list[Inventory]:
	"""Visible devices (own + global) already using this ip:port. Overlap is
	legitimate (NAT, VRFs, port-forwarded labs), so callers warn, never
	block; other users' private devices are never considered.

	:param exclude_id: the device being edited (it doesn't clash with itself)"""
	query = db_session.query(Inventory).filter(
		visible_devices_clause(user_id),
		Inventory.ip == ip, Inventory.port == int(port))
	if exclude_id is not None:
		query = query.filter(Inventory.id != exclude_id)
	return query.order_by(Inventory.label).all()


def same_endpoint_warning(devices: Sequence[Inventory], ip: str,
                          port: int | str) -> str | None:
	"""One warning naming the devices that share ip:port. Build it while the
	DB session is open (it reads labels); flash it after the success message.

	:returns: the warning; None when no device shares it"""
	if not devices:
		return None
	names = ", ".join(f"{d.label} (global)" if d.is_global else d.label
	                  for d in devices[:5]) + (", …" if len(devices) > 5 else "")
	return (f"{endpoint(ip, port)} is already used by {names}. That's fine for NAT, "
	        f"VRFs or port-forwarded labs, but they can't be in the same "
	        f"rollout.")


def partition_devices(devices: Sequence[Inventory]) -> tuple[list[Inventory], list[Inventory]]:
	"""Split visible devices into (global_devices, my_devices)."""
	global_devices = [d for d in devices if d.is_global]
	my_devices = [d for d in devices if not d.is_global]
	return global_devices, my_devices


#######################Query helpers###############################
def compile_query_rules(node: dict[str, Any],
                        allowed_fields: dict[str, tuple[Any, set[str]]]) -> ColumnElement[bool]:
	"""A jQuery QueryBuilder tree as an SQL filter. Each node is either
	 - a GROUP: {"condition": "AND"/"OR", "rules": [...child nodes...]}
	 - a LEAF: {"field": "status", "operator": "equal", "value": "success"}

	:param allowed_fields: the fields that may be filtered on → (their
	 column, the operators allowed on it) - the only columns that reach SQL
	:raises ValueError: a field or operator not allowed, or a bad date
	:raises KeyError: a node without its keys"""
	if "condition" in node:
		combinator = and_ if node["condition"] == "AND" else or_
		return combinator(*[compile_query_rules(r, allowed_fields) for r in
		                    node["rules"]])
	field_name = node["field"]
	operator = node["operator"]
	value = node["value"]

	if field_name not in allowed_fields:
		raise ValueError(f"Field not allowed: {field_name}")

	column, allowed_ops = allowed_fields[field_name]
	if operator not in allowed_ops:
		raise ValueError(
			f"Operator {operator} not allowed for field {field_name}")

	# DateTime columns need a Python datetime object, not a raw string
	if hasattr(column, "type") and column.type.__class__.__name__ == 'DateTime':
		try:
			value = datetime.strptime(value, "%Y-%m-%d")
		except (ValueError, TypeError):
			raise ValueError(f"Invalid date: {value}")

	# Boolean columns: QueryBuilder sends string keys ("true"/"false")
	if hasattr(column, "type") and column.type.__class__.__name__ == 'Boolean':
		if isinstance(value, str):
			value = value.lower() == "true"

	return QUERY_OPS[operator](column, value)


def build_kpi(results_30d: Sequence[DeviceResult],
              label_map: dict[str, str]) -> dict[str, Any]:
	"""The dashboard's tiles from the last 30 days' device results.

	:param label_map: device IP → its label, to name the most-failed device
	:returns: success_rate (%, None without results), jobs_30d,
	 devices_reached, commands_pushed, top_failed ({ip, label, fail_count} or
	 None)"""
	total_ops = len(results_30d)
	jobs_30d = len({r.job_id for r in results_30d})
	success_count = sum(1 for r in results_30d if r.status == "success")

	fail_counts_ip: dict[str, int] = defaultdict(int)
	for r in results_30d:
		if r.status == "failed":
			fail_counts_ip[r.device_ip] += 1
	top_failed = None
	if fail_counts_ip:
		top_ip = max(fail_counts_ip, key=lambda ip: fail_counts_ip[ip])
		top_failed = {"ip": top_ip, "label": label_map.get(top_ip),
		              "fail_count": fail_counts_ip[top_ip]}

	return {
		"success_rate": round(
			success_count / total_ops * 100) if total_ops else None,
		"jobs_30d": jobs_30d,
		"devices_reached": total_ops,
		"commands_pushed": sum(r.commands_sent for r in results_30d),
		"top_failed": top_failed
	}

##################Backend facing helpers#######################################
class WebServices:
	"""The web app's services on top of the backend (current_app.web)."""

	def __init__(self, backend: BackendServices) -> None:
		self.backend = backend
		# resolved per use: the Redis connection can be hot-swapped
		self.reachability = ReachabilityChecker(
			lambda: backend.redis.client,
			ttl=lambda: backend.settings.get("reachability_cache_seconds"))

	##########################Audit############################################
	def audit(self, action: str, *, object_type: str | None = None,
	          object_id: uuid.UUID | str | None = None,
	          object_label: str | None = None, detail: dict[str, Any] | None = None,
	          success: bool = True, username: str | None = None,
	          actor_id: uuid.UUID | None = None) -> None:
		"""Write one append-only audit row, in its own DB session so it
		commits independently of the calling route's transaction. During a
		database move's maintenance it's printed instead (it would be lost).

		:param action: e.g. "user.created"
		:param username: who did it; the signed-in user (or "anonymous") when None
		:param actor_id: their id; the signed-in user's when None"""
		if username is None:
			username = current_user.username if current_user.is_authenticated else "anonymous"
		if current_app.maintenance.writes_blocked:
			# a database move copies the data: a row now would be lost at
			# the switch (the gate lets only views that don't write through)
			print(f"[NetRollout] not audited during maintenance: {action} by "
			      f"{username}", flush=True)
			return
		if actor_id is None:
			actor_id = current_user.id if current_user.is_authenticated else None
		with self.backend.postgres.get_session() as db_session:
			db_session.add(AuditLog(
				actor_id=actor_id,
				actor_username=username,
				action=action,
				object_type=object_type,
				object_id=object_id,
				object_label=object_label,
				success=success,
				ip_address=request.remote_addr,
				detail=detail,
			))

	#######################DB functional abstractions###############################
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

	def update_op(self, fields: dict[str, Any], audit_action: str,
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

	def delete_op(self, audit_action: str,
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

	###################Route helpers###########################################
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
		self.audit("security_profile.create", object_type="SecurityProfile",
		           object_label=label or username)
		return profile_id

	#######################Auth helpers###############################
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
