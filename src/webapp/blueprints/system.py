from flask import Blueprint, current_app, jsonify

from src.webapp.startup import INSTANCE_PATH

bp = Blueprint("system", __name__)


@bp.route(INSTANCE_PATH)
def instance():
	"""This process's random per-run token — lets the startup check prove the
	reverse proxy forwards to *this* instance. No login, no session write, no
	DB; the token means nothing outside this process."""
	return jsonify(instance=current_app.config["INSTANCE_TOKEN"])
