#!/usr/bin/env bash
# NetRollout management (Linux, Docker Engine). Run as root (sudo): Docker,
# the secrets in .env and the folders' owners need it.
#
#   sudo ./linux/install.sh                      install in this folder
#   sudo ./linux/netrollout.sh start | stop | status | open | logs [service] | uninstall | help
#   sudo ./linux/netrollout.sh                   the menu
#
# The install folder is this script's parent folder. The script does what
# needs this machine (checks, Docker, owners) and leaves the thinking to the
# setup core inside the app image (python -m src.setup, docs/plans/stage-9.md).
# The whole body is one function, read before it runs, so `update` can
# replace this file safely.

main() {
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
VERSION="$(tr -d ' \r\n' < "$ROOT/VERSION" 2>/dev/null || true)"
PROJECT="${NETROLLOUT_PROJECT:-netrollout}"   # another name only for testing
APP_IMAGE="itamarweinstein/netrollout:$VERSION"
ENV_FILE="$ROOT/.env"
APP_UID=10001                                  # the app container's user

# ── options ──────────────────────────────────────────────────────────────────
COMMAND="${1:-}"; [ $# -gt 0 ] && shift
SERVICE="" YES="" NO_BROWSER="" DELETE_DATA="" KEEP_DATA=""
ANSWERS=()
while [ $# -gt 0 ]; do
	case "$1" in
		--yes) YES=1 ;;
		--no-browser) NO_BROWSER=1 ;;
		--delete-data) DELETE_DATA=1 ;;
		--keep-data) KEEP_DATA=1 ;;
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
	[ -f "$ENV_FILE" ] || fail "NetRollout isn't installed in $ROOT - run: sudo $ROOT/linux/install.sh"
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
	# the app container (uid 10001) writes these; the secrets stay root's
	chown -R "$APP_UID:$APP_UID" "$ROOT/config" "$ROOT/certs" "$ROOT/logs"
	chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"
	chmod 700 "$ROOT/backups"
}

do_start() {
	need_root; need_docker
	step "Checking the ports"
	facts
	setup_core prepare-start --busy-ports "$(busy_ports)" "${FACTS[@]}" | sed 's/^/   /'
	step "Starting NetRollout (the first time takes a few minutes: the images are downloaded)"
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
	say  "   Manage it with: sudo $ROOT/linux/netrollout.sh  (status, start, stop, logs, ...)"
	if ! systemctl is-enabled docker >/dev/null 2>&1; then
		warn "Docker doesn't start at boot - so neither does NetRollout: sudo systemctl enable docker"
	fi
	open_browser
}

do_stop() {
	need_installed; need_root
	if ! docker info >/dev/null 2>&1; then good "NetRollout isn't running (Docker isn't)."; return; fi
	local port running
	port="$(sed -n 's/^HTTPS_PORT=//p' "$ENV_FILE" | tail -1)"
	running="$( (curl -fsk --max-time 5 "https://127.0.0.1:${port:-443}/_netrollout/health" 2>/dev/null || true) |
		sed -n 's/.*"running": *\([0-9]*\).*/\1/p')"
	if [ -n "$running" ] && [ "$running" -gt 0 ]; then
		warn "$running rollout(s) running - they finish and are recorded first (up to 10 minutes)."
	fi
	step "Stopping NetRollout"
	compose stop >/dev/null 2>&1 || fail "Docker couldn't stop it: $(compose stop 2>&1 | tail -3)"
	good "Stopped. Start it again with: sudo $ROOT/linux/netrollout.sh start"
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
	local delete="$DELETE_DATA"
	if [ -z "$DELETE_DATA" ] && [ -z "$KEEP_DATA" ] && [ -n "$INTERACTIVE" ]; then
		local a=""
		read -r -p "Also delete NetRollout's data - the database, settings, certificates, logs and backups? This can't be undone. [y/N] " a || true
		case "$a" in y|Y|yes) delete=1 ;; esac
	fi
	if docker info >/dev/null 2>&1; then
		step "Removing NetRollout's containers${delete:+ and its data}"
		if [ -n "$delete" ]; then compose down --remove-orphans -v >/dev/null 2>&1 || warn "Docker couldn't remove everything."
		else compose down --remove-orphans >/dev/null 2>&1 || warn "Docker couldn't remove everything."; fi
	elif [ -n "$delete" ]; then
		warn "Docker isn't running: the database volumes stay (docker volume ls: ${PROJECT}_*)."
	fi
	if [ -n "$delete" ]; then
		rm -rf "$ENV_FILE" "$ROOT/config" "$ROOT/certs" "$ROOT/logs" "$ROOT/backups"
		good "NetRollout and its data are removed."
	else
		good "NetRollout is removed. Its data stays (the database volumes, and .env, config, certs,"
		good "logs, backups in $ROOT): installing again in this folder picks it up."
	fi
}

show_help() {
	say "NetRollout $VERSION - $ROOT"
	say ""
	say "  sudo linux/install.sh                    install NetRollout in this folder"
	say "  sudo linux/netrollout.sh start           start it"
	say "  sudo linux/netrollout.sh stop            stop it (running rollouts finish first)"
	say "  sudo linux/netrollout.sh status          is everything well? what to do if not"
	say "  sudo linux/netrollout.sh open            its address (and the browser, on a desktop)"
	say "  sudo linux/netrollout.sh logs [service]  recent log lines (app, nginx, postgres, ...)"
	say "  sudo linux/netrollout.sh uninstall       remove it (asks whether to delete the data too)"
	say "  sudo linux/netrollout.sh                 the menu"
	say ""
	say "  --yes   unattended (accepts the licence, defaults); with install also"
	say "          --hostname --https-port --monitoring y/n --org-certificate y/n --timezone"
	say "  --no-browser   --delete-data / --keep-data (uninstall)"
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
			say " NetRollout $VERSION   [not installed - run: sudo $ROOT/linux/install.sh]"
		fi
		say ""
		say "  1  Open (the address)"
		say "  2  Status"
		say "  3  Start"
		say "  4  Stop"
		say "  5  Logs"
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
			*) continue ;;
		esac
		say ""
		read -r -p " Press Enter to return to the menu" _ || return 0
	done
}

main "$@"
