"""Devices' SSH host keys as NetRollout stores them (table host_keys): the
keys accepted per target (ip:port as connected), a key a device was
refused for waiting for an admin, and what each connection is checked
against (HostKeyPolicies: System Settings' host_key_mode, the stored keys).
The check itself is src/rollout/host_keys.py; this decides what it checks
against and records what it saw. No Flask: the routes build the actor."""
from __future__ import annotations   # type hints are never evaluated

import datetime
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.audit import Actor, AuditAction, AuditTrail
from src.db.settings import HostKeyMode
from src.db.tables import HostKey, HostKeyState
from src.rollout.host_keys import OFF, HostKeyPolicy, fingerprint

if TYPE_CHECKING:   # annotations only
	from src.db.connections import PostgresConnection
	from src.rollout.engine import Device


class HostKeyError(ValueError):
	"""A host key change refused, in words for the person (the page)."""


class HostKeys:
	"""The stored host keys, in the caller's session (the routes', or a
	connection's own when it records what it saw)."""

	def __init__(self, session: Session) -> None:
		self.session = session

	def accepted(self, target: str) -> list[str]:
		""":returns: the target's accepted keys ("type base64"), oldest first"""
		return [row.key for row in self.session.query(HostKey).filter_by(
			target=target, state=HostKeyState.ACCEPTED).order_by(HostKey.first_seen)]

	def learn(self, target: str, key: str, seen_by: str) -> bool:
		"""Store a target's first key (trust on first use). Two connections
		learning the same key at once store it once.

		:returns: whether it was stored now (False: there already) - a key
		 refused earlier (the old key since removed) is accepted now"""
		now = datetime.datetime.now()
		if self.session.query(HostKey).filter_by(target=target, key=key, state=HostKeyState.REFUSED).update(
				{HostKey.state: HostKeyState.ACCEPTED, HostKey.last_seen: now,
				 HostKey.seen_by: seen_by[:128]}):
			return True
		stored = self.session.execute(insert(HostKey).values(
			id=uuid.uuid4(), target=target, key_type=key.split()[0], key=key, fingerprint=fingerprint(key),
			state=HostKeyState.ACCEPTED, first_seen=now, last_seen=now, seen_by=seen_by[:128],
		).on_conflict_do_nothing(index_elements=["target", "key"]).returning(HostKey.id))
		return stored.scalar_one_or_none() is not None

	def seen(self, target: str, key: str) -> None:
		"""An accepted key was shown again: its last seen time."""
		self.session.query(HostKey).filter_by(target=target, key=key).update(
			{HostKey.last_seen: datetime.datetime.now()})

	def refuse(self, target: str, key: str, seen_by: str) -> bool:
		"""A device at the target showed `key`, which isn't accepted: kept
		(refused) for an admin to accept or not.

		:returns: whether it's the first refusal of this key today (the
		 audit's: once per target and key per day)"""
		now = datetime.datetime.now()
		row = self.session.query(HostKey).filter_by(target=target, key=key).first()
		if row is None:
			self.session.add(HostKey(target=target, key_type=key.split()[0], key=key, fingerprint=fingerprint(key),
			                         state=HostKeyState.REFUSED, first_seen=now, last_seen=now,
			                         seen_by=seen_by[:128]))
			return True
		first_today = row.last_seen.date() != now.date()
		row.last_seen = now
		return first_today

	def listing(self) -> list[HostKey]:
		""":returns: every stored key, by target, the refused ones first in theirs"""
		return (self.session.query(HostKey)
		        .order_by(HostKey.target, HostKey.state.desc(), HostKey.first_seen).all())

	def refused_targets(self, targets: Iterable[str]) -> set[str]:
		""":returns: which of these targets have a refused key waiting"""
		wanted = set(targets)
		if not wanted:
			return set()
		return {t for (t,) in self.session.query(HostKey.target).filter(
			HostKey.target.in_(wanted), HostKey.state == HostKeyState.REFUSED).distinct()}

	def accept(self, key_id: uuid.UUID, by: str, keep_others: bool) -> HostKey:
		"""Accept a refused key: a replaced device's new key (the target's
		other accepted keys of that type removed), or an HA pair's other
		unit (`keep_others`).

		:raises HostKeyError: no such key, or it's accepted already"""
		row = self.session.get(HostKey, key_id)
		if row is None:
			raise HostKeyError("That host key isn't stored any more - reload the page.")
		if row.state == HostKeyState.ACCEPTED:
			raise HostKeyError(f"The key {row.fingerprint} of {row.target} is accepted already.")
		if not keep_others:
			self.session.query(HostKey).filter(
				HostKey.target == row.target, HostKey.key_type == row.key_type, HostKey.id != row.id,
				HostKey.state == HostKeyState.ACCEPTED).delete(synchronize_session=False)
		row.state = HostKeyState.ACCEPTED
		row.accepted_by, row.accepted_at = by[:64], datetime.datetime.now()
		return row

	def remove(self, key_id: uuid.UUID) -> HostKey:
		"""Forget one key (an HA unit gone, a refused key not wanted).

		:raises HostKeyError: no such key"""
		row = self.session.get(HostKey, key_id)
		if row is None:
			raise HostKeyError("That host key isn't stored any more - reload the page.")
		self.session.delete(row)
		return row

	def reset(self, target: str) -> int:
		"""Forget every key of a target: its next connection learns again.

		:returns: how many were removed"""
		return self.session.query(HostKey).filter_by(target=target).delete(synchronize_session=False)

	def count(self) -> int:
		return self.session.query(func.count(HostKey.id)).scalar() or 0


