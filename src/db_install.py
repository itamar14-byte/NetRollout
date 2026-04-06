from sqlalchemy.exc import SQLAlchemyError

from db import Base, engine, get_session
from werkzeug.security import generate_password_hash
from tables import User

try:
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
