from db import Base

from datetime import datetime

from sqlalchemy import DateTime, String, Boolean, Uuid
import uuid
from sqlalchemy.orm import Mapped, mapped_column
from flask_login import UserMixin

class User(UserMixin, Base):
	__tablename__ = 'users'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
	username: Mapped[str] = mapped_column(String(64),unique=True,index=True,
	                                      nullable=False)
	password_hash: Mapped[str] = mapped_column(String(255),nullable=False)
	email: Mapped[str] = mapped_column(String(120), unique=True,nullable=False)
	full_name: Mapped[str] = mapped_column(String(120), nullable=False)
	role: Mapped[str] = mapped_column(String(40), default='user', nullable=False)
	position: Mapped[str] = mapped_column(String(64),nullable=True)
	is_active: Mapped[bool] = mapped_column(Boolean, default=False,
	                                      nullable=False)
	is_approved: Mapped[bool] = mapped_column(Boolean, default=False,
	                                      nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime,default=datetime.now,
	                                             nullable=False)
	otp_secret: Mapped[str] = mapped_column(String(32),nullable=True)


