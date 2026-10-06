#!/usr/bin/env bash
# NetRollout management (Linux, Docker Engine). Run as root (sudo): Docker,
# the secrets in .env and the folders' owners need it.
#
#   sudo ./bin/install.sh                      install in this folder
#   sudo ./bin/netrollout.sh start | stop | status | open | logs [service] |
#                            backup | restore <file> | update | apply | uninstall | help
#   sudo ./bin/netrollout.sh                   the menu
#
# The install folder is this script's parent folder. The script does what
# needs this machine (checks, Docker, owners) and leaves the thinking to the
# setup core inside the app image (python -m src.setup, docs/plans/stage-9.md).
# The whole body is one function, read before it runs, so `update` can
# replace this file safely.

main() {
set -euo pipefail

BIN="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"   # the scripts (bin/)
ROOT="$(dirname "$BIN")"
VERSION="$(tr -d ' \r\n' < "$ROOT/VERSION" 2>/dev/null || true)"
PROJECT="${NETROLLOUT_PROJECT:-netrollout}"   # another name only for testing
UNIT="netrollout-port-$PROJECT"               # the port helper's systemd units
APP_IMAGE="itamarweinstein/netrollout:$VERSION"
ENV_FILE="$ROOT/.env"
APP_UID=10001                                  # the app container's user

# ── options ──────────────────────────────────────────────────────────────────
COMMAND="${1:-}"; [ $# -gt 0 ] && shift
SERVICE="" YES="" NO_BROWSER="" DELETE_DATA="" KEEP_DATA="" NO_SAFETY_BACKUP=""
DELETE_BACKUPS="" REMOVE_FILES=""
CHECK="" WANTED="" FROM_ZIP="" FEED=""
ANSWERS=()
while [ $# -gt 0 ]; do
	case "$1" in
		--yes) YES=1 ;;
		--no-browser) NO_BROWSER=1 ;;
		--delete-data) DELETE_DATA=1 ;;
		--keep-data) KEEP_DATA=1 ;;
		--delete-backups) DELETE_BACKUPS=1 ;;
		--remove-files) REMOVE_FILES=1 ;;
		--no-safety-backup) NO_SAFETY_BACKUP=1 ;;
		--check) CHECK=1 ;;
		--version|--from|--feed)
			[ $# -ge 2 ] || fail "$1 needs a value"
			case "$1" in --version) WANTED="$2" ;; --from) FROM_ZIP="$2" ;; --feed) FEED="$2" ;; esac
			shift ;;
		--hostname|--https-port|--monitoring|--org-certificate|--timezone)
			[ $# -ge 2 ] || fail "$1 needs a value"
			ANSWERS+=("$1" "$2"); shift ;;
		-*) fail "Unknown option: $1 (see: netrollout help)" ;;
		*) SERVICE="$1" ;;
	esac
	shift
done
INTERACTIVE=""; [ -t 0 ] && [ -z "$YES" ] && INTERACTIVE=1

case "$COMMAND" in
	install) do_install ;;
	start) need_installed; do_start; open_browser ;;
	stop) do_stop ;;
	status) do_status ;;
	open) need_installed; address; echo; open_browser force ;;
	logs) need_installed; need_docker; compose logs --tail 100 --no-log-prefix "${SERVICE:-app}" ;;
	backup) do_backup ;;
	restore) do_restore ;;
	update) do_update ;;
	update-finish) do_update_finish ;;     # the new script, run by update
	apply) do_apply ;;
	uninstall) do_uninstall ;;
	help|-h|--help) show_help ;;
	"") show_menu ;;
	*) show_help; fail "Unknown command: $COMMAND" ;;
esac
}

