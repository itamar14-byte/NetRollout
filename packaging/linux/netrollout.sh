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
# setup core inside the app image (python -m src.setup, docs/architecture.md §9).
# The whole body is one function, read before it runs, so `update` can
# replace this file safely.

main() {
set -euo pipefail

BIN="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"   # the scripts (bin/)
ROOT="$(dirname "$BIN")"
VERSION="$(tr -d ' \r\n' < "$ROOT/VERSION" 2>/dev/null || true)"
PROJECT=netrollout
UNIT="netrollout-port-$PROJECT"               # the port helper's systemd units
APP_IMAGE="itamarweinstein/netrollout:$VERSION"
IMAGE_VERSION=""      # compose's images of another version (a kept install's backup)
ENV_FILE="$ROOT/.env"
# an update's second half (update-finish) hasn't finished while this is here
# (and names this folder's VERSION): start (and install) finish it
MARKER="$ROOT/.update-pending"
# the folder whose data an uninstall kept (the volumes are the project's,
# not the folder's: an install elsewhere would reuse them)
KEPT_RECORD=/etc/netrollout/kept-data
APP_UID=10001                                  # the app container's user
# what a release brings: an update replaces these, an uninstall removes them
RELEASE_ITEMS=(bin compose.yaml compose.http.yaml deploy VERSION LICENSE README.md)

# ── options ──────────────────────────────────────────────────────────────────
COMMAND="${1:-}"; [ $# -gt 0 ] && shift
SERVICE="" YES="" NO_BROWSER="" DELETE_DATA="" KEEP_DATA="" NO_SAFETY_BACKUP=""
DELETE_BACKUPS="" REMOVE_FILES="" START_FRESH=""
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
		--start-fresh) START_FRESH=1 ;;
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
	start) need_installed; start_or_finish; open_browser ;;
	stop) do_stop ;;
	status) do_status ;;
	open) need_installed; address; echo; open_browser force ;;
	logs) need_installed; need_docker; compose logs --tail 100 --no-log-prefix "${SERVICE:-app}" ;;
	backup) do_backup ;;
	restore) do_restore ;;
	update) do_update ;;
	update-finish) do_update_finish ;;     # the new script, run by update
	apply) do_apply ;;
	port) do_port_by_hand ;;
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
fail() {
	printf '\n\033[31m%s\033[0m\n' "$1" >&2
	# a stop or update that may have asked for a stop now isn't happening: a
	# later stop waits for the rollouts again (nothing asked: nothing changes)
	if [ -n "${CLEAR_STOP_NOW:-}" ]; then
		compose exec -T app python -m src.jobs stop-now --clear >/dev/null 2>&1 || true
	fi
	exit "${2:-1}"
}

# ── this machine ─────────────────────────────────────────────────────────────
# From the install folder: compose reads .env's COMPOSE_FILE relative to the
# folder it runs in, not to --project-directory
compose() {
	(cd "$ROOT" && NETROLLOUT_VERSION="${IMAGE_VERSION:-$VERSION}" docker compose -p "$PROJECT" \
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

# port=who for every listening TCP port; NetRollout's own published ports
# left out. Another container's port names it (also one with no listener
# here: Docker's userland proxy off)
busy_ports() {
	local ours out="" port who line
	local -A named=()
	ours=" $(our_ports) "
	while IFS='=' read -r port who; do
		[ -n "$port" ] && named[$port]="$who"
	done < <(other_container_ports)
	while read -r line; do
		port="$(printf '%s' "$line" | awk '{print $4}' | sed 's/.*://')"
		[ -n "$port" ] || continue
		case "$ours" in *" $port "*) continue ;; esac
		case ",$out," in *",$port="*) continue ;; esac
		who="$(printf '%s' "$line" | sed -n 's/.*users:(("\([^"]*\)".*/\1/p')"
		case "$who" in docker-proxy|dockerd) who="${named[$port]:-Docker (another container)}" ;; esac
		out="$out${out:+,}$port=${who//[,=]/ }"
	done < <(ss -Htlnp 2>/dev/null || true)
	for port in "${!named[@]}"; do
		case "$ours" in *" $port "*) continue ;; esac
		case ",$out," in *",$port="*) continue ;; esac
		out="$out${out:+,}$port=${named[$port]}"
	done
	printf '%s' "$out"
}

# port=<name> (another Docker container), one per line: the host ports the
# other containers publish (not this NetRollout's) - every port of a
# published range too ("0.0.0.0:8000-8010->8000-8010/tcp")
other_container_ports() {
	command -v docker >/dev/null 2>&1 || return 0
	local name labels ports range port
	docker ps --format '{{.Names}}|{{.Labels}}|{{.Ports}}' 2>/dev/null |
	while IFS='|' read -r name labels ports; do
		case ",$labels," in *",com.docker.compose.project=$PROJECT,"*) continue ;; esac
		for range in $(printf '%s' "$ports" | grep -oE ':[0-9]+(-[0-9]+)?->' | sed 's/^://; s/->$//' | sort -u); do
			for port in $(seq "${range%-*}" "${range#*-}"); do
				printf '%s=%s (another Docker container)\n' "$port" "${name//[,=]/ }"
			done
		done
	done
}

