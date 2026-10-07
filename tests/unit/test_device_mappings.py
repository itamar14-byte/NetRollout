"""Device.from_inventory + substitution: global devices share one mapping
join table across users, so only the rolling-out user's mappings may apply."""
import os
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
from src.core import Device, RolloutEngine, RolloutOptions, mapping_resolvable
from src.logging_utils import RolloutLogger

USER_A, USER_B, USER_C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture(autouse=True)
def cipher():
	"""A fresh encryption key for every test, so profiles can be encrypted."""
	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	enc.init_encryption(None)


def mapping(token, prop, user_id, index=None):
	return SimpleNamespace(token=token, property_name=prop, index=index,
	                       user_id=user_id)


def global_row(**overrides):
	"""A global inventory row (CORE-X) with an encrypted profile and the token
	$$HOST$$ bound by USER_A to hostname and by USER_B to vrfs[1]; overrides win."""
	row = SimpleNamespace(
		ip="10.50.0.1", label="CORE-X", device_type="cisco_ios", port=22,
		var_maps={"hostname": "core-x", "vrfs": ["red", "blue"]},
		security_profile=SimpleNamespace(
			username="netops", password_secret=enc.encrypt("pw"),
			enable_secret=enc.encrypt("en")),
		# same token bound by two users to different properties
		var_mappings=[mapping("$$HOST$$", "hostname", USER_A),
		              mapping("$$HOST$$", "vrfs", USER_B, index=1)])
	for k, v in overrides.items():
		setattr(row, k, v)
	return row


def test_only_rolling_out_users_mappings_are_applied():
	"""from_inventory keeps only the given user's mappings: USER_A gets hostname,
	USER_B vrfs[1], USER_C (no binding) none."""
	row = global_row()
	assert Device.from_inventory(row, USER_A).var_map_subs == \
	       {"$$HOST$$": ("hostname", None)}
	assert Device.from_inventory(row, USER_B).var_map_subs == \
	       {"$$HOST$$": ("vrfs", 1)}
	assert Device.from_inventory(row, USER_C).var_map_subs == {}


def test_substitution_uses_each_users_own_binding():
	"""The same command substitutes per user: "hostname core-x" for USER_A,
	"hostname blue" for USER_B, and the token untouched for USER_C."""
	row = global_row()
	engine = RolloutEngine(RolloutOptions(), [], ["hostname $$HOST$$"])
	assert engine._substitute_commands(Device.from_inventory(row, USER_A)) == \
	       ["hostname core-x"]
	assert engine._substitute_commands(Device.from_inventory(row, USER_B)) == \
	       ["hostname blue"]
	# a user with no binding pushes the token untouched
	assert engine._substitute_commands(Device.from_inventory(row, USER_C)) == \
	       ["hostname $$HOST$$"]


def test_credentials_are_decrypted_from_assigned_profile():
	"""The device's username, password and enable secret come decrypted from
	its security profile."""
	device = Device.from_inventory(global_row(), USER_C)
	assert (device.username, device.password, device.secret) == \
	       ("netops", "pw", "en")


def test_missing_enable_secret_becomes_empty_string():
	"""A profile without an enable secret gives the device secret ""."""
	row = global_row()
	row.security_profile.enable_secret = None
	assert Device.from_inventory(row, USER_A).secret == ""


@pytest.mark.parametrize("var_maps,prop,index,expected", [
	({"hostname": "r1"}, "hostname", None, True),
	({"hostname": ""}, "hostname", None, False),          # set but empty
	({}, "hostname", None, False),                        # missing
	(None, "hostname", None, False),
	({"vrfs": ["a", "b"]}, "vrfs", 1, True),
	({"vrfs": ["a", "b"]}, "vrfs", 2, False),             # index out of range
	({"vrfs": ["a"]}, "vrfs", -1, False),
	({"hostname": "r1"}, "hostname", 0, False),           # indexing a string
])
def test_mapping_resolvable(var_maps, prop, index, expected):
	"""mapping_resolvable is True only for a set, non-empty attribute (and, when
	indexed, a list long enough); empty, missing, None var_maps, out-of-range or
	negative indexes and indexing a string are False."""
	assert mapping_resolvable(var_maps, prop, index) is expected


def test_unresolvable_device_fails_alone_without_ssh(monkeypatch):
	"""A device whose mapping can't resolve fails alone: e.g. an admin removed an
	attribute from a global device after users bound mappings to it. It is never
	connected to and is "failed", while the other device is pushed, verified and
	"success"."""
	ok_row = global_row(ip="10.0.0.1", var_mappings=[
		mapping("$$HOST$$", "hostname", USER_A)])
	broken_row = global_row(ip="10.0.0.2", var_maps={}, var_mappings=[
		mapping("$$HOST$$", "hostname", USER_A)])
	devices = [Device.from_inventory(r, USER_A) for r in (ok_row, broken_row)]
	engine = RolloutEngine(RolloutOptions(verify=True), devices,
	                       ["hostname $$HOST$$"])
	conn = MagicMock()
	conn.send_config_set.return_value = "ok"
	with patch("netmiko.ConnectHandler", return_value=conn) as connect, \
			patch.object(Device, "fetch_config", return_value="hostname core-x"):
		results = engine.run(threading.Event(), RolloutLogger(False, False))
	assert [c.kwargs["ip"] for c in connect.call_args_list] == ["10.0.0.1"]
	assert {r["device_ip"]: r["status"] for r in results} == \
	       {"10.0.0.1": "success", "10.0.0.2": "failed"}


def test_no_security_profile_raises():
	"""from_inventory on a row without a security profile raises ValueError
	("no security profiles")."""
	with pytest.raises(ValueError, match="no security profiles"):
		Device.from_inventory(global_row(security_profile=None), USER_A)
