import uuid
from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import (DateTime, String, Boolean, Integer, Uuid, Text,
                        ForeignKey, JSON, Table, Column, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .postgres_db import Base

var_mapping_to_devices = Table("var_mapping_to_devices",
                               Base.metadata,
                               Column("mapping_id", Uuid,
                                      ForeignKey("variable_mappings.id"),
                                      primary_key=True),
                               Column("device_id", Uuid, ForeignKey(
	                               "inventory.id"), primary_key=True),
                               )


class User(UserMixin, Base):
	__tablename__ = 'users'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	username: Mapped[str] = mapped_column(String(64), unique=True, index=True,
	                                      nullable=False)
	password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
	email: Mapped[str | None] = mapped_column(String(120), unique=True,
	                                     nullable=True)
	full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
	role: Mapped[str] = mapped_column(String(40), default='user',
	                                  nullable=False)
	position: Mapped[str] = mapped_column(String(64), nullable=True)
	is_active: Mapped[bool] = mapped_column(Boolean, default=False,
	                                        nullable=False)
	is_approved: Mapped[bool] = mapped_column(Boolean, default=False,
	                                          nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
	                                             nullable=False)
	otp_secret: Mapped[str] = mapped_column(String(255), nullable=True)

	auth_type: Mapped[str] = mapped_column(String(20), default="local",
	                                       nullable=False)
	ldap_server_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey(
		"ldap_servers.id", ondelete="SET NULL"), nullable=True)

	security_profiles: Mapped[list["SecurityProfile"]] = relationship(
		back_populates="user", cascade="all, delete-orphan")
	inventory: Mapped[list["Inventory"]] = relationship(
		back_populates="user", cascade="all, delete-orphan")
	variable_mappings: Mapped[list["VariableMapping"]] = relationship(
		back_populates="user", cascade="all, delete-orphan")
	results: Mapped[list["DeviceResult"]] = relationship(
		back_populates="user", cascade="all, delete-orphan")
	job_metadata: Mapped[list["JobMetadata"]] = relationship(
		back_populates="user",
		cascade="all, delete-orphan")
	property_definitions: Mapped[list["PropertyDefinition"]] = relationship(
		back_populates="user", cascade="all, delete-orphan")
	ldap_server: Mapped["LDAPServer | None"] = relationship(
		back_populates="users")


class SecurityProfile(Base):
	__tablename__ = 'security_profiles'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	label: Mapped[str] = mapped_column(String(64), nullable=False)
	username: Mapped[str] = mapped_column(String(64), nullable=False)
	password_secret: Mapped[str] = mapped_column(String(255), nullable=False)
	enable_secret: Mapped[str] = mapped_column(String(255), nullable=True)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="security_profiles")
	inventory: Mapped[list["Inventory"]] = relationship(
		back_populates="security_profile")


class Inventory(Base):
	__tablename__ = 'inventory'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	ip: Mapped[str] = mapped_column(String(64), nullable=False)
	device_type: Mapped[str] = mapped_column(String(64), nullable=False)
	port: Mapped[int] = mapped_column(Integer, nullable=False)
	label: Mapped[str] = mapped_column(String(64), nullable=False)
	var_maps: Mapped[dict | None] = mapped_column(JSON, nullable=True)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)
	sec_profile_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(
		"security_profiles.id"), nullable=True)

	security_profile: Mapped["SecurityProfile"] = relationship(
		back_populates="inventory")
	user: Mapped["User"] = relationship(back_populates="inventory")
	var_mappings: Mapped[list["VariableMapping"]] = \
		relationship(secondary=var_mapping_to_devices, back_populates="devices")


