#!/bin/sh
# Start Postgres through the official entrypoint with pg_cron's settings.
# They're start arguments (not a config file written at first start), so they
# apply at every start and come with each new image, also on an old volume.
#   cron.use_background_workers  jobs run inside the server, as the role that
#                                scheduled them — no password login needed
#   cron.timezone                "0 3 * * *" means 03:00 local, outside
#                                business hours (follows daylight saving)
#   timezone / log_timezone      NOW() in the retention statements compares
#                                in the same local time the app stores
set -e
tz="${TZ:-UTC}"
exec docker-entrypoint.sh postgres \
    -c shared_preload_libraries=pg_cron \
    -c cron.database_name="${POSTGRES_DB:-netrollout}" \
    -c cron.use_background_workers=on \
    -c max_worker_processes=20 \
    -c cron.timezone="$tz" \
    -c timezone="$tz" \
    -c log_timezone="$tz" \
    "$@"
