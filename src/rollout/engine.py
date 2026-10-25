"""The rollout engine, shared by the CLI and the web app: push commands to
many devices over SSH (Netmiko) in parallel, finish each the way its
platform needs (save / commit / ...), optionally verify it by the device's
own configuration before and after (src/rollout/evidence.py), and classify
each device's outcome. Platform knowledge is in src/rollout/platforms.py,
one device's SSH conversation in src/rollout/session.py; this module runs
the rollout across the devices."""
import os
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

from src import encryption
from src.rollout.address import is_ip, qualify
from src.rollout.evidence import Judgment, Outcome, Verdict
from src.rollout.log import Tone, redact
from src.rollout.session import NetmikoSession, Notifier, PushResult, RunReport
if TYPE_CHECKING:   # type hints only: the CLI (.exe) must not load the DB stack
	from src.db.tables import Inventory, SecurityProfile


class SubstitutionError(ValueError):
	"""A $$TOKEN$$ can't be resolved on a device."""


def endpoint(ip: str, port: int | str) -> str:
	"""How a target is written: ip:port, an IPv6 address in brackets
	([2001:db8::1]:22, as in a URL - its own colons would hide the port)."""
	return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def mapping_resolvable(var_maps: dict[str, Any] | None, property_name: str,
                       index: int | None) -> bool:
	"""Whether a mapping can substitute on a device. Shared by the binding
	routes and the engine, so they can't disagree.

	:param var_maps: the device's attribute values (a text or a list each)
	:param property_name: the attribute the mapping reads
	:param index: a position in a list attribute (e.g. vrfs[2]); None: the
	 whole value
	:returns: the attribute is set and non-empty - and long enough, when
	 indexed"""
	value = (var_maps or {}).get(property_name)
	if not value:
		return False
	if index is None:
		return True
	return isinstance(value, list) and 0 <= index < len(value)


def missing_value(var_maps: dict[str, Any] | None, property_name: str,
                  index: int | None) -> str | None:
	"""Why a mapping can't substitute on a device, in the words unresolved()
	and the New Rollout page use (see mapping_resolvable).

	:returns: e.g. "no value for 'vrfs[2]'"; None when it can"""
	if mapping_resolvable(var_maps, property_name, index):
		return None
	where = property_name if index is None else f"{property_name}[{index}]"
	return f"no value for '{where}'"


# a $$TOKEN$$ in a command (a mapping's token shape: inputs.token_problem); a
# lone $$ isn't one
TOKEN_IN_COMMAND = re.compile(r"\$\$[A-Za-z0-9_]{1,64}\$\$")


class DeviceStatus(StrEnum):
	"""A device's outcome in a rollout (device_results.status; a job's status
	is one of them too: src/results.job_status), in the order the summary and
	the Results filter list them."""
	SUCCESS = "success"
	REVIEW = "review"              # verified, but a person must decide something
	PARTIAL = "partial"
	FAILED = "failed"
	NOT_VERIFIED = "not verified"  # applied; verification off, or not possible
	CANCELLED = "cancelled"


class ReviewOutcome(StrEnum):
	"""A person's decision on a device in review (device_results.review_outcome):
	confirmed makes it a success, rejected a failure."""
	CONFIRMED = "confirmed"
	REJECTED = "rejected"


# the verdicts the configuration proves (the rest is navigation or review)
CERTAIN = (Verdict.TOOK_EFFECT, Verdict.IN_PLACE, Verdict.REMOVED)


def classify(push: PushResult | None) -> tuple[DeviceStatus, int, int | None]:
	"""A device's outcome, by these rules:
	- never started (cancelled first) → cancelled
	- not applied (no connection, no config mode, a failed commit) → failed
	- interrupted (the device stopped answering before the last command)
	  → partial, the commands sent counted
	- not judged (verification off, or a snapshot couldn't be taken) → not
	  verified
	- judged: any command rejected → partial when another was proven, else
	  failed; any in review → review; else success

	:param push: None if the device never started
	:returns: (status, commands_sent, commands proven by the configuration -
	 None when not judged)"""
	if push is None:
		return DeviceStatus.CANCELLED, 0, None
	if not push.applied:
		return DeviceStatus.FAILED, 0, None
	sent = len(push.replies)
	if push.interrupted:
		return DeviceStatus.PARTIAL, push.sent or sent, None
	if push.outcome is None:
		return DeviceStatus.NOT_VERIFIED, sent, None
	proven = sum(1 for j in push.outcome.commands if j.verdict in CERTAIN)
	if push.outcome.rejected:
		status = DeviceStatus.PARTIAL if proven else DeviceStatus.FAILED
	else:
		status = DeviceStatus.REVIEW if push.outcome.needs_review else DeviceStatus.SUCCESS
	return status, sent, proven


