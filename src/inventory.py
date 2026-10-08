"""The device inventory's rules (web app): which devices a user sees and may
edit (their own and the global ones), devices sharing an endpoint, a
device's attribute values, and whether a device is reachable from the
NetRollout server.

Attributes: the system properties' values are the device's own (var_maps),
shared by everyone who sees it and set by who may edit it; a custom
property is one user's, and so are its values (DeviceAttribute rows) - any
user who sees a device sets their own. attributes() is the one way to read
them: what a user's mappings and rollouts substitute.

Reachability: one fast TCP connect to ip:port - the management endpoint a
push would use, from the server's point of view (the one that matters).
Results are cached briefly in Redis, so page loads stay cheap and "recent"
means at most CACHE_TTL seconds old."""
from __future__ import annotations   # type hints are never evaluated

import hmac
import json
import socket
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from csv import DictReader
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Iterable, TypedDict, cast, Any

import redis
from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import Session, selectinload

from src.db.connections import REDIS_UNAVAILABLE
from src.db.tables import DeviceAttribute, Inventory, SecurityProfile, User
from src.encryption import decrypt, encrypt
from src.rollout.engine import endpoint, Device
from src.rollout.inputs import InputParser


PROBE_TIMEOUT = 2.0   # seconds; single attempt — this is a status hint
CACHE_TTL = 60        # seconds
MAX_PARALLEL = 32

Target = tuple[str, int]      # (ip, port)

# The properties every device has (their values: the device's var_maps)
SYSTEM_PROPERTIES: list[dict[str, Any]] = [
	{"name": "hostname", "label": "Hostname", "icon": "bi-type-h1",
	 "is_list": False},
	{"name": "loopback_ip", "label": "Loopback IP", "icon": "bi-hdd-network",
	 "is_list": False},
	{"name": "asn", "label": "ASN", "icon": "bi-diagram-3", "is_list": False},
	{"name": "mgmt_vrf", "label": "Management VRF", "icon": "bi-box",
	 "is_list": False},
	{"name": "mgmt_interface", "label": "Management Interface",
	 "icon": "bi-ethernet", "is_list": False},
	{"name": "site", "label": "Site", "icon": "bi-geo-alt", "is_list": False},
	{"name": "domain", "label": "Domain", "icon": "bi-globe2",
	 "is_list": False},
	{"name": "timezone", "label": "Timezone", "icon": "bi-clock",
	 "is_list": False},
	{"name": "vrfs", "label": "VRFs", "icon": "bi-layers", "is_list": True},
]
SYSTEM_NAMES = frozenset(p["name"] for p in SYSTEM_PROPERTIES)

AttrValue = str | list[str]


class Reach(TypedDict):
	"""One target's state, as cached and as the pages get it."""
	reachable: bool
	checked_at: str               # ISO 8601, UTC


def probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
	"""One TCP connect, no retry.

	:param timeout: seconds before an unanswered connect counts as down
	:returns: whether ip:port accepted the connection (closed at once)"""
	try:
		with socket.create_connection((ip, int(port)), timeout=timeout):
			return True
	except (OSError, ValueError):
		return False


def _cache_key(target: Target) -> str:
	""":returns: the target's Redis key"""
	return f"reach:{target[0]}:{target[1]}"


