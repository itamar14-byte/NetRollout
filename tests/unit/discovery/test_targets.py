"""What a discovery scans (src/discovery/targets.py): each way of writing
the addresses, the notes read into them, every refusal and its message,
overlaps counted once, and the limit counted without listing the addresses.
The mock-up's 22 inputs (docs/plans/platform-detect-and-discover.md) are
among them."""
from itertools import islice

import pytest

from src.discovery.targets import TargetError, parse_targets


LIMIT = 4096


def addresses(text, limit=LIMIT):
	return list(parse_targets(text, limit).addresses())


@pytest.mark.parametrize("text, count, first, last", [
	("10.20.0.0/27", 30, "10.20.0.1", "10.20.0.30"),           # no network or broadcast address
	("10.0.0.0/22", 1022, "10.0.0.1", "10.0.3.254"),
	("10.0.0.0/30", 2, "10.0.0.1", "10.0.0.2"),
	("10.0.0.0/31", 2, "10.0.0.0", "10.0.0.1"),                # RFC 3021: both
	("10.0.0.5/32", 1, "10.0.0.5", "10.0.0.5"),
	("10.20.0.10-50", 41, "10.20.0.10", "10.20.0.50"),         # IPv4's short end
	("10.20.0.10-10.20.1.20", 267, "10.20.0.10", "10.20.1.20"),
	("10.20.0.7", 1, "10.20.0.7", "10.20.0.7"),
	("2001:db8::/120", 255, "2001:db8::1", "2001:db8::ff"),    # no subnet-router anycast
	("2001:db8::/127", 2, "2001:db8::", "2001:db8::1"),        # RFC 6164: both
	("2001:db8::1-2001:db8::ff", 255, "2001:db8::1", "2001:db8::ff"),
	("2001:DB8::A", 1, "2001:db8::a", "2001:db8::a"),          # written as Python writes it
])
def test_each_form_holds_the_addresses_it_names(text, count, first, last):
	"""A subnet, a range (long or short) and one address, IPv4 and IPv6:
	the count, the first and the last address are exact."""
	targets = parse_targets(text, LIMIT)
	found = list(targets.addresses())
	assert (targets.count, len(found), found[0], found[-1]) == (count, count, first, last)
	assert targets.entries == (text,)


def test_host_bits_mean_the_network_and_say_so():
	"""10.20.0.5/24 is the /24 it's in, with a note saying so."""
	targets = parse_targets("10.20.0.5/24", LIMIT)
	assert targets.notes == ("10.20.0.5/24 means 10.20.0.0/24",)
	assert targets.count == 254


