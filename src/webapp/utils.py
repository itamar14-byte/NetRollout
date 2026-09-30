# python utilities
import functools
from collections import defaultdict
from datetime import datetime

# services
#flask
from flask import jsonify, request, redirect, url_for, flash
from flask_login import current_user
#sqlalchemy
from sqlalchemy import and_, or_

# local modules
from src.db.backend import BackendServices
from src.db.tables import (AuditLog, SecurityProfile, PropertyDefinition,
                           Inventory)
from src.encryption import encrypt
from src.reachability import ReachabilityChecker

##########################Constants#######################################
SYSTEM_PROPERTIES = [
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


QUERY_OPS = {
	"equal": lambda x, y: x == y,
	"not_equal": lambda x, y: x != y,
	"greater_or_equal": lambda x, y: x >= y,
	"less_or_equal": lambda x, y: x <= y,
	"contains": lambda x, y: x.ilike(f"%{y}%"),
	"begins_with": lambda x, y: x.ilike(f"{y}%"),
	"ends_with": lambda x, y: x.ilike(f"%{y}")
}


##########################Jsonify helpers#######################################

def ok(message=None, **extra):
	body = {"status": "ok"}
	if message is not None:
		body["message"] = message
	body.update(extra)
	return jsonify(body)


def err(message, code=400):
	return jsonify({"status": "error", "message": message}), code


##########################Decorators###########################################
def require_admin(f):
	@functools.wraps(f)
	def decorated(*args, **kwargs):
		if current_user.role != "admin":
			if (request.is_json or request.headers.get("X-Requested-With")
					== "XMLHttpRequest"):
				return err("Forbidden", 403)
			return redirect(request.referrer or url_for("jobs.dashboard"))
		return f(*args, **kwargs)

	return decorated


def with_json(*required_fields, on_invalid=None):
	def decorator(f):
		@functools.wraps(f)
		def decorated(*args, **kwargs):
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


def with_form(*required_fields):
	def decorator(f):
		@functools.wraps(f)
		def decorated(*args, **kwargs):
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
def flash_redirect(msg, endpoint, category="success"):
	flash(msg, category)
	return redirect(url_for(endpoint))

#######################Device visibility###############################
def visible_devices_clause(user_id):
	"""Devices a user may see and roll out to: their own plus all global ones."""
	return or_(Inventory.user_id == user_id, Inventory.is_global.is_(True))


def query_visible_devices(db_session, user_id):
	"""Visible devices with the relationships templates and rollout need,
	preloaded so rows survive expunge."""
	devices = (db_session.query(Inventory)
	           .filter(visible_devices_clause(user_id))
	           .order_by(Inventory.label)
	           .all())
	_ = [d.security_profile for d in devices]
	_ = [d.var_mappings for d in devices]
	return devices


def can_edit_device(device, user):
	"""Owners edit their own devices; any admin may edit a global device."""
	return device.user_id == user.id or (
			device.is_global and user.role == "admin")


def same_endpoint_devices(db_session, user_id, ip, port, exclude_id=None):
	"""Visible devices (own + global) already using this ip:port. Overlap is
	legitimate (NAT, VRFs, port-forwarded labs), so callers warn, never
	block; other users' private devices are never considered."""
	query = db_session.query(Inventory).filter(
		visible_devices_clause(user_id),
		Inventory.ip == ip, Inventory.port == int(port))
	if exclude_id is not None:
		query = query.filter(Inventory.id != exclude_id)
	return query.order_by(Inventory.label).all()


def same_endpoint_warning(devices, ip, port) -> str | None:
	"""One warning naming the devices that share ip:port. Build it while the
	DB session is open (it reads labels); flash it after the success message."""
	if not devices:
		return None
	names = ", ".join(f"{d.label} (global)" if d.is_global else d.label
	                  for d in devices[:5]) + (", …" if len(devices) > 5 else "")
	return (f"{ip}:{port} is already used by {names}. That's fine for NAT, "
	        f"VRFs or port-forwarded labs, but they can't be in the same "
	        f"rollout.")


def partition_devices(devices):
	"""Split visible devices into (global_devices, my_devices)."""
	global_devices = [d for d in devices if d.is_global]
	my_devices = [d for d in devices if not d.is_global]
	return global_devices, my_devices


#######################Query helpers###############################
def compile_query_rules(node, allowed_fields):
	"""jQuery QueryBuilder produces a tree.
	Each node is either:
	 - a GROUP: {"condition": "AND"/"OR", "rules": [...child
	nodes...]}
	 - a LEAF: {"field": "status", "operator": "equal", "value":
	"success"}"""

	if "condition" in node:
		# GROUP node — recurse into each child, then combine with
		# AND / OR
		combinator = and_ if node["condition"] == "AND" else or_
		return combinator(*[compile_query_rules(r, allowed_fields) for r in
		                    node["rules"]])
	# LEAF node — a single data_filter condition
	field_name = node["field"]
	operator = node["operator"]
	value = node["value"]

	# Security(SQL injection hardening): reject fields/operators
	# not in our allowlist
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

	# Dispatch to the right SQLAlchemy expression via the OPS table
	return QUERY_OPS[operator](column, value)


def build_kpi(results_30d, label_map):
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
	def __init__(self, backend: BackendServices):
		self.backend = backend
		# resolved per use: the Redis connection can be hot-swapped
		self.reachability = ReachabilityChecker(
			lambda: backend.redis.client,
			ttl=lambda: backend.settings.get("reachability_cache_seconds"))

	##########################Audit############################################
	def audit(self, action, *, object_type=None, object_id=None,
	          object_label=None,
	          detail=None, success=True, username=None, actor_id=None):
		"""Write one append-only audit row.
		Opens its own DB session so the write
		commits independently of the calling route transaction."""
		if username is None:
			username = current_user.username if current_user.is_authenticated else "anonymous"
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
	def act_on_db_obj(self, model, obj_id, func, user_id=None, many=False,
	                  on_missing=None, can_access=None, **extra_filters):
		# can_access: optional predicate for access rules filter_by can't
		# express (e.g. owner OR admin-on-global). A denied object gets the
		# same response as a missing one, so its existence isn't leaked.
		with self.backend.postgres.get_session() as db_session:
			filters = {"id": obj_id} if obj_id is not None else {}
			if user_id is not None:
				filters["user_id"] = user_id
			filters.update(extra_filters)
			obj = db_session.query(model).filter_by(**filters)
			if many:
				return func(obj.all(), db_session)
			else:
				obj = obj.first()
				if not obj or (can_access and not can_access(obj)):
					return on_missing() if on_missing else err("Not found", 404)
				return func(obj, db_session)

	# CRUD factories
	@staticmethod
	def get_label(obj):
		return (getattr(obj, 'label', None) or
		        getattr(obj, 'name', None) or
		        getattr(obj, 'token', None) or
		        str(obj.id))

	def update_op(self, fields, audit_action, label_func=None, skip_none=False,
	              on_success=None):
		def func(obj, _):
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

	def delete_op(self, audit_action, data_filter=None, label_func=None,
	              on_success=None):
		def func(obj, db_session):
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
	def build_security_profile(self, label, username, password, enable_secret,
	                           user_id):
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
	def get_property_defs(self, user_id):
		with self.backend.postgres.get_session() as db_session:
			user_props = db_session.query(PropertyDefinition).filter_by(
				user_id=user_id).order_by(PropertyDefinition.name).all()
			user_defs = [{"name": p.name, "label": p.label, "icon": p.icon,
			              "is_list": p.is_list, "id": str(p.id)}
			             for p in user_props]
		return SYSTEM_PROPERTIES, user_defs
