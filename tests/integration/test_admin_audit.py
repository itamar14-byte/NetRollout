"""Admin -> Audit log: the page and its filters."""
from src.db.tables import AuditLog
from tests.integration.test_admin_users import admin  # noqa: F401 - fixture


# ── Audit + analytics ────────────────────────────────────────────────────────

def test_audit_page_filters(admin, client_for, make_user, session_scope):
	"""The audit page filters by actor and by success."""
	with session_scope() as s:
		s.add_all([AuditLog(actor_username="alice", action="inventory.create"),
		           AuditLog(actor_username="bob", action="auth.login",
		                    success=False)])
	c = client_for(admin)
	html = c.get("/admin/audit?actor=alice").get_data(as_text=True)
	assert "inventory.create" in html and "bob" not in html
	assert "alice" not in c.get("/admin/audit?success=false").get_data(as_text=True)
