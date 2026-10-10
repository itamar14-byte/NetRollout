"""The device inventory's rules (web app), as one user - the viewer - sees
it (InventoryView): which devices they see and may edit (their own and the
global ones), devices sharing an endpoint, the labels that name IPs, the
properties and attribute values, their mapping bindings and the rules for
saving a device; and whether a device is reachable from the NetRollout
server.

Attributes: the system properties' values are the device's own (var_maps),
shared by everyone who sees it and set by who may edit it; a custom
property is one user's, and so are its values (DeviceAttribute rows) - any
user who sees a device sets their own. InventoryView.attributes() is the
one way to read them: what a user's mappings and rollouts substitute.

Reachability: one fast TCP connect to ip:port - the management endpoint a
push would use, from the server's point of view (the one that matters).
Results are cached briefly in Redis, so page loads stay cheap and "recent"
means at most CACHE_TTL seconds old."""
from __future__ import annotations   # type hints are never evaluated

import hmac
import json
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from csv import DictReader
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Iterable, NotRequired, TypedDict, cast, Any

import redis
from sqlalchemy import ColumnElement, or_, tuple_
from sqlalchemy.orm import Query, Session, selectinload

from src.accounts.users import Viewer
from src.db.connections import REDIS_UNAVAILABLE
from src.db.tables import (DeviceAttribute, Inventory, PropertyDefinition, SecurityProfile,
                           VariableMapping)
from src.encryption import decrypt, encrypt
from src.rollout.engine import endpoint, mapping_resolvable, Device
from src.rollout import inputs
from src.rollout.inputs import InputParser
from src.rollout.log import Tone


PROBE_TIMEOUT = 2.0   # seconds; single attempt — this is a status hint
CACHE_TTL = 60        # seconds
MAX_PARALLEL = 32

Target = tuple[str, int]      # (ip, port)


class PropertyDef(TypedDict):
	"""A property as the pages and the CSV import get it - a dict, as the
	inventory page hands the list to its script as JSON."""
	name: str           # the key $$TOKEN$$ substitution reads
	label: str
	icon: str           # a Bootstrap Icons class
	is_list: bool
	id: NotRequired[str]   # a user's own property's (system ones have none)


# The properties every device has (their values: the device's var_maps)
SYSTEM_PROPERTIES: list[PropertyDef] = [
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
	"""One TCP connect, no retry (inputs.tcp_reachable once).

	:param timeout: seconds before an unanswered connect counts as down
	:returns: whether ip:port accepted the connection (closed at once)"""
	try:
		number = int(port)
	except ValueError:
		return False
	return inputs.tcp_reachable(ip, number, attempts=1, timeout=timeout)


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
                properties: Iterable[PropertyDef]) -> dict[str, AttrValue | None]:
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


# ── the inventory as one user sees it ───────────────────────────────────────

class LabelScope(StrEnum):
	"""Whose devices name the IPs in a label map (InventoryView.label_map)."""
	VISIBLE = "visible"   # the viewer's own and the global ones - their own win
	ANYONE = "anyone"     # anyone's inventory (what an admin is shown)


class RuleRefused(ValueError):
	"""A device-saving rule refused the change, in words for the person -
	raised before anything is changed (only InventoryView raises it)."""


GLOBAL_NEEDS_PROFILE = ("A global device needs a security profile — users "
                        "can't assign their own to it.")


@dataclass(frozen=True)
class DeviceFields:
	"""A device's fields from the page's form, already checked (a valid IP,
	port and platform; the label defaulted to the IP)."""
	ip: str
	port: int
	device_type: str
	label: str
	profile_id: uuid.UUID | None
	make_global: bool     # what the form asked; only an admin's request counts


@dataclass(frozen=True)
class SavedDevice:
	"""What saving a device did, for the page."""
	label: str
	is_global: bool
	was_global: bool
	shared_endpoint: str | None   # the warning: other devices on its ip:port
	unbound: list[str]            # mapping tokens not bound: no value for them


