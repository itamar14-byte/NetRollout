"""NetRollout's database tables (SQLAlchemy models). The schema itself is
the migrations' (src/db/alembic/versions) - a change here needs a new
revision. Each user's devices, profiles, mappings and properties are their
own; global devices are shared."""
import uuid
from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import (DateTime, String, Boolean, Integer, Uuid, Text,
                        ForeignKey, Index, JSON, Table, Column, UniqueConstraint,
                        false)
from sqlalchemy.orm import Mapped, mapped_column, relationship, DeclarativeBase


class Base(DeclarativeBase):
	"""Every NetRollout table (Base.metadata: the migrations' target)."""

# which devices a variable mapping applies to (many to many)
var_mapping_to_devices = Table("var_mapping_to_devices",
                               Base.metadata,
                               Column("mapping_id", Uuid,
                                      ForeignKey("variable_mappings.id"),
                                      primary_key=True),
                               Column("device_id", Uuid, ForeignKey(
	                               "inventory.id"), primary_key=True),
                               )


class User(UserMixin, Base):
	"""A person who signs in: local (a password, 2FA) or from LDAP (the
	directory checks the password). Owns their devices, profiles, mappings,
	properties and rollouts."""
	__tablename__ = 'users'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	username: Mapped[str] = mapped_column(String(64), unique=True, index=True,
	                                      nullable=False)
	password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
	email: Mapped[str | None] = mapped_column(String(120), unique=True,
	                                     nullable=True)
	full_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
	# "admin" or "operator" — the app only ever checks for "admin"
	role: Mapped[str] = mapped_column(String(40), default='operator',
	                                  nullable=False)
	position: Mapped[str | None] = mapped_column(String(64), nullable=True)
	is_active: Mapped[bool] = mapped_column(Boolean, default=False,
	                                        nullable=False)
	is_approved: Mapped[bool] = mapped_column(Boolean, default=False,
	                                          nullable=False)
	created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
	                                             nullable=False)
	# encrypted; None until 2FA is enrolled (LDAP users and the factory admin never)
	otp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
	# The seeded admin and a user after an admin reset: every page redirects
	# to the change-password page until they pick their own password
	must_change_password: Mapped[bool] = mapped_column(
		Boolean, default=False, server_default=false(), nullable=False)

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
	# the database deletes them with the user (ON DELETE CASCADE)
	device_attributes: Mapped[list["DeviceAttribute"]] = relationship(
		back_populates="user", cascade="all, delete-orphan",
		passive_deletes=True)
	ldap_server: Mapped["LDAPServer | None"] = relationship(
		back_populates="users")


class SecurityProfile(Base):
	"""Device login credentials, encrypted (password, enable secret), shared
	by the devices assigned to it."""
	__tablename__ = 'security_profiles'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	# Optional: the UI falls back to the username when no label is given
	label: Mapped[str | None] = mapped_column(String(64), nullable=True)
	username: Mapped[str] = mapped_column(String(64), nullable=False)
	password_secret: Mapped[str] = mapped_column(String(255), nullable=False)
	enable_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="security_profiles")
	inventory: Mapped[list["Inventory"]] = relationship(
		back_populates="security_profile")


class Inventory(Base):
	"""A device: where it is (ip:port), what it is (Netmiko device type), its
	login (a security profile) and its system attribute values for variable
	mappings (var_maps: the system properties only, shared by everyone who
	sees it; each user's custom values are DeviceAttribute rows)."""
	__tablename__ = 'inventory'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	ip: Mapped[str] = mapped_column(String(64), nullable=False)
	device_type: Mapped[str] = mapped_column(String(64), nullable=False)
	port: Mapped[int] = mapped_column(Integer, nullable=False)
	label: Mapped[str] = mapped_column(String(64), nullable=False)
	var_maps: Mapped[dict | None] = mapped_column(JSON, nullable=True)

	# Global devices are visible to (and rollout-able by) all users;
	# only admins may edit or delete them
	is_global: Mapped[bool] = mapped_column(Boolean, default=False,
	                                        server_default=false(),
	                                        nullable=False)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)
	sec_profile_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey(
		"security_profiles.id"), nullable=True)

	security_profile: Mapped["SecurityProfile | None"] = relationship(
		back_populates="inventory")
	user: Mapped["User"] = relationship(back_populates="inventory")
	var_mappings: Mapped[list["VariableMapping"]] = \
		relationship(secondary=var_mapping_to_devices, back_populates="devices")
	# every user's custom values on it (the database deletes them with it)
	custom_attributes: Mapped[list["DeviceAttribute"]] = relationship(
		back_populates="device", cascade="all, delete-orphan",
		passive_deletes=True)


