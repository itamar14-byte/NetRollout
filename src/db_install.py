from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from db import Base, engine, get_session
from werkzeug.security import generate_password_hash
from tables import (User, Inventory, SecurityProfile, VariableMapping,
                    RolloutSession, DeviceResult, var_mapping_to_devices,
                    JobMetadata, AuditLog)

def install():
	try:
		# NOTE: create_all only creates tables that don't exist — it never alters existing ones.
		# During development, drop changed tables manually before running this script.
		# Phase 4: replace with Alembic migrations.
		with engine.connect() as conn:
			conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron;"))
			conn.execute(text("""
			    DO $$
			    BEGIN
			        PERFORM cron.unschedule('job_metadata_retention');
			    EXCEPTION WHEN OTHERS THEN NULL;
			    END;
			    $$;
			    SELECT cron.schedule(
			        'job_metadata_retention',
			        '0 3 * * *',
			        $q$DELETE FROM job_metadata WHERE created_at < NOW() - INTERVAL '7 days'$q$
			    );
			"""))
			conn.execute(text("""
			    DO $$
			    BEGIN
			        PERFORM cron.unschedule('device_result_retention');
			    EXCEPTION WHEN OTHERS THEN NULL;
			    END;
			    $$;
			    SELECT cron.schedule(
			        'device_result_retention',
			        '0 3 * * *',
			        $q$DELETE FROM device_results WHERE created_at < NOW() - INTERVAL '30 days'$q$
			    );
			"""))
			conn.execute(text("""
			    DO $$
			    BEGIN
			        PERFORM cron.unschedule('audit_log_retention');
			    EXCEPTION WHEN OTHERS THEN NULL;
			    END;
			    $$;
			    SELECT cron.schedule(
			        'audit_log_retention',
			        '0 3 * * *',
			        $q$DELETE FROM audit_log WHERE timestamp < NOW() - INTERVAL '90 days'$q$
			    );
			"""))
			conn.commit()

		Base.metadata.create_all(bind=engine)


		with get_session() as session:
			if not session.query(User).filter_by(username="admin").first():
				user = User(username="admin",
				            password_hash=generate_password_hash("admin"),
				            email="example@test.com",
				            full_name="Net Rollout",
				            role="admin",
				            is_active=True,
				            is_approved=True)
				session.add(user)
				session.flush()
		print("DB Initialized")
	except SQLAlchemyError as e:
		print(f"Initialization Error: {e}")
