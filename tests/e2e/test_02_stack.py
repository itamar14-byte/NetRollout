"""Check 1: the stack - every service of an install with monitoring up and
healthy, the app's health through nginx, /metrics closed from outside."""
from tests.e2e.harness import SERVICES, TAG, VERSION, Browser


def test_every_service_runs_and_those_with_a_health_check_are_healthy(install):
	"""`up --wait` returned; here each one is looked at: all nine services
	(monitoring on), running, healthy where they have a check (Loki and
	Alloy have none)."""
	rows = {row["Service"]: row for row in install.services()}
	assert set(rows) == SERVICES
	for name, row in rows.items():
		assert row["State"] == "running", (name, row["State"], row.get("Status"))
		assert row.get("Health", "") in ("", "healthy"), (name, row.get("Health"))
	assert {n for n, r in rows.items() if r.get("Health")} >= {
		"postgres", "redis", "app", "nginx", "prometheus", "grafana", "grafana-setup"}
	assert rows["app"]["Image"] == f"itamarweinstein/netrollout:{TAG}"
	assert rows["nginx"]["Image"] == f"itamarweinstein/netrollout-nginx:{TAG}"


def test_health_through_nginx_says_both_services_up_and_this_version(install):
	answer = Browser(install).get("/_netrollout/health")
	assert answer.status == 200
	health = answer.json()
	assert (health["status"], health["postgres"], health["redis"]) == ("ok", True, True)
	assert health["version"] == VERSION
	assert health["maintenance"] is None and health["draining"] is False


def test_metrics_are_served_inside_but_not_through_nginx(install):
	"""Prometheus scrapes the app inside the network; from outside /metrics
	is nginx's 404 (and the app does answer it inside - so it's nginx)."""
	inside = install.python(
		"import urllib.request as u; "
		"print(u.urlopen('http://127.0.0.1:8080/metrics', timeout=10).status)")
	assert inside == "200"
	answer = Browser(install).get("/metrics")
	assert answer.status == 404
	assert b"python_info" not in answer.body and b"flask_" not in answer.body
