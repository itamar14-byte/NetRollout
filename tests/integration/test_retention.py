"""The nightly clean-up, run by the app (src/webapp/retention.py) on the test
database: the settings' periods, its outcome recorded for System Settings."""
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from src.db.settings import seed_settings
from src.webapp import retention

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def test_the_clean_up_follows_the_settings_and_is_recorded(app, session_scope, make_user, client_for):
	engine = app.backend.postgres.engine
	with session_scope() as s:
		seed_settings(s)                 # job records 30 days, audit 90, snapshots 7
	user = uuid.uuid4()
	now = dt.datetime.now()
	with engine.begin() as c:
		c.execute(text("insert into users (id, username, role, is_active, is_approved, created_at, "
		               "auth_type) values (:u, 'ret', 'operator', true, true, now(), 'local')"), {"u": user})
		for days, ip in ((40, "10.0.0.1"), (10, "10.0.0.2"), (1, "10.0.0.3")):
			c.execute(text(
				"insert into device_results (id, job_id, started_at, completed_at, device_ip, "
				"device_type, commands_sent, commands_verified, fetched_config, status, user_id) "
				"values (gen_random_uuid(), gen_random_uuid(), :t, :t, :ip, 'cisco_ios', 1, 1, "
				"'cfg', 'success', :u)"), {"t": now - dt.timedelta(days=days), "ip": ip, "u": user})
		for days in (100, 5):
			c.execute(text("insert into audit_log (id, timestamp, actor_username, action, success) "
			               "values (gen_random_uuid(), :t, 'ret', 'auth.login', true)"),
			          {"t": now - dt.timedelta(days=days)})

	status = retention.run_once(engine, now)
	counts = status["counts"]
	assert counts["device_result_retention"] == 1 and counts["audit_log_retention"] == 1
	assert counts["device_result_config_retention"] == 1        # the 10-day-old snapshot
	with engine.connect() as c:
		rows = dict(c.execute(text("select device_ip, fetched_config from device_results")).all())
		audit = c.execute(text("select count(*) from audit_log where actor_username = 'ret'")).scalar()
	assert rows == {"10.0.0.2": None, "10.0.0.3": "cfg"}       # 40 days gone, 10 days' config cleared
	assert audit == 1

	# System Settings -> Retention shows it
	assert retention.read_status() == status
	page = client_for(make_user(role="admin")).get("/admin/settings").data.decode()
	assert "Last clean-up " + now.isoformat(timespec="seconds").replace("T", " ") in page
	assert "1 device result and" in page and "1 audit entry removed" in page


def test_a_failure_is_recorded_and_shown(app, make_user, client_for, monkeypatch):
	def down(engine):
		raise RuntimeError("the database is down\nmore detail")
	monkeypatch.setattr(retention, "run_retention", down)
	with pytest.raises(RuntimeError):
		retention.run_once(app.backend.postgres.engine)
	assert retention.read_status()["ok"] is False
	page = client_for(make_user(role="admin")).get("/admin/settings").data.decode()
	assert "failed: the database is down" in page and "more detail" not in page
