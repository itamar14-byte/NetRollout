"""The database move's pure parts: the SQL a DBA runs, places compared."""
from src.db.connections import PostgresConfig
from src.db.move import Plan, preparation_sql
from src.webapp.db_move import same_database


def test_the_sql_in_public_owns_the_schema_and_lets_grafana_in():
	"""With the defaults (schema public), the DBA's SQL creates both logins and the
	database, makes the app's login own public and lets grafana_reader use it."""
	sql = preparation_sql(Plan(password="app-pw", grafana_password="gr-pw"))
	assert sql.splitlines()[1:] == [
		"""CREATE ROLE "netrollout" LOGIN PASSWORD 'app-pw';""",
		"CREATE ROLE grafana_reader LOGIN PASSWORD 'gr-pw';",
		'CREATE DATABASE "netrollout" OWNER "netrollout";',
		'\\connect "netrollout"',
		'ALTER SCHEMA public OWNER TO "netrollout";',
		'GRANT USAGE ON SCHEMA "public" TO grafana_reader;',
	]


def test_a_schema_of_its_own_and_quoting():
	"""A schema of its own is created owned by the login; names and passwords are
	quoted; with Grafana's password unknown its CREATE ROLE line is commented out."""
	sql = preparation_sql(Plan(database="ops", schema="net rollout", login='nr"app',
	                           password="it's", grafana_password=None))
	assert """CREATE ROLE "nr""app" LOGIN PASSWORD 'it''s';""" in sql
	assert 'CREATE SCHEMA "net rollout" AUTHORIZATION "nr""app";' in sql
	assert 'CREATE DATABASE "ops" OWNER "nr""app";' in sql
	# Grafana's password unknown here: the line is left for the DBA to fill
	assert "-- CREATE ROLE grafana_reader LOGIN PASSWORD '<Grafana's database password>';" in sql


def test_places_compare_by_server_database_and_schema():
	"""Two configs are the same database whatever the login, not with another
	schema; describe shows host:port/database and the schema, never the password."""
	a = PostgresConfig(host="db1", port="5432", database="nr", user="x", password="p")
	url = PostgresConfig(url="postgresql+psycopg2://y:q@db1:5432/nr")
	assert same_database(a, url)                              # other login, same place
	assert not same_database(a, PostgresConfig(host="db1", port="5432", database="nr",
	                                           user="x", password="p", schema="other"))
	assert PostgresConfig(host="db1", port="6432", database="nr", user="x",
	                      password="secret", schema="ops").describe() == "db1:6432/nr (schema ops)"