class ReachabilityChecker:
	"""Probes targets in parallel, caching the answers in Redis."""

	def __init__(self, redis_client: Callable[[], redis.Redis],
	             ttl: int | Callable[[], int] = CACHE_TTL,
	             prober: Callable[[str, int], bool] = probe):
		""":param redis_client: returns the client in use now - a Redis switch
		 replaces it, so it's resolved on every use
		:param ttl: seconds a result is reused - or a callable (the System
		 Setting), read each time results are cached
		:param prober: probes one target (a fake in tests)"""
		self._redis = redis_client
		self._ttl = ttl
		self._probe = prober

	def check(self, targets: Iterable[tuple[str, int | str]], refresh: bool = False,
	          recheck_unreachable: bool = False) -> dict[Target, Reach]:
		"""Whether each target is reachable, from the cache where it's fresh.

		:param targets: (ip, port) pairs; the port may come as text
		:param refresh: ignore the cache and probe everything (Recheck)
		:param recheck_unreachable: trust cached "reachable" results but
		 re-probe cached failures — used before blocking a rollout, so a
		 device that just came back isn't blocked by a stale result
		:returns: every target's state, by (ip, port)"""
		wanted = {(ip, int(port)) for ip, port in targets}
		results = {} if refresh else self._cached(wanted)
		if recheck_unreachable:
			results = {t: r for t, r in results.items() if r["reachable"]}
		todo = sorted(t for t in wanted if t not in results)
		if todo:
			now = datetime.now(timezone.utc).isoformat()
			with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(todo))) as ex:
				outcomes = ex.map(lambda t: self._probe(*t), todo)
				fresh: dict[Target, Reach] = {t: {"reachable": ok, "checked_at": now}
				                              for t, ok in zip(todo, outcomes)}
			self._store(fresh)
			results.update(fresh)
		return results

	def _cached(self, targets: set[Target]) -> dict[Target, Reach]:
		""":returns: the cached states of these targets ({} when Redis is
		 down: everything is probed then)"""
		if not targets:
			return {}
		ordered = sorted(targets)
		try:
			# redis-py types a reply as maybe-awaitable; this client is sync
			raw = cast(list[bytes | None], self._redis().mget([_cache_key(t) for t in ordered]))
		except REDIS_UNAVAILABLE:
			return {}
		return {t: json.loads(v) for t, v in zip(ordered, raw) if v}

	def _current_ttl(self) -> int:
		""":returns: seconds a result is reused now (the default when the
		 setting can't be read)"""
		if not callable(self._ttl):
			return self._ttl
		try:
			return int(self._ttl())
		except Exception:   # settings unreachable: the default, not an error
			return CACHE_TTL

	def _store(self, results: dict[Target, Reach]) -> None:
		"""Cache fresh results (skipped when Redis is down)."""
		ttl = self._current_ttl()
		try:
			pipe = self._redis().pipeline()
			for t, r in results.items():
				pipe.setex(_cache_key(t), ttl, json.dumps(r))
			pipe.execute()
		except REDIS_UNAVAILABLE:
			pass


def visible_devices_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
	"""Devices a user may see and roll out to: their own plus all global ones."""
	return or_(Inventory.user_id == user_id, Inventory.is_global.is_(True))


def query_visible_devices(db_session: Session, user_id: uuid.UUID) -> list[Inventory]:
	"""Visible devices with the relationships templates and rollout need,
	preloaded so rows survive expunge."""
	return (db_session.query(Inventory)
	        .filter(visible_devices_clause(user_id))
	        .options(selectinload(Inventory.security_profile),
	                 selectinload(Inventory.var_mappings))
	        .order_by(Inventory.label)
	        .all())


def can_edit_device(device: Inventory, user: User) -> bool:
	"""Owners edit their own devices; any admin may edit a global device."""
	return device.user_id == user.id or (
			device.is_global and user.role == "admin")


def same_endpoint_devices(db_session: Session, user_id: uuid.UUID, ip: str,
                          port: int | str,
                          exclude_id: uuid.UUID | None = None) -> list[Inventory]:
	"""Visible devices (own + global) already using this ip:port. Overlap is
	legitimate (NAT, VRFs, port-forwarded labs), so callers warn, never
	block; other users' private devices are never considered.

	:param exclude_id: the device being edited (it doesn't clash with itself)"""
	query = db_session.query(Inventory).filter(
		visible_devices_clause(user_id),
		Inventory.ip == ip, Inventory.port == int(port))
	if exclude_id is not None:
		query = query.filter(Inventory.id != exclude_id)
	return query.order_by(Inventory.label).all()