@pytest.mark.parametrize("text", [
	"10.0.0.1, 10.0.0.2 10.0.0.3;10.0.0.4",
	" 10.0.0.1 ,, 10.0.0.2\n10.0.0.3 ;; 10.0.0.4 ",
])
def test_entries_split_on_commas_spaces_and_semicolons(text):
	"""Commas, spaces, new lines and semicolons all separate entries, empty
	ones dropped."""
	targets = parse_targets(text, LIMIT)
	assert targets.entries == ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4")
	assert list(targets.addresses()) == ["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]


def test_overlapping_entries_are_counted_once_and_said():
	"""A range inside a subnet adds nothing, and the note says so."""
	targets = parse_targets("10.20.0.0/27, 10.20.0.10-20", LIMIT)
	assert targets.count == 30
	assert len(set(targets.addresses())) == 30
	assert targets.notes == ("overlapping entries counted once",)


def test_adjacent_entries_join_without_an_overlap_note():
	"""10.0.0.1-10 and 10.0.0.11-20 touch, they don't overlap: 20 addresses,
	no note."""
	targets = parse_targets("10.0.0.11-20, 10.0.0.1-10", LIMIT)
	assert (targets.count, targets.notes) == (20, ())
	assert len(targets.spans) == 1


def test_addresses_come_ipv4_first_each_in_ascending_order():
	"""Whatever order the entries were typed in."""
	assert addresses("2001:db8::5, 10.0.0.9, 2001:db8::1, 10.0.0.2") == \
		["10.0.0.2", "10.0.0.9", "2001:db8::1", "2001:db8::5"]


def test_the_limit_is_inclusive():
	"""Exactly the limit is fine; one more is refused."""
	assert parse_targets("10.0.0.1-100", 100).count == 100
	with pytest.raises(TargetError, match=r"^101 addresses - one discovery takes at most 100\. "
	                                      r"Split it, or narrow the subnet\.$"):
		parse_targets("10.0.0.1-101", 100)


@pytest.mark.parametrize("text, limit, said", [
	("10.0.0.0/8", LIMIT, "16,777,214"),
	("2001:db8::/64", LIMIT, "about 2^64"),
	("10.0.0.0/22, 10.1.0.0/22, 10.2.0.0/22, 10.3.0.0/22, 10.4.0.0/30", 4089, "4,090"),
])
def test_too_many_addresses_are_refused_with_the_count(text, limit, said):
	"""Over the limit in all (one entry or several) is refused, the count
	said - a huge IPv6 one as a power of two."""
	with pytest.raises(TargetError) as refused:
		parse_targets(text, limit)
	assert str(refused.value) == f"{said} addresses - one discovery takes at most {limit:,}. " 	                             "Split it, or narrow the subnet."


def test_a_huge_subnet_is_refused_without_being_listed():
	"""A /32 of IPv6 (2^96 addresses) is refused at once: counted, not
	built (listing it wouldn't end)."""
	with pytest.raises(TargetError, match=r"^about 2\^96 addresses"):
		parse_targets("2001:db8::/32", LIMIT)


def test_the_addresses_are_listed_only_when_asked():
	"""A big but allowed set is held as spans; listing the first few doesn't
	build the rest."""
	targets = parse_targets("10.0.0.0/20", LIMIT)
	assert list(islice(targets.addresses(), 3)) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


@pytest.mark.parametrize("text, message", [
	("", "Enter the addresses to scan: a subnet (10.0.0.0/24), a range (10.0.0.10-50) or an address"),
	(" , ; ", "Enter the addresses to scan: a subnet (10.0.0.0/24), a range (10.0.0.10-50) or an address"),
	("core1", '"core1" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.20.0.300", '"10.20.0.300" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.20.0.0/24/1", '"10.20.0.0/24/1" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.20.0.0/", '"10.20.0.0/" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.0.0.0/255.255.255.0", '"10.0.0.0/255.255.255.0" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.0.0.1-", '"10.0.0.1-" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.0.0.1-5-9", '"10.0.0.1-5-9" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("010.0.0.1", '"010.0.0.1" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("fe80::1%eth0", '"fe80::1%eth0" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	("10.20.0.0/33", '"10.20.0.0/33": an IPv4 prefix is 0 to 32'),
	("2001:db8::/129", '"2001:db8::/129": an IPv6 prefix is 0 to 128'),
	("10.20.0.10-256", '"10.20.0.10-256": the range\'s end is 0 to 255'),
	("10.20.0.50-10", '"10.20.0.50-10" runs backwards - write the lower address first'),
	("10.20.1.1-10.20.0.1", '"10.20.1.1-10.20.0.1" runs backwards - write the lower address first'),
	("10.20.0.1-2001:db8::1", '"10.20.0.1-2001:db8::1" mixes IPv4 and IPv6'),
	("::ffff:10.0.0.1", '"::ffff:10.0.0.1": write an IPv4 address as 10.0.0.1'),
	("127.0.0.1", '"127.0.0.1" is in 127.0.0.0/8: loopback - NetRollout\'s own server'),
	("::1", '"::1" is in ::1/128: loopback - NetRollout\'s own server'),
	("126.255.255.250-127.0.0.3", '"126.255.255.250-127.0.0.3" reaches into 127.0.0.0/8: loopback - NetRollout\'s own server'),
	("0.0.0.0", '"0.0.0.0" is in 0.0.0.0/8: no device addresses'),
	("::", '"::" is in ::/128: no device address'),
	("224.0.0.0/24", '"224.0.0.0/24" reaches into 224.0.0.0/4: multicast, not devices'),
	("ff02::1", '"ff02::1" is in ff00::/8: multicast, not devices'),
	("255.255.255.255", '"255.255.255.255" is in 240.0.0.0/4: reserved, not devices'),
	("fe80::1", '"fe80::1" is in fe80::/10: link-local - scan the devices\' global addresses'),
	("10.0.0.0/1", '"10.0.0.0/1" reaches into 0.0.0.0/8: no device addresses'),
])
def test_each_refusal_says_why(text, message):
	"""Nothing to scan, what isn't an address / subnet / range, a prefix or a
	range end out of bounds, a backwards or mixed range, a mapped address,
	and anything reaching loopback, 0/8, ::, multicast, 240/4 or link-local
	- each refused with its own message, naming the entry."""
	with pytest.raises(TargetError) as refused:
		parse_targets(text, LIMIT)
	assert str(refused.value) == message


def test_one_bad_entry_refuses_the_whole_text():
	"""The good entries around it don't make it through."""
	with pytest.raises(TargetError, match='^"core1" '):
		parse_targets("10.0.0.0/24, core1, 10.0.1.5", LIMIT)
