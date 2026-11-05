"""SSH host keys (src/rollout/host_keys.py and session._connect) against a
real SSH server in this process (paramiko's own server side): with the
check off nothing changes; a first key is learned and pins the device; a
known key passes; a changed key, or one of another type, is refused BEFORE
authentication (the server records every password it is sent); an HA
pair's other accepted key passes; the known_hosts file is named as paramiko
looks it up and removed after. Netmiko re-raises paramiko's
BadHostKeyException as its own timeout error - refused_key finds the key
in that chain, which these tests pin."""
import os
import socket
import threading
from unittest.mock import patch

import netmiko
import paramiko
import pytest

from src.rollout.engine import Device
from src.rollout.host_keys import (OFF, HostKeyCheck, HostKeyPolicy, HostKeyRefused, fingerprint, key_line,
                                   known_hosts_name, refused_key)
from src.rollout.session import connect_unknown

RSA_A = paramiko.RSAKey.generate(1024)
RSA_B = paramiko.RSAKey.generate(1024)
ECDSA = paramiko.ECDSAKey.generate()


class Server(paramiko.ServerInterface):
	"""One SSH server on 127.0.0.1: its host key set per test; every
	password sent to it recorded; a shell that answers a prompt."""

	def __init__(self):
		self.key = RSA_A
		self.passwords = []
		self.listener = socket.create_server(("127.0.0.1", 0))
		self.port = self.listener.getsockname()[1]
		threading.Thread(target=self._serve, daemon=True).start()

	def _serve(self):
		while True:
			try:
				sock, _ = self.listener.accept()
			except OSError:
				return
			threading.Thread(target=self._session, args=(sock,), daemon=True).start()

	def _session(self, sock):
		transport = paramiko.Transport(sock)
		transport.add_server_key(self.key)
		try:
			transport.start_server(server=self)
		except (paramiko.SSHException, EOFError, OSError):
			return
		channel = transport.accept(10)
		if channel is None:
			return
		try:
			channel.send(b"R1#")
			while True:
				data = channel.recv(1024)
				if not data:
					return
				if b"\r" in data or b"\n" in data:
					channel.send(b"\r\nR1#")
		except (OSError, EOFError, paramiko.SSHException):
			pass

	def close(self):
		self.listener.close()

	# paramiko.ServerInterface
	def get_allowed_auths(self, username):
		return "password"

	def check_auth_password(self, username, password):
		self.passwords.append(password)
		return paramiko.AUTH_SUCCESSFUL if password == "pw" else paramiko.AUTH_FAILED

	def check_channel_request(self, kind, chanid):
		return paramiko.OPEN_SUCCEEDED

	def check_channel_pty_request(self, *args):
		return True

	def check_channel_shell_request(self, channel):
		return True


@pytest.fixture
def server():
	s = Server()
	yield s
	s.close()


def device(server, policy=OFF):
	return Device(ip="127.0.0.1", label="R1", username="netops", password="pw", device_type="cisco_ios",
	              secret="", port=server.port, host_keys=policy)


def connect(dev):
	conn = connect_unknown(dev)
	conn.disconnect()


def test_off_connects_whatever_the_key_and_adds_nothing(server):
	"""Off (the default, the CLI): Netmiko gets no host key argument at all,
	and any key connects - today's behaviour."""
	with patch("netmiko.ConnectHandler", wraps=netmiko.ConnectHandler) as handler:
		connect(device(server))
		server.key = RSA_B
		connect(device(server))
	for call in handler.call_args_list:
		assert not {"ssh_strict", "alt_host_keys", "alt_key_file"} & set(call.kwargs)
	assert server.passwords == ["pw", "pw"]


def test_a_first_key_is_learned_and_pins_the_device(server):
	"""Learning: the key the device showed is handed over, and the device's
	later connections in this run verify it - a changed key then is refused."""
	learned = []
	dev = device(server, HostKeyPolicy(learn=True, seen=learned.append))
	connect(dev)
	assert learned == [key_line(RSA_A)]
	assert (dev.host_keys.known, dev.host_keys.learn) == ((key_line(RSA_A),), False)
	server.key = RSA_B
	with pytest.raises(HostKeyRefused):
		connect(dev)
	assert server.passwords == ["pw"]


def test_a_known_key_passes(server):
	seen = []
	connect(device(server, HostKeyPolicy(known=(key_line(RSA_A),), seen=seen.append)))
	assert seen == [key_line(RSA_A)]
	assert server.passwords == ["pw"]


