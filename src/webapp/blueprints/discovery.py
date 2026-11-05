"""Discover devices (admins only): scan addresses for SSH, sign in with one
of the admin's own security profiles, detect each platform, and create
Inventory devices from what was found. The scan is app.discovery's
(src/webapp/discoveries.py); the page asks for what's new (status?since=N)."""
import uuid
from collections.abc import Callable
from typing import Any

from flask import Blueprint, render_template, request
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required

from src.access.nginx import server_ips
from src.audit import Actor, AuditAction
from src.discovery.scan import MAP_LIMIT, MAX_ADDRESSES, Login, sending_from
from src.discovery.targets import TargetError, Targets, parse_targets
from src.encryption import decrypt
from src.host_keys import HostKeyPolicies
from src.inventory import DeviceFields, InventoryView, RuleRefused, SecurityProfiles
from src.rollout import inputs
from src.rollout.engine import endpoint
from src.rollout.host_keys import HostKeyPolicy
from src.runtime import in_container
from src.webapp.app import current_app
from src.webapp.discoveries import Discovery, DiscoveryRefused, Starter, skip_rule
from src.webapp.hooks import signed_in_user
from src.webapp.http import actor, err, ok, require_admin, viewer, with_json

bp = Blueprint("discovery", __name__, url_prefix="/inventory/discover")


def _sources(first: str, port: int) -> list[str]:
	"""The server's addresses the network sees the scan come from: in a
	container the host's (the installer's record), else the one this
	computer sends from toward the first address; [] when unknown."""
	if in_container():
		return server_ips()
	found = sending_from(first, port)
	return [found] if found else []


def _asked(data: dict[str, Any]) -> tuple[Targets, int, uuid.UUID]:
	"""The addresses, port and profile a check or a start names, checked.

	:raises DiscoveryRefused: 400 with what's wrong"""
	try:
		targets = parse_targets(str(data.get("addresses") or ""), MAX_ADDRESSES)
	except TargetError as e:
		raise DiscoveryRefused(str(e), 400) from None
	port = str(data.get("port") or "22").strip()
	if not inputs.validate_port(port):
		raise DiscoveryRefused("Port must be between 1 and 65535", 400)
	try:
		profile_id = uuid.UUID(str(data.get("profile_id") or ""))
	except ValueError:
		raise DiscoveryRefused("Choose one of your security profiles", 400) from None
	return targets, int(port), profile_id


def _status(found: Discovery | None, since: int) -> dict[str, Any]:
	""":returns: the discovery's summary and its events after `since`
	 (None: no discovery)"""
	if found is None:
		return {"discovery": None}
	status = found.scan.status(since)
	return {"discovery": {**found.summary(), "seq": status["seq"], "events": status["events"],
	                      "created": [f.id for f in found.scan.findings() if f.created],
	                      "mine": found.starter.user_id == viewer().id}}


@bp.route("")
@login_required
@require_admin
def discover() -> str:
	"""The page: the admin's profiles to sign in with, the labels Inventory
	has already (a card's label warning), and the running or last
	discovery, if any."""
	with current_app.backend.postgres.get_session() as db_session:
		profiles = [{"id": str(p.id), "label": p.label or p.username, "username": p.username}
		            for p in signed_in_user(db_session).security_profiles]
		labels = sorted({d.label for d in InventoryView(db_session, viewer()).visible()})
	return render_template("discover.html", profiles=profiles, labels=labels,
	                       discovery=_status(current_app.discovery.current(), 0)["discovery"],
	                       max_addresses=MAX_ADDRESSES, map_limit=MAP_LIMIT, active_section="inventory")


@bp.route("/check", methods=["POST"])
@login_required
@require_admin
@with_json("addresses")
def check(data: Any) -> ResponseReturnValue:
	"""What a start would do, for the warning the admin confirms - nothing
	is started: the address count, the server's addresses it is sent from,
	the port and the profile's username."""
	try:
		targets, port, profile_id = _asked(data)
		with current_app.backend.postgres.get_session() as db_session:
			profile = SecurityProfiles(db_session, viewer()).owned(profile_id)
			if profile is None:
				raise DiscoveryRefused("Choose one of your security profiles", 404)
			username = profile.username
	except DiscoveryRefused as e:
		return err(str(e), e.code)
	return ok(addresses=targets.count, entries=list(targets.entries), notes=list(targets.notes),
	          port=port, username=username, sources=_sources(next(targets.addresses()), port),
	          refusal=current_app.discovery.refusal())


