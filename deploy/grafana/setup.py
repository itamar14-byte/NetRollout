"""Grafana setup — the grafana-setup service (on the app image: standard
library only). Applies the layout below at start, then again every
REAPPLY_SECONDS (a permission changed by hand is put back), and stays running:
`docker compose up --wait` counts a container that exits as a failure. A new
release recreates the container, so its dashboards are imported at once.
Idempotent.

  NetRollout                 the shipped dashboards: view-only for everyone
  ├── Operations             (they're re-imported from deploy/grafana/dashboards
  ├── Jobs                    on every run, so each release updates them)
  └── Security
  Custom                     the admins' own (Editors may create, edit, nest);
                             never read, changed or deleted here

Every Grafana user is a NetRollout admin (nginx lets nobody else through) and
an Editor. Why folders can't separate admins from operators — free Grafana lets
anyone signed in query every datasource — is in docs/workplan.md (post-v1).

The files are dashboard v2 resources: one subfolder of the dashboards folder
per NetRollout subfolder; a dashboard's metadata.name is its stable id.

The NetRollout database data source is kept here too (not provisioned: a
provisioned one is read-only), from the app's connection - config/runtime.env
(written by a database move, src/webapp/db_move.py), else the bundled
database - so the dashboards follow a move. The file is looked at every
CHECK_SECONDS; a change is applied at once. The dashboards refer to it by its
uid, which never changes.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GRAFANA = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
REAPPLY_SECONDS = int(os.environ.get("REAPPLY_SECONDS", "300"))
# The health check: present while the last run succeeded
DONE_FILE = Path(os.environ.get("DONE_FILE", "/tmp/grafana-setup.done"))
DASHBOARDS = Path(os.environ.get("DASHBOARDS_DIR", "/dashboards"))
API_V2 = "/apis/dashboard.grafana.app/v2/namespaces/default/dashboards"
FOLDER_ANNOTATION = "grafana.app/folder"
RUNTIME_ENV = Path(os.environ.get("RUNTIME_ENV", "/data/config/runtime.env"))
CHECK_SECONDS = 5

DATASOURCE_UID = "cfjxoedixn7r4d"           # the dashboards' reference
DATASOURCE_NAME = "NetRollout database"
# The bundled database, as the containers see it
BUNDLED = {"host": "postgres", "port": "5432", "database": "netrollout"}
# What a host's 127.0.0.1 means to the app running there (development): the
# bundled database, which containers reach by its service name
LOOPBACK = ("localhost", "127.0.0.1", "::1")

ROOT = "netrollout"
CUSTOM = "custom"
SUBFOLDERS = {"Operations": "netrollout-operations",
              "Jobs": "netrollout-jobs",
              "Security": "netrollout-security"}
# Grafana's folder permission levels: 1 view, 2 edit, 4 admin
VIEW_ONLY = [{"role": "Editor", "permission": 1}, {"role": "Viewer", "permission": 1}]
EDITABLE = [{"role": "Editor", "permission": 2}, {"role": "Viewer", "permission": 1}]


def log(message: str) -> None:
	print(f"[grafana-setup] {message}", flush=True)


def call(method: str, path: str, body: Any = None,
         ok: tuple[int, ...] = (200,)) -> tuple[int, Any]:
	"""One Grafana API call as its admin.

	:param path: under Grafana's address, e.g. /api/folders
	:param body: sent as JSON; None: no body
	:param ok: the statuses that aren't a failure
	:returns: (status, the parsed body - None when empty)
	:raises RuntimeError: another status"""
	auth = base64.b64encode(
		f"admin:{os.environ['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
	request = urllib.request.Request(
		GRAFANA + path, method=method,
		data=json.dumps(body).encode() if body is not None else None,
		headers={"Authorization": f"Basic {auth}",
		         "Content-Type": "application/json", "Accept": "application/json"})
	try:
		with urllib.request.urlopen(request, timeout=30) as response:
			status, raw = response.status, response.read()
	except urllib.error.HTTPError as e:
		status, raw = e.code, e.read()
	data = json.loads(raw) if raw else None
	if status not in ok:
		raise RuntimeError(f"{method} {path} -> {status}: {raw[:300]!r}")
	return status, data


def read_env(path: Path) -> dict[str, str]:
	"""KEY=value lines (what the app writes); missing file -> {}."""
	try:
		lines = path.read_text(encoding="utf-8").splitlines()
	except FileNotFoundError:
		return {}
	values: dict[str, str] = {}
	for line in lines:
		key, sep, value = line.partition("=")
		if sep and not key.lstrip().startswith("#"):
			value = value.strip()
			if len(value) >= 2 and value[0] == value[-1] == "'":    # as the app writes it
				value = re.sub(r"\\(['\\])", r"\1", value[1:-1])
			values[key.strip()] = value.strip("\"")
	return values


def database(values: dict[str, str]) -> dict[str, str]:
	"""Where NetRollout's data is, for Grafana: host, port, database, sslmode.

	:param values: the app's connection settings (config/runtime.env)"""
	place = dict(BUNDLED)
	if values.get("DATABASE_URL"):
		url = urllib.parse.urlsplit(values["DATABASE_URL"])
		place = {"host": url.hostname or "", "port": str(url.port or 5432),
		         "database": url.path.lstrip("/")}
	elif values.get("PG_HOST"):
		place = {"host": values["PG_HOST"], "port": values.get("PG_PORT") or "5432",
		         "database": values.get("PG_NAME") or BUNDLED["database"]}
	if place["host"] in LOOPBACK:
		place.update(host=BUNDLED["host"], port=BUNDLED["port"])
	place["sslmode"] = values.get("NETROLLOUT_GRAFANA_SSLMODE") or "disable"
	return place


