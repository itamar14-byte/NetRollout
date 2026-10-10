"""grafana-setup keeps the NetRollout database data source at the app's
connection (deploy/grafana/setup.py), so the dashboards follow a move."""
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def setup(monkeypatch, tmp_path):
	"""deploy/grafana/setup.py loaded fresh, its runtime.env in tmp_path,
	GRAFANA_DB_PASSWORD set."""
	spec = importlib.util.spec_from_file_location("grafana_setup", ROOT / "deploy/grafana/setup.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(module, "RUNTIME_ENV", tmp_path / "runtime.env")
	monkeypatch.setenv("GRAFANA_DB_PASSWORD", "gr-secret")
	return module


@pytest.mark.parametrize("env, expected", [
	("", ("postgres", "5432", "netrollout", "disable")),                     # bundled, never moved
	("PG_HOST=db.corp\nPG_PORT=6432\nPG_NAME=ops\nPG_SCHEMA=nr\n"
	 "NETROLLOUT_GRAFANA_SSLMODE=require\n", ("db.corp", "6432", "ops", "require")),
	("PG_HOST=\nDATABASE_URL=postgresql+psycopg2://netrollout:p%40w@postgres:5432/netrollout\n",
	 ("postgres", "5432", "netrollout", "disable")),                          # moved back (a URL)
	# development: the host app's 127.0.0.1 is the bundled database to a container
	("PG_HOST=127.0.0.1\nPG_PORT=5432\nPG_NAME=netrollout\n", ("postgres", "5432", "netrollout", "disable")),
	("# a comment\nPG_HOST='db2'\n", ("db2", "5432", "netrollout", "disable")),
	# as the app writes it (quoted, escaped): a password with a quote doesn't matter here
	("PG_HOST='db3'\nPG_PASSWORD='it\\'s'\nPG_NAME='o\\'ps'\n", ("db3", "5432", "o'ps", "disable")),
])
def test_where_the_data_is(setup, env, expected):
	"""runtime.env gives the data source's host, port, database and sslmode: bundled when
	empty, a moved-to server's PG_* and sslmode, a URL back to the bundled one, the dev
	host's 127.0.0.1 as `postgres`, comments skipped and quotes removed."""
	setup.RUNTIME_ENV.write_text(env, encoding="utf-8")
	place = setup.database(setup.read_env(setup.RUNTIME_ENV))
	assert (place["host"], place["port"], place["database"], place["sslmode"]) == expected


@pytest.mark.parametrize("env, url", [
	("PG_HOST=db.corp\nPG_PORT=6432\n", "db.corp:6432"),
	("PG_HOST=10.1.2.3\n", "10.1.2.3:5432"),
	("PG_HOST=fd00::5\nPG_PORT=6432\n", "[fd00::5]:6432"),
	("PG_HOST=[fd00::5]\n", "[fd00::5]:5432"),
	("DATABASE_URL=postgresql+psycopg2://nr:p@[fd00::7]:6543/ops\n", "[fd00::7]:6543"),
])
def test_the_data_source_url_brackets_an_ipv6_host(setup, env, url):
	"""The data source's url is host:port, an IPv6 host in brackets (Grafana's
	PostgreSQL data source splits it as Go's net.SplitHostPort does) - given
	bare, bracketed, or in a DATABASE_URL."""
	setup.RUNTIME_ENV.write_text(env, encoding="utf-8")
	body = setup.datasource_body(setup.database(setup.read_env(setup.RUNTIME_ENV)))
	assert body["url"] == url


def test_no_runtime_env_is_the_bundled_database(setup):
	"""Without a runtime.env the data source points at the bundled `postgres`."""
	assert setup.database(setup.read_env(setup.RUNTIME_ENV))["host"] == "postgres"


class FakeGrafana:
	"""Grafana's API stand-in: records each call; GET answers `existing` (404 when None)."""
	def __init__(self, existing=None):
		self.existing, self.calls = existing, []

	def __call__(self, method, path, body=None, ok=(200,)):
		self.calls.append((method, path, body))
		if method == "GET":
			return (404, None) if self.existing is None else (200, self.existing)
		return 200, {}


def test_created_when_missing_with_the_dashboards_uid(setup, monkeypatch):
	"""A missing data source is POSTed with the dashboards' uid, grafana_reader and its
	password, and runtime.env's database; the place is returned as host:port/database."""
	grafana = FakeGrafana()
	monkeypatch.setattr(setup, "call", grafana)
	setup.RUNTIME_ENV.write_text("PG_HOST=db.corp\nPG_NAME=ops\n", encoding="utf-8")
	assert setup.ensure_datasource() == "db.corp:5432/ops"
	method, path, body = grafana.calls[-1]
	assert (method, path) == ("POST", "/api/datasources")
	assert body["uid"] == "cfjxoedixn7r4d" and body["user"] == "grafana_reader"
	assert body["secureJsonData"] == {"password": "gr-secret"}
	assert body["jsonData"]["database"] == "ops"


def test_updated_in_place_after_a_move(setup, monkeypatch):
	"""An existing, editable data source is updated with a PUT on its uid."""
	grafana = FakeGrafana(existing={"uid": "cfjxoedixn7r4d", "readOnly": False})
	monkeypatch.setattr(setup, "call", grafana)
	setup.ensure_datasource()
	assert grafana.calls[-1][:2] == ("PUT", "/api/datasources/uid/cfjxoedixn7r4d")


def test_the_provisioned_one_of_an_earlier_version_is_reported_not_overwritten(setup, monkeypatch):
	"""A read-only (provisioned) data source raises "restart Grafana" after the GET alone."""
	grafana = FakeGrafana(existing={"uid": "cfjxoedixn7r4d", "readOnly": True})
	monkeypatch.setattr(setup, "call", grafana)
	with pytest.raises(RuntimeError, match="restart Grafana"):
		setup.ensure_datasource()
	assert [c[0] for c in grafana.calls] == ["GET"]


def test_healthy_means_the_last_run_succeeded(setup, monkeypatch, tmp_path):
	"""The health check is the done marker: a successful `--once` run writes it, and a
	failure after a success exits and removes it."""
	monkeypatch.setattr(setup, "DONE_FILE", tmp_path / "done")
	monkeypatch.setattr(setup.sys, "argv", ["setup.py", "--once"])
	monkeypatch.setattr(setup, "apply", lambda: None)
	setup.main()
	assert setup.DONE_FILE.exists()

	def broken():
		raise RuntimeError("Grafana answered 500")
	monkeypatch.setattr(setup, "apply", broken)
	with pytest.raises(SystemExit):
		setup.main()
	assert not setup.DONE_FILE.exists()


class FakeUsers:
	"""Grafana's user API as grafana-setup sees it: who may sign in with the
	administrator's password (`password_works_for`), the server's users and the
	organisation's members; every write recorded."""

	def __init__(self, password_works_for, users, members):
		self.password_works_for = password_works_for
		self.users, self.members = users, members
		self.writes = []

	def __call__(self, method, path, body=None, ok=(200,), login="netrollout-grafana-admin"):
		# login: as setup.call's default, the administrator's name
		if path == "/api/user":
			if login == self.password_works_for:
				return 200, {"id": 1, "login": login, "email": "admin@localhost", "name": "admin"}
			return 401, None
		if method == "GET" and path.startswith("/api/users"):
			return 200, self.users
		if method == "GET" and path == "/api/org/users":
			return 200, self.members
		self.writes.append((method, path, body, login))
		if method == "PUT" and path == "/api/users/1":
			self.password_works_for = body["login"]
		return 200, {}


def test_an_install_from_before_has_its_grafana_administrator_renamed(setup, monkeypatch):
	"""Grafana's administrator was named admin - like NetRollout's factory
	account, which Grafana then signed in as its administrator. When the new
	name is refused and admin is accepted, that account is renamed to
	netrollout-grafana-admin (email and name kept); then the new name works."""
	grafana = FakeUsers("admin", users=[], members=[])
	monkeypatch.setattr(setup, "call", grafana)
	setup.ensure_admin()
	assert grafana.writes == [("PUT", "/api/users/1", {"login": "netrollout-grafana-admin",
	                                                    "email": "admin@localhost", "name": "admin"},
	                           "admin")]
	assert grafana.password_works_for == "netrollout-grafana-admin"


def test_an_administrator_already_renamed_is_left_alone(setup, monkeypatch):
	"""A new install's administrator (or one renamed earlier) signs in under the
	new name: nothing is written."""
	grafana = FakeUsers("netrollout-grafana-admin", users=[], members=[])
	monkeypatch.setattr(setup, "call", grafana)
	setup.ensure_admin()
	assert grafana.writes == []


def test_neither_name_signing_in_is_an_error(setup, monkeypatch):
	"""The password works for neither name (a wrong GRAFANA_ADMIN_PASSWORD): a
	readable error, nothing written."""
	grafana = FakeUsers("someone-else", users=[], members=[])
	monkeypatch.setattr(setup, "call", grafana)
	with pytest.raises(RuntimeError, match="administrator"):
		setup.ensure_admin()
	assert grafana.writes == []


def test_every_user_signed_in_through_netrollout_is_an_editor_and_no_server_admin(setup, monkeypatch):
	"""Everyone but Grafana's own administrator: a Grafana server admin is
	demoted, and an organisation role other than Editor (Admin, Viewer) becomes
	Editor; users already right, and the administrator, are left alone."""
	grafana = FakeUsers(
		"netrollout-grafana-admin",
		users=[{"id": 1, "login": "netrollout-grafana-admin", "isAdmin": True},
		       {"id": 2, "login": "admin", "isAdmin": True},
		       {"id": 3, "login": "dana", "isAdmin": False}],
		members=[{"userId": 1, "login": "netrollout-grafana-admin", "role": "Admin"},
		         {"userId": 2, "login": "admin", "role": "Admin"},
		         {"userId": 3, "login": "dana", "role": "Editor"},
		         {"userId": 4, "login": "eve", "role": "Viewer"}])
	monkeypatch.setattr(setup, "call", grafana)
	setup.enforce_roles()
	writes = [(m, p, b) for m, p, b, _ in grafana.writes]
	assert writes == [("PUT", "/api/admin/users/2/permissions", {"isGrafanaAdmin": False}),
	                  ("PATCH", "/api/org/users/2", {"role": "Editor"}),
	                  ("PATCH", "/api/org/users/4", {"role": "Editor"})]