def test_a_changed_key_is_refused_before_the_password_is_sent(server):
	"""The device shows another key of the known type: refused, the offered
	key handed over, and the server never got a password."""
	refused = []
	server.key = RSA_B
	with pytest.raises(HostKeyRefused) as error:
		connect(device(server, HostKeyPolicy(known=(key_line(RSA_A),), refused=refused.append)))
	assert server.passwords == []
	assert refused == [key_line(RSA_B)]
	assert error.value.offered == key_line(RSA_B)
	assert str(error.value) == (f"127.0.0.1:{server.port}: its SSH host key changed - not signed in to, nothing "
	                            f"was sent. If the device was replaced, an admin accepts its new key in Server "
	                            f"Management -> Host keys.")


def test_a_key_of_another_type_is_refused_too(server):
	"""Only an RSA key known, the device shows an ECDSA one: refused before
	authentication (paramiko doesn't fall back to accepting it)."""
	server.key = ECDSA
	with pytest.raises(HostKeyRefused):
		connect(device(server, HostKeyPolicy(known=(key_line(RSA_A),))))
	assert server.passwords == []


def test_the_other_unit_of_an_ha_pair_passes(server):
	"""Two accepted RSA keys (paramiko loads one per type): the device shows
	the second, the connection is made again with it loaded - once, and the
	password sent only then."""
	server.key = RSA_B
	with patch("netmiko.ConnectHandler", wraps=netmiko.ConnectHandler) as handler:
		connect(device(server, HostKeyPolicy(known=(key_line(RSA_A), key_line(RSA_B)))))
	assert handler.call_count == 2
	assert server.passwords == ["pw"]


def test_a_key_neither_ha_unit_has_is_refused(server):
	server.key = ECDSA
	with pytest.raises(HostKeyRefused):
		connect(device(server, HostKeyPolicy(known=(key_line(RSA_A), key_line(RSA_B)))))
	assert server.passwords == []


def test_netmiko_hides_the_refusal_in_its_timeout_error(server):
	"""What refused_key relies on: Netmiko raises NetmikoTimeoutException,
	paramiko's BadHostKeyException (with the device's key) in its chain."""
	server.key = RSA_B
	with HostKeyCheck(HostKeyPolicy(known=(key_line(RSA_A),)), "127.0.0.1", server.port) as check:
		with pytest.raises(netmiko.exceptions.NetmikoTimeoutException) as error:
			netmiko.ConnectHandler(host="127.0.0.1", port=server.port, username="u", password="pw",
			                       device_type="terminal_server", **check.args)
	assert refused_key(error.value) == key_line(RSA_B)
	assert refused_key(RuntimeError("other")) is None


def test_the_known_hosts_file_is_private_named_as_paramiko_looks_it_up_and_removed():
	"""One line per key type under [host]:port (host alone on 22), owner
	only, deleted on leaving - also when the connection failed."""
	policy = HostKeyPolicy(known=(key_line(RSA_A), key_line(RSA_B), key_line(ECDSA)))
	with pytest.raises(RuntimeError):
		with HostKeyCheck(policy, "10.0.0.1", 2222) as check:
			path = check.args["alt_key_file"]
			lines = open(path, encoding="ascii").read().splitlines()
			if os.name == "posix":
				assert os.stat(path).st_mode & 0o077 == 0
			raise RuntimeError("the connection failed")
	assert lines == [f"[10.0.0.1]:2222 {key_line(RSA_A)}", f"[10.0.0.1]:2222 {key_line(ECDSA)}"]
	assert check.args["ssh_strict"] is True
	assert not os.path.exists(path)


@pytest.mark.parametrize("host, port, name", [
	("10.0.0.1", 22, "10.0.0.1"), ("10.0.0.1", 2222, "[10.0.0.1]:2222"),
	("2001:db8::1", 22, "2001:db8::1"), ("2001:db8::1", 2222, "[2001:db8::1]:2222"),
	("core1.lab", 22, "core1.lab"),
])
def test_the_name_paramiko_looks_keys_up_by(host, port, name):
	assert known_hosts_name(host, port) == name
	# and paramiko agrees: a key stored under that name is found for the connection's name
	keys = paramiko.HostKeys()
	keys.add(name, RSA_A.get_name(), RSA_A)
	assert keys.lookup(host if port == 22 else f"[{host}]:{port}") is not None


def test_learning_or_off_add_no_file():
	with HostKeyCheck(HostKeyPolicy(learn=True), "10.0.0.1", 22) as check:
		assert check.args == {}
	with HostKeyCheck(OFF, "10.0.0.1", 22) as check:
		assert check.args == {}


def test_the_fingerprint_is_openssh_s():
	assert fingerprint(key_line(RSA_A)) == RSA_A.fingerprint