def datasource_body(place: dict[str, str]) -> dict[str, Any]:
	""":returns: Grafana's data source definition for that database, as the
	 read-only grafana_reader"""
	host = place["host"]
	if ":" in host and not host.startswith("["):
		host = f"[{host}]"      # an IPv6 address: Grafana splits host:port as Go does
	return {"uid": DATASOURCE_UID, "name": DATASOURCE_NAME,
	        "type": "grafana-postgresql-datasource", "access": "proxy",
	        "url": f"{host}:{place['port']}", "user": "grafana_reader",
	        "jsonData": {"database": place["database"], "sslmode": place["sslmode"],
	                     "postgresVersion": 1700, "timescaledb": False},
	        "secureJsonData": {"password": os.environ.get("GRAFANA_DB_PASSWORD", "")}}


def ensure_datasource() -> str:
	"""The data source at NetRollout's database.

	:returns: where (host:port/database), for the log
	:raises RuntimeError: Grafana refused, or it's still the provisioned one"""
	body = datasource_body(database(read_env(RUNTIME_ENV)))
	status, current = call("GET", f"/api/datasources/uid/{DATASOURCE_UID}", ok=(200, 404))
	if status == 404:
		call("POST", "/api/datasources", body)
	elif current.get("readOnly"):
		# still the provisioned one (Grafana not restarted since the update
		# that retired it): it can't be changed until Grafana starts again
		raise RuntimeError("the database data source is still the provisioned one - "
		                   "restart Grafana (netrollout start)")
	else:
		call("PUT", f"/api/datasources/uid/{DATASOURCE_UID}", body)
	return f"{body['url']}/{body['jsonData']['database']}"


def wait_for_grafana(seconds: float = 180) -> None:
	""":raises RuntimeError: Grafana's database wasn't ready in time"""
	deadline = time.monotonic() + seconds
	while time.monotonic() < deadline:
		try:
			with urllib.request.urlopen(GRAFANA + "/api/health", timeout=5) as r:
				if json.loads(r.read()).get("database") == "ok":
					return
		except (OSError, ValueError):
			pass
		time.sleep(2)
	raise RuntimeError("Grafana didn't become ready")


def ensure_folder(uid: str, title: str, parent: str | None = None) -> None:
	"""The folder exists with this title under this parent - created, or put
	back when moved or renamed by hand.

	:param parent: the parent folder's uid; None: at the top"""
	status, folder = call("GET", f"/api/folders/{uid}", ok=(200, 404))
	if status == 404:
		body: dict[str, str] = {"uid": uid, "title": title}
		if parent:
			body["parentUid"] = parent
		call("POST", "/api/folders", body)
		log(f"folder created: {title}")
		return
	if (folder.get("parentUid") or None) != parent:      # moved by hand: put back
		call("POST", f"/api/folders/{uid}/move", {"parentUid": parent or ""})
	if folder.get("title") != title:
		call("PUT", f"/api/folders/{uid}",
		     {"title": title, "version": folder.get("version"), "overwrite": True})


