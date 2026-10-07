"""Backups through the app's own connection: a round trip into another
database — through a role that isn't a superuser, in its own schema, next to
another application's table (an organisation's database) — an older backup
migrating forward, a key that doesn't decrypt changing nothing, Grafana's
database. Two scratch databases on the test Postgres; never the app's."""
import os
import sqlite3

import pytest
from alembic import command as alembic_command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src import backup
from src.db.connections import PostgresConfig, PostgresConnection
from src.db.tables import AuditLog, SecurityProfile, SystemSetting, User
from tests.integration.conftest import PG_ADMIN_URL

pytestmark = [pytest.mark.postgres]

SOURCE_DB, TARGET_DB = "rollout_backup_src", "rollout_backup_dst"
ROLE, ROLE_PASSWORD, SCHEMA = "nr_backup_byo", "Byo-pass-1", "nrapp"
OLDER = "device_results_action_needed"    # before role_user_to_operator


def _url(db, user="postgres", password=None):
	"""The test Postgres URL for database `db`, as the superuser or the given login."""
	base, _ = PG_ADMIN_URL.rsplit("/", 1)
	if user != "postgres":
		base = base.split("://")[0] + f"://{user}:{password}@" + base.split("@")[1]
	return f"{base}/{db}"


@pytest.fixture
def databases():
	"""(source, target) engines on two fresh scratch databases: the source as the
	superuser, the target through a non-superuser login in its own schema that
	already holds another application's table; both and the login dropped after."""
	admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")

	def drop():
		with admin.connect() as conn:
			for db in (SOURCE_DB, TARGET_DB):
				conn.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
			conn.execute(text(f'DROP ROLE IF EXISTS "{ROLE}"'))

	drop()
	with admin.connect() as conn:
		conn.execute(text(f'CREATE DATABASE "{SOURCE_DB}"'))
		conn.execute(text(f'CREATE DATABASE "{TARGET_DB}"'))
		conn.execute(text(f"CREATE ROLE \"{ROLE}\" LOGIN PASSWORD '{ROLE_PASSWORD}'"))
	target_admin = create_engine(_url(TARGET_DB), isolation_level="AUTOCOMMIT")
	with target_admin.connect() as conn:
		conn.execute(text(f'CREATE SCHEMA "{SCHEMA}" AUTHORIZATION "{ROLE}"'))
	target_admin.dispose()
	source = PostgresConnection._build_engine(PostgresConfig(url=_url(SOURCE_DB)))
	target = PostgresConnection._build_engine(PostgresConfig(
		url=_url(TARGET_DB, ROLE, ROLE_PASSWORD), schema=SCHEMA))
	with target.begin() as conn:
		# another application's table in the same schema: never touched
		conn.execute(text("CREATE TABLE other_app (note text)"))
		conn.execute(text("INSERT INTO other_app VALUES ('theirs')"))
	yield source, target
	source.dispose()
	target.dispose()
	drop()
	admin.dispose()


@pytest.fixture
def places(tmp_path):
	"""Backup folders under tmp_path (backups, certs, logs, grafana); certs, logs made."""
	p = backup.Places(tmp_path / "backups", tmp_path / "certs", tmp_path / "logs",
	                  tmp_path / "grafana")
	for folder in (p.certs, p.logs):
		folder.mkdir()
	return p


def migrate(engine, revision="head"):
	with engine.begin() as conn:
		alembic_command.upgrade(backup._alembic_config(conn), revision)


def revision_of(engine):
	with engine.connect() as conn:
		return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def populate(engine, key, role="admin"):
	"""Add a user, a security profile encrypted with `key`, the https_port setting
	and an audit row."""
	cipher = Fernet(key)
	with Session(engine) as s, s.begin():
		user = User(username="alice", password_hash="x", email="a@example.com",
		            full_name="Alice", role=role, is_active=True, is_approved=True)
		s.add(user)
		s.flush()
		s.add(SecurityProfile(label="lab", username="netops", user_id=user.id,
		                      password_secret=cipher.encrypt(b"s3cret").decode()))
		s.add(SystemSetting(key="https_port", value=443))
		s.add(AuditLog(actor_username="alice", action="auth.login"))