# ── output ───────────────────────────────────────────────────────────────────
say()  { printf '%s\n' "$*"; }
step() { printf '\033[36m-> %s\033[0m\n' "$*"; }
good() { printf '\033[32m   %s\033[0m\n' "$*"; }
warn() { printf '\033[33m   %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$1" >&2; exit "${2:-1}"; }

# ── this machine ─────────────────────────────────────────────────────────────
# From the install folder: compose reads .env's COMPOSE_FILE relative to the
# folder it runs in, not to --project-directory
compose() {
	(cd "$ROOT" && NETROLLOUT_VERSION="$VERSION" docker compose -p "$PROJECT" \
		--project-directory "$ROOT" --env-file "$ENV_FILE" "$@")
}

need_root() {
	[ "$(id -u)" -eq 0 ] || fail "Run it as root: sudo $0 $COMMAND"
}

need_installed() {
	[ -f "$ENV_FILE" ] || fail "NetRollout isn't installed in $ROOT - run: sudo $BIN/install.sh"
}

need_docker() {
	if ! command -v docker >/dev/null 2>&1; then
		fail "Docker isn't installed. NetRollout runs on Docker Engine with its compose plugin:
  Ubuntu / Debian: https://docs.docker.com/engine/install/  (or: curl -fsSL https://get.docker.com | sh)
Then run this again."
	fi
	if ! docker compose version >/dev/null 2>&1; then
		fail "Docker's compose plugin is missing: install docker-compose-plugin (see https://docs.docker.com/compose/install/linux/)."
	fi
	if ! docker info >/dev/null 2>&1; then
		if [ "$(id -u)" -ne 0 ]; then fail "Docker refused this user - run it as root: sudo $0 $COMMAND"; fi
		fail "Docker isn't running: sudo systemctl start docker (and enable it at boot: sudo systemctl enable docker)."
	fi
}

# port=who for every listening TCP port; NetRollout's own published ports left out
busy_ports() {
	local ours out="" port who
	ours=" $(our_ports) "
	while read -r line; do
		port="$(printf '%s' "$line" | awk '{print $4}' | sed 's/.*://')"
		[ -n "$port" ] || continue
		case "$ours" in *" $port "*) continue ;; esac
		case ",$out," in *",$port="*) continue ;; esac
		who="$(printf '%s' "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p')"
		case "$who" in docker-proxy|dockerd) who="Docker (another container)" ;; esac
		out="$out${out:+,}$port=${who//[,=]/ }"
	done < <(ss -Htlnp 2>/dev/null || true)
	printf '%s' "$out"
}

our_ports() {
	[ -f "$ENV_FILE" ] || return 0
	compose ps --format '{{range .Publishers}}{{.PublishedPort}} {{end}}' 2>/dev/null | tr '\n' ' '
}

server_ips() {
	ip -4 -o addr show scope global 2>/dev/null |
		awk '$2 !~ /^(docker|br-|veth|virbr|lo)/ {split($4, a, "/"); print a[1]}' | paste -sd, -
}

host_timezone() {
	local tz=""
	tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
	[ -n "$tz" ] || tz="$(readlink -f /etc/localtime 2>/dev/null | sed -n 's|.*/zoneinfo/||p')"
	[ -n "$tz" ] || tz="$(cat /etc/timezone 2>/dev/null || true)"
	printf '%s' "${tz:-UTC}"
}

facts() {
	FACTS=(--os linux --computer-name "$(hostname -s 2>/dev/null || hostname)"
	       --host-timezone "$(host_timezone)" --server-ips "$(server_ips)"
	       --account "${SUDO_USER:-$(id -un)}")
}

# the setup core in the app image, the install folder mounted (as root: it
# writes into this root-owned folder; the owners are set afterwards)
setup_core() {
	local tty=() net=()
	[ "${TALK:-}" = 1 ] && tty=(-it)
	if [ "${ON_NETWORK:-}" = 1 ] && docker network inspect "${PROJECT}_default" >/dev/null 2>&1; then
		net=(--network "${PROJECT}_default")
	fi
	docker run --rm "${tty[@]}" "${net[@]}" --user 0:0 -v "$ROOT:/install" \
		-e NETROLLOUT_HOME=/install "$APP_IMAGE" python -m src.setup "$@"
}

address() {
	local host port
	host="$(sed -n 's/^NETROLLOUT_HOSTNAME=//p' "$ROOT/config/nginx/site.env" 2>/dev/null | tail -1)"
	port="$(sed -n 's/^HTTPS_PORT=//p' "$ENV_FILE" 2>/dev/null | tail -1)"
	host="${host:-localhost}"; port="${port:-443}"
	if [ "$port" = 443 ]; then printf 'https://%s' "$host"; else printf 'https://%s:%s' "$host" "$port"; fi
}

reachable() {
	local port; port="$(sed -n 's/^HTTPS_PORT=//p' "$ENV_FILE" | tail -1)"
	curl -fsk --max-time 5 -o /dev/null "https://127.0.0.1:${port:-443}/_netrollout/health"
}

open_browser() {
	[ -z "$NO_BROWSER" ] || return 0
	[ "${1:-}" = force ] || [ -n "$INTERACTIVE" ] || return 0
	if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
		xdg-open "$(address)" >/dev/null 2>&1 &
	fi
}

