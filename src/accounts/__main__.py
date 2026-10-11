"""python -m src.accounts reset-admin [--new-password] - the scripts'
`netrollout reset-admin` (packaging/windows/manage.ps1,
packaging/linux/netrollout.sh), run in the app's container: the factory admin
made usable again when nobody can sign in as an administrator - enabled, its
2FA off, its wrong passwords and lock cleared, signed out everywhere, and
(--new-password) a temporary password shown once."""
import argparse
import sys
from enum import IntEnum

import msgspec
from sqlalchemy.exc import SQLAlchemyError

from src import runtime
from src.accounts.users import FACTORY_USERNAME, AccountError, Accounts, SessionStore
from src.audit import Actor, AuditAction, AuditTrail
from src.db.connections import (REDIS_UNAVAILABLE, PostgresConnection, RedisConnection,
                                load_config)
from src.db.tables import AuditLog


# what the audit log names as the actor: no user is signed in, it ran on the
# server itself (as netrollout port's)
BY_HAND_ACTOR = "netrollout reset-admin (on the server)"


class AccountsExit(IntEnum):
	"""`python -m src.accounts`'s exit codes - the scripts branch on them."""
	OK = 0
	FAILED = 1      # nothing changed (why, on stderr)


def end_sessions(redis_conn: RedisConnection, user_id: str) -> int | None:
	"""Sign the account out everywhere: its sessions in the app's Redis
	(flask-session stores them as msgpack).

	:returns: how many were ended; None: Redis didn't answer"""
	try:
		return SessionStore(lambda: redis_conn.client, msgspec.msgpack.Decoder(),
		                    lambda: 0).end_for(user_id)
	except REDIS_UNAVAILABLE:
		return None


def reset_admin(postgres: PostgresConnection, redis_conn: RedisConnection,
                new_password: bool) -> AccountsExit:
	"""The factory admin reset (Accounts.reset_factory_admin), signed out
	everywhere, audited user.reset_admin in the same transaction; what was
	done (and a temporary password) printed.

	:param new_password: also a temporary password, shown once
	:returns: the exit code"""
	try:
		with postgres.get_session() as session:
			user, temporary = Accounts(session).reset_factory_admin(new_password)
			user_id, username = user.id, user.username
			ended = end_sessions(redis_conn, str(user_id))
			session.add(AuditLog(**AuditTrail.row(
				Actor.system(BY_HAND_ACTOR), AuditAction.USER_RESET_ADMIN,
				object_type="User", object_id=user_id, object_label=username,
				detail={"new_password": new_password, "sessions_ended": ended})))
	except AccountError as e:
		print(str(e), file=sys.stderr)
		return AccountsExit.FAILED
	except SQLAlchemyError as e:
		print(f"NetRollout's database didn't answer: {str(e).splitlines()[0]}. "
		      f"Nothing was changed.", file=sys.stderr)
		return AccountsExit.FAILED
	print(f"The factory admin ({username}) is enabled, its two-factor sign-in is off "
	      f"and its failed sign-ins are cleared.")
	if ended is None:
		print("Its sessions couldn't be ended (Redis didn't answer) - they end at "
		      "NetRollout's next start.")
	elif ended:
		print(f"It was signed out everywhere ({ended} session{'s' if ended != 1 else ''}).")
	if temporary:
		print(f"Temporary password (shown once): {temporary}")
		print(f"Sign in as {username} with it - NetRollout asks for a new password at once.")
	else:
		print(f"Sign in as {username} with its password.")
	return AccountsExit.OK


def main(argv: list[str] | None = None) -> AccountsExit:
	"""`reset-admin` (the scripts call it), on the app's database and Redis -
	config/runtime.env over the environment, as the app resolves them.

	:param argv: the arguments; sys.argv's when None
	:returns: the exit code"""
	parser = argparse.ArgumentParser(prog="python -m src.accounts")
	sub = parser.add_subparsers(dest="command", required=True)
	reset = sub.add_parser("reset-admin", help=f"make the factory admin ({FACTORY_USERNAME}) "
	                                           f"usable again")
	reset.add_argument("--new-password", action="store_true",
	                   help="also a temporary password, shown once (it must be replaced "
	                        "at the next sign-in)")
	args = parser.parse_args(argv)
	load_config(runtime.runtime_env())
	postgres, redis_conn = PostgresConnection(), RedisConnection()
	try:
		return reset_admin(postgres, redis_conn, args.new_password)
	finally:
		postgres.engine.dispose()
		redis_conn.disconnect()


if __name__ == "__main__":
	sys.exit(main())
