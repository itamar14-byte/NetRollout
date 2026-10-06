"""grafana-setup keeps the NetRollout database data source at the app's
connection (deploy/grafana/setup.py), so the dashboards follow a move."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def setup(monkeypatch, tmp_path):
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
])
def test_where_the_data_is(setup, env, expected):
	setup.RUNTIME_ENV.write_text(env, encoding="utf-8")
	place = setup.database(setup.read_env(setup.RUNTIME_ENV))
	assert (place["host"], place["port"], place["database"], place["sslmode"]) == expected


def test_no_runtime_env_is_the_bundled_database(setup):
	assert setup.database(setup.read_env(setup.RUNTIME_ENV))["host"] == "postgres"


class FakeGrafana:
	def __init__(self, existing=None):
		self.existing, self.calls = existing, []

	def __call__(self, method, path, body=None, ok=(200,)):
		self.calls.append((method, path, body))
		if method == "GET":
			return (404, None) if self.existing is None else (200, self.existing)
		return 200, {}


def test_created_when_missing_with_the_dashboards_uid(setup, monkeypatch):
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
	grafana = FakeGrafana(existing={"uid": "cfjxoedixn7r4d", "readOnly": False})
	monkeypatch.setattr(setup, "call", grafana)
	setup.ensure_datasource()
	assert grafana.calls[-1][:2] == ("PUT", "/api/datasources/uid/cfjxoedixn7r4d")


def test_the_provisioned_one_of_an_earlier_version_is_reported_not_overwritten(setup, monkeypatch):
	grafana = FakeGrafana(existing={"uid": "cfjxoedixn7r4d", "readOnly": True})
	monkeypatch.setattr(setup, "call", grafana)
	with pytest.raises(RuntimeError, match="restart Grafana"):
		setup.ensure_datasource()
	assert [c[0] for c in grafana.calls] == ["GET"]


def test_healthy_means_the_last_run_succeeded(setup, monkeypatch, tmp_path):
	# the health check is the marker: a failure after a success removes it
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
