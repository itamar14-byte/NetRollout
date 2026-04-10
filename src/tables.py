from db import Base

from datetime import datetime

from sqlalchemy import DateTime, String, Boolean, Integer, Uuid, ForeignKey
import uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship
from flask_login import UserMixin


class User(UserMixin, Base):
    __tablename__ = 'users'
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True,
                                          nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default='user', nullable=False)
    position: Mapped[str] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 nullable=False)
    otp_secret: Mapped[str] = mapped_column(String(255), nullable=True)

    security_profiles: Mapped[list["SecurityProfile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    inventory: Mapped[list["Inventory"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    variable_mappings: Mapped[list["VariableMapping"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list["RolloutSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")
    results: Mapped[list["DeviceResult"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")


class SecurityProfile(Base):
    __tablename__ = 'security_profiles'
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    enable_secret: Mapped[str] = mapped_column(String(255), nullable=True)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
                                               nullable=False)

    user: Mapped["User"] = relationship(back_populates="security_profiles")
    inventory: Mapped[list["Inventory"]] = relationship(back_populates="security_profile")


class Inventory(Base):
    __tablename__ = 'inventory'
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
                                               nullable=False)
    sec_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(
        "security_profiles.id"), nullable=True)

    security_profile: Mapped["SecurityProfile"] = relationship(
        back_populates="inventory")
    user: Mapped["User"] = relationship(back_populates="inventory")


class VariableMapping(Base):
    __tablename__ = 'variable_mappings'
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(64), nullable=True)
    # token to replace in _commands, in $$token$$ format
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    # device attribute name to substitute
    property_name: Mapped[str] = mapped_column(String(64), nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
                                               nullable=False)

    user: Mapped["User"] = relationship(back_populates="variable_mappings")


class RolloutSession(Base):
    __tablename__ = 'rollout_sessions'
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
                                                 nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
                                               nullable=False)

    user: Mapped["User"] = relationship(back_populates="sessions")


class DeviceResult(Base):
    __tablename__ = 'device_results'
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    device_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    device_type: Mapped[str] = mapped_column(String(64), nullable=False)
    commands_sent: Mapped[int] = mapped_column(Integer, nullable=False)
    commands_verified: Mapped[int| None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
                                               nullable=False)

    user: Mapped["User"] = relationship(back_populates="results")