# Whether nginx holds the HTTPS port in use - Docker's own answer: its
# container running with 443/tcp published on HTTPS_PORT. A taken port
# leaves it Created (Docker refused to start it); started again later it
# runs without the binding, even once the port is free (Docker 29)
https_published() {
	local id port seen
	port="$(env_value HTTPS_PORT)"; port="${port:-443}"
	id="$(compose ps -a -q nginx 2>/dev/null | head -1)"
	[ -n "$id" ] || return 1
	seen="$(docker inspect --format '{{.State.Running}}|{{range $p, $b := .NetworkSettings.Ports}}{{if eq $p "443/tcp"}}{{range $b}}{{.HostPort}} {{end}}{{end}}{{end}}' "$id" 2>/dev/null)" || return 1
	[ "${seen%%|*}" = true ] || return 1
	case " ${seen#*|} " in *" $port "*) return 0 ;; esac
	return 1
}

# Who holds port $1 in busy_ports' list $2 (nothing when nobody does)
port_owner() {
	local item IFS=,
	for item in $2; do
		if [ "${item%%=*}" = "$1" ]; then
			if [ -n "${item#*=}" ]; then printf '%s' "${item#*=}"; else printf 'another program'; fi
			return 0
		fi
	done
}

# The words for a taken HTTPS port (status says the same: src/setup/manage.py)
port_taken() {
	printf "Port %s is in use by %s - NetRollout can't publish it. Stop that, then: sudo %s/netrollout.sh start - or move NetRollout to another port: sudo %s/netrollout.sh port <N>" "$1" "$2" "$BIN" "$BIN"
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

# A key's value in .env (its last line wins, as for compose)
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

# The version .env was last set up by: its "# Updated to NetRollout X on ..."
# line (setup upgrade), else the installer's "# NetRollout X — written by the
# installer"; empty when it has neither (an early install)
kept_version() {
	local v
	v="$(sed -n 's/^# Updated to NetRollout \([^ ]*\) .*/\1/p' "$ENV_FILE" | tail -1)"
	[ -n "$v" ] || v="$(sed -n 's/^# NetRollout \([^ ]*\) .*written by the installer.*/\1/p' "$ENV_FILE" | tail -1)"
	printf '%s' "$v"
}

# The unfinished update this folder's files are for: its version - the
# marker naming this VERSION; empty: none, or a stale one (an update stopped
# before its files were replaced names a version not here)
pending_update() {
	local v=""
	if [ -n "$VERSION" ] && [ -f "$MARKER" ]; then
		v="$(sed -n 's/^version=//p' "$MARKER" | tail -1 | tr -d ' \r')"
	fi
	if [ "$v" = "$VERSION" ]; then printf '%s' "$v"; fi
}

# The marker, written before an update's second half can start: the version
# it brings, and whether the kept data's backup is still owed.
#   write_marker <version> [backup-first]
write_marker() {
	{
		printf "# NetRollout's update to %s hasn't finished while this file is here:\n" "$1"
		printf '# netrollout.sh start (or install.sh) finishes it\n'
		printf 'version=%s\nstarted=%s\n' "$1" "$(date '+%Y-%m-%d %H:%M')"
		if [ "${2:-}" = backup-first ]; then printf 'backup-first=yes\n'; fi
	} > "$MARKER"
}

# The kept-data record: write_record (uninstall --keep-data), record_value
# folder|version, clear_record (only when it names this folder)
write_record() {
	local v=""
	[ -f "$ENV_FILE" ] && v="$(kept_version)"
	mkdir -p "$(dirname "$KEPT_RECORD")"
	printf "# NetRollout's data is kept here (netrollout.sh uninstall --keep-data)\nfolder=%s\nversion=%s\n" \
		"$ROOT" "${v:-$VERSION}" > "$KEPT_RECORD"
}
record_value() { [ -f "$KEPT_RECORD" ] && sed -n "s/^$1=//p" "$KEPT_RECORD" | tail -1; return 0; }
clear_record() {
	if [ "$(record_value folder)" = "$ROOT" ]; then rm -f "$KEPT_RECORD"; fi
	return 0
}

# The project's Docker volumes (the data), each once: by compose's project
# label, and by name - compose names them <project>_<volume>, and one made or
# copied outside compose has only the name. The name must start with the
# project and "_" exactly: netrollout-dev_* (the dev stack's) never matches.
project_volumes() {
	{
		docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null
		docker volume ls -q 2>/dev/null | grep -E "^${PROJECT}_"
	} | sort -u || true
}

# The folders the project's containers (running or not) were started from
project_folders() {
	docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
		--format '{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null | sed '/^$/d' | sort -u || true
}

# A fresh install here, over data kept for another folder (the record's,
# while that folder still holds .env; else the project's Docker volumes - an
# rc1 uninstall left no record, or NetRollout is installed elsewhere): it
# would put new passwords and a new key on the old database. Refused, exit
# 2, nothing created; --start-fresh deletes it first, --yes alone never does.
refuse_over_kept() {
	local folder version dirs dir usable=""
	folder="$(record_value folder)"; version="$(record_value version)"
	if [ -n "$folder" ] && [ "$folder" != "$ROOT" ] && [ -f "$folder/.env" ]; then
		fail "NetRollout's data is kept in $folder${version:+ (version $version)}. Run the install there to update it, or start fresh: sudo $BIN/install.sh --start-fresh (deletes the kept data; its backups are kept)." 2
	fi
	if [ -n "$(project_volumes)" ]; then
		dirs="$(project_folders | paste -sd' ' -)"
		for dir in $dirs; do [ -f "$dir/.env" ] && usable=1; done
		if [ -n "$usable" ]; then
			fail "NetRollout's data is kept in Docker volumes from an earlier install (in $dirs) (docker volume ls: ${PROJECT}_*). Install it there again to use it, or start fresh: sudo $BIN/install.sh --start-fresh (deletes the kept data; its backups are kept)." 2
		fi
		# no known folder holds its .env - never assumed gone (an rc1 uninstall
		# recorded none): Windows' Setup says the same
		fail "NetRollout's data from an earlier install is still in Docker, but no known folder holds its .env${folder:+ (not $folder)}. Nothing was installed. If it's in another folder, run that folder's bin/install.sh: NetRollout is updated or started there with its data. If that folder is gone, start fresh: sudo $BIN/install.sh --start-fresh deletes it (can't be undone; backups in a folder you still have are kept)." 2
	fi
	# a record whose data is gone (deleted by hand) says nothing any more
	if [ -n "$folder" ]; then rm -f "$KEPT_RECORD"; fi
	return 0
}

# install --start-fresh: the kept data deleted first (asked unless --yes) -
# what Windows' Setup "Start fresh" deletes: the project's containers,
# volumes and network by compose's label, then - only once no volume is
# left - .env, config, certs, logs and the update marker of the folders it
# was kept for (the record's, the containers', this one). The backups stay.
# Refused, nothing deleted, while any of the project's containers runs.
start_fresh() {
	local label="label=com.docker.compose.project=$PROJECT" folders dir a="" record
	local -a ids
	need_docker
	record="$(record_value folder)"
	folders="$( { printf '%s\n' "$record"; project_folders; } | sed '/^$/d' | sort -u | paste -sd' ' -)"
	if [ -f "$ENV_FILE" ]; then folders="$folders $ROOT"; fi
	if [ -z "$(project_volumes)" ] && [ -z "$folders" ]; then
		good "No kept data - installing fresh."
		return 0
	fi
	if [ -n "$(docker container ls -q --filter "$label")" ]; then
		fail "NetRollout's containers are running (Docker project $PROJECT${folders:+, in $folders}) - nothing was deleted. Stop that NetRollout first (sudo <its folder>/bin/netrollout.sh stop)." 2
	fi
	if [ -z "$YES" ]; then
		say ""
		say "Start fresh: NetRollout's kept data is deleted - the database and the other volumes${folders:+, and .env, config, certs and logs in $folders}. Its backups are kept."
		read -r -p "This can't be undone. Delete it? [y/N] " a || true
		case "$a" in y|Y|yes) ;; *) fail "Nothing was deleted - nothing was installed." 1 ;; esac
	fi
	step "Deleting NetRollout's kept data (the database and the other volumes)"
	mapfile -t ids < <(docker container ls -aq --filter "$label")
	[ ${#ids[@]} -eq 0 ] || docker container rm -f "${ids[@]}" >/dev/null || true
	mapfile -t ids < <(project_volumes)
	[ ${#ids[@]} -eq 0 ] || docker volume rm "${ids[@]}" >/dev/null || true
	mapfile -t ids < <(docker network ls -q --filter "$label")
	[ ${#ids[@]} -eq 0 ] || docker network rm "${ids[@]}" >/dev/null || true
	# the volumes are the data: still there -> the files stay too
	if [ -n "$(project_volumes)" ]; then
		show "$(project_volumes)"
		fail "Docker didn't delete all of NetRollout's volumes (above) - nothing else was deleted, nothing was installed." 1
	fi
	for dir in $folders; do
		rm -rf "${dir:?}/.env" "$dir/config" "$dir/certs" "$dir/logs" "$dir/.update-pending"
	done
	rm -f "$KEPT_RECORD"
	good "The kept data is deleted - backups stay in their folders' backups/."
}

# Our two images of this version, pulled when this machine doesn't have them;
# a release whose image isn't on Docker Hub is incomplete - exit 3, "report it".
#   our_images [what to do after fixing the connection] [a last sentence]
our_images() {
	local image out
	for image in netrollout netrollout-nginx; do
		docker image inspect "itamarweinstein/$image:$VERSION" >/dev/null 2>&1 && continue
		if ! out="$(docker pull "itamarweinstein/$image:$VERSION" 2>&1)"; then
			show "$out"
			if printf '%s' "$out" | grep -qE 'not found|manifest unknown'; then
				fail "NetRollout $VERSION isn't published on Docker Hub (itamarweinstein/$image:$VERSION doesn't exist there). This release is incomplete - please report it: https://github.com/itamar14-byte/NetRollout/issues.${2:+ $2}" 3
			fi
			fail "Couldn't download itamarweinstein/$image:$VERSION - check the internet connection${1:-}.${2:+ $2}"
		fi
	done
}

# NetRollout's health on this computer (the port in use)
health_url() {
	local port; port="$(env_value HTTPS_PORT)"
	printf 'https://127.0.0.1:%s/_netrollout/health' "${port:-443}"
}

address() {
	local host port
	host="$(sed -n 's/^NETROLLOUT_HOSTNAME=//p' "$ROOT/config/nginx/site.env" 2>/dev/null | tail -1)"
	port="$(env_value HTTPS_PORT 2>/dev/null)"
	host="${host:-localhost}"; port="${port:-443}"
	if [ "$port" = 443 ]; then printf 'https://%s' "$host"; else printf 'https://%s:%s' "$host" "$port"; fi
}

reachable() {
	curl -fsk --max-time 5 -o /dev/null "$(health_url)"
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
	local busy port owner out
	busy="$(busy_ports)"
	setup_core prepare-start --busy-ports "$busy" "${FACTS[@]}" | sed 's/^/   /'
	# something else on the HTTPS port: Docker would refuse nginx - say who,
	# start nothing
	port="$(env_value HTTPS_PORT)"; port="${port:-443}"
	owner="$(port_owner "$port" "$busy")"
	[ -z "$owner" ] || fail "$(port_taken "$port" "$owner")"
	if docker image inspect "$APP_IMAGE" >/dev/null 2>&1; then step "Starting NetRollout"
	else step "Starting NetRollout (the first time takes a few minutes: the images are downloaded)"; fi
	if ! out="$(compose up -d --wait --wait-timeout 600 2>&1)"; then
		printf '%s\n' "$out"
		owner="$(port_owner "$port" "$(busy_ports)")"
		[ -z "$owner" ] || fail "$(port_taken "$port" "$owner")"
		compose logs --tail 30 app 2>&1 || true
		fail "NetRollout didn't start - above: what Docker said and the app's last lines."
	fi
	# a container whose start failed on a taken port runs without the
	# binding when started again - only a new container gets it
	if ! https_published; then
		owner="$(port_owner "$port" "$(busy_ports)")"
		[ -z "$owner" ] || fail "$(port_taken "$port" "$owner")"
		step "nginx runs without port $port - recreating it"
		out="$(compose up -d --no-deps --force-recreate --wait --wait-timeout 120 nginx 2>&1)" || show "$out"
		if ! https_published; then
			owner="$(port_owner "$port" "$(busy_ports)")"
			[ -z "$owner" ] || fail "$(port_taken "$port" "$owner")"
			fail "NetRollout's nginx runs without port $port (Docker didn't publish it) - above: what Docker said."
		fi
	fi
	clear_record
	if reachable; then good "NetRollout is running: $(address)"
	else warn "NetRollout runs, but $(address) didn't answer from this machine yet - see: netrollout status"; fi
}

# start with an unfinished update here: the update's second half first (its
# restart is the start) - never the new version without setup upgrade
start_or_finish() {
	if [ -n "$(pending_update)" ]; then
		step "The update to NetRollout $VERSION didn't finish - finishing it first"
		do_update_finish
	else
		do_start
	fi
}

do_install() {
	need_root
	if [ -n "$START_FRESH" ]; then start_fresh; fi
	if [ -f "$ENV_FILE" ]; then
		install_over_kept
		return
	fi
	need_docker
	refuse_over_kept
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
	our_images
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

# .env already here - kept by `uninstall --keep-data` (which promises this),
# or installed already: brought back with its own settings, on this release.
# The version .env was last set up by decides: the same one starts as it was;
# an older one is updated (checked by its image, a backup by it, then the
# update's own steps); a newer one is refused - no downgrades, nothing changed.
install_over_kept() {
	local kept old judge code=0 out have_old=""
	need_docker
	kept="$(kept_version)"
	[ ${#ANSWERS[@]} -eq 0 ] ||
		warn "The answers given aren't applied to an existing install - change them in System Settings."
	if [ -n "$(pending_update)" ]; then
		step "The update to NetRollout $VERSION didn't finish - finishing it now"
		do_update_finish
		return
	fi
	if [ "$kept" = "$VERSION" ]; then
		good "NetRollout's settings and data are already in $ROOT - starting it with them."
		do_start
		return
	fi
	step "Getting NetRollout $VERSION"
	our_images
	if [ -n "$kept" ]; then
		old="itamarweinstein/netrollout:$kept"
		if docker image inspect "$old" >/dev/null 2>&1 || docker pull -q "$old" >/dev/null 2>&1; then have_old=1; fi
		# the kept version decides, as an update's installed version does (its
		# image gone: this one - the versions compare the same way)
		judge="$APP_IMAGE"; [ -z "$have_old" ] || judge="$old"
		step "Checking the update: NetRollout $kept (its settings and data, kept in $ROOT) -> $VERSION"
		out="$(docker run --rm "$judge" python -m src.setup check-update --installed "$kept" --new "$VERSION" 2>&1)" || code=$?
		if [ "$code" -eq 2 ] && printf '%s' "$out" | grep -q 'the same version'; then
			good "NetRollout's settings and data are already in $ROOT - starting it with them."
			do_start
			return
		fi
		[ "$code" -ne 2 ] || { show "$out"; fail "Nothing was changed." 2; }
		[ "$code" -eq 0 ] || { show "$out"; fail "Couldn't compare the versions ($judge didn't run) - nothing was changed."; }
		[ -n "$have_old" ] || fail "NetRollout $kept's image ($old) isn't on this machine and couldn't be downloaded - it makes the backup before the update (one NetRollout $kept can restore). Nothing was changed. Check the internet connection, then run the install again."
	else
		warn "$ENV_FILE doesn't say which version set it up (an early install): taken as an older version and updated. The backup before the update is made by NetRollout $VERSION - it restores into $VERSION, not into the earlier version."
	fi
	# the update's own steps, the backup by the kept version first; resumable
	# (start or this install again) until its restart worked
	write_marker "$VERSION" backup-first
	do_update_finish
}

running_rollouts() {
	local running
	running="$( (curl -fsk --max-time 5 "$(health_url)" 2>/dev/null || true) |
		sed -n 's/.*"running": *\([0-9]*\).*/\1/p')"
	if [ -n "$running" ] && [ "$running" -gt 0 ]; then
		warn "$running rollout(s) running - they finish and are recorded first (up to 10 minutes)."
	fi
}

# The running app's rollouts, asked inside its container (python -m src.jobs:
# its Redis and database settings are there): the table, nothing when none;
# fails when it can't say (not running, a version without the command)
rollouts_list() { compose exec -T app python -m src.jobs rollouts 2>/dev/null; }

# Before a stop or an update: the rollouts running or queued and - asked here,
# not with --yes, nor with "shown" - wait for them (up to the drain deadline),
# cancel them all now (the app's drain cancels them at once: devices being
# configured finish, results recorded), or don't. Returns 1: don't stop.
#   confirm_rollouts stop|update [shown]
confirm_rollouts() {
	local list a out
	if ! list="$(rollouts_list)"; then running_rollouts; return 0; fi   # an older version: the count
	[ -n "$list" ] || return 0
	warn "$(printf '%s\n' "$list" | head -1)"
	printf '%s\n' "$list" | tail -n +2
	if [ -z "$INTERACTIVE" ] || [ "${2:-}" = shown ]; then
		warn "They finish and are recorded first (up to 10 minutes)."
		return 0
	fi
	while true; do
		read -r -p "   [W]ait for them (up to 10 minutes), [C]ancel them all now, or [D]on't $1? [W] " a || a=""
		case "$a" in
			""|w|W|wait) warn "They finish and are recorded first."; return 0 ;;
			c*|C*)
				if out="$(compose exec -T app python -m src.jobs stop-now 2>&1)"; then show "$out"
				else warn "Couldn't ask NetRollout to cancel them - they finish first."; fi
				return 0 ;;
			d*|D*) return 1 ;;
		esac
	done
}

do_stop() {
	need_installed; need_root; CLEAR_STOP_NOW=1
	if ! docker info >/dev/null 2>&1; then good "NetRollout isn't running (Docker isn't)."; return; fi
	confirm_rollouts stop || { good "Not stopped - NetRollout keeps running."; return 0; }
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
	pw="$(env_value GRAFANA_ADMIN_PASSWORD)"
	printf '%s' "$pw" | compose exec -T grafana grafana cli --homepath /usr/share/grafana \
			admin reset-admin-password --password-from-stdin >/dev/null 2>&1 ||
		warn "Grafana's admin password couldn't be reset - Grafana's dashboards may not update until it is (netrollout.sh logs grafana-setup)."
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
	local code=0
	out="$(compose run --rm --no-deps --user 0 "${grafana[@]}" app python -m src.backup restore "$name" \
			--https-port "$(env_value HTTPS_PORT)" \
			--key-out /data/backups/.restored-key "${grafana_dir[@]}" 2>&1)" || code=$?
	show "$out"
	# 3: the database restored (its key handed over), not every file
	if [ "$code" -ne 0 ] && [ "$code" -ne 3 ]; then
		step "Starting NetRollout again, as it was"
		do_start
		fail "Not restored - see above. Nothing was changed."
	fi
	out="$(setup_core restore-key 2>&1)" ||
		{ show "$out"; fail "Restored, but the backup's encryption key couldn't be put into .env - NetRollout isn't started (it couldn't decrypt the saved credentials). See above."; }
	show "$out"
	if monitoring_on; then
		# Grafana alone first (not grafana-setup), its password reset, then the rest
		if out="$(compose up -d --wait grafana 2>&1)"; then reset_grafana_admin
		else show "$out"; warn "Grafana didn't start - its admin password wasn't reset (netrollout.sh logs grafana)."; fi
	fi
	do_start
	if [ "$code" -eq 3 ]; then
		# exit 3: a script calling this can tell it from a full restore
		fail "Restored $shown's database, but not all of its files - see above for which. NetRollout runs on the restored data (everyone signs in again); put the missing files back by hand, or fix the cause and restore again." 3
	else
		good "Restored $shown. Everyone signs in again."
	fi
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
	need_installed; need_root; need_docker; CLEAR_STOP_NOW=1
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
	elif [ "$code" -ne 0 ]; then warn "Couldn't compare the versions ($APP_IMAGE didn't run) - continuing."; fi
	if [ -n "$INTERACTIVE" ]; then
		local a=""
		read -r -p "Update to NetRollout $new? A backup is made first; then about a minute without NetRollout, and everyone signs in again. [y/N] " a || true
		case "$a" in y|Y|yes) ;; *) rm -rf "$stage"; fail "Nothing was changed." 2 ;; esac
	fi
	confirm_rollouts update || { rm -rf "$stage"; fail "Nothing was changed." 2; }
	step "Backing up first"
	backup_create before-update || { rm -rf "$stage"; fail "Couldn't back up - nothing was changed. Fix it (above), then update again."; }
	step "Putting NetRollout $new's files in place"
	# from here on the update counts as unfinished until update-finish has run
	write_marker "$new"
	for item in "${RELEASE_ITEMS[@]}"; do
		[ -e "$folder/$item" ] || continue
		rm -rf "${ROOT:?}/$item"
		cp -a "$folder/$item" "$ROOT/"
	done
	chmod +x "$ROOT"/bin/*.sh
	# the new script finishes: its own steps, its own version (this one was
	# read whole before it ran, so replacing it underneath is safe)
	exec "$ROOT/bin/netrollout.sh" update-finish --no-browser
}

# The new version's script, after update put its files in place (or install
# over kept data of an older version): the new images while the old version
# keeps running, the kept data's backup when owed, .env up to date, the
# restart. Resumable: the marker says the update is unfinished until the
# restart worked (start finishes it), and whether the backup is still owed.
do_update_finish() {
	need_installed; need_root; need_docker; CLEAR_STOP_NOW=1
	local out owed="" kept old back="The backup made before the update is in $ROOT/backups (...-before-update.zip)."
	if [ -n "$(pending_update)" ] && grep -qx 'backup-first=yes' "$MARKER"; then owed=backup-first; fi
	write_marker "$VERSION" "$owed"
	step "Downloading NetRollout $VERSION (it keeps running meanwhile)"
	# failures of others' images surface at the start; ours are checked here
	compose pull --quiet --ignore-pull-failures >/dev/null 2>&1 || true
	our_images ", then: sudo $BIN/netrollout.sh start (it finishes the update)" "$back"
	if [ -n "$owed" ]; then
		kept="$(kept_version)"
		if [ -n "$kept" ]; then
			old="itamarweinstein/netrollout:$kept"
			docker image inspect "$old" >/dev/null 2>&1 || docker pull -q "$old" >/dev/null 2>&1 ||
				fail "NetRollout $kept's image ($old) isn't on this machine and couldn't be downloaded - it makes the backup before the update. NetRollout wasn't started; its data is as NetRollout $kept left it. Check the internet connection, then: sudo $BIN/netrollout.sh start (it finishes the update)."
		fi
		set_owners
		step "Backing up first (the data NetRollout ${kept:-had} left here)"
		IMAGE_VERSION="$kept"     # by the version that wrote the data (none known: this one)
		backup_create before-update || fail "Couldn't back up - NetRollout wasn't started and its data wasn't changed. Fix it (above), then: sudo $BIN/netrollout.sh start (it finishes the update)."
		IMAGE_VERSION=""
		write_marker "$VERSION"   # the backup is made: not again on a resume
	fi
	step "Updating the settings"
	out="$(setup_core upgrade 2>&1)" || { show "$out"; fail "The update stopped before the restart - see above. $back"; }
	show "$out"
	confirm_rollouts update shown     # asked before the files were replaced
	step "Restarting on the new version (about a minute; running rollouts finish first)"
	do_start
	rm -f "$MARKER"
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
	# the folder as systemd reads it: % starts a specifier (%% is one %), and
	# the command is quoted - a path with spaces is one argument
	local root="${ROOT//%/%%}"
	cat > "/etc/systemd/system/$UNIT.service" <<UNITEOF
[Unit]
Description=NetRollout port helper ($root): applies an HTTPS port saved in System Settings
[Service]
Type=oneshot
ExecStart="$root/bin/netrollout.sh" apply --yes
TimeoutStartSec=600
UNITEOF
	cat > "/etc/systemd/system/$UNIT.path" <<UNITEOF
[Unit]
Description=NetRollout port helper ($root): watches its site.env
[Path]
PathModified=$root/config/nginx/site.env
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

# One port-* step of the setup core; when it fails, what it said and then $1 -
# the state it leaves (the unit's journal; the person's terminal)
#   port_step "<why it stopped>" port-close --outcome ...
port_step() {
	local why="$1" out
	shift
	out="$(setup_core "$@" 2>&1)" || { show "$out"; fail "$why"; }
}

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
		current="$(env_value HTTPS_PORT)"
		case "$action" in
			none)
				if [ -n "$message" ]; then warn "Port $port not applied: $message"; else say "No port change to apply."; fi
				return 0 ;;
			wait)
				if ! wait_port_trial "$id"; then
					port_step "Port $port wasn't confirmed within 2 minutes, and ending its trial failed (above) - nginx wasn't changed: it still answers on port $port too. Run: sudo $BIN/netrollout.sh apply" \
						port-close --outcome rollback --id "$id" --timed-out
					update_nginx >/dev/null 2>&1 || true
					warn "Port $port wasn't confirmed within 2 minutes (it didn't open from a browser - a firewall?) - NetRollout stays on port $current."
					return 0
				fi ;;
			try)
				step "Opening port $port next to port $current"
				if ! out="$(setup_core port-open --port "$port" 2>&1)"; then
					show "$out"
					# recorded as failed (the page shows it; not retried by itself)
					setup_core port-close --outcome failed --id "$id" --message "port $port: its trial couldn't be prepared (see the port helper's log)" >/dev/null 2>&1 ||
						warn "Recording that failed too - the page may still be waiting."
					fail "Port $port wasn't opened: preparing its trial failed (above). nginx wasn't changed - NetRollout stays on port $current."
				fi
				if ! out="$(update_nginx 2>&1)"; then
					show "$out"     # what Docker said (the unit's journal)
					why="$(printf '%s\n' "$out" | grep -iE 'error|failed|allocated' | head -1)"
					[ -n "$why" ] || why="nginx did not start with it"
					port_step "Port $port couldn't be opened ($why), and ending its trial failed (above) - nginx may still be set up with port $port. Run: sudo $BIN/netrollout.sh apply" \
						port-close --outcome failed --id "$id" --message "port $port: $why"
					update_nginx >/dev/null 2>&1 || true
					warn "Port $port couldn't be opened: $why - port $current stays."
					return 0
				fi
				if ! out="$(setup_core port-trying --port "$port" --id "$id" 2>&1)"; then
					show "$out"
					# nginx answers on both ports, but no trial is running: closed again
					port_step "Port $port opened, but its trial couldn't be started (above), nor ended - nginx still answers on port $port too. Run: sudo $BIN/netrollout.sh apply" \
						port-close --outcome failed --id "$id" --message "port $port: its trial couldn't be started (see the port helper's log)"
					update_nginx >/dev/null 2>&1 || true
					fail "Port $port opened, but its trial couldn't be started (above) - closed again: NetRollout stays on port $current."
				fi
				good "Port $port is open next to port $current. Open NetRollout on port $port within 2 minutes to keep it - else port $current stays." ;;
			keep)
				port_step "Port $port was confirmed, but keeping it failed (above) - nginx wasn't changed: it answers on ports $current and $port. Run: sudo $BIN/netrollout.sh apply" \
					port-close --outcome keep --id "$id"
				update_nginx >/dev/null 2>&1 || true
				good "Port $port kept - NetRollout is at $(address)." ;;   # then: anything newer?
			rollback)
				port_step "Rolling the port change back failed (above) - nginx wasn't changed: it may still answer on the trial's port too. Run: sudo $BIN/netrollout.sh apply" \
					port-close --outcome rollback --id "$id" --message "$message"
				update_nginx >/dev/null 2>&1 || true
				warn "Port change rolled back ($message) - NetRollout stays on port $current." ;;
			*)
				show "$out"; fail "Unexpected answer from the port helper: $(printf '%s\n' "$out" | head -1)" ;;
		esac
	done
}

# ── netrollout.sh port N: the HTTPS port by hand ─────────────────────────────
# For when the port in use is blocked (another program holds it) and System
# Settings can't be reached. .env, site.env and the System Settings HTTPS
# port all name the new port afterwards (so neither the port helper nor the
# next start undoes it); any step failing puts the previous port back in
# all three. Not while a System Settings port change is applied (its lock).

# System Settings' HTTPS port, through the app's own settings rules: in the
# running app's container, else a one-off one next to the database
set_port_setting() {
	local out code=0
	if app_running; then
		out="$(compose exec -T app python -m src.setup port-setting --wanted "$1" 2>&1)" || code=$?
	else
		out="$(compose up -d --wait postgres 2>&1)" || { show "$out"; return 1; }
		out="$(compose run --rm --no-deps -T app python -m src.setup port-setting --wanted "$1" 2>&1)" || code=$?
	fi
	[ "$code" -eq 0 ] || show "$out"
	return "$code"
}

# The previous port back in all three, NetRollout started on it (it may be
# blocked still - then start says by whom)
restore_previous_port() {
	local out
	warn "Putting port $1 back"
	out="$(setup_core port-set --wanted "$1" --previous 2>&1)" || show "$out"
	set_port_setting "$1" || warn "The System Settings HTTPS port couldn't be put back - set it to $1 there."
	( do_start ) || true
}

# Where restore_previous_port left NetRollout, in words (after "... - "):
# back on the port; or put back, but another program holds it (named); or
# put back, but it didn't start there for another reason
port_back_text() {
	local owner
	if https_published; then printf 'NetRollout is back on port %s.' "$1"; return 0; fi
	owner="$(port_owner "$1" "$(busy_ports)")"
	if [ -n "$owner" ]; then
		printf "it's put back to port %s - but %s is taken by %s: stop that, then sudo %s/netrollout.sh start." "$1" "$1" "$owner" "$BIN"
	else
		printf "it's put back to port %s - but it didn't start there (above): see sudo %s/netrollout.sh status." "$1" "$BIN"
	fi
}

do_port_by_hand() {
	need_installed; need_root; need_docker
	local wanted="$SERVICE" current out previous a="" i
	[ -n "$wanted" ] || fail "Which port? sudo $BIN/netrollout.sh port <N>  (e.g. port 8443)"
	exec 9>"$ROOT/config/.port-helper.lock"
	flock -n 9 || fail "A port change from System Settings is being applied - try again in a few minutes. Nothing was changed."
	current="$(env_value HTTPS_PORT)"; current="${current:-443}"
	# checked before the question: an unusable, current or taken port is said
	# at once, never "Change the port from 7443 to 7443?"
	step "Checking port $wanted"
	if ! out="$(setup_core port-set --wanted "$wanted" --busy-ports "$(busy_ports)" --check 2>&1)"; then
		show "$out"
		fail "Nothing was changed - NetRollout stays on port $current."
	fi
	if [ -n "$INTERACTIVE" ]; then
		read -r -p "NetRollout restarts on port $wanted (running rollouts finish first; everyone signs in again). Change the port from $current to $wanted? [y/N] " a || true
		case "$a" in y|Y|yes) ;; *) say "Nothing was changed."; return 0 ;; esac
	fi
	if ! out="$(setup_core port-set --wanted "$wanted" --busy-ports "$(busy_ports)" 2>&1)" ||
		! previous="$(printf '%s\n' "$out" | sed -n 's/^previous=\([0-9]*\).*/\1/p' | head -1)" || [ -z "$previous" ]; then
		show "$out"
		fail "Nothing was changed - NetRollout stays on port $current."
	fi
	step "Saving port $wanted in System Settings"
	if ! set_port_setting "$wanted"; then
		restore_previous_port "$previous"
		fail "The System Settings HTTPS port couldn't be saved (above) - $(port_back_text "$previous")"
	fi
	if ( do_start ); then
		for i in $(seq 1 20); do reachable && break; sleep 3; done
	fi
	if ! reachable; then
		restore_previous_port "$previous"
		fail "NetRollout didn't answer on port $wanted (above) - $(port_back_text "$previous")"
	fi
	good "NetRollout now uses port $wanted - $(address)"
}

do_status() {
	need_installed; need_root
	local states reach=no published=no pending code=0
	# first: the new version's image may not be here yet (the report needs it)
	pending="$(pending_update)"
	if [ -n "$pending" ]; then
		printf '\033[33m%s\033[0m\n' "Update:       the update to NetRollout $pending DIDN'T FINISH - run: sudo $BIN/netrollout.sh start (it finishes the update, then starts)"
	fi
	need_docker
	states="$(compose ps -a --format '{{.Service}}={{.State}}/{{.Health}}' 2>/dev/null | sed 's|/$||' | paste -sd, -)"
	reachable && reach=yes
	if https_published; then published=yes; fi
	facts
	ON_NETWORK=1 setup_core status --containers "$states" --reachable "$reach" \
		--https-published "$published" --busy-ports "$(busy_ports)" "${FACTS[@]}" || code=$?
	if [ -n "$pending" ] && [ "$code" -eq 0 ]; then code=1; fi
	return "$code"
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
		compose down --remove-orphans ${delete:+-v} >/dev/null 2>&1 || warn "Docker couldn't remove everything."
	elif [ -n "$delete" ]; then
		warn "Docker isn't running: the database volumes stay (docker volume ls: ${PROJECT}_*)."
	fi
	if [ -n "$delete" ]; then
		rm -rf "$ENV_FILE" "$ROOT/config" "$ROOT/certs" "$ROOT/logs"
		rm -f "$MARKER"
		clear_record
		if [ -n "$delete_backups" ]; then rm -rf "$ROOT/backups"; fi
		if [ -d "$ROOT/backups" ]; then
			good "NetRollout and its data are removed - the backups stay in $ROOT/backups"
			good "(restore one into a new install: netrollout.sh restore <file>)."
		else
			good "NetRollout and its data are removed."
		fi
	else
		write_record
		good "NetRollout is removed. Its data stays (the database volumes, and .env, config, certs,"
		good "logs, backups in $ROOT): installing again in this folder picks it up."
	fi
	if [ -n "$remove_files" ]; then
		local item
		for item in "${RELEASE_ITEMS[@]}" .update; do
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
	say "  sudo $BIN/netrollout.sh update          update to the newest release (asks first; --check only looks)"
	say "  sudo $BIN/netrollout.sh apply           apply an HTTPS port saved in System Settings (systemd does it by itself)"
	say "  sudo $BIN/netrollout.sh port <N>        move NetRollout to port N when its port is taken (else: System Settings)"
	say "  sudo $BIN/netrollout.sh uninstall       remove it (asks whether to delete the data too)"
	say "  sudo $BIN/netrollout.sh                 the menu"
	say ""
	say "  --yes   unattended (accepts the licence, defaults); with install also"
	say "          --hostname --https-port --monitoring y/n --org-certificate y/n --timezone"
	say "  install --start-fresh: delete the data an uninstall kept (elsewhere or here) first;"
	say "          the backups stay (asks; --yes alone never deletes it)"
	say "  --no-browser   --no-safety-backup (restore)"
	say "  uninstall: --delete-data / --keep-data, --delete-backups (with --delete-data; else kept),"
	say "             --remove-files (the scripts and compose files too)"
	say "  update: --version X (not the latest)  --from <zip> (offline; checked against a SHA256SUMS next to it)"
	say "          --feed <url|file> (a mirror's release JSON: one release or a list)  --check"
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
		say "  9  Change the HTTPS port (when its port is taken)"
		say "  0  Exit"
		say ""
		local choice=""
		read -r -p " Choose a number: " choice || return 0
		say ""
		case "$choice" in
			0) return 0 ;;
			1) ( COMMAND=open; need_installed; say "$(address)"; open_browser force ) || true ;;
			2) ( COMMAND=status; do_status ) || true ;;
			3) ( COMMAND=start; need_installed; start_or_finish ) || true ;;
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
			9) ( COMMAND=port
			     need_installed
			     read -r -p " The new HTTPS port (Enter: back to the menu): " SERVICE || exit 0
			     [ -n "$SERVICE" ] || exit 0
			     do_port_by_hand ) || true ;;
			*) continue ;;
		esac
		say ""
		read -r -p " Press Enter to return to the menu" _ || return 0
	done
}

main "$@"