@bp.route("/start", methods=["POST"])
@login_required
@require_admin
@with_json("addresses")
def start(data: Any) -> ResponseReturnValue:
	"""Start a discovery once the admin confirmed the warning (JSON
	confirmed: true): the devices they see already in Inventory are left
	out, the profile's login is decrypted once."""
	if data.get("confirmed") is not True:
		return err("Confirm the warning first", 400)
	user = viewer()
	try:
		targets, port, profile_id = _asked(data)
		with current_app.backend.postgres.get_session() as db_session:
			profile = SecurityProfiles(db_session, user).owned(profile_id)
			if profile is None:
				raise DiscoveryRefused("Choose one of your security profiles", 404)
			login = Login(profile.username, decrypt(profile.password_secret),
			              decrypt(profile.enable_secret) if profile.enable_secret else "")
			label = profile.label or profile.username
			known: dict[str, str] = {}
			for device in InventoryView(db_session, user).visible():
				for address in (device.ip, device.resolved_ip):
					if address:
						known.setdefault(endpoint(inputs.normalize_address(address), device.port), device.label)
		found = current_app.discovery.start(
			targets, port, login, starter=Starter(user.id, current_user.username, request.remote_addr),
			profile_id=profile_id, profile_label=label,
			sources=tuple(_sources(next(targets.addresses()), port)),
			skip=skip_rule(known, current_app.orchestrator, current_app.backend.postgres),
			host_keys=_host_keys(current_app.host_keys, actor()))
	except DiscoveryRefused as e:
		return err(str(e), e.code)
	return ok(**_status(found, 0))


def _host_keys(policies: HostKeyPolicies, who: Actor) -> Callable[[str], HostKeyPolicy]:
	""":returns: the scan's host key check per endpoint (its threads have no
	 request: the service and the actor are taken now)"""
	return lambda target: policies.policy(target, who, "discovery")


@bp.route("/status")
@login_required
@require_admin
def status() -> ResponseReturnValue:
	"""The discovery's state and the events after ?since=N (the page's
	polls are background requests)."""
	try:
		since = int(request.args.get("since", "0"))
	except ValueError:
		return err("since is a number", 400)
	return ok(**_status(current_app.discovery.current(), since))


@bp.route("/continue", methods=["POST"])
@login_required
@require_admin
def proceed() -> ResponseReturnValue:
	"""Continue after the pause (any admin)."""
	found = current_app.discovery.current()
	if found is None or not found.scan.proceed():
		return err("No discovery is paused", 409)
	return ok(**_status(found, 0))


@bp.route("/cancel", methods=["POST"])
@login_required
@require_admin
def cancel() -> ResponseReturnValue:
	"""Cancel the running discovery (any admin; its audit row names who)."""
	if not current_app.discovery.cancel(current_user.username):
		return err("No discovery is running", 409)
	return ok()


@bp.route("/create", methods=["POST"])
@login_required
@require_admin
@with_json("cards")
def create(data: Any) -> ResponseReturnValue:
	"""Create a device from each card (JSON cards: [{finding, label,
	device_type, is_global}]) - each through Inventory's rules in its own
	database session, so one refused leaves the others created, with the
	discovery's profile linked. Only the admin who started the discovery;
	a finding already created is refused, never created twice.

	:returns: {results: [{finding, created, label?, warning?, message?}]}"""
	found = current_app.discovery.current()
	if found is None:
		return err("The discovery's findings are gone - run it again", 404)
	if found.starter.user_id != viewer().id:
		return err(f"Only {found.starter.username} can add devices from their discovery", 403)
	cards = data.get("cards")
	if not isinstance(cards, list):
		return err("cards is a list", 400)
	return ok(results=[_create(found, card) for card in cards])


def _create(found: Discovery, card: Any) -> dict[str, Any]:
	"""One card: checked, claimed (once), saved, audited; released again
	when it wasn't saved."""
	if not isinstance(card, dict):
		return {"finding": None, "created": False, "message": "Not a card"}
	finding_id = card.get("finding")
	label = str(card.get("label") or "").strip()
	device_type = str(card.get("device_type") or "").strip()
	if not label:
		return {"finding": finding_id, "created": False, "message": "Give the device a label"}
	if not inputs.validate_platform(device_type):
		return {"finding": finding_id, "created": False, "message": "Choose the device's platform"}
	finding = found.scan.claim(finding_id) if isinstance(finding_id, int) else None
	if finding is None:
		return {"finding": finding_id, "created": False,
		        "message": "Already added, or not one of this discovery's devices"}
	fields = DeviceFields(ip=finding.address, port=finding.port, device_type=device_type, label=label,
	                      profile_id=found.profile_id, make_global=card.get("is_global") is True)
	try:
		with current_app.backend.postgres.get_session() as db_session:
			saved = InventoryView(db_session, viewer()).save_device(None, fields)
	except RuleRefused as e:
		found.scan.release(finding.id)
		return {"finding": finding.id, "created": False, "message": str(e)}
	except Exception:
		found.scan.release(finding.id)
		raise
	if finding.host_key:      # the key its discovery saw: stored with it (trust on first use)
		current_app.host_keys.store(endpoint(finding.address, finding.port), finding.host_key, actor(), "discovery")
	current_app.web.audit(AuditAction.INVENTORY_CREATE, object_type="Inventory", object_label=label,
	                      detail={"source": "discover", "is_global": saved.is_global})
	return {"finding": finding.id, "created": True, "label": label, "warning": saved.shared_endpoint}
