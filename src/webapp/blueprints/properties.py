# python utilities
import uuid

# services
# flask
from flask import Blueprint, render_template, request, current_app
from flask_login import current_user, login_required

# local modules
from src.db.tables import PropertyDefinition
from src.webapp.utils import ok, err, SYSTEM_PROPERTIES

bp = Blueprint('properties', __name__, url_prefix='/properties')


##############################Routes#######################################
@bp.route("")
@login_required
def properties():
	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	return render_template("properties.html", sys_props=sys_props,
	                       user_props=user_props, active_section="properties")


@bp.route("/create", methods=["POST"])
@bp.route("/quick_create", methods=["POST"])
@login_required
def properties_create():
	data = request.get_json(silent=True) or {}
	name = data.get("name", "").strip().lower().replace(" ", "_")
	label = data.get("label", "").strip()
	icon = data.get("icon", "bi-tag").strip() or "bi-tag"
	is_list = bool(data.get("is_list", False))
	if not name or not label:
		return err("Name and label are required.")
	with current_app.backend.postgres.get_session() as db_session:
		existing = db_session.query(PropertyDefinition).filter_by(
			name=name, user_id=current_user.id).first()
		if existing:
			return err("Property name already exists.")
		# Also block shadowing system property names
		sys_names = {p["name"] for p in SYSTEM_PROPERTIES}
		if name in sys_names:
			return err("Cannot shadow a system property.")
		prop = PropertyDefinition(name=name, label=label, icon=icon,
		                          is_list=is_list, user_id=current_user.id)
		db_session.add(prop)
		db_session.flush()
		prop_id = str(prop.id)
	current_app.web.audit("property.create", object_type="PropertyDefinition",
	                      object_id=uuid.UUID(prop_id), object_label=name)
	return ok(id=prop_id, name=name, label=label, icon=icon, is_list=is_list)


@bp.route("/<uuid:prop_id>/edit", methods=["POST"])
@login_required
def properties_edit(prop_id):
	data = request.get_json(silent=True) or {}
	label = data.get("label", "").strip()
	icon = data.get("icon", "bi-tag").strip() or "bi-tag"
	is_list = bool(data.get("is_list", False))
	if not label:
		return err("Label is required.")
	return current_app.web.act_on_db_obj(
		PropertyDefinition, prop_id,
		current_app.web.update_op({"label": label, "icon": icon, "is_list":
			is_list},
		                          "property.edit", label_func=lambda p: p.name),
		user_id=current_user.id
	)


@bp.route("/<uuid:prop_id>/delete", methods=["POST"])
@login_required
def properties_delete(prop_id):
	return current_app.web.act_on_db_obj(
		PropertyDefinition, prop_id,
		current_app.web.delete_op("property.delete", label_func=lambda p:
		p.name),
		user_id=current_user.id
	)