def replied(push: PushResult | None, commands: Sequence[str]) -> list[dict[str, Any]] | None:
	"""What a device that wasn't judged replied, as stored
	(device_results.replies): each command it answered with its reply's
	lines, redacted as the log is. Shown, not judged - without the
	configuration a refusal can't be told from a notice.

	:param commands: the commands as sent to this device
	:returns: None when the push was judged (its transcript has the replies)
	 or the device said nothing"""
	if push is None or push.outcome is not None:
		return None
	answered = [{"command": redact(command), "reply": [redact(line) for line in said]}
	            for command, said in zip(commands, push.replies) if said]
	return answered or None


def needs_decision(transcript: dict[str, Any] | None) -> bool:
	""":returns: whether a judged device holds a command a person may
	 overturn or must decide - rejected or in review (an interrupted or
	 unjudged device has no transcript)"""
	if not transcript:
		return False
	return any(c.get("verdict") in (Verdict.REVIEW, Verdict.REJECTED)
	           for c in transcript.get("commands", ()))


def transcript(outcome: Outcome | None,
               replies: tuple[tuple[str, ...], ...]) -> dict[str, Any] | None:
	"""A device's judged push as stored (device_results.transcript): per
	command its verdict, the device lines it rests on, why, and what the
	device replied; then the changes no command explains, and every changed
	line with what made it. Redacted as the log is.

	:returns: None when the push wasn't judged"""
	if outcome is None:
		return None

	def entry(i: int, j: Judgment) -> dict[str, Any]:
		return {"command": redact(j.command), "verdict": j.verdict.value,
		        "evidence": [redact(e) for e in j.evidence], "note": j.note,
		        "reply": [redact(r) for r in (replies[i] if i < len(replies) else ())]}

	return {"commands": [entry(i, j) for i, j in enumerate(outcome.commands)],
	        "other_changes": [redact(c) for c in outcome.other_changes],
	        # every changed line: the commands it rests on (their positions
	        # above), none (another person) or the device's own churn
	        "changes": [{"sign": c.sign, "line": redact(c.line), "section": redact(c.section),
	                     "by": list(c.by), "review": list(c.review), "device": c.device}
	                    for c in outcome.changes]}


def diff_tags(diff: str, stored: dict[str, Any] | None) -> list[dict[str, Any] | None] | None:
	"""Each line of a stored before → after diff with what made it, from the
	transcript's changes. Matched by the line as the transcript keeps it
	(redacted: a secret line in the diff matches its "<redacted>" twin), in
	order, so a line repeated in two sections takes its own change.

	:param stored: the device's transcript
	:returns: per diff line its change ({sign, line, section, by, review,
	 device}) or None (context, headers); None when the transcript has no
	 changes (judged before they were kept)"""
	changes = (stored or {}).get("changes")
	if changes is None:
		return None
	waiting: dict[str, list[dict[str, Any]]] = {}
	for change in changes:
		waiting.setdefault(change["sign"] + change["line"].strip(), []).append(change)
	tags: list[dict[str, Any] | None] = []
	for line in diff.splitlines():
		sign = line[:1]
		if sign not in "+-" or line[:3] in ("+++", "---"):
			tags.append(None)
			continue
		queue = waiting.get(sign + redact(line[1:]).strip())
		tags.append(queue.pop(0) if queue else None)
	return tags


class DeviceResultDict(TypedDict):
	"""One device's outcome - a device_results row's fields."""
	device_ip: str
	device_port: int
	device_type: str
	commands_sent: int
	commands_verified: int | None   # proven by the configuration; None: not judged
	fetched_config: str | None      # the configuration after, kept for a review
	config_diff: str | None         # the configuration before → after
	transcript: dict[str, Any] | None
	replies: list[dict[str, Any]] | None   # a device not judged: what it replied (replied())
	status: str
	action_needed: str | None   # what only a person can resolve, or None