def same_endpoint_warning(devices: Sequence[Inventory], ip: str,
                          port: int | str) -> str | None:
	"""One warning naming the devices that share ip:port. Build it while the
	DB session is open (it reads labels); flash it after the success message.

	:returns: the warning; None when no device shares it"""
	if not devices:
		return None
	names = ", ".join(f"{d.label} (global)" if d.is_global else d.label
	                  for d in devices[:5]) + (", …" if len(devices) > 5 else "")
	return (f"{endpoint(ip, port)} is already used by {names}. That's fine for NAT, "
	        f"VRFs or port-forwarded labs, but they can't be in the same "
	        f"rollout.")


def partition_devices(devices: Sequence[Inventory]) -> tuple[list[Inventory], list[Inventory]]:
	"""Split visible devices into (global_devices, my_devices)."""
	global_devices = [d for d in devices if d.is_global]
	my_devices = [d for d in devices if not d.is_global]
	return global_devices, my_devices


# ── attribute values ────────────────────────────────────────────────────────

def system_values(device: Inventory) -> dict[str, AttrValue]:
	""":returns: the device's system property values (its var_maps)"""
	return {k: v for k, v in (device.var_maps or {}).items()
	        if k in SYSTEM_NAMES}


def attributes(db_session: Session, devices: Iterable[Inventory],
               user_id: uuid.UUID) -> dict[uuid.UUID, dict[str, AttrValue]]:
	"""Each device's attribute values as one user sees them: the device's
	system values and the user's own custom values (never another user's) -
	what their mappings and rollouts substitute. One query, however many
	devices.

	:param devices: saved rows (they have ids)
	:returns: {device id: {property name: a text or a list of texts}}"""
	values = {d.id: system_values(d) for d in devices}
	if values:
		rows = db_session.query(DeviceAttribute.device_id, DeviceAttribute.name,
		                        DeviceAttribute.value).filter(
			DeviceAttribute.user_id == user_id,
			DeviceAttribute.device_id.in_(list(values)))
		for device_id, name, value in rows:
			values[device_id][name] = value
	return values


def attributes_of(db_session: Session, device: Inventory,
                  user_id: uuid.UUID) -> dict[str, AttrValue]:
	""":returns: one device's attribute values as the user sees them (see
	 attributes())"""
	return attributes(db_session, [device], user_id)[device.id]


def parse_value(raw: str | list[str], is_list: bool) -> AttrValue | None:
	"""A value as typed in the edit form or a CSV cell: a list property's
	split on commas, its items stripped.

	:returns: the value; None when blank (no value)"""
	if is_list:
		parts = raw if isinstance(raw, list) else raw.split(",")
		items = [v.strip() for v in parts if v.strip()]
		return items or None
	text = raw if isinstance(raw, str) else ", ".join(raw)
	return text.strip() or None


def form_values(form: Mapping[str, str],
                properties: Iterable[dict[str, Any]]) -> dict[str, AttrValue | None]:
	"""The attr_<name> fields a form sent, for these properties: a field
	naming no such property is ignored, a field not sent isn't in the answer
	(its value stays as it is).

	:param properties: {name, is_list} each
	:returns: {property name: its value, None for a blank field}"""
	by_name = {p["name"]: p for p in properties}
	values: dict[str, AttrValue | None] = {}
	for key, raw in form.items():
		prop = by_name.get(key[5:]) if key.startswith("attr_") else None
		if prop is not None:
			values[prop["name"]] = parse_value(raw, prop["is_list"])
	return values


def set_system_values(device: Inventory,
                      values: Mapping[str, AttrValue | None]) -> None:
	"""Set the device's own (system) values - for who may edit it. Names
	that aren't system properties are ignored.

	:param values: {name: value}; None removes the value"""
	var_maps = system_values(device)
	for name, value in values.items():
		if name not in SYSTEM_NAMES:
			continue
		if value:
			var_maps[name] = value
		else:
			var_maps.pop(name, None)
	device.var_maps = var_maps or None


