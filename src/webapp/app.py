"""The NetRollout Flask application's type: Flask plus the services the pages
use, attached once by `setup.launch_app` (and `maintenance.register_maintenance`).

Pages and helpers import `current_app` from here instead of from flask: the
same proxy, typed as `NetRolloutApp`, so `current_app.backend.postgres` and
the rest are checked like any other attribute.
"""
from typing import TYPE_CHECKING, cast

from flask import Flask, current_app as flask_current_app
from flask_session.redis import RedisSessionInterface
if TYPE_CHECKING:   # annotations only: these modules import this one's users
	from src.accounts.users import SessionStore
	from src.db.connections import BackendServices
	from src.jobs import RolloutOrchestrator
	from src.webapp.db_move import DatabaseMove
	from src.webapp.lifecycle import Shutdown
	from src.webapp.db_move import Maintenance
	from src.webapp.http import WebServices


class NetRolloutApp(Flask):
	"""Flask with NetRollout's services, one of each per process."""
	backend: "BackendServices"            # Postgres, Redis, the settings
	orchestrator: "RolloutOrchestrator"   # the rollouts running and queued
	web: "WebServices"                    # the pages' database helpers, the audit
	shutdown: "Shutdown"                  # stop / restart with a drain
	maintenance: "Maintenance"            # a database move's pause and lock
	db_move: "DatabaseMove"               # Server Management's database move
	session_interface: RedisSessionInterface   # sessions live in Redis
	sessions: "SessionStore"              # those sessions: who, signing out, the idle limit


# The request's app, typed (the same LocalProxy as flask.current_app)
current_app = cast(NetRolloutApp, flask_current_app)
