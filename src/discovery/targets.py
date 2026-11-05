"""What a discovery scans: the addresses an admin typed - several entries
separated by commas, spaces or semicolons, each a subnet of any prefix length
(IPv4 or IPv6), a range (10.0.0.10-10.0.0.50, or IPv4's short 10.0.0.10-50)
or one address. Overlapping entries are counted once, and the total is
counted without listing the addresses, so a /8 is refused without being
built. The page checks the same rules as they're typed; this is the word
that counts. No Flask, no project imports."""
from __future__ import annotations   # type hints are never evaluated

import ipaddress
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass

_SEPARATORS = re.compile(r"[\s,;]+")
_PREFIX = re.compile(r"\d{1,3}")
_LAST_OCTET = re.compile(r"\d{1,3}")
HUGE = 10 ** 15     # above this a count is said as a power of two

# blocks a discovery never touches: (network, what it is)
_REFUSED: tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...] = (
	(ipaddress.ip_network("0.0.0.0/8"), "not a device address (0.0.0.0/8)"),
	(ipaddress.ip_network("127.0.0.0/8"), "NetRollout's own server (loopback)"),
	(ipaddress.ip_network("224.0.0.0/4"), "multicast, not a device"),
	(ipaddress.ip_network("240.0.0.0/4"), "reserved, not a device (240.0.0.0/4)"),
	(ipaddress.ip_network("::1/128"), "NetRollout's own server (loopback)"),
	(ipaddress.ip_network("::/128"), "not a device address (::)"),
	(ipaddress.ip_network("fe80::/10"), "link-local: it needs an interface - use the device's global address"),
	(ipaddress.ip_network("ff00::/8"), "multicast, not a device"),
)


class TargetError(ValueError):
	"""The addresses can't be scanned; the message says why, for the page."""


@dataclass(frozen=True)
class Span:
	"""Consecutive addresses of one family, as integers, both ends included."""
	version: int
	first: int
	last: int

	@property
	def size(self) -> int:
		return self.last - self.first + 1


@dataclass(frozen=True)
class Targets:
	"""The addresses to scan: the entries as typed, what was read into them
	(notes for the page), the merged spans and how many addresses they hold."""
	entries: tuple[str, ...]
	notes: tuple[str, ...]
	spans: tuple[Span, ...]
	count: int

	def addresses(self) -> Iterator[str]:
		""":returns: every address, IPv4 first, each in ascending order, once"""
		for span in self.spans:
			for number in range(span.first, span.last + 1):
				yield str(ipaddress.IPv4Address(number) if span.version == 4
				          else ipaddress.IPv6Address(number))


def _bad(entry: str) -> TargetError:
	return TargetError(f'"{entry}" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)')


def _address(text: str, entry: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
	""":returns: `text` as an address
	:raises TargetError: it isn't one, or it is written with a zone or as an
	 IPv4-mapped IPv6 address"""
	if "%" in text:
		raise _bad(entry)
	try:
		address = ipaddress.ip_address(text)
	except ValueError:
		raise _bad(entry) from None
	if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
		raise TargetError(f'"{entry}": write an IPv4 address as {address.ipv4_mapped}')
	return address


def _network(entry: str, notes: list[str]) -> Span:
	"""A subnet: its usable addresses - IPv4 up to /30 without the network
	and broadcast addresses, IPv6 up to /126 without the subnet-router
	anycast; /31, /32, /127 and /128 whole (RFC 3021, RFC 6164)."""
	address_text, _, prefix_text = entry.partition("/")
	if not _PREFIX.fullmatch(prefix_text):
		raise _bad(entry)
	address = _address(address_text, entry)
	prefix, bits = int(prefix_text), address.max_prefixlen
	if prefix > bits:
		raise TargetError(f'"{entry}": an IPv{address.version} prefix is 0 to {bits}')
	network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
	if network.network_address != address:
		notes.append(f"{entry} means {network}")
	first, last = int(network.network_address), int(network.broadcast_address)
	if address.version == 4 and prefix <= 30:
		first, last = first + 1, last - 1
	elif address.version == 6 and prefix <= 126:
		first += 1
	return Span(address.version, first, last)


def _range(entry: str) -> Span:
	"""A range: both ends included, the lower first; IPv4's end may be the
	last number alone (10.0.0.10-50)."""
	start_text, _, end_text = entry.partition("-")
	if not end_text or "-" in end_text:
		raise _bad(entry)
	start = _address(start_text, entry)
	end: ipaddress.IPv4Address | ipaddress.IPv6Address
	if start.version == 4 and _LAST_OCTET.fullmatch(end_text):
		if int(end_text) > 255:
			raise TargetError(f'"{entry}": the range\'s end is 0 to 255')
		end = ipaddress.IPv4Address((int(start) & ~0xFF) + int(end_text))
	else:
		end = _address(end_text, entry)
	if end.version != start.version:
		raise TargetError(f'"{entry}" mixes IPv4 and IPv6')
	if int(end) < int(start):
		raise TargetError(f'"{entry}" runs backwards - write the lower address first')
	return Span(start.version, int(start), int(end))


def _entry(entry: str, notes: list[str]) -> Span:
	""":returns: the addresses one entry stands for
	:raises TargetError: it isn't one, or it reaches a block never scanned"""
	if "/" in entry:
		span = _network(entry, notes)
	elif "-" in entry:
		span = _range(entry)
	else:
		address = _address(entry, entry)
		span = Span(address.version, int(address), int(address))
	for network, what in _REFUSED:
		if network.version == span.version and span.first <= int(network.broadcast_address) \
				and int(network.network_address) <= span.last:
			verb = "is" if span.size == 1 else "includes"
			raise TargetError(f'"{entry}" {verb} {what}')
	return span


def _merged(spans: list[Span]) -> tuple[list[Span], bool]:
	""":returns: the spans sorted, overlapping or adjacent ones joined, and
	 whether any overlapped"""
	merged: list[Span] = []
	overlapped = False
	for span in sorted(spans, key=lambda s: (s.version, s.first)):
		last = merged[-1] if merged else None
		if last and last.version == span.version and span.first <= last.last + 1:
			overlapped = overlapped or span.first <= last.last
			merged[-1] = Span(last.version, last.first, max(last.last, span.last))
		else:
			merged.append(span)
	return merged, overlapped


def _said(count: int) -> str:
	return f"about 2^{round(math.log2(count))}" if count > HUGE else f"{count:,}"


def parse_targets(text: str, limit: int) -> Targets:
	"""Read what an admin typed into the addresses to scan.

	:param limit: the most addresses one discovery may take
	:returns: the targets
	:raises TargetError: nothing to scan, an entry that isn't an address,
	 subnet or range, one reaching a block never scanned (loopback, multicast,
	 reserved, link-local), or more than `limit` addresses in all"""
	entries = tuple(entry for entry in _SEPARATORS.split(text) if entry)
	if not entries:
		raise TargetError("Enter the addresses to scan: a subnet (10.0.0.0/24), a range (10.0.0.10-50) or an address")
	notes: list[str] = []
	spans, overlapped = _merged([_entry(entry, notes) for entry in entries])
	if overlapped:
		notes.append("overlapping entries counted once")
	count = sum(span.size for span in spans)
	if count > limit:
		raise TargetError(f"{_said(count)} addresses - one discovery takes at most {limit:,}. "
		                  "Split it, or narrow the subnet.")
	return Targets(entries, tuple(notes), tuple(spans), count)