class InventoryView:
	"""The inventory as one user - the viewer - sees it: the devices they
	may see (their own and the global ones) and edit (their own; an admin
	also the global ones), the attribute values they see, their mapping
	bindings, and the rules for saving a device. The viewer is usually the
	signed-in user; a rollback works with the job owner's.

	Every method works in the caller's session; the caller commits."""

	def __init__(self, session: Session, viewer: Viewer) -> None:
		self.session = session
		self.viewer = viewer

	# ── which devices ──

	def visible(self, ips: Collection[str] | None = None) -> list[Inventory]:
		"""The visible devices, by label, with the relationships templates and
		rollouts need preloaded (rows survive expunge).

		:param ips: only devices at one of these IPs; None: every one"""
		query = self.session.query(Inventory).filter(visible_devices_clause(self.viewer.id))
		if ips is not None:
			query = query.filter(Inventory.ip.in_(ips))
		return (query.options(selectinload(Inventory.security_profile),
		                      selectinload(Inventory.var_mappings))
		        .order_by(Inventory.label)
		        .all())

	def visible_ids(self, ids: Collection[uuid.UUID]) -> list[Inventory]:
		""":returns: the devices of these ids the viewer may see (relationships
		 preloaded); an id they can't see is left out, as a missing one"""
		return (self.session.query(Inventory)
		        .filter(visible_devices_clause(self.viewer.id), Inventory.id.in_(ids))
		        .options(selectinload(Inventory.security_profile),
		                 selectinload(Inventory.var_mappings))
		        .all())

	def visible_endpoints(self) -> set[Target]:
		""":returns: the ip:port of every visible device"""
		return {(ip, port) for ip, port in self.session.query(
			Inventory.ip, Inventory.port).filter(visible_devices_clause(self.viewer.id))}

	def get_visible(self, device_id: uuid.UUID) -> Inventory | None:
		""":returns: the device; None when there's none - or the viewer can't
		 see it (the same answer: its existence isn't revealed)"""
		return self.session.query(Inventory).filter(
			Inventory.id == device_id, visible_devices_clause(self.viewer.id)).first()

	def can_edit(self, device: Inventory) -> bool:
		"""Owners edit their own devices; any admin may edit a global device."""
		return device.user_id == self.viewer.id or (device.is_global and self.viewer.is_admin)

	def get_editable(self, device_id: uuid.UUID) -> Inventory | None:
		""":returns: the device; None when there's none - or the viewer may not
		 edit it (the same answer: its existence isn't revealed)"""
		device = self.session.get(Inventory, device_id)
		return device if device is not None and self.can_edit(device) else None

	def same_endpoint(self, ip: str, port: int | str,
	                  exclude: uuid.UUID | None = None) -> list[Inventory]:
		"""Visible devices (own + global) already using this ip:port. Overlap is
		legitimate (NAT, VRFs, port-forwarded labs), so callers warn, never
		block; other users' private devices are never considered.

		:param exclude: the device being edited (it doesn't clash with itself)"""
		query = self.session.query(Inventory).filter(
			visible_devices_clause(self.viewer.id),
			Inventory.ip == ip, Inventory.port == int(port))
		if exclude is not None:
			query = query.filter(Inventory.id != exclude)
		return query.order_by(Inventory.label).all()

	# ── names for IPs ──

	def label_map(self, scope: LabelScope,
	              ips: Collection[str] | None = None) -> dict[str, str]:
		"""IP → a device's label, from the scope's devices (VISIBLE: the
		viewer's own win when one shares an IP with a global one).

		:param ips: only these IPs; None: every device in the scope"""
		if ips is not None and not ips:
			return {}
		query = self._in_scope(
			self.session.query(Inventory.ip, Inventory.label, Inventory.user_id), scope)
		if ips is not None:
			query = query.filter(Inventory.ip.in_(ips))
		rows = query.all()
		if scope is LabelScope.VISIBLE:
			rows.sort(key=lambda r: r.user_id == self.viewer.id)
		return {r.ip: r.label for r in rows}

	def endpoint_labels(self, scope: LabelScope,
	                    endpoints: Collection[Target] | None = None) -> dict[Target, str]:
		"""(ip, port) → label, to name each device: the IP alone is ambiguous
		when several share it (NAT, port forwarding). VISIBLE: the viewer's
		own win; ANYONE (only these endpoints' devices are read): an empty
		label is shown as the IP.

		:param endpoints: only these endpoints; None: every device in the scope"""
		if endpoints is not None and not endpoints:
			return {}
		query = self._in_scope(self.session.query(
			Inventory.ip, Inventory.port, Inventory.label, Inventory.user_id), scope)
		if endpoints is not None:
			query = query.filter(tuple_(Inventory.ip, Inventory.port).in_(endpoints))
		rows = query.all()
		if scope is LabelScope.VISIBLE:
			rows.sort(key=lambda r: r.user_id == self.viewer.id)
		if scope is LabelScope.ANYONE:
			return {(r.ip, r.port): (r.label or r.ip) for r in rows}
		return {(r.ip, r.port): r.label for r in rows}

	def _in_scope(self, query: Query[Any], scope: LabelScope) -> Query[Any]:
		""":returns: the query limited to the scope's devices"""
		if scope is LabelScope.VISIBLE:
			return query.filter(visible_devices_clause(self.viewer.id))
		return query

	# ── properties and attribute values ──

	def property_defs(self) -> tuple[list[PropertyDef], list[PropertyDef]]:
		""":returns: (the system properties, the viewer's own, by name) -
		 {name, label, icon, is_list, and the viewer's: id}"""
		own = self.session.query(PropertyDefinition).filter_by(
			user_id=self.viewer.id).order_by(PropertyDefinition.name).all()
		return SYSTEM_PROPERTIES, [
			{"name": p.name, "label": p.label, "icon": p.icon, "is_list": p.is_list,
			 "id": str(p.id)} for p in own]

	def attributes(self, devices: Iterable[Inventory]) -> dict[uuid.UUID, dict[str, AttrValue]]:
		"""Each device's attribute values as the viewer sees them: the device's
		system values and the viewer's own custom values (never another
		user's) - what their mappings and rollouts substitute. One query,
		however many devices.

		:param devices: saved rows (they have ids)
		:returns: {device id: {property name: a text or a list of texts}}"""
		values = {d.id: system_values(d) for d in devices}
		if values:
			rows = self.session.query(DeviceAttribute.device_id, DeviceAttribute.name,
			                          DeviceAttribute.value).filter(
				DeviceAttribute.user_id == self.viewer.id,
				DeviceAttribute.device_id.in_(list(values)))
			for device_id, name, value in rows:
				values[device_id][name] = value
		return values

	def set_custom_values(self, device: Inventory, values: Mapping[str, AttrValue | None],
	                      own: Iterable[str]) -> None:
		"""Set the viewer's custom values on a device; other users' are never
		touched.

		:param device: a saved row, or one just added to the session
		:param values: {name: value}; None removes the viewer's value
		:param own: the names of the viewer's own properties - any other name
		 is ignored"""
		own_names = set(own)
		wanted = {n: v for n, v in values.items() if n in own_names}
		if not wanted:
			return
		existing = {a.name: a for a in self.session.query(DeviceAttribute).filter(
			DeviceAttribute.device_id == device.id,
			DeviceAttribute.user_id == self.viewer.id,
			DeviceAttribute.name.in_(list(wanted)))} if device.id else {}
		for name, value in wanted.items():
			row = existing.get(name)
			if not value:
				if row is not None:
					self.session.delete(row)
			elif row is not None:
				row.value = value
			else:
				self.session.add(DeviceAttribute(device=device, user_id=self.viewer.id,
				                                 name=name, value=value))

	def drop_other_users_values(self, device: Inventory) -> None:
		"""Remove everyone's values on a device but its owner's (it was made
		local: nobody else can see it - their bindings go too)."""
		self.session.query(DeviceAttribute).filter(
			DeviceAttribute.device_id == device.id,
			DeviceAttribute.user_id != device.user_id).delete(synchronize_session=False)

	def delete_property_values(self, name: str) -> None:
		"""Remove the viewer's values of one of their properties, on every
		device (the property is being deleted)."""
		self.session.query(DeviceAttribute).filter(
			DeviceAttribute.user_id == self.viewer.id,
			DeviceAttribute.name == name).delete(synchronize_session=False)

	def bind_mappings(self, device: Inventory, mapping_ids: list[uuid.UUID]) -> list[str]:
		"""The device's bindings to the viewer's mappings become `mapping_ids`.
		The join table is shared across users: only the viewer's bindings are
		replaced, others' on a global device stay. A mapping the device can't
		resolve with the viewer's values (attribute missing, list index out of
		range) isn't bound.

		:returns: the tokens not bound, for the page to tell the user"""
		selected = self.session.query(VariableMapping).filter(
			VariableMapping.id.in_(mapping_ids),
			VariableMapping.user_id == self.viewer.id
		).all() if mapping_ids else []
		values = self.attributes([device])[device.id] if selected else {}
		eligible = [m for m in selected if mapping_resolvable(
			values, m.property_name, m.index)]
		device.var_mappings = [m for m in device.var_mappings
		                       if m.user_id != self.viewer.id] + eligible
		return sorted(m.token for m in selected if m not in eligible)

	# ── saving a device ──

	def profile_allowed(self, profile_id: uuid.UUID | None,
	                    current_profile_id: uuid.UUID | None = None) -> bool:
		"""A device may only carry a profile the viewer owns — otherwise a
		user could attach another user's (e.g. an admin's global) credentials
		to a device they control. Keeping the device's profile unchanged is
		always allowed (an admin saving another admin's global device); None
		(no profile) too."""
		if profile_id is None or profile_id == current_profile_id:
			return True
		return self.session.query(SecurityProfile).filter_by(
			id=profile_id, user_id=self.viewer.id).first() is not None

	def save_device(self, device: Inventory | None, fields: DeviceFields, *,
	                values: Mapping[str, AttrValue | None] | None = None,
	                own: Iterable[str] = (),
	                mapping_ids: list[uuid.UUID] | None = None) -> SavedDevice:
		"""Add a device (None) or save one the viewer may edit, with the
		device-saving rules: only an admin makes a device global or local
		(anyone else's request is ignored), a global device needs a security
		profile, a device carries only a profile the viewer owns (or keeps
		its own), and a device made local loses the other users' bindings and
		values - nobody else sees it any more. Every rule is checked before
		anything changes. An ip:port another visible device uses already is
		allowed, with a warning (on an edit: only when it changed).

		:param values: the attribute values to set (system ones on the
		 device, the viewer's own custom ones); None: none
		:param own: the viewer's own property names (for `values`)
		:param mapping_ids: the viewer's bindings on the device become these
		 (bind_mappings); None: left as they are
		:returns: what was saved and the warnings for the page
		:raises RuleRefused: a rule refused it - nothing changed"""
		was_global = device.is_global if device is not None else False
		is_global = fields.make_global if self.viewer.is_admin else was_global
		if is_global and fields.profile_id is None:
			raise RuleRefused(GLOBAL_NEEDS_PROFILE)
		if not self.profile_allowed(fields.profile_id,
		                            device.sec_profile_id if device is not None else None):
			raise RuleRefused("Security profile not found.")

		if device is None:
			shared = same_endpoint_warning(self.same_endpoint(fields.ip, fields.port),
			                               fields.ip, fields.port)
			device = Inventory(user_id=self.viewer.id, label=fields.label, ip=fields.ip,
			                   port=fields.port, device_type=fields.device_type,
			                   sec_profile_id=fields.profile_id, is_global=is_global)
			self.session.add(device)
		else:
			old_endpoint = (device.ip, device.port)
			device.label = fields.label
			device.ip = fields.ip
			device.port = fields.port
			# warn only when the endpoint changed — not on every save
			shared = None
			if (device.ip, device.port) != old_endpoint:
				shared = same_endpoint_warning(self.same_endpoint(
					device.ip, device.port, exclude=device.id), device.ip, device.port)
			device.device_type = fields.device_type
			device.sec_profile_id = fields.profile_id
			device.is_global = is_global

		if values is not None:
			set_system_values(device, values)
			self.set_custom_values(device, values, own)
		unbound = self.bind_mappings(device, mapping_ids) if mapping_ids is not None else []
		if was_global and not is_global:
			# Other users can no longer see this device — drop their bindings
			# rather than leave invisible orphans in the join table.
			device.var_mappings = [m for m in device.var_mappings
			                       if m.user_id == device.user_id]
			self.drop_other_users_values(device)
		return SavedDevice(label=device.label, is_global=is_global, was_global=was_global,
		                   shared_endpoint=shared, unbound=unbound)

	def assign_profile(self, device: Inventory, profile_id: uuid.UUID | None) -> bool:
		"""Give a device the viewer may edit one of the viewer's profiles, or
		none - except a global device, which keeps one (other users can't give
		it theirs).

		:returns: whether it was assigned (False: refused, nothing changed)"""
		if device.is_global and profile_id is None:
			return False
		device.sec_profile_id = profile_id
		return True


