import os
from waitress import serve

from src.webapp.setup import launch_app
from src.webapp.blueprints.auth import bp as auth_bp


def create_app():
	app = launch_app()
	app.register_blueprint(auth_bp)
	# app.register_blueprint(rollout_bp)
	# app.register_blueprint(inventory_bp)
	# app.register_blueprint(security_bp)
	# app.register_blueprint(mappings_bp)
	# app.register_blueprint(properties_bp)
	# app.register_blueprint(analytics_bp)
	# app.register_blueprint(admin_bp)
	return app


if __name__ == "__main__":
	app = create_app()
	serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
