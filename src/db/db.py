import os
from contextlib import contextmanager

from sqlalchemy.orm import sessionmaker,DeclarativeBase
from sqlalchemy import create_engine


def construct_url():
	host = os.getenv("DB_HOST","localhost")
	port = os.getenv("DB_PORT","5432")
	name = os.getenv("DB_NAME","rollout_db")
	user = os.getenv("DB_USER","dbadmin")
	password = os.getenv("DB_PASSWORD","Pass123")
	return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

def build_engine():
	url = os.getenv("DATABASE_URL") or construct_url()
	schema = os.getenv("DB_SCHEMA")
	connect_args = {"options": f"-c search_path={schema}"} if schema else {}
	return create_engine(url, connect_args=connect_args)


class Base(DeclarativeBase):
	pass
engine = build_engine()
session_local = sessionmaker(bind=engine)

@contextmanager
def get_session():
	session = session_local()
	try:
		yield session
		session.commit()
	except Exception:
		session.rollback()
		raise
	finally:
		session.close()