from db import Base

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

class User(Base):
	__tablename__ = 'users'
	id: Mapped[int] = mapped_column(primary_key=True)
	username: Mapped[str] = mapped_column(String(64),unique=True,index=True,
	                                      nullable=False)
	password_hash: Mapped[str] = mapped_column(String(255),nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime,default=datetime.now)