class VariableMapping(Base):
	__tablename__ = 'variable_mappings'
	__table_args__ = (UniqueConstraint('token', 'user_id'),)
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	label: Mapped[str] = mapped_column(String(64), nullable=True)
	# token to replace in _commands, in $$token$$ format
	token: Mapped[str] = mapped_column(String(64), nullable=False)
	# device attribute name to substitute
	property_name: Mapped[str] = mapped_column(String(64), nullable=False)
	# Optional positional argument
	index: Mapped[int | None] = mapped_column(Integer, nullable=True)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="variable_mappings")
	devices: Mapped[list["Inventory"]] = relationship(
		secondary=var_mapping_to_devices, back_populates="var_mappings",
		cascade="all, delete")


class DeviceResult(Base):
	__tablename__ = 'device_results'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
	started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
	completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
	device_ip: Mapped[str] = mapped_column(String(64), nullable=False)
	device_type: Mapped[str] = mapped_column(String(64), nullable=False)
	commands_sent: Mapped[int] = mapped_column(Integer, nullable=False)
	commands_verified: Mapped[int | None] = mapped_column(Integer,
	                                                      nullable=True)
	fetched_config: Mapped[str | None] = mapped_column(Text, nullable=True)
	status: Mapped[str] = mapped_column(String(64), nullable=False)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="results")


class JobMetadata(Base):
	__tablename__ = 'job_metadata'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
	commands: Mapped[list[str]] = mapped_column(JSON, nullable=False)
	comment: Mapped[str] = mapped_column(String(255), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime,
	                                             default=datetime.now,
	                                             nullable=False)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="job_metadata")


class AuditLog(Base):
	__tablename__ = 'audit_log'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
	                                            nullable=False, index=True)
	# Denormalized — survives user deletion (actor_id goes NULL, username stays)
	actor_id: Mapped[uuid.UUID | None] = mapped_column(
		Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
	actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
	# Dot-namespaced: "inventory.create", "auth.login", "rollout.start", etc.
	action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
	object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
	object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
	object_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
	success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
	ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
	detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PropertyDefinition(Base):
	__tablename__ = 'property_definition'
	__table_args__ = (UniqueConstraint('name', 'user_id'),)
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	name: Mapped[str] = mapped_column(String(64), nullable=False)
	label: Mapped[str] = mapped_column(String(64), nullable=False)
	icon: Mapped[str] = mapped_column(String(64), nullable=False)
	is_list: Mapped[bool] = mapped_column(Boolean, nullable=False,
	                                      default=False)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="property_definitions")


class LDAPServer(Base):
	__tablename__ = 'ldap_servers'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	name: Mapped[str] = mapped_column(String(64), nullable=False)
	host: Mapped[str] = mapped_column(String(255), nullable=False)
	port: Mapped[int] = mapped_column(Integer, nullable=False,
	                                  default=389)
	base_dn: Mapped[str] = mapped_column(String(255), nullable=False)
	cn_identifier: Mapped[str] = mapped_column(String(64),
	                                           nullable=False,
	                                           default='sAMAccountName')
	bind_type: Mapped[str] = mapped_column(String(20), nullable=False,
	                                       default='anonymous')
	bind_dn: Mapped[str | None] = mapped_column(String(255),
	                                            nullable=True)
	bind_password: Mapped[str | None] = mapped_column(String(255),
	                                                  nullable=True)
	use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False,
	                                      default=False)
	is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
	                                        default=True)

	users: Mapped[list["User"]] = relationship(back_populates="ldap_server")
	user_groups: Mapped[list["LDAPGroup"]] = relationship(
		back_populates="server", cascade="all, delete-orphan")


class LDAPGroup(Base):
	__tablename__ = 'ldap_groups'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	group_dn: Mapped[str] = mapped_column(String(512), nullable=False)
	label: Mapped[str] = mapped_column(String(128), nullable=False)
	role: Mapped[str] = mapped_column(String(40), nullable=False,
	                                  default='user')
	is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
	                                        default=True)

	ldap_server_id: Mapped[uuid.UUID] = mapped_column(Uuid,
	                                                  ForeignKey(
		                                                  "ldap_servers.id",
		                                                  ondelete="CASCADE"),
	                                                  nullable=False)
	server: Mapped["LDAPServer"] = relationship(back_populates="user_groups")
