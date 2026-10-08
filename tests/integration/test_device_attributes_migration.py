"""The device_attributes migration on a scratch database seeded in the old
shape (every value in var_maps): custom keys become rows for the users who
define them and see the device (else the owner's), var_maps keeps the system
keys; the downgrade folds the rows back (the owner's value winning a clash,
else the lowest user id's). Its own scratch database; never the app's."""
import json
import uuid

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, text

from src.backup import archive
from tests.integration.conftest import PG_ADMIN_URL

pytestmark = [pytest.mark.postgres]

SCRATCH_DB = "rollout_attr_migration"
BEFORE = "device_results_indexes"

# users: A owns the devices; B and C define "rack" (B's id is the lower)
A, B, C = (uuid.UUID(f"00000000-0000-0000-0000-00000000000{n}") for n in (9, 1, 2))
# devices
G = uuid.uuid4()     # A's global device
G2 = uuid.uuid4()    # A's second global device
P = uuid.uuid4()     # B's private device
Q = uuid.uuid4()     # A's private device
N = uuid.uuid4()     # A's device without values (a JSON null)


@pytest.fixture
def engine():
	"""An engine on a fresh scratch database (dropped after)."""
	admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")

	def drop():
		with admin.connect() as conn:
			conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)'))

	drop()
	with admin.connect() as conn:
		conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
	eng = create_engine(PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + SCRATCH_DB)
	yield eng
	eng.dispose()
	drop()
	admin.dispose()


def migrate(engine, command, revision):
	with engine.begin() as conn:
		getattr(alembic_command, command)(archive._alembic_config(conn), revision)


def seed_old_shape(engine):
	"""Users A, B, C; "rack" defined by B and C, "uplinks" by B only; devices:
	G global with a shared key, an orphan key and a system key; G2 global; P
	B's private one with "rack" (C defines it too but can't see P); Q A's
	private one with "rack" (A doesn't define it); N without values."""
	with engine.begin() as conn:
		for uid, name in ((A, "a"), (B, "b"), (C, "c")):
			conn.execute(text(
				"INSERT INTO users (id, username, role, is_active, is_approved, "
				"created_at, auth_type) VALUES (:id, :n, 'operator', true, true, "
				"now(), 'local')"), {"id": uid, "n": name})
		for uid, name in ((B, "rack"), (C, "rack"), (B, "uplinks")):
			conn.execute(text(
				"INSERT INTO property_definition (id, name, label, icon, is_list, "
				"user_id) VALUES (:id, :n, :n, 'bi-tag', false, :u)"),
				{"id": uuid.uuid4(), "n": name, "u": uid})
		devices = [
			(G, A, True, {"hostname": "g", "rack": "R1", "orphan": "O",
			              "uplinks": ["Gi0/1", "Gi0/2"]}),
			(G2, A, True, {"rack": "R2"}),
			(P, B, False, {"rack": "RP", "vrfs": ["red"]}),
			(Q, A, False, {"rack": "RQ"}),
			(N, A, False, None),
		]
		for did, owner, is_global, values in devices:
			conn.execute(text(
				"INSERT INTO inventory (id, ip, device_type, port, label, var_maps, "
				"user_id, is_global) VALUES (:id, '10.0.0.1', 'cisco_ios', 22, :l, "
				"CAST(:v AS json), :u, :g)"),
				{"id": did, "l": str(did)[:8], "v": json.dumps(values), "u": owner,
				 "g": is_global})


def var_maps(engine):
	with engine.connect() as conn:
		return {r.id: r.var_maps for r in conn.execute(
			text("SELECT id, var_maps FROM inventory"))}


def rows(engine):
	with engine.connect() as conn:
		return {(r.device_id, r.user_id, r.name): r.value for r in conn.execute(
			text("SELECT device_id, user_id, name, value FROM device_attributes"))}


def test_upgrade_moves_custom_values_to_their_users_and_downgrade_folds_them_back(
		engine):
	"""Upgrade: a shared key on a global device becomes a row for each user who
	defines it, an orphan key the owner's row, a private device's key only its
	owner's (defined or not); var_maps keep the system keys (NULL when none).
	Downgrade: the rows fold back into var_maps - the owner's value winning a
	clash, else the lowest user id's - and the table is gone."""
	migrate(engine, "upgrade", BEFORE)
	seed_old_shape(engine)
	migrate(engine, "upgrade", "device_attributes")

	assert rows(engine) == {
		(G, B, "rack"): "R1", (G, C, "rack"): "R1",
		(G, A, "orphan"): "O",
		(G, B, "uplinks"): ["Gi0/1", "Gi0/2"],
		(G2, B, "rack"): "R2", (G2, C, "rack"): "R2",
		(P, B, "rack"): "RP",
		(Q, A, "rack"): "RQ",
	}
	assert var_maps(engine) == {G: {"hostname": "g"}, G2: None,
	                            P: {"vrfs": ["red"]}, Q: None, N: None}

	# clashes for the downgrade: on G the owner A has a value too; on G2, B and
	# C differ (B's id is the lower)
	with engine.begin() as conn:
		conn.execute(text("UPDATE device_attributes SET value = '\"C-OTHER\"' "
		                  "WHERE user_id = :c"), {"c": C})
		conn.execute(text(
			"INSERT INTO device_attributes (id, device_id, user_id, name, value) "
			"VALUES (:id, :g, :a, 'rack', '\"A-OWN\"')"),
			{"id": uuid.uuid4(), "g": G, "a": A})
	migrate(engine, "downgrade", BEFORE)

	assert var_maps(engine) == {
		G: {"hostname": "g", "rack": "A-OWN", "orphan": "O",
		    "uplinks": ["Gi0/1", "Gi0/2"]},
		G2: {"rack": "R2"},
		P: {"vrfs": ["red"], "rack": "RP"},
		Q: {"rack": "RQ"},
		N: None,
	}
	with engine.connect() as conn:
		assert conn.execute(text(
			"SELECT to_regclass('device_attributes')")).scalar() is None
