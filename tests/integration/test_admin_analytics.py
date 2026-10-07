"""Admin -> Analytics: the organisation's page, the active-job count, and the
audit query (allowlisted fields and operators only)."""
from src.db.tables import AuditLog
from tests.integration.test_admin_users import admin  # noqa: F401 - fixture


def test_audit_query_is_allowlisted(admin, client_for, session_scope):
	"""The admin analytics query filters the audit log on an allowed field; a
	field outside the allowlist gets 400."""
	with session_scope() as s:
		s.add(AuditLog(actor_username="alice", action="auth.login",
		               success=False))
	c = client_for(admin)
	resp = c.post("/admin/analytics/query", json={"rules": {
		"field": "success", "operator": "equal", "value": "false"}})
	assert [r["actor_username"] for r in resp.json["rows"]] == ["alice"]
	bad = c.post("/admin/analytics/query", json={"rules": {
		"field": "detail", "operator": "contains", "value": "x"}})
	assert bad.status_code == 400


def test_admin_analytics_page_and_job_count(app, admin, client_for,
                                            monkeypatch):
	"""The analytics page renders, and the active job count is the
	orchestrator's running + queued."""
	c = client_for(admin)
	assert c.get("/admin/analytics").status_code == 200
	# The orchestrator's own count (running + queued), not a Redis counter
	monkeypatch.setattr(app.orchestrator, "counts",
	                    lambda: {"running": 2, "queued": 1})
	body = c.get("/admin/active_job_count").json
	assert (body["count"], body["running"], body["queued"]) == (3, 2, 1)