def set_custom_values(db_session: Session, device: Inventory,
                      user_id: uuid.UUID, values: Mapping[str, AttrValue | None],
                      own: Iterable[str]) -> None:
	"""Set one user's custom values on a device; other users' are never
	touched.

	:param device: a saved row, or one just added to the session
	:param values: {name: value}; None removes the user's value
	:param own: the names of the user's own properties - any other name is
	 ignored"""
	own_names = set(own)
	wanted = {n: v for n, v in values.items() if n in own_names}
	if not wanted:
		return
	existing = {a.name: a for a in db_session.query(DeviceAttribute).filter(
		DeviceAttribute.device_id == device.id,
		DeviceAttribute.user_id == user_id,
		DeviceAttribute.name.in_(list(wanted)))} if device.id else {}
	for name, value in wanted.items():
		row = existing.get(name)
		if not value:
			if row is not None:
				db_session.delete(row)
		elif row is not None:
			row.value = value
		else:
			db_session.add(DeviceAttribute(device=device, user_id=user_id,
			                               name=name, value=value))


def drop_other_users_values(db_session: Session, device: Inventory) -> None:
	"""Remove everyone's values on a device but its owner's (it was made
	local: nobody else can see it - their bindings go too)."""
	db_session.query(DeviceAttribute).filter(
		DeviceAttribute.device_id == device.id,
		DeviceAttribute.user_id != device.user_id).delete(synchronize_session=False)


def delete_property_values(db_session: Session, user_id: uuid.UUID,
                           name: str) -> None:
	"""Remove a user's values of one of their properties, on every device
	(the property is being deleted)."""
	db_session.query(DeviceAttribute).filter(
		DeviceAttribute.user_id == user_id,
		DeviceAttribute.name == name).delete(synchronize_session=False)


# ── the CSV import ──────────────────────────────────────────────────────────

@dataclass
class ImportReport:
	"""Outcome of a CSV import. `notices` are (category, message) pairs for
	the UI: "success" / "info" / "warning"."""
	devices: list[Device] = field(default_factory=list)
	errors: list[str] = field(default_factory=list)
	notices: list[tuple[str, str]] = field(default_factory=list)
	created_profiles: list[tuple[uuid.UUID, str]] = field(default_factory=list)


@dataclass
class _Columns:
	"""An import CSV's columns, sorted by what they become."""
	props: dict[str, dict[str, Any]]   # CSV header → property definition
	credentials: list[str]   # credential headers present
	ignored: list[str]       # headers that are neither


def _classify_columns(headers: list[str], properties: list[dict[str, Any]]) -> _Columns:
	"""Match headers to properties by name or label, case-insensitively,
	treating spaces and underscores alike ("Loopback IP" → loopback_ip)."""
	def norm(name: str) -> str:
		return name.strip().lower().replace(" ", "_")
	lookup: dict[str, dict[str, Any]] = {}
	for prop in properties:
		lookup.setdefault(norm(prop["name"]), prop)
		if prop.get("label"):
			lookup.setdefault(norm(prop["label"]), prop)
	core = InputParser.CORE_KEYS - set(InputParser.CREDENTIAL_KEYS)
	columns = _Columns(props={}, credentials=[], ignored=[])
	for header in headers:
		if header in core:
			continue
		if header in InputParser.CREDENTIAL_KEYS:
			columns.credentials.append(header)
		elif norm(header) in lookup:
			columns.props[header] = lookup[norm(header)]
		else:
			columns.ignored.append(header)
	return columns


def _cell_values(extra: dict[str, str | list[str]],
                 columns: _Columns) -> dict[str, AttrValue]:
	"""A row's attribute cells by property name, with the edit modal's
	rules: list properties split on commas, empty values skipped."""
	values: dict[str, AttrValue] = {}
	for header, prop in columns.props.items():
		value = parse_value(extra.get(header) or "", bool(prop.get("is_list")))
		if value:
			values[prop["name"]] = value
	return values