class DeviceAttribute(Base):
	"""One user's value of one of their custom properties on a device (a text
	or a list of texts) - theirs alone: another user's property of the same
	name has its own row."""
	__tablename__ = 'device_attributes'
	__table_args__ = (UniqueConstraint('device_id', 'user_id', 'name'),)
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	device_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(
		"inventory.id", ondelete="CASCADE"), nullable=False)
	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey(
		"users.id", ondelete="CASCADE"), nullable=False, index=True)
	# the property's name (PropertyDefinition.name of that user)
	name: Mapped[str] = mapped_column(String(64), nullable=False)
	value: Mapped[str | list[str]] = mapped_column(JSON, nullable=False)

	device: Mapped["Inventory"] = relationship(back_populates="custom_attributes")
	user: Mapped["User"] = relationship(back_populates="device_attributes")


class VariableMapping(Base):
	"""A token in rollout commands ($$token$$) replaced, per device, by one of
	its attribute values - for the devices it's assigned to."""
	__tablename__ = 'variable_mappings'
	__table_args__ = (UniqueConstraint('token', 'user_id'),)
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	label: Mapped[str | None] = mapped_column(String(64), nullable=True)
	# token to replace in _commands, in $$token$$ format
	token: Mapped[str] = mapped_column(String(64), nullable=False)
	# device attribute name to substitute
	property_name: Mapped[str] = mapped_column(String(64), nullable=False)
	# Optional positional argument
	index: Mapped[int | None] = mapped_column(Integer, nullable=True)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="variable_mappings")
	# No delete cascade: on a secondary relationship it deletes the Inventory
	# rows themselves. Join-table rows are removed automatically.
	devices: Mapped[list["Inventory"]] = relationship(
		secondary=var_mapping_to_devices, back_populates="var_mappings")


class DeviceResult(Base):
	"""One device's outcome in one rollout (job_id): status, commands sent and
	verified, the fetched config, anything a person must do."""
	__tablename__ = 'device_results'
	# Results pages a user's jobs (user_id, grouped by job_id); a job's page
	# reads its devices by job_id alone
	__table_args__ = (Index('ix_device_results_user_id_job_id', 'user_id', 'job_id'),
	                  Index('ix_device_results_job_id', 'job_id'))
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
	started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
	completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
	device_ip: Mapped[str] = mapped_column(String(64), nullable=False)
	# ip:port identifies the target — several devices can share an IP
	# (NAT / port forwarding). Rows from before this column default to SSH.
	device_port: Mapped[int] = mapped_column(Integer, nullable=False,
	                                         server_default="22")
	device_type: Mapped[str] = mapped_column(String(64), nullable=False)
	commands_sent: Mapped[int] = mapped_column(Integer, nullable=False)
	commands_verified: Mapped[int | None] = mapped_column(Integer,
	                                                      nullable=True)
	fetched_config: Mapped[str | None] = mapped_column(Text, nullable=True)
	status: Mapped[str] = mapped_column(String(64), nullable=False)
	# What only a person can resolve on the device (e.g. "the change is live
	# but NOT saved — save it on the device"); shown on the Results page
	action_needed: Mapped[str | None] = mapped_column(Text, nullable=True)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="results")


class JobMetadata(Base):
	"""A rollout's commands and comment, as submitted (its results are
	device_results with the same job_id)."""
	__tablename__ = 'job_metadata'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	job_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
	commands: Mapped[list[str]] = mapped_column(JSON, nullable=False)
	comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
	created_at: Mapped[datetime] = mapped_column(DateTime,
	                                             default=datetime.now,
	                                             nullable=False)

	user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"),
	                                           nullable=False)

	user: Mapped["User"] = relationship(back_populates="job_metadata")


class AuditLog(Base):
	"""Who did what, when, from where - append-only (the nightly clean-up
	removes rows past the audit retention)."""
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
	"""A device attribute a user defined (name, label, icon; one value or a
	list) - what variable mappings substitute."""
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
	"""A directory people sign in through (its bind password encrypted)."""
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
	"""A directory group whose members may sign in, and the role they get."""
	__tablename__ = 'ldap_groups'
	id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True,
	                                      default=uuid.uuid4)
	group_dn: Mapped[str] = mapped_column(String(512), nullable=False)
	label: Mapped[str] = mapped_column(String(128), nullable=False)
	role: Mapped[str] = mapped_column(String(40), nullable=False,
	                                  default='operator')
	is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
	                                        default=True)

	ldap_server_id: Mapped[uuid.UUID] = mapped_column(Uuid,
	                                                  ForeignKey(
		                                                  "ldap_servers.id",
		                                                  ondelete="CASCADE"),
	                                                  nullable=False)
	server: Mapped["LDAPServer"] = relationship(back_populates="user_groups")


class SystemSetting(Base):
	"""The runtime value of one system setting — the only runtime source.
	install() seeds a row for every setting at each start (install value from
	the env if valid, else the default) and never overwrites one; admins
	change it in System Settings (src/db/settings.py is the registry of what
	settings exist, their defaults and rules)."""
	__tablename__ = 'system_settings'
	key: Mapped[str] = mapped_column(String(64), primary_key=True)
	value: Mapped[object] = mapped_column(JSON, nullable=False)
	updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now,
	                                             onupdate=datetime.now,
	                                             nullable=False)
	updated_by: Mapped[uuid.UUID | None] = mapped_column(
		Uuid, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