class HostKeyPolicies:
	"""What each connection checks a device's host key against, and the
	recording of what it saw (app.host_keys): off unless System Settings
	turns it on; the target's accepted keys, else learn. What a connection
	saw is written in a session of its own (rollouts run in threads, without
	a request), never failing the connection; skipped while a database move
	blocks writes."""

	def __init__(self, postgres: Callable[[], PostgresConnection], mode: Callable[[], str],
	             audit_trail: AuditTrail, writes_blocked: Callable[[], bool]) -> None:
		""":param postgres: the current connection (a move switches it)
		:param mode: host_key_mode's value now"""
		self._postgres, self._mode = postgres, mode
		self._audit_trail, self._writes_blocked = audit_trail, writes_blocked

	@property
	def on(self) -> bool:
		return self._mode() != HostKeyMode.OFF

	def known(self, target: str) -> list[str]:
		""":returns: the target's accepted keys; [] when checking is off"""
		if not self.on:
			return []
		with self._postgres().get_session() as session:
			return HostKeys(session).accepted(target)

	def policy(self, target: str, actor: Actor, how: str, learn: bool = True) -> HostKeyPolicy:
		"""One device's check, from now.

		:param target: ip:port as connected (Device.target)
		:param actor: who connects (their keys learned are theirs)
		:param how: what connects - "rollout", "test sign-in", "detect", "discovery"
		:param learn: a target without keys learns the one seen (False: a
		 device not saved yet - Detect, the window's Test sign-in - stores nothing)
		:returns: off; the accepted keys; or learn with nothing known"""
		if not self.on:
			return OFF
		known = tuple(self.known(target))
		return HostKeyPolicy(known=known, learn=learn and not known,
		                     seen=lambda key: self._seen(target, key, actor, how, learned=not known),
		                     refused=lambda key: self._refused(target, key, actor, how))

	def apply(self, devices: Iterable[Device], addresses: Mapping[str, str], actor: Actor, how: str) -> None:
		"""Every device's check (a rollout's, a rollback's): the one place a
		web connection's devices get theirs.

		:param addresses: hostname -> resolved address (Device.target)"""
		for device in devices:
			device.host_keys = self.policy(device.target(addresses), actor, how)

	def store(self, target: str, key: str, actor: Actor, how: str) -> None:
		"""A key seen without connecting through a policy (Discover's
		finding, when its device is added): learned unless the target has
		accepted keys already. Nothing when checking is off."""
		if self.on:
			self._seen(target, key, actor, how, learned=True)

	def _seen(self, target: str, key: str, actor: Actor, how: str, *, learned: bool) -> None:
		if self._writes_blocked():
			return
		try:
			with self._postgres().get_session() as session:
				keys = HostKeys(session)
				stored = learned and not keys.accepted(target) and keys.learn(target, key, f"{actor.username} ({how})")
				if not stored:
					keys.seen(target, key)
			if stored:
				self._audit(actor, AuditAction.HOST_KEY_LEARNED, target, key, how)
		except Exception as e:
			print(f"[NetRollout] host key of {target} not recorded ({e})", flush=True)

	def _refused(self, target: str, key: str, actor: Actor, how: str) -> None:
		if self._writes_blocked():
			return
		try:
			with self._postgres().get_session() as session:
				first_today = HostKeys(session).refuse(target, key, f"{actor.username} ({how})")
			if first_today:
				self._audit(actor, AuditAction.HOST_KEY_REFUSED, target, key, how, success=False)
		except Exception as e:
			print(f"[NetRollout] refused host key of {target} not recorded ({e})", flush=True)

	def _audit(self, actor: Actor, action: AuditAction, target: str, key: str, how: str,
	           success: bool = True) -> None:
		self._audit_trail.record(actor, action, object_type="HostKey", object_label=target,
		                         detail={"fingerprint": fingerprint(key), "key_type": key.split()[0], "via": how},
		                         success=success)
