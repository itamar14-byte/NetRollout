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
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

GRAFANA = os.environ.get("GRAFANA_URL", "http://grafana:3000").rstrip("/")
REAPPLY_SECONDS = int(os.environ.get("REAPPLY_SECONDS", "300"))
# The health check: present once a run succeeded
DONE_FILE = Path(os.environ.get("DONE_FILE", "/tmp/grafana-setup.done"))
DASHBOARDS = Path(os.environ.get("DASHBOARDS_DIR", "/dashboards"))
API_V2 = "/apis/dashboard.grafana.app/v2/namespaces/default/dashboards"
FOLDER_ANNOTATION = "grafana.app/folder"

ROOT = "netrollout"
CUSTOM = "custom"
SUBFOLDERS = {"Operations": "netrollout-operations",
              "Jobs": "netrollout-jobs",
              "Security": "netrollout-security"}
# Grafana's folder permission levels: 1 view, 2 edit, 4 admin
VIEW_ONLY = [{"role": "Editor", "permission": 1}, {"role": "Viewer", "permission": 1}]
EDITABLE = [{"role": "Editor", "permission": 2}, {"role": "Viewer", "permission": 1}]


def log(message):
	print(f"[grafana-setup] {message}", flush=True)


def call(method, path, body=None, ok=(200,)):
	"""One Grafana API call as its admin; returns (status, parsed body)."""
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


def wait_for_grafana(seconds=180):
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


def ensure_folder(uid, title, parent=None):
	status, folder = call("GET", f"/api/folders/{uid}", ok=(200, 404))
	if status == 404:
		body = {"uid": uid, "title": title}
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


def set_permissions(uid, items):
	call("POST", f"/api/folders/{uid}/permissions", {"items": items})


def import_dashboard(path, folder_uid):
	"""Create or overwrite one shipped dashboard in its folder."""
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


def remove_retired(folder_uids, shipped):
	"""A dashboard a release no longer ships leaves the NetRollout folders."""
	_, found = call("GET", "/api/search?type=dash-db&limit=5000")
	for dashboard in found:
		if dashboard.get("folderUid") in folder_uids and \
				dashboard["uid"] not in shipped:
			call("DELETE", f"{API_V2}/{dashboard['uid']}", ok=(200, 202, 404))
			log(f"retired: {dashboard['title']}")


def apply():
	wait_for_grafana()
	ensure_folder(ROOT, "NetRollout")
	for title, uid in SUBFOLDERS.items():
		ensure_folder(uid, title, ROOT)
	ensure_folder(CUSTOM, "Custom")

	shipped = set()
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
		log(f"done: {len(shipped)} dashboards in NetRollout; Custom untouched")


def main():
	once = "--once" in sys.argv
	while True:
		try:
			apply()
			DONE_FILE.touch()
		except Exception as e:      # one readable line in `docker compose logs`
			log(f"FAILED: {e}")
			if once:
				sys.exit(1)
		if once:
			return
		time.sleep(REAPPLY_SECONDS)


if __name__ == "__main__":
	main()