def set_permissions(uid: str, items: list[dict[str, Any]]) -> None:
	"""The folder's permissions become exactly `items` (VIEW_ONLY / EDITABLE)."""
	call("POST", f"/api/folders/{uid}/permissions", {"items": items})


def import_dashboard(path: Path, folder_uid: str) -> str:
	"""Create or overwrite one shipped dashboard in its folder.

	:param path: its dashboard v2 file
	:returns: its uid"""
	resource = json.loads(path.read_text(encoding="utf-8"))
	name = resource["metadata"]["name"]
	body = {"apiVersion": resource["apiVersion"], "kind": "Dashboard",
	        "metadata": {"name": name,
	                     "annotations": {FOLDER_ANNOTATION: folder_uid}},
	        "spec": resource["spec"]}
	status, existing = call("GET", f"{API_V2}/{name}", ok=(200, 404))
	if status == 404:
		call("POST", API_V2, body, ok=(200, 201))
	else:
		body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
		call("PUT", f"{API_V2}/{name}", body, ok=(200, 201))
	return name


def remove_retired(folder_uids: set[str], shipped: set[str]) -> None:
	"""A dashboard a release no longer ships leaves the NetRollout folders.

	:param folder_uids: the shipped folders (Custom is never looked at)
	:param shipped: the uids this release ships"""
	_, found = call("GET", "/api/search?type=dash-db&limit=5000")
	for dashboard in found:
		if dashboard.get("folderUid") in folder_uids and \
				dashboard["uid"] not in shipped:
			call("DELETE", f"{API_V2}/{dashboard['uid']}", ok=(200, 202, 404))
			log(f"retired: {dashboard['title']}")


def apply() -> None:
	"""The whole layout: the data source, the folders, the shipped dashboards
	(retired ones removed), the permissions.

	:raises RuntimeError: Grafana not ready, or a call refused"""
	wait_for_grafana()
	where = ensure_datasource()
	ensure_folder(ROOT, "NetRollout")
	for title, uid in SUBFOLDERS.items():
		ensure_folder(uid, title, ROOT)
	ensure_folder(CUSTOM, "Custom")

	shipped: set[str] = set()
	for title, uid in SUBFOLDERS.items():
		for path in sorted((DASHBOARDS / title).glob("*.json")):
			shipped.add(import_dashboard(path, uid))
	remove_retired(set(SUBFOLDERS.values()), shipped)

	# Permissions last, and on every subfolder too (not only inherited), so a
	# subfolder created with Grafana's defaults can't stay editable
	for uid in [ROOT, *SUBFOLDERS.values()]:
		set_permissions(uid, VIEW_ONLY)
	set_permissions(CUSTOM, EDITABLE)
	if not DONE_FILE.exists():   # say it once, not every REAPPLY_SECONDS
		log(f"done: {len(shipped)} dashboards in NetRollout; Custom untouched; "
		    f"data from {where}")


def _stamp() -> int | None:
	""":returns: when the app's connection settings last changed; None: none"""
	try:
		return RUNTIME_ENV.stat().st_mtime_ns
	except FileNotFoundError:
		return None


def main() -> None:
	"""Apply the layout, then again every REAPPLY_SECONDS - and the data
	source at once when the app's database changes. --once: a single run
	(exit 1 when it fails). DONE_FILE (the health check) says the last run
	succeeded."""
	once = "--once" in sys.argv
	while True:
		stamp = _stamp()
		try:
			apply()
			DONE_FILE.touch()
		except Exception as e:      # one readable line in `docker compose logs`
			log(f"FAILED: {e}")
			DONE_FILE.unlink(missing_ok=True)      # unhealthy until a run succeeds
			if once:
				sys.exit(1)
		if once:
			return
		# the full layout every REAPPLY_SECONDS; the data source at once
		# when the app's connection changes (a database move)
		deadline = time.monotonic() + REAPPLY_SECONDS
		while time.monotonic() < deadline:
			time.sleep(CHECK_SECONDS)
			if _stamp() != stamp:
				stamp = _stamp()
				try:
					log(f"the app's database changed: data from {ensure_datasource()}")
					DONE_FILE.touch()
				except Exception as e:
					log(f"FAILED: {e}")
					DONE_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
	main()
