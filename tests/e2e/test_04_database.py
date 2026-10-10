"""Check 4: the database logins - the app's isn't a superuser; Grafana's
reads exactly the three tables the dashboards use."""
# The dashboards' tables (the brief's list, not the code's: a change there
# must show up here)
GRAFANA_READS = {"device_results", "job_metadata", "audit_log"}


# NetRollout's tables (schema public) for which a privilege check holds;
# by oid: a name built from pg_tables could be checked before the schema is
PUBLIC_TABLES = ("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = "
                 "c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')")


def tables(install, where: str = "") -> set[str]:
	out = install.sql(PUBLIC_TABLES + (f" AND {where}" if where else ""))
	return set(out.stdout.decode().split())


def test_the_apps_login_is_not_a_superuser(install):
	out = install.sql("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
	                  "WHERE rolname = 'netrollout'")
	assert out.stdout.decode().strip() == "f|f|f"
	owner = install.sql("SELECT pg_get_userbyid(datdba) FROM pg_database "
	                    "WHERE datname = 'netrollout'")
	assert owner.stdout.decode().strip() == "netrollout"


def test_grafanas_login_may_select_exactly_the_dashboards_tables(install):
	every = tables(install)
	# the refusals below mean something: these tables exist
	assert {"users", "security_profiles", "inventory", "system_settings"} <= every
	assert GRAFANA_READS <= every
	readable = tables(install, "has_table_privilege('grafana_reader', c.oid, "
	                           "'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')")
	assert readable == GRAFANA_READS
	assert tables(install, "has_table_privilege('grafana_reader', c.oid, "
	                       "'INSERT,UPDATE,DELETE,TRUNCATE')") == set()
	login = install.sql("SELECT rolsuper FROM pg_roles WHERE rolname = 'grafana_reader'")
	assert login.stdout.decode().strip() == "f"


def test_grafanas_login_reads_its_tables_and_is_refused_the_others(install):
	"""As grafana_reader itself, with its password: real SELECTs."""
	password = install.env()["GRAFANA_DB_PASSWORD"]
	for table in sorted(GRAFANA_READS):
		done = install.sql(f"SELECT count(*) FROM {table}", user="grafana_reader",
		                   password=password)
		assert done.stdout.decode().strip().isdigit()
	for table in ("users", "security_profiles", "inventory", "system_settings"):
		done = install.sql(f"SELECT count(*) FROM {table}", user="grafana_reader",
		                   password=password, check=False)
		assert done.returncode != 0
		assert b"permission denied" in done.stderr, (table, done.stderr)