# ── NetRollout ───────────────────────────────────────────────────────────────
set_owners() {
	# the app container (uid 10001) writes these (backups: the scheduled ones,
	# closed to everyone else - they hold the encryption key); .env stays root's
	mkdir -p "$ROOT/backups"
	chown -R "$APP_UID:$APP_UID" "$ROOT/config" "$ROOT/certs" "$ROOT/logs" "$ROOT/backups"
	chmod 700 "$ROOT/backups"
	chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"
}

do_start() {
	need_root; need_docker
	set_owners
	port_helper_on
	step "Checking the ports"
	facts
	setup_core prepare-start --busy-ports "$(busy_ports)" "${FACTS[@]}" | sed 's/^/   /'
	if docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then step "Starting NetRollout"
	else step "Starting NetRollout (the first time takes a few minutes: the images are downloaded)"; fi
	local out
	if ! out="$(compose up -d --wait --wait-timeout 600 2>&1)"; then
		printf '%s\n' "$out"
		compose logs --tail 30 app 2>&1 || true
		fail "NetRollout didn't start - above: what Docker said and the app's last lines."
	fi
	if reachable; then good "NetRollout is running: $(address)"
	else warn "NetRollout runs, but $(address) didn't answer from this machine yet - see: netrollout status"; fi
}

do_install() {
	need_root
	[ ! -f "$ENV_FILE" ] || fail "NetRollout is already installed in $ROOT - see: netrollout status (or netrollout update)." 2
	if [ -z "$YES" ]; then
		cat <<'NOTICE'

NetRollout is free software under the GNU Affero General Public License v3
(https://www.gnu.org/licenses/agpl-3.0.html): you may use, change and share
it; if you offer a changed version to others over a network, you must offer
them its source too. It comes with no warranty.

NetRollout runs on Docker Engine (open source, Apache License 2.0).

NOTICE
		local answer=""
		read -r -p "Type yes to accept and continue: " answer || true
		[ "$answer" = yes ] || fail "The licence terms weren't accepted - nothing was installed."
	fi
	need_docker
	step "Getting NetRollout $VERSION"
	if ! docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then
		docker pull -q "$APP_IMAGE" >/dev/null || fail "Couldn't download $APP_IMAGE - check the internet connection."
	fi
	step "Setting up"
	facts
	local args=(init --licence-accepted --busy-ports "$(busy_ports)" "${FACTS[@]}" "${ANSWERS[@]}")
	local code=0
	if [ -n "$YES" ]; then setup_core "${args[@]}" --defaults || code=$?
	else TALK=1 setup_core "${args[@]}" || code=$?; fi
	[ "$code" -eq 0 ] || fail "Setup stopped - nothing was started." "$code"
	set_owners
	do_start
	say ""
	good "Installed. Open $(address) and sign in as admin / admin - you'll set a new password."
	say  "   Manage it with: sudo $BIN/netrollout.sh  (status, start, stop, logs, ...)"
	if ! systemctl is-enabled docker >/dev/null 2>&1; then
		warn "Docker doesn't start at boot - so neither does NetRollout: sudo systemctl enable docker"
	fi
	open_browser
}

running_rollouts() {
	local port running
	port="$(sed -n 's/^HTTPS_PORT=//p' "$ENV_FILE" | tail -1)"
	running="$( (curl -fsk --max-time 5 "https://127.0.0.1:${port:-443}/_netrollout/health" 2>/dev/null || true) |
		sed -n 's/.*"running": *\([0-9]*\).*/\1/p')"
	if [ -n "$running" ] && [ "$running" -gt 0 ]; then
		warn "$running rollout(s) running - they finish and are recorded first (up to 10 minutes)."
	fi
}

do_stop() {
	need_installed; need_root
	if ! docker info >/dev/null 2>&1; then good "NetRollout isn't running (Docker isn't)."; return; fi
	running_rollouts
	step "Stopping NetRollout"
	compose stop >/dev/null 2>&1 || fail "Docker couldn't stop it: $(compose stop 2>&1 | tail -3)"
	good "Stopped. Start it again with: sudo $BIN/netrollout.sh start"
}

# ── backups (the engine: python -m src.backup, in the app image) ─────────────
# compose run / exec's own progress lines left out of what people read
show() { printf '%s\n' "$1" | grep -Ev '^[[:space:]]*(Container|Network|Volume) |^[[:space:]]*$' | sed 's/^/   /' || true; }

app_running() { compose ps --status running --services 2>/dev/null | grep -qx app; }

monitoring_on() { grep -Eq '^COMPOSE_PROFILES=.*monitoring' "$ENV_FILE"; }

