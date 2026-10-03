#!/bin/sh
# Keeps nginx on the latest *valid* site: renders the values (env +
# shared/site.env) into staging, tests the whole config there with nginx -t,
# and only then swaps it in and reloads (graceful: open connections, live log
# streams included, carry on). A rejected change leaves the last good site
# serving. The outcome goes to shared/status.json for the app to show.
#   boot   wait for the certificate, then put the first site in place
#   watch  every NETROLLOUT_WATCH_INTERVAL seconds (3), apply a change of
#          site.env or the certificate files
nr=/etc/nginx/netrollout
shared=$nr/shared
certs=/etc/nginx/certs
interval="${NETROLLOUT_WATCH_INTERVAL:-3}"

log() { echo "[netrollout-nginx] $*"; }

fingerprint() {
    cat "$shared/site.env" "$certs/fullchain.pem" "$certs/privkey.pem" \
        2>/dev/null | sha256sum | cut -d ' ' -f 1
}

# status.json, written atomically: state (applied|rejected) + message
status() {
    msg=$(printf '%s' "$2" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g' |
          awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }')
    printf '{"state": "%s", "message": "%s", "time": "%s"}\n' \
        "$1" "$msg" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$shared/.status.json.tmp" &&
        mv "$shared/.status.json.tmp" "$shared/status.json"
}

# Render + test in staging; on success swap into active. Sets $result to the
# values used, or to the reason it was refused.
stage_and_swap() {
    rm -f "$nr"/staging/*
    if ! result=$(netrollout-render.sh "$nr/staging" "${1:-}" 2>&1); then
        return 1
    fi
    sed "s#$nr/active/#$nr/staging/#" /etc/nginx/nginx.conf > "$nr/staging/nginx.test"
    if ! out=$(nginx -t -q -c "$nr/staging/nginx.test" 2>&1); then
        result="$out"
        return 1
    fi
    cp "$nr/staging/site.conf" "$nr/active/site.conf.new" &&
        mv "$nr/active/site.conf.new" "$nr/active/site.conf"
}

boot() {
    waited=0
    until [ -s "$certs/fullchain.pem" ] && [ -s "$certs/privkey.pem" ]; do
        [ $((waited % 10)) -eq 0 ] &&
            log "waiting for the certificate ($certs/fullchain.pem + privkey.pem) — the installer creates it"
        sleep 1
        waited=$((waited + 1))
    done
    if stage_and_swap; then
        log "site: $result"
        status applied "$result"
        return 0
    fi
    # The app's values (or the certificate) don't work: start on the
    # installer's values so the app stays reachable to fix them
    refused="$result"
    log "refused: $refused"
    if stage_and_swap env-only; then
        log "started with the installer's values instead: $result"
        status rejected "$refused"
        return 0
    fi
    log "can't start: $result"
    status rejected "$result"
    return 1
}

watch() {
    last=$(fingerprint)
    while sleep "$interval"; do
        now=$(fingerprint)
        [ "$now" = "$last" ] && continue
        last="$now"
        if stage_and_swap && nginx -s reload 2>/dev/null; then
            log "applied: $result"
            status applied "$result"
        else
            log "rejected (still serving the previous site): $result"
            status rejected "$result"
        fi
    done
}

case "${1:-}" in
    boot)  boot ;;
    watch) watch ;;
    *)     echo "usage: $0 boot|watch" >&2; exit 2 ;;
esac