@dataclass(slots=True, kw_only=True)
class RolloutOptions:
	"""How a rollout runs."""
	verify: bool = False
	verbose: bool = False
	webapp: bool = False      # log for the page (HTML) rather than a console
	max_workers: int = 10     # devices configured at the same time


@dataclass(kw_only=True)
class Device:
	"""A rollout target: where it is, how to log in (credentials decrypted),
	and its variable mappings with the attribute values they read."""
	ip: str
	label: str
	username: str
	password: str = field(repr=False)
	device_type: str
	secret: str = field(repr=False)
	port: int
	# $$TOKEN$$ -> (attribute name, index in a list attribute or None)
	var_map_subs: dict[str, tuple[str, int | None]] = field(
		default_factory=dict)
	# the attribute values the rolling-out user sees: text, or a list of texts
	extra: dict[str, Any] = field(default_factory=dict)
	# added to a bare name when connecting (System Settings; the CLI: none -
	# the computer's own search domains apply)
	dns_suffix: str = ""

	@property
	def host(self) -> str:
		"""What is connected to: the address, a bare name with the DNS suffix
		(`ip` stays as saved - logs, results and labels show it)."""
		return qualify(self.ip, self.dns_suffix)

	@property
	def endpoint(self) -> str:
		"""ip:port - what identifies a reachable target (the IP alone doesn't:
		NAT / port forwarding put several devices behind one address)."""
		return endpoint(self.ip, self.port)

	def target(self, addresses: Mapping[str, str] | None = None) -> str:
		"""The box it is, as ip:port: a hostname counts as the address its
		reachability check resolved it to, so one router saved by name and by
		its IP is one target (the same-target check, the device locks); a name
		without one is compared as written.

		:param addresses: hostname (as saved) -> address (resolved_addresses)"""
		host = self.host
		address = host if is_ip(host) else (addresses or {}).get(self.ip, host)
		return endpoint(address, self.port)

	def unresolved(self, commands: list[str]) -> list[str]:
		"""Why the commands can't be filled in for this device - checked by the
		launch and rollback routes before a rollout is queued, and again at the
		push (a value can go while a job waits).

		:param commands: the commands it would get
		:returns: one line per token the commands use that has no mapping on
		 the device, or whose value is missing (an index past the end); in
		 order, each once; [] when every token resolves"""
		problems: list[str] = []
		seen: set[str] = set()
		for command in commands:
			for token in TOKEN_IN_COMMAND.findall(command):
				if token in seen:
					continue
				seen.add(token)
				if token not in self.var_map_subs:
					problems.append(f"{token}: no mapping on this device")
					continue
				property_name, index = self.var_map_subs[token]
				if missing := missing_value(self.extra, property_name, index):
					problems.append(f"{token}: {missing}")
		return problems

	def commands_for(self, commands: list[str]) -> list[str]:
		"""The commands with the device's $$TOKEN$$s replaced by its values.

		:raises SubstitutionError: a token the commands use can't be filled in
		 (unresolved: no mapping on the device, or its value gone - e.g.
		 removed from a global device while the job was queued)"""
		if problems := self.unresolved(commands):
			raise SubstitutionError("; ".join(problems))
		commands_copy = commands.copy()
		for token, (property_name, index) in self.var_map_subs.items():
			if not any(token in command for command in commands_copy):
				continue      # bound, but not used by these commands
			property_value = self.extra[property_name]
			if index is not None:
				property_value = property_value[index]

			property_value = str(property_value).strip()
			commands_copy = [command.replace(token, property_value)
			                 for command in commands_copy]
		return commands_copy

	@classmethod
	def from_inventory(cls, row: "Inventory", user_id: uuid.UUID,
	                   profile: "SecurityProfile | None",
	                   attributes: dict[str, Any] | None = None,
	                   dns_suffix: str = "") -> "Device":
		"""A rollout target from an inventory row, credentials decrypted.

		:param user_id: whose mappings apply - only their own (the join table
		 is shared across users through global devices)
		:param profile: the credentials to log in with: the profile of the
		 person who rolls out (InventoryView.profiles - never anyone else's)
		:param attributes: the device's attribute values as that user sees
		 them (the web app: InventoryView.attributes - the device's system
		 values and the user's own custom ones); None: the row's var_maps
		:param dns_suffix: added to a bare name when connecting
		:raises ValueError: no profile"""
		mappings = [m for m in row.var_mappings if m.user_id == user_id]
		if profile is None:
			raise ValueError(f"you have no security profile on {row.label or row.ip}")

		parsed_mappings = {m.token: (m.property_name, m.index) for m in
		                   mappings}

		return cls(ip=row.ip, label=row.label, device_type=row.device_type,
		           port=row.port, username=profile.username,
		           password=encryption.decrypt(profile.password_secret),
		           secret=encryption.decrypt(profile.enable_secret) if
		           profile.enable_secret else "",
		           var_map_subs=parsed_mappings,
		           extra=(row.var_maps or {}) if attributes is None else attributes,
		           dns_suffix=dns_suffix)


