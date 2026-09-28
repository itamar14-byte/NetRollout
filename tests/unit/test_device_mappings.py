"""Device.from_inventory + substitution: global devices share one mapping
join table across users, so only the rolling-out user's mappings may apply."""
import os
import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
from src.core import Device, RolloutEngine, RolloutOptions

USER_A, USER_B, USER_C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture(autouse=True)
def cipher():
	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	enc.init_encryption(None)


def mapping(token, prop, user_id, index=None):
	return SimpleNamespace(token=token, property_name=prop, index=index,
	                       user_id=user_id)


def global_row(**overrides):
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
	row = global_row()
	assert Device.from_inventory(row, USER_A).var_map_subs == \
	       {"$$HOST$$": ("hostname", None)}
	assert Device.from_inventory(row, USER_B).var_map_subs == \
	       {"$$HOST$$": ("vrfs", 1)}
	assert Device.from_inventory(row, USER_C).var_map_subs == {}


def test_substitution_uses_each_users_own_binding():
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
	device = Device.from_inventory(global_row(), USER_C)
	assert (device.username, device.password, device.secret) == \
	       ("netops", "pw", "en")


def test_missing_enable_secret_becomes_empty_string():
	row = global_row()
	row.security_profile.enable_secret = None
	assert Device.from_inventory(row, USER_A).secret == ""


def test_no_security_profile_raises():
	with pytest.raises(ValueError, match="no security profiles"):
		Device.from_inventory(global_row(security_profile=None), USER_A)
