# python utilities
import functools
from collections import defaultdict
from datetime import datetime

# services
#flask
from flask import jsonify, request, redirect, url_for, flash, session
from flask_login import current_user, login_user
#sqlalchemy
from sqlalchemy import and_, or_

# local modules
from src.db.backend import BackendServices
from src.db.tables import (DeviceResult, AuditLog, SecurityProfile,
                           PropertyDefinition)
from src.encryption import encrypt
from src.validation import Validator

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

QUERY_DEVICE_RESULT_FIELDS = {
	"started_at": (
		DeviceResult.started_at,
		{"equal", "less_or_equal", "greater_or_equal"}),
	"device_type": (
		DeviceResult.device_type, {"equal", "not_equal"}),
	"status": (
		DeviceResult.status, {"equal", "not_equal"}),
	"commands_sent": (
		DeviceResult.commands_sent,
		{"equal", "not_equal", "greater_or_equal",
		 "less_or_equal"}),
	"device_ip": (
		DeviceResult.device_ip, {"equal", "contains", "begins_with"}),
}
DEVICE_RESULT_COLUMNS = ["job_id", "device_ip", "device_type",
                         "status",
                         "commands_sent", "commands_verified",
                         "started_at", "completed_at"]

QUERY_AUDIT_LOG_FIELDS = {
	"timestamp": (
		AuditLog.timestamp, {"equal", "less_or_equal", "greater_or_equal"}),
	"actor_username": (
		AuditLog.actor_username,
		{"equal", "not_equal", "contains", "begins_with"}),
	"action": (
		AuditLog.action, {"equal", "not_equal", "contains", "begins_with"}),
	"object_type": (
		AuditLog.object_type, {"equal", "not_equal"}),
	"success": (
		AuditLog.success, {"equal"}),
	"ip_address": (
		AuditLog.ip_address, {"equal", "contains", "begins_with"}),
}

AUDIT_LOG_COLUMNS = ["timestamp", "actor_username", "action",
                     "object_type",
                     "object_label", "success", "ip_address"]

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
			return redirect(request.referrer or url_for("rollout.dashboard"))
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


def validate_mapping_fields(index, property_name, inner_token):
	status, msg = Validator.validate_var_map_inner_token(inner_token)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings.mappings"))

	status, msg = Validator.validate_var_map_property_name(property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings.mappings"))

	status, msg = Validator.validate_var_index(index, property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings.mappings"))


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


def job_status(rows: list[DeviceResult]) -> str:
	statuses = {r.status for r in rows}
	if "cancelled" in statuses:
		return "cancelled"
	if all(r.status == "failed" for r in rows):
		return "failed"
	if any(r.status in ("failed", "partial") for r in rows):
		return "partial"
	return "success"

##################Backend facing helpers#######################################
class WebServices:
	def __init__(self, backend: BackendServices):
		self.backend = backend

	##########################Audit############################################
	def audit(self, action, *, object_type=None, object_id=None,
	          object_label=None,
	          detail=None, success=True, username=None, actor_id=None):
		"""Write one append-only audit row.
		Opens its own redis_session so the write
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
	                  on_missing=None, **extra_filters):
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
				if not obj:
					return on_missing() if on_missing else err("Not found", 404)
				return func(obj, db_session)

	# CRUD factories
	@staticmethod
	def get_label(obj):
		return (getattr(obj, 'label', None) or
		        getattr(obj, 'name', None) or
		        getattr(obj, 'token', None) or
		        str(obj.id))

	def create_op(self, model_class, fields, audit_action, db_session,
	              label_func=None):
		obj = model_class(**fields)
		db_session.add(obj)
		db_session.flush()

		label = label_func(obj) if label_func else self.get_label(obj)
		self.audit(audit_action, object_type=type(obj).__name__,
		           object_id=obj.id,
		           object_label=label)
		return ok(id=str(obj.id))

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

	def user_owns_job(self, job_id, user_id):
		with self.backend.postgres.get_session() as db_session:
			return bool(db_session.query(DeviceResult).filter_by(
				job_id=job_id, user_id=user_id).first())

	#######################Auth helpers###############################


	def get_property_defs(self, user_id):
		with self.backend.postgres.get_session() as db_session:
			user_props = db_session.query(PropertyDefinition).filter_by(
				user_id=user_id).order_by(PropertyDefinition.name).all()
			user_defs = [{"name": p.name, "label": p.label, "icon": p.icon,
			              "is_list": p.is_list, "id": str(p.id)}
			             for p in user_props]
		return SYSTEM_PROPERTIES, user_defs