class RolloutEngine:
	"""One rollout: its devices, its commands and how it runs."""

	def __init__(self, param: RolloutOptions, devices: list[Device],
	             commands: list[str]) -> None:
		""":param param: verify, the parallelism (and how it logs)
		:param commands: pushed in order, $$TOKEN$$s resolved per device"""
		self._devices = devices
		self._verify_flag = param.verify
		self._webapp = param.webapp
		self._max_workers = param.max_workers
		self._commands = commands
		self._report: RunReport | None = None   # the run's, from its logger

	@property
	def device_count(self) -> int:
		""":returns: how many devices it targets"""
		return len(self._devices)

	def cancelled_results(self) -> list[DeviceResultDict]:
		"""Every device recorded as cancelled - for a job that never
		started."""
		return [DeviceResultDict(device_ip=d.ip, device_port=int(d.port),
		                         device_type=d.device_type, commands_sent=0,
		                         commands_verified=None, fetched_config=None,
		                         config_diff=None, transcript=None, replies=None,
		                         status=DeviceStatus.CANCELLED, action_needed=None)
		        for d in self._devices]

	def _reporting(self, notifier: Notifier) -> RunReport:
		""":returns: the rollout's report, logging through `notifier` (made
		 at its first use)"""
		if self._report is None:
			self._report = RunReport(notifier)
		return self._report

	def _push_device(self, device: Device, cancel_event: threading.Event,
	                 report: RunReport) -> PushResult | None:
		"""One device's push (NetmikoSession.push), its $$TOKEN$$s resolved
		first. Called concurrently by _push_config via ThreadPoolExecutor.

		:param cancel_event: set: a device not connected yet is skipped
		:returns: its PushResult; None if cancelled before connecting"""
		if cancel_event and cancel_event.is_set():
			return None
		# Resolve $$TOKEN$$s before opening SSH: a device whose mappings can't
		# resolve fails on its own with a clear reason, and is never touched
		try:
			commands = device.commands_for(self._commands)
		except SubstitutionError as e:
			report.notify(f"{device.endpoint} skipped - {e}", Tone.ERROR)
			return PushResult(applied=False)
		result = NetmikoSession(device, report).push(commands, verify=self._verify_flag)
		if result.outcome is not None:
			self._log_outcome(device, result.outcome, result.replies, report)
		return result

	@staticmethod
	def _log_outcome(device: Device, outcome: Outcome, replies: tuple[tuple[str, ...], ...],
	                 report: RunReport) -> None:
		"""Each command's verdict with the device line it rests on (or why
		not), then the changes no command explains. A command in review is
		always shown (the console too, as ACTION NEEDED is), with what the
		device replied to it."""
		for i, j in enumerate(outcome.commands):
			if j.verdict == Verdict.NAVIGATION:
				continue
			unproven = j.verdict in (Verdict.REVIEW, Verdict.REJECTED)
			shown = j.evidence[0].strip() if j.evidence else ""
			said = " | ".join(replies[i]) if i < len(replies) and \
				(unproven or j.verdict == Verdict.EXEC) else ""
			detail = "; ".join(part for part in (shown, j.note, f"the device replied: {said}" if said else "")
			                   if part)
			tone = Tone.ERROR if j.verdict == Verdict.REJECTED else \
				Tone.WARNING if j.verdict == Verdict.REVIEW else \
				Tone.INFO if j.verdict == Verdict.EXEC else Tone.SUCCESS
			report.notify(f"{device.endpoint}: '{j.command.strip()}' {j.verdict}"
			              + (f" - {detail}" if detail else ""), tone, important=unproven)
		for change in outcome.other_changes:
			report.notify(f"{device.endpoint}: changed, not by these commands: {change}",
			              Tone.WARNING)

	def _push_config(self, cancel_event: threading.Event,
	                 notifier: Notifier) -> tuple[bool, dict[int, PushResult]]:
		"""Push every device, max_workers at a time.
		A cancel stops devices that haven't connected yet; devices already
		mid-push finish their commands (a half-applied config is worse than
		either state). Every result is collected - returning at the first
		cancelled device used to drop the results of devices still in flight,
		recording them as cancelled although their config was applied.

		:param cancel_event: set by a cancel (the page) or Ctrl+C (the CLI)
		:returns: (cancelled, push_results): push_results maps each device's
		 index in self._devices to its result; devices that never connected
		 are absent"""
		report = self._reporting(notifier)
		# Keyed by position, not IP: several devices can share an IP (NAT /
		# port forwarding, overlapping address space) and must not overwrite
		# each other's result
		push_results: dict[int, PushResult] = {}
		cancelled = False

		def collect(done: Iterable[Future[PushResult | None]]) -> None:
			nonlocal cancelled
			for future in done:
				result = future.result()
				if result is None:
					if not cancelled:
						report.notify("Rollout Canceled By User", Tone.ERROR)
					cancelled = True
					continue
				push_results[futures[future]] = result

		executor = ThreadPoolExecutor(max_workers=self._max_workers)
		try:
			futures = {
				executor.submit(self._push_device, device, cancel_event,
				                report): idx
				for idx, device in enumerate(self._devices)
			}
			try:
				collect(as_completed(futures))
			except KeyboardInterrupt:
				# Ctrl+C (the CLI): the devices not reached yet are skipped,
				# those being configured finish and are recorded; a second
				# Ctrl+C leaves at once (the CLI exits without waiting)
				cancel_event.set()
				report.notify("Interrupted - the devices being configured now "
				              "finish, the rest are skipped (Ctrl+C again to "
				              "quit at once)", Tone.ERROR, important=True)
				cancelled = True
				collect(as_completed([f for f in futures
				                      if futures[f] not in push_results]))
		finally:
			# not waiting here: after a second Ctrl+C the CLI leaves at once
			executor.shutdown(wait=False, cancel_futures=True)
		return cancelled, push_results

	def _results(self, push_results: dict[int, PushResult],
	             report: RunReport) -> list[DeviceResultDict]:
		""":returns: each device's outcome (classify), in the devices' order"""
		results: list[DeviceResultDict] = []
		for idx, device in enumerate(self._devices):
			push = push_results.get(idx)
			status, commands_sent, commands_verified = classify(push)
			if push is not None and push.outcome is not None:
				judged = [j for j in push.outcome.commands
				          if j.verdict not in (Verdict.NAVIGATION, Verdict.EXEC)]
				rejected = sum(1 for j in judged if j.verdict == Verdict.REJECTED)
				review = sum(1 for j in judged if j.verdict == Verdict.REVIEW)
				report.notify(f"{device.endpoint}: {len(judged) - rejected - review} of {len(judged)} "
				              f"commands proven by the configuration"
				              + (f", {rejected} rejected" if rejected else "")
				              + (f", {review} for review" if review else ""), important=True)
			results.append(DeviceResultDict(
				device_ip=device.ip, device_port=int(device.port),
				device_type=device.device_type, commands_sent=commands_sent,
				commands_verified=commands_verified,
				fetched_config=push.after if push and push.outcome is not None and (
					push.outcome.needs_review or push.outcome.rejected) else None,
				config_diff=push.diff if push else None,
				transcript=transcript(push.outcome, push.replies) if push else None,
				replies=replied(push, self._sent(device)) if push and any(push.replies) else None,
				status=status, action_needed=report.actions(device.endpoint)))
		return results

	def _sent(self, device: Device) -> list[str]:
		""":returns: the commands as sent to the device (its tokens filled
		 in); [] when they can't be (it was skipped)"""
		try:
			return device.commands_for(self._commands)
		except SubstitutionError:
			return []

	def _log_summary(self, results: list[DeviceResultDict], report: RunReport,
	                 replied: Sequence[str] = ()) -> None:
		"""Final line, counted from the per-device statuses (it used to
		report every attempted device as configured).

		:param replied: the devices not judged that replied to a command"""
		counts = Counter(r["status"] for r in results)
		parts = [f"{counts[s]} {s}" for s in DeviceStatus if counts[s]]
		ok = counts[DeviceStatus.SUCCESS]
		color = (Tone.SUCCESS if ok == len(results)
		         else Tone.ERROR if counts[DeviceStatus.FAILED] + counts[DeviceStatus.CANCELLED] == len(results)
		         else Tone.WARNING)
		report.notify(f"Configuration rollout complete: "
		              f"{', '.join(parts)} (of {len(results)} devices)",
		              color, important=True)
		if report.needs_action:
			count = len(report.needs_action)
			report.notify(f"ACTION NEEDED on {count} device"
			              f"{'s' if count != 1 else ''} "
			              f"({', '.join(sorted(report.needs_action))}) - see the "
			              f"lines marked ACTION NEEDED", Tone.ERROR, important=True)
		rejected = sorted(endpoint(r["device_ip"], r["device_port"]) for r in results
		                  if any(c["verdict"] == Verdict.REJECTED
		                         for c in (r.get("transcript") or {}).get("commands", ())))
		if rejected:
			where = ("confirm it in Results → Verification" if self._webapp
			         else "check it on the device")
			report.notify(f"REJECTED commands on {len(rejected)} device{'s' if len(rejected) != 1 else ''} "
			              f"({', '.join(rejected)}) - not in the configuration, and the device replied: "
			              f"see the lines marked rejected. A command whose effect isn't kept in the "
			              f"configuration (generating keys) is rejected too - {where}",
			              Tone.ERROR, important=True)
		review = sorted(endpoint(r["device_ip"], r["device_port"]) for r in results
		                if r["status"] == DeviceStatus.REVIEW)
		if review:
			where = "decide them in Results → Verification" if self._webapp else "decide them on the devices"
			report.notify(f"REVIEW on {len(review)} device{'s' if len(review) != 1 else ''} "
			              f"({', '.join(review)}) - NetRollout couldn't prove every command there: "
			              f"check the lines marked review, then {where}", Tone.WARNING, important=True)
		if counts[DeviceStatus.NOT_VERIFIED]:
			why = ("the configuration couldn't be read (the reason is above)" if self._verify_flag
			       else "verification was off - turn it on (Verify after push; the CLI's -vf) "
			            "to have NetRollout check the result")
			report.notify(f"Not verified: {why}", Tone.WARNING, important=True)
		if replied:
			report.notify(f"{len(replied)} device{'s' if len(replied) != 1 else ''} not verified "
			              f"replied to some commands ({', '.join(replied)}) - read the lines "
			              f"marked 'the device replied': without Verify NetRollout can't tell a "
			              f"refusal from a notice", Tone.WARNING, important=True)
		report.notify(
			f"Please see Execution logs in {os.path.abspath(report.notifier.logfile)}",
			important=True)

	def run(self, cancel_flag: threading.Event, logger: Notifier) -> list[
		DeviceResultDict]:
		"""The whole rollout: push (each device verified in its own session,
		when on), classify each device, log the summary.

		:param cancel_flag: set to cancel - devices not reached yet are skipped
		:param logger: where it logs (a RolloutLogger)
		:returns: one result per device, in the devices' order; [] when there
		 are no devices or no commands"""
		report = self._reporting(logger)
		report.notify("Starting configuration rollout", important=True)
		if not (self._devices and self._commands):
			report.notify("Device input invalid", Tone.ERROR)
			return []
		_, push_results = self._push_config(cancel_flag, logger)
		results = self._results(push_results, report)
		# not judged, but the device said something to a command: named in
		# the summary (what it said is never judged - it may be "new entry
		# added" as well as an error)
		replied = sorted(self._devices[idx].endpoint for idx, push in push_results.items()
		                 if push.applied and push.outcome is None and any(push.replies))
		self._log_summary(results, report, replied)
		return results
