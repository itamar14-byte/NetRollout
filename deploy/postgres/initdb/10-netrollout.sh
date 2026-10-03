#!/bin/bash
# First start only (empty data volume), run by the official entrypoint as the
# superuser, in the netrollout database. Creates the roles; the app creates
# its tables (migrations) and grants grafana_reader its tables at its start.
set -euo pipefail
: "${NETROLLOUT_DB_PASSWORD:?NETROLLOUT_DB_PASSWORD is required (the app role)}"
: "${GRAFANA_DB_PASSWORD:?GRAFANA_DB_PASSWORD is required (grafana_reader)}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v db="$POSTGRES_DB" \
     -v app_pw="$NETROLLOUT_DB_PASSWORD" \
     -v grafana_pw="$GRAFANA_DB_PASSWORD" <<'SQL'
-- The app: owns its database and schema (migrations create the tables), but
-- is no superuser
CREATE ROLE netrollout LOGIN PASSWORD :'app_pw';
ALTER DATABASE :"db" OWNER TO netrollout;
ALTER SCHEMA public OWNER TO netrollout;

-- pg_cron: creating it needs a superuser; the app schedules its own jobs
CREATE EXTENSION IF NOT EXISTS pg_cron;
GRANT USAGE ON SCHEMA cron TO netrollout;

-- Grafana: may connect and look into public; which tables it may read is
-- granted by the app after its migrations (none until then)
CREATE ROLE grafana_reader LOGIN PASSWORD :'grafana_pw';
GRANT CONNECT ON DATABASE :"db" TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
SQL
echo "[netrollout-postgres] roles netrollout and grafana_reader created"
