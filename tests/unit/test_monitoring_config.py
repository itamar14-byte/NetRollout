"""deploy/grafana: the shipped dashboards must only reference datasources the
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


def test_there_are_dashboards_to_check():
	assert len(list((GRAFANA / "dashboards").glob("*.json"))) == 4


def test_every_dashboard_datasource_is_provisioned():
	uids = provisioned_uids()
	for path in (GRAFANA / "dashboards").glob("*.json"):
		refs = referenced(json.loads(path.read_text(encoding="utf-8")), set())
		missing = refs - uids - BUILT_IN
		assert not missing, f"{path.name} uses unknown datasources {missing}"
