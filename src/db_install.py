from sqlalchemy.exc import SQLAlchemyError

from db import Base, engine
from tables import User

try:
	Base.metadata.create_all(bind=engine)
	print("DB Initialized")
except SQLAlchemyError as e:
	print(f"Initialization Error: {e}")