# In the running app; when it isn't running, a one-off app container next to
# the database (the same settings, folders and Grafana's data)
backup_create() {
	local out code=0
	if app_running; then
		out="$(compose exec -T app python -m src.backup create --kind "$1" 2>&1)" || code=$?
	else
		out="$(compose up -d --wait postgres 2>&1)" || { show "$out"; fail "The database didn't start - see above."; }
		out="$(compose run --rm --no-deps app python -m src.backup create --kind "$1" 2>&1)" || code=$?
	fi
	show "$out"
	return "$code"
}

do_backup() {
	need_installed; need_root; need_docker
	step "Backing up"
	backup_create manual || fail "Not backed up - see above."
	good "In $ROOT/backups. Keep a copy somewhere else too: it holds the key to the saved credentials."
}

# Grafana's admin password back to this installation's (.env): the restored
# Grafana database has the backup's, and grafana-setup signs in with ours.
# Grafana must run, grafana-setup not yet (it would sign in with the wrong one).
reset_grafana_admin() {
	local pw
	pw="$(sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' "$ENV_FILE" | tail -1)"
	if printf '%s' "$pw" | compose exec -T grafana grafana cli --homepath /usr/share/grafana \
			admin reset-admin-password --password-from-stdin >/dev/null 2>&1; then
		return 0
	else
		warn "Grafana's admin password couldn't be reset - Grafana's dashboards may not update until it is (netrollout.sh logs grafana-setup)."
	fi
}

do_restore() {
	need_installed; need_root
	[ -n "$SERVICE" ] || fail "Which backup? sudo $0 restore <file>  (they're in $ROOT/backups)"
	local file name shown out
	if [ -f "$SERVICE" ]; then file="$(readlink -f "$SERVICE")"
	elif [ -f "$ROOT/backups/$SERVICE" ]; then file="$ROOT/backups/$SERVICE"
	else fail "No such file: $SERVICE"; fi
	shown="$(basename "$file")"
	name="$shown"
	need_docker
	set_owners
	# the app sees only the backups folder: a file from elsewhere is staged
	# there under a hidden name and removed afterwards (the original stays)
	if [ "$(dirname "$file")" != "$(readlink -f "$ROOT/backups")" ]; then
		name=".restoring-$shown"
		STAGED="$ROOT/backups/$name"
		trap 'rm -f "$STAGED"' EXIT
		cp -f "$file" "$STAGED"
		chown "$APP_UID:$APP_UID" "$STAGED"; chmod 600 "$STAGED"
	fi
	step "Checking $shown"
	out="$(compose run --rm --no-deps app python -m src.backup check "$name" 2>&1)" ||
		{ show "$out"; fail "This backup can't be restored - nothing was changed."; }
	show "$out"
	if [ -n "$INTERACTIVE" ]; then
		local a=""
		read -r -p "Restore it? Everything in NetRollout since then is replaced, and everyone signs in again (the current state is backed up first). [y/N] " a || true
		case "$a" in y|Y|yes) ;; *) fail "Nothing was changed." 2 ;; esac
	fi
	if [ -n "$NO_SAFETY_BACKUP" ]; then
		warn "Without a backup of the current state (--no-safety-backup)."
	else
		step "Backing up the current state first"
		backup_create before-restore || fail "Couldn't back up the current state - nothing was changed. To restore anyway (e.g. the database is damaged): sudo $0 restore \"$file\" --no-safety-backup"
	fi
	local stop=(stop app) grafana=() grafana_dir=()
	if monitoring_on; then
		stop+=(grafana-setup grafana)
		grafana=(-v "${PROJECT}_grafana:/data/grafana-restore")
		grafana_dir=(--grafana-dir /data/grafana-restore)
	fi
	step "Stopping NetRollout (running rollouts finish first)"
	out="$(compose "${stop[@]}" 2>&1)" || { show "$out"; fail "Docker couldn't stop it - nothing was changed."; }
	out="$(compose up -d --wait postgres 2>&1)" || { show "$out"; do_start; fail "The database didn't start - nothing was changed."; }
	step "Restoring $shown"
	# as root: the files get their folder's owner (Grafana's volume is Grafana's)
	if ! out="$(compose run --rm --no-deps --user 0 "${grafana[@]}" app python -m src.backup restore "$name" \
			--https-port "$(sed -n 's/^HTTPS_PORT=//p' "$ENV_FILE" | tail -1)" \
			--key-out /data/backups/.restored-key "${grafana_dir[@]}" 2>&1)"; then
		show "$out"
		step "Starting NetRollout again, as it was"
		do_start
		fail "Not restored - see above. Nothing was changed."
	fi
	show "$out"
	out="$(setup_core restore-key 2>&1)" ||
		{ show "$out"; fail "Restored, but the backup's encryption key couldn't be put into .env - NetRollout isn't started (it couldn't decrypt the saved credentials). See above."; }
	show "$out"
	if monitoring_on; then
		# Grafana alone first (not grafana-setup), its password reset, then the rest
		if out="$(compose up -d --wait grafana 2>&1)"; then reset_grafana_admin
		else show "$out"; warn "Grafana didn't start - its admin password wasn't reset (netrollout.sh logs grafana)."; fi
	fi
	do_start
	good "Restored $shown. Everyone signs in again."
}