class _ProfileResolver:
	"""Credential columns → security profiles, for one import.
	A row reuses the user's profile with exactly the same username, password
	and enable secret; otherwise a new profile is created with a unique label
	and later rows with the same credentials share it. Existing profiles are
	never modified. Matching decrypts the user's own profiles in memory only —
	the same exposure as a rollout; nothing is logged or stored in clear."""

	def __init__(self, user_id: uuid.UUID, db_session: Session):
		self._user_id, self._db = user_id, db_session
		self._known: list[tuple[tuple[str, str, str], SecurityProfile]] = []
		for p in db_session.query(SecurityProfile).filter_by(user_id=user_id):
			secret = decrypt(p.enable_secret) if p.enable_secret else ""
			self._known.append(
				((p.username, decrypt(p.password_secret), secret), p))
		self._labels = {p.label for _, p in self._known if p.label}
		self._created: list[SecurityProfile] = []
		self._assigned: Counter[SecurityProfile] = Counter()
		self._warnings: list[str] = []

	def resolve(self, username: str, password: str,
	            secret: str) -> SecurityProfile:
		""":returns: the user's profile with exactly these credentials - one
		 that existed, one this import created, or a new one"""
		creds = (username, password, secret or "")
		profile = next((p for known, p in self._known
		                if _same_credentials(known, creds)), None)
		if profile is None:
			profile = self._create(creds)
		self._assigned[profile] += 1
		return profile

	def created(self) -> list[tuple[uuid.UUID, str]]:
		"""(id, label) of the profiles this import created — after flush."""
		return [(p.id, _name(p)) for p in self._created]   # always labelled

	def summary(self) -> list[tuple[str, str]]:
		""":returns: (category, message) per profile used, then the warnings -
		 for the page's notices"""
		lines = []
		for profile, count in self._assigned.items():
			devices = f"{count} device{'s' if count != 1 else ''}"
			if profile in self._created:
				lines.append(("success", f"{devices} assigned to new profile "
				                         f"'{_name(profile)}'"))
			else:
				lines.append(("info", f"{devices} assigned to existing profile "
				                      f"'{_name(profile)}'"))
		return lines + [("warning", w) for w in self._warnings]

	def _create(self, creds: tuple[str, str, str]) -> SecurityProfile:
		"""A new profile for these credentials (encrypted), with a warning when
		another of the user's profiles has the same username."""
		username, password, secret = creds
		label = self._unique_label(username)
		profile = SecurityProfile(
			label=label, username=username,
			password_secret=encrypt(password),
			enable_secret=encrypt(secret) if secret else None,
			user_id=self._user_id)
		self._db.add(profile)
		# Same username, different credentials: a typo or an old password in
		# the CSV is the likely cause, so say so instead of hiding it
		for (other_user, other_pw, _), other in self._known:
			if other_user == username:
				what = ("password" if not hmac.compare_digest(
					other_pw.encode(), password.encode()) else "enable secret")
				self._warnings.append(
					f"Created profile '{label}' — your profile "
					f"'{_name(other)}' also uses username {username}, with a "
					f"different {what}. If the CSV has a typo or an old "
					f"password, fix the row or move the devices to "
					f"'{_name(other)}'.")
				break
		self._known.append((creds, profile))
		self._labels.add(label)
		self._created.append(profile)
		return profile

	def _unique_label(self, username: str) -> str:
		""":returns: "<username> · CSV import 7 Oct", numbered when taken"""
		today = date.today()
		base = f"{username[:36]} · CSV import {today.day} {today:%b}"
		label, n = base, 2
		while label in self._labels:
			label, n = f"{base} ({n})", n + 1
		return label


