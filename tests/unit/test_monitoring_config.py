"""deploy/grafana: the shipped dashboards (one subfolder per NetRollout
folder, imported by setup.py) must only reference datasources the
provisioning defines — a renamed datasource or a newly exported dashboard
would otherwise leave panels empty with no error anywhere but Grafana."""
import json
from pathlib import Path

import yaml

GRAFANA = Path(__file__).resolve().parents[2] / "deploy" / "grafana"
BUILT_IN = {"-- Grafana --", "-- Dashboard --", "-- Mixed --"}


def provisioned_uids():
	data = yaml.safe_load((GRAFANA / "provisioning" / "datasources"
	                       / "netrollout.yml").read_text(encoding="utf-8"))
	return {d["uid"] for d in data["datasources"]}


def referenced(node, found):
	"""Every datasource a dashboard points at: {"uid": ...} / {"name": ...}
	(both carry the uid in these exports) or a bare string."""
	if isinstance(node, dict):
		ds = node.get("datasource")
		if isinstance(ds, dict):
			ref = ds.get("uid") or ds.get("name")
			if ref and not ref.startswith("$"):   # template variables
				found.add(ref)
		elif isinstance(ds, str) and not ds.startswith("$"):
			found.add(ds)
		for value in node.values():
			referenced(value, found)
	elif isinstance(node, list):
		for value in node:
			referenced(value, found)
	return found


def shipped():
	return sorted((GRAFANA / "dashboards").glob("*/*.json"))


def test_there_are_dashboards_to_check():
	assert len(shipped()) == 4


def test_every_subfolder_is_one_setup_imports():
	# a dashboard in a folder setup.py doesn't know would never be imported
	import importlib.util
	spec = importlib.util.spec_from_file_location("setup", GRAFANA / "setup.py")
	setup = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(setup)
	assert {p.parent.name for p in shipped()} <= set(setup.SUBFOLDERS)


def test_every_dashboard_datasource_is_provisioned():
	uids = provisioned_uids()
	for path in shipped():
		refs = referenced(json.loads(path.read_text(encoding="utf-8")), set())
		missing = refs - uids - BUILT_IN
		assert not missing, f"{path.name} uses unknown datasources {missing}"
