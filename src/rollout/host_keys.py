"""A device's SSH host key checked before its password is sent (paramiko
does the check; this decides what it checks against). A device carries a
HostKeyPolicy - off by default (the CLI, checking turned off): nothing
loaded, nothing added to Netmiko's arguments. With keys known for it they
are loaded into the connection (HostKeyCheck: a private known_hosts file
for this one connection) and any other key is refused before
authentication; with none known and learning on, the key seen is handed to
`seen` (the web app stores it: trust on first use). No database here: the
web app builds the policies (src/host_keys.py)."""
from __future__ import annotations   # type hints are never evaluated

import base64
import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import paramiko


@dataclass(frozen=True)
class HostKeyPolicy:
	"""What one device's connections check its host key against."""
	known: tuple[str, ...] = ()     # the accepted keys, "type base64" each
	learn: bool = False             # none known: accept the key seen, hand it to `seen`
	# after a checked connection: the key the device showed ("type base64")
	seen: Callable[[str], None] | None = None
	# a key refused: the one the device offered ("type base64")
	refused: Callable[[str], None] | None = None

	@property
	def checks(self) -> bool:
		""":returns: whether host keys are checked at all (off: today's behaviour)"""
		return bool(self.known) or self.learn

	def pinned(self, key: str) -> HostKeyPolicy:
		""":returns: this policy once `key` was learned: the device's later
		 connections in this run (a new session, the commit check) verify it"""
		return replace(self, known=(key,), learn=False)


OFF = HostKeyPolicy()


class HostKeyRefused(Exception):
	"""The device showed a host key that isn't an accepted one: nothing was
	sent to it (the check comes before authentication)."""

	def __init__(self, endpoint: str, offered: str) -> None:
		super().__init__(f"{endpoint}: its SSH host key changed - not signed in to. If the device was "
		                 f"replaced, an admin accepts its new key in Server Management -> Host keys.")
		self.endpoint = endpoint
		self.offered = offered


def key_line(key: paramiko.PKey) -> str:
	""":returns: a key as a known_hosts file writes it: "type base64" """
	return f"{key.get_name()} {key.get_base64()}"


def shown_key(conn: Any) -> str:
	""":returns: the host key a connected Netmiko session's device showed
	 ("type base64")"""
	return key_line(conn.remote_conn_pre.get_transport().get_remote_server_key())


def fingerprint(line: str) -> str:
	""":returns: a key's SHA-256 fingerprint as OpenSSH shows it (SHA256:...)"""
	digest = hashlib.sha256(base64.b64decode(line.split()[1])).digest()
	return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def known_hosts_name(host: str, port: int) -> str:
	""":returns: the name paramiko looks a host's keys up by: the host on
	 port 22, else [host]:port (an IPv6 address too)"""
	return host if port == 22 else f"[{host}]:{port}"


def refused_key(error: BaseException) -> str | None:
	"""The key a device was refused for, from the error its connection
	raised: Netmiko re-raises paramiko's BadHostKeyException as a
	NetmikoTimeoutException, so it is found in the chain of errors.

	:returns: the key the device offered ("type base64"); None: another error"""
	seen: set[int] = set()
	current: BaseException | None = error
	while current is not None and id(current) not in seen:
		seen.add(id(current))
		if isinstance(current, paramiko.BadHostKeyException):
			return key_line(current.key)
		current = current.__cause__ or current.__context__
	return None


class HostKeyCheck:
	"""One connection's check: with keys known, a private known_hosts file
	holding them (one per key type - paramiko keeps one per name and type)
	and Netmiko told to refuse any other key; removed on leaving."""

	def __init__(self, policy: HostKeyPolicy, host: str, port: int) -> None:
		self._policy = policy
		self._name = known_hosts_name(host, port)
		self._path: str | None = None
		self._loaded: dict[str, str] = {}     # key type -> the key in the file

	def __enter__(self) -> HostKeyCheck:
		if self._policy.known:
			for line in self._policy.known:
				self._loaded.setdefault(line.split()[0], line)
			handle, self._path = tempfile.mkstemp(prefix="netrollout-known-hosts-")
			os.close(handle)      # mkstemp: owner-only; closed so Netmiko can open it (Windows)
			self._write()
		return self

	def __exit__(self, *exc: object) -> None:
		if self._path:
			try:
				os.remove(self._path)
			except OSError:
				pass

	@property
	def args(self) -> dict[str, Any]:
		""":returns: Netmiko's arguments for it - none when nothing is known
		 (off, or learning: Netmiko's default accepts the key)"""
		if not self._path:
			return {}
		return {"ssh_strict": True, "alt_host_keys": True, "alt_key_file": self._path}

	def alternative(self, offered: str) -> bool:
		"""The device offered another accepted key of a type already loaded
		(an HA pair: the other unit answers): load that one instead.

		:returns: whether to connect again"""
		key_type = offered.split()[0]
		if offered not in self._policy.known or self._loaded.get(key_type) == offered:
			return False
		self._loaded[key_type] = offered
		self._write()
		return True

	def _write(self) -> None:
		assert self._path
		with open(self._path, "w", encoding="ascii") as out:
			out.writelines(f"{self._name} {line}\n" for line in self._loaded.values())