def _same_credentials(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
	"""Equal credentials - every field compared (no early exit), each in
	constant time: how long it takes says nothing about the secrets."""
	return all([hmac.compare_digest(x.encode(), y.encode())
	            for x, y in zip(a, b)])


def _name(profile: SecurityProfile) -> str:
	""":returns: how the page names a profile (its label, else its username)"""
	return profile.label or profile.username


def import_csv(parser: InputParser, device_path: str, user_id: uuid.UUID,
               db_session: Session, label: str | None = None,
               properties: list[dict[str, Any]] | None = None,
               create_profiles: bool = False) -> "ImportReport":
	"""Import a devices CSV into the user's inventory (web app).
	The same CSV the CLI takes: core columns make the device, attribute
	columns that name one of the user's properties (system or custom, by
	name or label) become its attribute values (a custom property's: the
	importing user's own), and credential columns
	become security profiles when `create_profiles` is set. Other columns
	are reported, never dropped silently. No reachability check.
	:param device_path: the uploaded CSV
	:param user_id: whose inventory it goes into
	:param db_session: the rows are added to it; the caller commits
	:param label: applied to every device (form label > row label > IP)
	:param properties: the user's property definitions
	 ({name, label, is_list}) — see WebServices.get_property_defs
	:param create_profiles: credential columns become security profiles
	:returns: what was imported, the rows' errors and notices for the page"""
	report = ImportReport()
	device_path = device_path.strip('"')
	if not parser.validator.validate_file_extension(device_path, "csv"):
		report.errors.append("File must be a .csv")
		return report
	try:
		with open(device_path, "r", encoding="utf-8-sig") as file:
			reader = DictReader(file)
			headers = [h.strip() for h in (reader.fieldnames or []) if h]
			missing = [k for k in ("ip", "device_type", "port")
			           if k not in headers]
			if missing:
				report.errors.append(
					f"Missing required columns: {', '.join(missing)}")
				return report
			rows = list(reader)
	except UnicodeDecodeError:
		report.errors.append("The CSV must be UTF-8 text")
		return report
	except OSError as e:
		parser.logger.notify(f"Reading CSV failed: {e}", "red")
		report.errors.append("The CSV file could not be read")
		return report

	columns = _classify_columns(headers, properties or [])
	if columns.ignored:
		report.notices.append(("warning",
			f"Ignored columns: {', '.join(columns.ignored)} — not a property; "
			f"create it under Properties first"))
	if columns.credentials and not create_profiles:
		report.notices.append(("info",
			"Credential columns were not imported (profile creation was "
			"off) — assign a security profile to the devices"))

	devices, report.errors = parser.prepare_devices(
		rows, require_credentials=False, check_reachable=False)
	profiles = (_ProfileResolver(user_id, db_session)
	            if create_profiles and columns.credentials else None)
	half_credentials: list[str] = []
	own = [p["name"] for p in properties or [] if p["name"] not in SYSTEM_NAMES]
	for device in devices:
		row = Inventory(user_id=user_id, ip=device.ip, port=device.port,
		                device_type=device.device_type,
		                label=label or device.label)  # form > row > IP
		values = _cell_values(device.extra, columns)
		set_system_values(row, values)
		set_custom_values(db_session, row, user_id, values, own)
		if profiles:
			if device.username and device.password:
				row.security_profile = profiles.resolve(
					device.username, device.password, device.secret)
			elif device.username or device.password:
				half_credentials.append(row.label)
		db_session.add(row)
	if half_credentials:
		shown = ", ".join(half_credentials[:5]) + (
			", …" if len(half_credentials) > 5 else "")
		report.notices.append(("warning",
			f"{len(half_credentials)} device"
			f"{'s' if len(half_credentials) != 1 else ''} imported without "
			f"a profile (username and password are both needed): {shown}"))
	if profiles:
		db_session.flush()  # ids for the audit trail
		report.created_profiles = profiles.created()
		report.notices.extend(profiles.summary())
	report.devices = devices
	parser.logger.notify(
		f"CSV processed: {len(devices)} imported, "
		f"{len(report.errors)} failed",
		"green" if not report.errors else "yellow", important=True)
	return report