# ── update (the release: python -m src.setup release, in the app image) ─────
# a feed file must be where the setup core sees it: inside this folder
in_container() {
	case "$1" in
		*://*) printf '%s' "$1" ;;
		"$ROOT"/*) printf '/install/%s' "${1#"$ROOT"/}" ;;
		*) fail "A feed file must be inside $ROOT (or give a URL): $1" ;;
	esac
}

do_update() {
	need_installed; need_root; need_docker
	local stage="$ROOT/.update" out code=0 new folder item
	local args=(--out /install/.update)
	rm -rf "$stage"; mkdir -p "$stage"
	if [ -n "$FROM_ZIP" ]; then
		[ -f "$FROM_ZIP" ] || fail "No such file: $FROM_ZIP"
		cp -f "$FROM_ZIP" "$stage/"
		if [ -f "$(dirname "$FROM_ZIP")/SHA256SUMS" ]; then cp -f "$(dirname "$FROM_ZIP")/SHA256SUMS" "$stage/"; fi
		args+=(--from-zip "/install/.update/$(basename "$FROM_ZIP")")
	fi
	if [ -n "$WANTED" ]; then args+=(--release-version "$WANTED"); fi
	if [ -n "$FEED" ]; then args+=(--feed "$(in_container "$FEED")"); fi
	if [ -n "$CHECK" ]; then
		setup_core release --check "${args[@]}" | sed 's/^/   /' || true
		rm -rf "$stage"; return 0
	fi
	step "Getting the release"
	out="$(setup_core release "${args[@]}" 2>&1)" || code=$?
	if [ "$code" -ne 0 ]; then show "$out"; rm -rf "$stage"; fail "Nothing was changed."; fi
	new="$(printf '%s\n' "$out" | sed -n 's/^version=//p' | tail -1)"
	folder="$stage/netrollout"
	step "Checking the update: NetRollout $VERSION -> $new"
	code=0; out="$(docker run --rm "$APP_IMAGE" python -m src.setup check-update --installed "$VERSION" --new "$new" 2>&1)" || code=$?
	if [ "$code" -eq 2 ]; then show "$out"; rm -rf "$stage"; fail "Nothing was changed." 2
	elif [ "$code" -ne 0 ]; then warn "Couldn't compare the versions ($APP_IMAGE didn't run) - continuing."
	elif [ "$(printf '%s\n' "$out" | tail -1)" = same ]; then good "The same version: its files again, then a start (a repair)."; fi
	if [ -n "$INTERACTIVE" ]; then
		local a=""
		read -r -p "Update to NetRollout $new? A backup is made first; then about a minute without NetRollout, and everyone signs in again. [y/N] " a || true
		case "$a" in y|Y|yes) ;; *) rm -rf "$stage"; fail "Nothing was changed." 2 ;; esac
	fi
	running_rollouts
	step "Backing up first"
	backup_create before-update || { rm -rf "$stage"; fail "Couldn't back up - nothing was changed. Fix it (above), then update again."; }
	step "Putting NetRollout $new's files in place"
	for item in bin compose.yaml compose.http.yaml deploy VERSION LICENSE README.md; do
		[ -e "$folder/$item" ] || continue
		rm -rf "${ROOT:?}/$item"
		cp -a "$folder/$item" "$ROOT/"
	done
	chmod +x "$ROOT"/bin/*.sh
	# the new script finishes: its own steps, its own version (this one was
	# read whole before it ran, so replacing it underneath is safe)
	exec "$ROOT/bin/netrollout.sh" update-finish --no-browser
}

# The new version's script, after update put its files in place: the new
# images while the old version keeps running, .env up to date, the restart.
do_update_finish() {
	need_installed; need_root; need_docker
	local out image back="The backup made before the update is in $ROOT/backups (...-before-update.zip)."
	step "Downloading NetRollout $VERSION (it keeps running meanwhile)"
	# failures of others' images surface at the start; ours are checked here
	compose pull --quiet --ignore-pull-failures >/dev/null 2>&1 || true
	for image in netrollout netrollout-nginx; do
		docker image inspect "itamarweinstein/$image:$VERSION" >/dev/null 2>&1 && continue
		if ! out="$(docker pull "itamarweinstein/$image:$VERSION" 2>&1)"; then
			show "$out"
			if printf '%s' "$out" | grep -qE 'not found|manifest unknown'; then
				fail "NetRollout $VERSION isn't published on Docker Hub (itamarweinstein/$image:$VERSION doesn't exist there). This release is incomplete - please report it: https://github.com/itamar14-byte/NetRollout/issues. $back" 3
			fi
			fail "Couldn't download itamarweinstein/$image:$VERSION - check the internet connection, then: sudo $BIN/netrollout.sh update-finish. $back"
		fi
	done
	step "Updating the settings"
	out="$(setup_core upgrade 2>&1)" || { show "$out"; fail "The update stopped before the restart - see above. $back"; }
	show "$out"
	running_rollouts
	step "Restarting on the new version (about a minute; running rollouts finish first)"
	do_start
	rm -rf "$ROOT/.update"
	good "Updated to NetRollout $VERSION. Everyone signs in again."
}

# ── the port helper (System Settings -> HTTPS port) ──────────────────────────
# The setup core decides (src/setup/port.py), this does the Docker part: a
# trial file adds the new port to nginx, nginx is recreated, the trial ends
# kept or rolled back. Run by a systemd path unit when site.env changes, or by
# hand: netrollout.sh apply.
has_systemd() { [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; }

# the path unit watching site.env (no systemd: the page says to run apply)
port_helper_on() {
	has_systemd || return 0
	local project_env=""
	if [ "$PROJECT" != netrollout ]; then project_env="Environment=NETROLLOUT_PROJECT=$PROJECT"; fi
	cat > "/etc/systemd/system/$UNIT.service" <<UNITEOF
[Unit]
Description=NetRollout port helper ($ROOT): applies an HTTPS port saved in System Settings
[Service]
Type=oneshot
$project_env
ExecStart=$ROOT/bin/netrollout.sh apply --yes
TimeoutStartSec=600
UNITEOF
	cat > "/etc/systemd/system/$UNIT.path" <<UNITEOF
[Unit]
Description=NetRollout port helper ($ROOT): watches its site.env
[Path]
PathModified=$ROOT/config/nginx/site.env
[Install]
WantedBy=multi-user.target
UNITEOF
	systemctl daemon-reload >/dev/null 2>&1 || true
	if systemctl enable --now "$UNIT.path" >/dev/null 2>&1; then
		setup_core port-ready >/dev/null 2>&1 || true    # the page knows a helper is here
	else
		warn "The port helper couldn't be enabled (systemctl enable $UNIT.path) - a new HTTPS port then needs: netrollout.sh apply"
	fi
}

port_helper_off() {
	has_systemd || return 0
	systemctl disable --now "$UNIT.path" >/dev/null 2>&1 || true
	rm -f "/etc/systemd/system/$UNIT.path" "/etc/systemd/system/$UNIT.service"
	systemctl daemon-reload >/dev/null 2>&1 || true
}

update_nginx() { compose up -d --no-deps --wait --wait-timeout 120 nginx; }

# the trial's confirmation within its 120 s, timed here (never compared with
# a deadline written elsewhere); 0 = confirmed
wait_port_trial() {
	local i=0
	while [ "$i" -lt 120 ]; do
		grep -qx "NETROLLOUT_PORT_CONFIRMED=$1" "$ROOT/config/nginx/site.env" 2>/dev/null && return 0
		sleep 1; i=$((i + 1))
	done
	return 1
}

do_apply() {
	need_installed; need_root; need_docker
	# one at a time (the path unit, and a hand-run apply): a second one waits
	# its turn, then handles what's still pending
	exec 9>"$ROOT/config/.port-helper.lock"
	flock -w 300 9 || { say "A port change is still being applied - try again in a few minutes."; return 0; }
	local out action port id message current why
	while true; do
		out="$(setup_core port-next --busy-ports "$(busy_ports)" 2>&1)" || { show "$out"; fail "The port helper couldn't decide - see above."; }
		read -r action port id <<< "$(printf '%s\n' "$out" | head -1)"
		message="$(printf '%s\n' "$out" | sed -n 2p)"
		current="$(sed -n 's/^HTTPS_PORT=//p' "$ENV_FILE" | tail -1)"
		case "$action" in
			none)
				if [ -n "$message" ]; then warn "Port $port not applied: $message"; else say "No port change to apply."; fi
				return 0 ;;
			wait)
				if ! wait_port_trial "$id"; then
					setup_core port-close --outcome rollback --id "$id" --timed-out >/dev/null 2>&1
					update_nginx >/dev/null 2>&1 || true
					warn "Port $port wasn't confirmed within 2 minutes (it didn't open from a browser - a firewall?) - NetRollout stays on port $current."
					return 0
				fi ;;
			try)
				step "Opening port $port next to port $current"
				setup_core port-open --port "$port" >/dev/null 2>&1
				if ! out="$(update_nginx 2>&1)"; then
					show "$out"     # what Docker said (the unit's journal)
					why="$(printf '%s\n' "$out" | grep -iE 'error|failed|allocated' | head -1)"
					[ -n "$why" ] || why="nginx did not start with it"
					setup_core port-close --outcome failed --id "$id" --message "port $port: $why" >/dev/null 2>&1
					update_nginx >/dev/null 2>&1 || true
					warn "Port $port couldn't be opened: $why - port $current stays."
					return 0
				fi
				setup_core port-trying --port "$port" --id "$id" >/dev/null 2>&1
				good "Port $port is open next to port $current. Open NetRollout on port $port within 2 minutes to keep it - else port $current stays." ;;
			keep)
				setup_core port-close --outcome keep --id "$id" >/dev/null 2>&1
				update_nginx >/dev/null 2>&1 || true
				good "Port $port kept - NetRollout is at $(address)." ;;   # then: anything newer?
			rollback)
				setup_core port-close --outcome rollback --id "$id" --message "$message" >/dev/null 2>&1
				update_nginx >/dev/null 2>&1 || true
				warn "Port change rolled back ($message) - NetRollout stays on port $current." ;;
			*)
				show "$out"; fail "Unexpected answer from the port helper: $(printf '%s\n' "$out" | head -1)" ;;
		esac
	done
}

do_status() {
	need_installed; need_root; need_docker
	local states reach=no
	states="$(compose ps -a --format '{{.Service}}={{.State}}/{{.Health}}' 2>/dev/null | sed 's|/$||' | paste -sd, -)"
	reachable && reach=yes
	facts
	ON_NETWORK=1 setup_core status --containers "$states" --reachable "$reach" \
		--busy-ports "$(busy_ports)" "${FACTS[@]}"
}

do_uninstall() {
	need_root
	if [ ! -f "$ENV_FILE" ]; then good "NetRollout isn't installed in $ROOT - nothing to remove."; return; fi
	local delete="$DELETE_DATA" delete_backups="$DELETE_BACKUPS" remove_files="$REMOVE_FILES"
	if [ -z "$DELETE_DATA" ] && [ -z "$KEEP_DATA" ] && [ -n "$INTERACTIVE" ]; then
		local a=""
		read -r -p "Also delete NetRollout's data - the database, settings, certificates and logs? This can't be undone. [y/N] " a || true
		case "$a" in y|Y|yes) delete=1 ;; esac
		if [ -n "$delete" ]; then
			a=""
			read -r -p "Keep the backups ($ROOT/backups)? They're the last copy of the data. [Y/n] " a || true
			case "$a" in n|N|no) delete_backups=1 ;; esac
		fi
		a=""
		read -r -p "Remove NetRollout's files in $ROOT too (the scripts, compose files)? [y/N] " a || true
		case "$a" in y|Y|yes) remove_files=1 ;; esac
	fi
	port_helper_off
	if docker info >/dev/null 2>&1; then
		step "Removing NetRollout's containers${delete:+ and its data}"
		if [ -n "$delete" ]; then compose down --remove-orphans -v >/dev/null 2>&1 || warn "Docker couldn't remove everything."
		else compose down --remove-orphans >/dev/null 2>&1 || warn "Docker couldn't remove everything."; fi
	elif [ -n "$delete" ]; then
		warn "Docker isn't running: the database volumes stay (docker volume ls: ${PROJECT}_*)."
	fi
	if [ -n "$delete" ]; then
		rm -rf "$ENV_FILE" "$ROOT/config" "$ROOT/certs" "$ROOT/logs"
		if [ -n "$delete_backups" ]; then rm -rf "$ROOT/backups"; fi
		if [ -d "$ROOT/backups" ]; then
			good "NetRollout and its data are removed - the backups stay in $ROOT/backups"
			good "(restore one into a new install: netrollout.sh restore <file>)."
		else
			good "NetRollout and its data are removed."
		fi
	else
		good "NetRollout is removed. Its data stays (the database volumes, and .env, config, certs,"
		good "logs, backups in $ROOT): installing again in this folder picks it up."
	fi
	if [ -n "$remove_files" ]; then
		local item
		for item in bin compose.yaml compose.http.yaml deploy VERSION LICENSE README.md .update; do
			rm -rf "${ROOT:?}/$item"
		done
		if rmdir "$ROOT" 2>/dev/null; then good "$ROOT is removed."
		else good "NetRollout's files in $ROOT are removed."; fi
	fi
}

show_help() {
	say "NetRollout $VERSION - $ROOT"
	say ""
	say "  sudo $BIN/install.sh                    install NetRollout in this folder"
	say "  sudo $BIN/netrollout.sh start           start it"
	say "  sudo $BIN/netrollout.sh stop            stop it (running rollouts finish first)"
	say "  sudo $BIN/netrollout.sh status          is everything well? what to do if not"
	say "  sudo $BIN/netrollout.sh open            its address (and the browser, on a desktop)"
	say "  sudo $BIN/netrollout.sh logs [service]  recent log lines (app, nginx, postgres, ...)"
	say "  sudo $BIN/netrollout.sh backup          back up now (into $ROOT/backups)"
	say "  sudo $BIN/netrollout.sh restore <file>  put NetRollout back to a backup (asks first)"
	say "  sudo $BIN/netrollout.sh update          update to the latest release (asks first; --check only looks)"
	say "  sudo $BIN/netrollout.sh apply           apply an HTTPS port saved in System Settings (systemd does it by itself)"
	say "  sudo $BIN/netrollout.sh uninstall       remove it (asks whether to delete the data too)"
	say "  sudo $BIN/netrollout.sh                 the menu"
	say ""
	say "  --yes   unattended (accepts the licence, defaults); with install also"
	say "          --hostname --https-port --monitoring y/n --org-certificate y/n --timezone"
	say "  --no-browser   --no-safety-backup (restore)"
	say "  uninstall: --delete-data / --keep-data, --delete-backups (with --delete-data; else kept),"
	say "             --remove-files (the scripts and compose files too)"
	say "  update: --version X (not the latest)  --from <zip> (offline; checked against a SHA256SUMS next to it)"
	say "          --feed <url|file> (a mirror's release JSON)  --check"
}

show_menu() {
	[ -t 0 ] || { show_help; return; }
	while true; do
		clear 2>/dev/null || true
		if [ -f "$ENV_FILE" ]; then
			local state="not running"
			reachable 2>/dev/null && state="running"
			say " NetRollout $VERSION  -  $(address)   [$state]"
		else
			say " NetRollout $VERSION   [not installed - run: sudo $BIN/install.sh]"
		fi
		say ""
		say "  1  Open (the address)"
		say "  2  Status"
		say "  3  Start"
		say "  4  Stop"
		say "  5  Logs"
		say "  6  Back up"
		say "  7  Restore a backup"
		say "  8  Update"
		say "  0  Exit"
		say ""
		local choice=""
		read -r -p " Choose a number: " choice || return 0
		say ""
		case "$choice" in
			0) return 0 ;;
			1) ( COMMAND=open; need_installed; say "$(address)"; open_browser force ) || true ;;
			2) ( COMMAND=status; do_status ) || true ;;
			3) ( COMMAND=start; need_installed; do_start ) || true ;;
			4) ( COMMAND=stop; do_stop ) || true ;;
			5) ( COMMAND=logs; need_installed; need_docker; compose logs --tail 100 --no-log-prefix app ) || true ;;
			6) ( COMMAND=backup; do_backup ) || true ;;
			7) ( COMMAND=restore
			     need_installed
			     say " The backups:"
			     for f in "$ROOT"/backups/netrollout-*.zip; do [ -e "$f" ] && say "   $(basename "$f")"; done
			     read -r -p " File name (or a path; Enter: back to the menu): " SERVICE || exit 0
			     [ -n "$SERVICE" ] || exit 0
			     do_restore ) || true ;;
			8) ( COMMAND=update; do_update ) || true ;;
			*) continue ;;
		esac
		say ""
		read -r -p " Press Enter to return to the menu" _ || return 0
	done
}

main "$@"
