"""The NetRollout Flask application's type: Flask plus the services the pages
use, attached once by `build.launch_app`.

Pages and helpers import `current_app` from here instead of from flask: the
same proxy, typed as `NetRolloutApp`, so `current_app.backend.postgres` and
the rest are checked like any other attribute.
"""
from typing import TYPE_CHECKING, Any, cast

from flask import Flask, current_app as flask_current_app
from flask_session.redis import RedisSessionInterface
if TYPE_CHECKING:   # annotations only: these modules import this one's users
	from src.access.service import Access
	from src.accounts.users import SessionStore
	from src.db.connections import BackendServices
	from src.jobs import RolloutOrchestrator
	from src.webapp.db_move import DatabaseMove
	from src.webapp.lifecycle import Maintenance, Shutdown
	from src.webapp.http import WebServices


class NetRolloutApp(Flask):
	"""Flask with NetRollout's services, one of each per process."""
	backend: "BackendServices"            # Postgres, Redis, the settings
	orchestrator: "RolloutOrchestrator"   # the rollouts running and queued
	web: "WebServices"                    # the pages' database helpers, the audit
	shutdown: "Shutdown"                  # stop / restart with a drain
	maintenance: "Maintenance"            # a database move's pause and lock
	db_move: "DatabaseMove"               # Server Management's database move
	access: "Access"                      # the hostname, the HTTPS port, the certificate
	session_interface: RedisSessionInterface   # sessions live in Redis
	sessions: "SessionStore"              # those sessions: who, signing out, the idle limit
	# this run's identity for the startup reverse-proxy check (startup.py)
	instance_token: str
	# the restart-only settings this process runs with (System Settings shows
	# "restart pending" while a saved value differs)
	settings_started_with: dict[str, Any]
	# the internal port Waitress serves on - set by the entry point
	# (src/webapp/__main__.py), shown in System Settings; None when not served
	app_port: int | None = None


# The request's app, typed (the same LocalProxy as flask.current_app)
current_app = cast(NetRolloutApp, flask_current_app)