def test_a_backup_restores_into_another_database_through_a_non_superuser(
		databases, places):
	"""A backup restored through a non-superuser into its own schema brings back
	the rows, the revision and the key (the credential decrypts), sets this
	install's HTTPS port, audits backup.restored, leaves the other app's table
	alone, and restores the certificate with its marker and only rollout logs."""
	source, target = databases
	key = Fernet.generate_key()
	migrate(source)
	populate(source, key)
	(places.certs / "fullchain.pem").write_text("CERT")
	(places.certs / "privkey.pem").write_text("KEY")
	(places.certs / ".selfsigned").write_text("")
	(places.logs / "rollout_20261005_job1.log").write_text("pushed")
	(places.logs / "install.log").write_text("not a rollout log")

	path = backup.create(source, "manual", places, key=key)
	manifest = backup.check(path)
	assert manifest.revision == revision_of(source)
	assert manifest.tables["users"] == 1 and manifest.tables["security_profiles"] == 1

	# restoring into an install with its own (organisation's) certificate
	restored_places = backup.Places(places.backups, places.certs.parent / "c2",
	                                places.logs.parent / "l2")
	restored_places.certs.mkdir()
	(restored_places.certs / "fullchain.pem").write_text("OTHER")
	done = backup.restore(path, target, restored_places, https_port=8443)

	assert done.key == key
	assert revision_of(target) == manifest.revision
	with Session(target) as s:
		profile = s.query(SecurityProfile).one()
		assert Fernet(key).decrypt(profile.password_secret.encode()) == b"s3cret"
		assert s.query(User).one().username == "alice"
		assert s.get(SystemSetting, "https_port").value == 8443
		actions = {a.action for a in s.query(AuditLog)}
		assert actions == {"auth.login", "backup.restored"}
		assert s.execute(text("SELECT note FROM other_app")).scalar() == "theirs"
	assert (restored_places.certs / "fullchain.pem").read_text() == "CERT"
	assert (restored_places.certs / ".selfsigned").exists()
	assert (restored_places.logs / "rollout_20261005_job1.log").read_text() == "pushed"
	assert not (restored_places.logs / "install.log").exists()


def test_an_older_backup_restores_and_the_app_migrates_it_forward(databases, places):
	"""A backup from an older migration level restores at that level into a
	database already at head; migrating forward then turns role 'user' into
	'operator'."""
	source, target = databases
	key = Fernet.generate_key()
	migrate(source, OLDER)
	populate(source, key, role="user")        # 'user' became 'operator' later
	path = backup.create(source, "manual", places, key=key)
	migrate(target)                           # the target is already at head

	backup.restore(path, target, places)
	assert revision_of(target) == OLDER
	migrate(target)                           # what the app does at its start
	with Session(target) as s:
		assert s.query(User).one().role == "operator"


def test_a_key_that_does_not_decrypt_the_credentials_changes_nothing(
		databases, places):
	"""A restore whose key doesn't decrypt the stored credential is refused and the
	target keeps exactly its own rows."""
	source, target = databases
	migrate(source)
	populate(source, Fernet.generate_key())
	path = backup.create(source, "manual", places, key=Fernet.generate_key())
	migrate(target)
	with Session(target) as s, s.begin():
		s.add(User(username="bob", password_hash="x", email="b@example.com",
		           full_name="Bob", role="admin", is_active=True, is_approved=True))

	with pytest.raises(backup.BackupError, match="doesn't decrypt"):
		backup.restore(path, target, places)
	with Session(target) as s:
		assert [u.username for u in s.query(User)] == ["bob"]


def test_grafanas_database_is_copied_and_restored(databases, places, tmp_path):
	"""Grafana's SQLite database is in the backup (the manifest says so) and is
	restored with its dashboard row; on POSIX with mode 640."""
	source, target = databases
	key = Fernet.generate_key()
	migrate(source)
	places.grafana.mkdir()
	db = sqlite3.connect(places.grafana / "grafana.db")
	with db:
		db.execute("CREATE TABLE dashboard (title text)")
		db.execute("INSERT INTO dashboard VALUES ('Custom one')")
	db.close()

	path = backup.create(source, "scheduled", places, key=key)
	assert backup.read_manifest(path).grafana
	grafana_target = tmp_path / "grafana-volume"
	backup.restore(path, target, places, grafana_target=grafana_target)
	db = sqlite3.connect(grafana_target / "grafana.db")
	assert db.execute("SELECT title FROM dashboard").fetchall() == [("Custom one",)]
	db.close()
	if os.name == "posix":
		# as Grafana keeps it: the app reads it through group 0 for the next backup
		assert (grafana_target / "grafana.db").stat().st_mode & 0o777 == 0o640


def test_a_database_without_netrollout_tables_is_not_backed_up(databases, places):
	"""A database with only an alembic_version table is refused ("no NetRollout
	tables") and no file is left in the backups folder."""
	source, _ = databases
	with source.begin() as conn:
		conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32))"))
	with pytest.raises(backup.BackupError, match="no NetRollout tables"):
		backup.create(source, "manual", places, key=Fernet.generate_key())
	assert not any(places.backups.iterdir())
