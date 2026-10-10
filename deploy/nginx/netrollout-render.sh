#!/bin/sh
# Render the site config into the folder $1 from the values: the environment,
# overridden by shared/site.env (written by the app) unless $2 is "env-only".
# The values are checked first, so nothing the app writes ever becomes nginx
# syntax. Prints the values used; exit 1 with the reason if one is invalid.
set -eu
out="$1"
mode="${2:-}"
shared=/etc/nginx/netrollout/shared

host="${NETROLLOUT_HOSTNAME:-}"
port="${NETROLLOUT_HTTPS_PORT:-443}"
upstream="${APP_UPSTREAM:-app:8080}"

# A key's last value in site.env, if the key is there (never `source` it)
from_site_env() {
    sed -n "s/^$1=//p" "$shared/site.env" | tail -n 1 | tr -d '\r'
}
if [ "$mode" != env-only ] && [ -f "$shared/site.env" ]; then
    if grep -q '^NETROLLOUT_HOSTNAME=' "$shared/site.env"; then
        host=$(from_site_env NETROLLOUT_HOSTNAME)
    fi
    if grep -q '^NETROLLOUT_HTTPS_PORT=' "$shared/site.env"; then
        port=$(from_site_env NETROLLOUT_HTTPS_PORT)
    fi
fi

fail() { echo "$*" >&2; exit 1; }
# shellcheck disable=SC2018,SC2019  # hostnames are ASCII: [:upper:] would depend on the locale
host=$(printf '%s' "$host" | tr 'A-Z' 'a-z' | sed 's/\.$//')
label='[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?'
if [ -n "$host" ] && ! printf '%s' "$host" | grep -Eq "^$label(\.$label)*\$"; then
    fail "Not a valid hostname: '$host'"
fi
case "$port" in
    ''|*[!0-9]*) fail "Not a valid HTTPS port: '$port'" ;;
esac
if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
    fail "Not a valid HTTPS port: '$port'"
fi
if ! printf '%s' "$upstream" | grep -Eq '^[A-Za-z0-9.-]+:[0-9]{1,5}$'; then
    fail "Not a valid APP_UPSTREAM (host:port): '$upstream'"
fi

# Without a hostname nothing is redirected between names
NR_CANONICAL="$host"
NR_CANONICAL_KEY="${host:-nr-no-canonical.invalid}"
NR_REDIRECT_OTHER_NAMES=$([ -n "$host" ] && echo 1 || echo 0)
NR_HTTPS_HOST_DEFAULT="${host:-\$host}"
NR_PORT_SUFFIX=$([ "$port" = 443 ] && echo "" || echo ":$port")
NR_APP_UPSTREAM="$upstream"
export NR_CANONICAL NR_CANONICAL_KEY NR_REDIRECT_OTHER_NAMES \
       NR_HTTPS_HOST_DEFAULT NR_PORT_SUFFIX NR_APP_UPSTREAM

# shellcheck disable=SC2016  # literal ${...}: the names envsubst may replace
envsubst '${NR_CANONICAL} ${NR_CANONICAL_KEY} ${NR_REDIRECT_OTHER_NAMES} ${NR_HTTPS_HOST_DEFAULT} ${NR_PORT_SUFFIX} ${NR_APP_UPSTREAM}' \
    < /etc/nginx/netrollout/site.conf.template > "$out/site.conf"
echo "hostname=${host:-(none)} https_port=$port app=$upstream"
