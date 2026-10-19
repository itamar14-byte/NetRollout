"""The SSH library still reaches older devices: IOS 12/15 and NX-OS 7 offer
only an ssh-rsa host key and the SHA-1 key exchanges. Paramiko 5.0 dropped
them, so NetRollout pins 4.x (requirements.in); these fail if it moves on.
The modern algorithms stay first, so a device that has them uses them."""
import paramiko


def test_ssh_rsa_host_keys_are_accepted_last():
	"""ssh-rsa is offered, and only after every SHA-2 host key type."""
	keys = paramiko.Transport._preferred_keys
	assert "ssh-rsa" in keys
	assert keys[-1] == "ssh-rsa"
	assert keys.index("rsa-sha2-256") < keys.index("ssh-rsa")
	assert keys.index("ssh-ed25519") < keys.index("ssh-rsa")


def test_sha1_key_exchanges_are_offered_last():
	"""diffie-hellman-group14-sha1 and group1-sha1 are offered, after the modern ones."""
	kex = paramiko.Transport._preferred_kex
	assert kex[-2:] == ("diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1")
	assert kex.index("curve25519-sha256@libssh.org") < kex.index("diffie-hellman-group14-sha1")