class SecurityProfiles:
	"""The viewer's security profiles: the credentials devices are reached
	with - their secrets are encrypted here, in one place. Every method works
	in the caller's session; the caller commits."""

	def __init__(self, session: Session, viewer: Viewer) -> None:
		self.session = session
		self.viewer = viewer

	def create(self, label: str | None, username: str, password: str,
	           enable_secret: str | None) -> SecurityProfile:
		"""A new profile of the viewer's, added to the session (not flushed:
		no id yet).

		:param label: its name; None: shown by its username
		:param enable_secret: None or "": none"""
		profile = SecurityProfile(
			label=label, username=username,
			password_secret=encrypt(password),
			enable_secret=encrypt(enable_secret) if enable_secret else None,
			user_id=self.viewer.id)
		self.session.add(profile)
		return profile

	def owned(self, profile_id: uuid.UUID) -> SecurityProfile | None:
		""":returns: the viewer's profile; None when there's none - or it's
		 another user's (the same answer)"""
		return self.session.query(SecurityProfile).filter_by(
			id=profile_id, user_id=self.viewer.id).first()

	def update(self, profile: SecurityProfile, *, label: str | None, username: str,
	           password: str, enable_secret: str, clear_enable_secret: bool) -> None:
		"""Change a profile: an empty password or enable secret keeps the
		stored one; clear_enable_secret removes the secret (a new one given
		with it wins)."""
		profile.label = label
		profile.username = username
		if password:
			profile.password_secret = encrypt(password)
		if enable_secret:
			profile.enable_secret = encrypt(enable_secret)
		elif clear_enable_secret:
			profile.enable_secret = None


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
	props: dict[str, PropertyDef]   # CSV header → property definition
	credentials: list[str]   # credential headers present
	ignored: list[str]       # headers that are neither


def _classify_columns(headers: list[str], properties: list[PropertyDef]) -> _Columns:
	"""Match headers to properties by name or label, case-insensitively,
	treating spaces and underscores alike ("Loopback IP" → loopback_ip)."""
	def norm(name: str) -> str:
		return name.strip().lower().replace(" ", "_")
	lookup: dict[str, PropertyDef] = {}
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
		self._profiles = SecurityProfiles(db_session, Viewer(user_id))
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
		profile = self._profiles.create(label, username, password, secret)
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
               properties: list[PropertyDef] | None = None,
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
	 ({name, label, is_list}) — see InventoryView.property_defs
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
		parser.logger.notify(f"Reading CSV failed: {e}", Tone.ERROR)
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
	view = InventoryView(db_session, Viewer(user_id))
	for device in devices:
		row = Inventory(user_id=user_id, ip=device.ip, port=device.port,
		                device_type=device.device_type,
		                label=label or device.label)  # form > row > IP
		values = _cell_values(device.extra, columns)
		set_system_values(row, values)
		view.set_custom_values(row, values, own)
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
		Tone.SUCCESS if not report.errors else Tone.WARNING, important=True)
	return report
