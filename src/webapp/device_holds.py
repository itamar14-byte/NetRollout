"""Devices in a rollout, as the pages show them: a device is held from the
moment its rollout is queued until it ends (RolloutOrchestrator.submit /
busy), and another rollout to it is refused. Here: which of a page's
inventory rows are held and by whose rollout (New Rollout's chips, the
Inventory badge, the dashboard's card), and the refusal the launch and the
rollback answer. The owner is named - to an operator too: a device is one
box whoever's entry it is."""
from collections.abc import Iterable, Sequence
from typing import Any

from flask_login import current_user

from src.db.tables import Inventory
from src.jobs import RolloutRow, with_owners
from src.rollout.address import is_ip
from src.rollout.engine import Device, endpoint
from src.webapp.app import current_app
from src.webapp.http import viewer


def row_target(row: Inventory) -> str:
	"""The box an inventory row is, as Device.target counts it: a hostname
	as its last reachability check resolved it (resolved_ip), else as
	written."""
	address = row.ip if is_ip(row.ip) else (row.resolved_ip or row.ip)
	return endpoint(address, row.port)


def _owner(row: RolloutRow, names: dict[str, str]) -> str:
	""":returns: "your" for the signed-in user's own job, else "<user>'s" """
	return "your" if row.user_id == str(current_user.id) else f"{names.get(row.job_id, '?')}'s"


def _named(rows: Iterable[RolloutRow]) -> dict[str, str]:
	""":returns: job id -> its owner's username (one query)"""
	unique = {r.job_id: r for r in rows}
	return {r["job_id"]: r["user"] for r in with_owners(list(unique.values()),
	                                                     current_app.backend.postgres)}


def _since(row: RolloutRow) -> str:
	""":returns: "queued", "running since 16:02:11" or "cancelling" """
	return f"running since {row.clock}" if row.state == "running" else row.state


def holds(rows: Sequence[Inventory]) -> dict[str, dict[str, Any]]:
	"""The rows in a rollout now.

	:returns: device id -> {"user": the owner's name, "mine": the signed-in
	 user's own, "state": queued / running / cancelling, "since": its start
	 time or "", "text": the sentence the pages show, "job_id": when the
	 viewer may open the job (theirs, or an admin)}; rows not held are absent"""
	busy = current_app.orchestrator.busy()
	held = {row.id: busy[t] for row in rows if (t := row_target(row)) in busy}
	if not held:
		return {}
	names = _named(held.values())
	is_admin = viewer().is_admin
	out: dict[str, dict[str, Any]] = {}
	for device_id, job in held.items():
		mine = job.user_id == str(current_user.id)
		out[str(device_id)] = {
			"user": names.get(job.job_id, "?"), "mine": mine, "state": job.state,
			"since": job.clock if job.started else "",
			"text": f"In {_owner(job, names)} rollout - {_since(job)}. Free when it ends.",
			"job_id": job.job_id if mine or is_admin else None}
	return out


def busy_message(conflicts: dict[str, RolloutRow], devices: Sequence[Device],
                 addresses: dict[str, str] | None = None) -> str:
	"""The launch's or rollback's refusal: each held device and whose rollout
	holds it.

	:param conflicts: target -> the holding job (conflicts() / DeviceBusy)
	:param devices: the rollout's devices, to name them by label
	:param addresses: hostname -> resolved address, as the targets were made"""
	labels: dict[str, list[str]] = {}
	for d in devices:
		labels.setdefault(d.target(addresses), []).append(d.label or d.ip)
	names = _named(conflicts.values())
	parts = [f"{' / '.join(labels.get(t, [t]))} ({t}) - {_owner(job, names)} rollout, {_since(job)}"
	         for t, job in conflicts.items()]
	return (f"In another rollout - rollout blocked: {'; '.join(parts)}. A device takes one "
	        f"rollout at a time: try again when it ends, or leave it out.")



def in_rollouts(rows: Sequence[Inventory]) -> list[dict[str, Any]]:
	"""The dashboard's card: the rows in a rollout now, each once per box
	(an own and a global entry for one router are one line), running ones
	first, then by label.

	:returns: holds()'s fields plus "label" and "target" (ip:port)"""
	held = holds(rows)
	lines: dict[str, dict[str, Any]] = {}
	for row in rows:
		if (hold := held.get(str(row.id))) and (target := row_target(row)) not in lines:
			lines[target] = {**hold, "label": row.label or row.ip, "target": target}
	return sorted(lines.values(), key=lambda h: (h["state"] != "running", h["label"].lower()))
