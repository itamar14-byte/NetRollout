"""A device's address: an IP (v4 or v6) or a hostname - what a valid name
is, the DNS suffix a bare name gets (System Settings), and looking a name up
with a bounded wait (the system resolver can take many seconds to give up).
No project imports: the session, the engine and the input checks all use it,
and so does the CLI."""
from __future__ import annotations   # type hints are never evaluated

import ipaddress
import re
import socket
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, wait

RESOLVE_TIMEOUT = 2.0    # seconds a lookup is waited for
MAX_NAME = 253           # a DNS name's length, without a trailing dot
MAX_PARALLEL = 32        # lookups at the same time (resolve_many)
# one label of a name: letters, digits and hyphens, 1-63, no hyphen at an end
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)


def is_ip(value: str) -> bool:
	""":returns: whether `value` is an IPv4 or IPv6 address"""
	try:
		ipaddress.ip_address(value)
		return True
	except ValueError:
		return False


def is_hostname(value: str) -> bool:
	"""A DNS name: labels of letters, digits and hyphens (1-63 each, no
	hyphen first or last), 253 at most, a trailing dot allowed (an absolute
	name). A last label of digits only is refused: 10.0.0 or 300.1.1.1 is a
	mistyped IP, not a name.

	:returns: whether `value` is one"""
	name = value[:-1] if value.endswith(".") else value
	if not name or len(name) > MAX_NAME:
		return False
	labels = name.split(".")
	return all(_LABEL.fullmatch(label) for label in labels) and not labels[-1].isdigit()


def is_bare(address: str) -> bool:
	""":returns: whether `address` is a name without a domain (core1) - the
	 one kind the DNS suffix is added to (an IPv4 has dots, an IPv6 colons)"""
	return "." not in address and ":" not in address


def qualify(address: str, suffix: str) -> str:
	"""The name NetRollout connects to: a bare name with the DNS suffix
	(core1 + corp.local = core1.corp.local); a full name or an IP as it is.

	:param suffix: System Settings' DNS suffix; "" for none"""
	suffix = suffix.strip().strip(".")
	return f"{address}.{suffix}" if suffix and is_bare(address) else address


def _lookup(host: str) -> list[str]:
	""":returns: the host's addresses, in the resolver's order, each once
	:raises OSError: it doesn't resolve"""
	found: list[str] = []
	for *_, sockaddr in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
		# an IPv6 link-local answer carries its zone (%eth0): not part of the address
		address = str(ipaddress.ip_address(str(sockaddr[0]).split("%")[0]))
		if address not in found:
			found.append(address)
	return found


def primary(addresses: Iterable[str]) -> str | None:
	"""The one address kept for a name (the "same device" warning compares
	it): its first IPv4 address - management addresses nearly always are,
	and a dual-stack name often answers ::1-style IPv6 first -, else its first.

	:returns: None for no addresses"""
	listed = list(addresses)
	return next((a for a in listed if ":" not in a), listed[0] if listed else None)


def resolve(host: str, timeout: float = RESOLVE_TIMEOUT) -> list[str] | None:
	"""Look a host up, waiting at most `timeout` seconds (a slow lookup goes
	on in the background and is ignored).

	:returns: its addresses (an IP: itself); None when it doesn't resolve in time"""
	return resolve_many([host], timeout)[host]


def resolve_many(hosts: Iterable[str], timeout: float = RESOLVE_TIMEOUT
                 ) -> dict[str, list[str] | None]:
	"""Look several hosts up at once (a CSV import), all within `timeout`.

	:returns: each host's addresses (an IP: itself), None when it didn't resolve in time"""
	results: dict[str, list[str] | None] = {}
	names = []
	for host in dict.fromkeys(hosts):
		if is_ip(host):
			results[host] = [str(ipaddress.ip_address(host))]
		else:
			names.append(host)
	if not names:
		return results
	pool = ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(names)))
	try:
		futures = {name: pool.submit(_lookup, name) for name in names}
		wait(futures.values(), timeout=timeout)
		for name, future in futures.items():
			if future.done() and future.exception() is None:
				results[name] = future.result() or None
			else:
				results[name] = None
	finally:
		pool.shutdown(wait=False, cancel_futures=True)
	return results
