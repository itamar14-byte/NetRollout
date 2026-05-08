import os
from waitress import serve

from src.webapp.setup import launch_app
from src.webapp.blueprints.auth import bp as auth_bp
from src.webapp.blueprints.rollout import bp as rollout_bp
from src.webapp.blueprints.inventory import bp as inventory_bp
from src.webapp.blueprints.security import bp as security_bp
from src.webapp.blueprints.mappings import bp as mappings_bp
from src.webapp.blueprints.properties import bp as properties_bp
from src.webapp.blueprints.analytics import bp as analytics_bp
from src.webapp.blueprints.admin_users import bp as admin_users_bp
from src.webapp.blueprints.admin_servers import bp as admin_servers_bp
from src.webapp.blueprints.admin_observability import bp as admin_observability_bp
from src.webapp.blueprints.jobs import bp as jobs_bp


def create_app():
	net_rollout = launch_app()
	net_rollout.register_blueprint(auth_bp)
	net_rollout.register_blueprint(rollout_bp)
	net_rollout.register_blueprint(inventory_bp)
	net_rollout.register_blueprint(security_bp)
	net_rollout.register_blueprint(mappings_bp)
	net_rollout.register_blueprint(properties_bp)
	net_rollout.register_blueprint(analytics_bp)
	net_rollout.register_blueprint(admin_users_bp)
	net_rollout.register_blueprint(admin_servers_bp)
	net_rollout.register_blueprint(admin_observability_bp)

	net_rollout.register_blueprint(jobs_bp)
	return net_rollout


if __name__ == "__main__":
	app = create_app()
	serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
