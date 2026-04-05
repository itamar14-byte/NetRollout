import os
from contextlib import contextmanager

from sqlalchemy.orm import sessionmaker,DeclarativeBase
from sqlalchemy import create_engine

url = os.getenv("DATABASE_URL")
if not url:
	raise ValueError("DATABASE_URL is not set")
class Base(DeclarativeBase):
	pass
engine = create_engine(url)
sessionLocal = sessionmaker(bind=engine)

@contextmanager
def get_session():
	session = sessionLocal()
	try:
		yield session
		session.commit()
	except Exception:
		session.rollback()
	finally:
		session.close()