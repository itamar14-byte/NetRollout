"""audit.summary: an audit row's detail in one line for the audit page - the
part that answers "what happened", from every shape of detail NetRollout
writes."""
import pytest

from src import audit


@pytest.mark.parametrize("detail, line", [
	# a failed sign-in: its reason, and the directory when one was involved
	({"reason": "ldap_bind_failed"}, "reason: ldap_bind_failed"),
	({"reason": "ldap_server_inactive", "ldap_server": "Lab"}, "reason: ldap_server_inactive (Lab)"),
	# one value changed (a group rule's role, a role from the directory)
	({"from": "operator", "to": "admin"}, "operator → admin"),
	({"from": "admin", "to": "operator", "groups": []}, "admin → operator"),
	# a System Settings change
	({"key": "session_idle_minutes", "old": 15, "new": 30}, "session_idle_minutes: 15 → 30"),
	# an LDAP server's save: each setting changed, the password only as changed
	({"changes": {"is_active": {"from": True, "to": False}}}, "is_active: true → false"),
	({"changes": {"host": {"from": "a", "to": "b"}, "port": {"from": 636, "to": 389}},
	  "bind_password": "changed"}, "host: a → b, port: 636 → 389; bind password changed"),
	({"changes": {}}, "nothing changed"),
	({"created": {"host": "nrlab-ldap"}}, "created"),
	# the lockout
	({"failed_passwords": 5, "minutes": 15}, "locked 15 min after 5 wrong passwords"),
	({"failed_passwords": 10}, "disabled after 10 wrong passwords"),
	({"minutes_left": 4}, "4 min left"),
	# anything else: the first few simple values
	({"auth_type": "ldap"}, "auth_type: ldap"),
	({"count": 2, "users": ["alice", "bob", "carol", "dave"]}, "count: 2; users: alice, bob, carol (+1)"),
	({"nested": {"a": 1}, "ok": True}, "ok: true"),
	# nothing to say
	(None, ""), ({}, ""), ("not a dict", ""),
])
def test_a_rows_detail_in_one_line(detail, line):
	assert audit.summary(detail) == line


def test_a_long_summary_is_cut_to_one_line():
	"""Past SUMMARY_LIMIT characters the line is cut, with an ellipsis."""
	line = audit.summary({"changes": {f"setting_{i}": {"from": "x" * 10, "to": "y" * 10}
	                                  for i in range(10)}})
	assert len(line) == audit.SUMMARY_LIMIT and line.endswith("…")
