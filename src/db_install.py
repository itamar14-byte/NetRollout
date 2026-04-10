from sqlalchemy.exc import SQLAlchemyError

from db import Base, engine, get_session
from werkzeug.security import generate_password_hash
from tables import (User, Inventory, SecurityProfile, VariableMapping,
                    RolloutSession, DeviceResult)

try:
	# NOTE: create_all only creates tables that don't exist — it never alters existing ones.
	# During development, drop changed tables manually before running this script.
	# Phase 4: replace with Alembic migrations.
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
