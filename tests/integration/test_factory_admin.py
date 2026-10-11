"""The factory admin under User Management: it may be disabled (and enabled
again), its 2FA reset - never deleted or demoted - and no action may take
away the last administrator who can sign in."""
import pytest

from src.accounts.users import NO_ADMIN_LEFT
from src.db.tables import AuditLog, LDAPServer, User
from src.encryption import encrypt
from tests.integration.conftest import TEST_PASSWORD

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


@pytest.fixture
def factory(make_user):
	return make_user(username="admin", role="admin")


def flashes(client):
	with client.session_transaction() as s:
		return [m for _, m in s.pop("_flashes", [])]


def test_an_admin_disables_the_factory_admin_and_it_cant_sign_in(
		admin, factory, client_for, db_get, session_scope):
	"""Disable works on the factory admin (audited user.disable); its right
	password then gets the disabled message, no sign-in. Enable brings it
	back."""
	c = client_for(admin)
	c.post(f"/admin/users/{factory.id}/disable")
	assert db_get(User, factory.id).is_active is False
	with session_scope() as s:
		assert s.query(AuditLog).filter_by(action="user.disable",
		                                   object_id=factory.id).count() == 1
	signing_in = client_for()
	resp = signing_in.post("/login", data={"username": "admin", "password": TEST_PASSWORD})
	assert resp.headers["Location"] == "/"
	assert flashes(signing_in) == ["User disabled, please check with administrator"]
	c.post(f"/admin/users/{factory.id}/enable")
	assert db_get(User, factory.id).is_active is True


def test_a_bulk_disable_includes_the_factory_admin(admin, factory, make_user,
                                                   client_for, db_get):
	"""Ticked with an operator, the factory admin is disabled too (it used to
	be skipped)."""
	op = make_user()
	client_for(admin).post("/admin/users/bulk/disable",
	                       data={"user_ids": f"{factory.id},{op.id}"})
	assert db_get(User, factory.id).is_active is False
	assert db_get(User, op.id).is_active is False


def test_reset_2fa_turns_the_factory_admins_2fa_off(admin, factory, client_for,
                                                     db_get, session_scope):
	"""Reset 2FA on the factory admin clears its authenticator: it signs in
	with the password alone again."""
	with session_scope() as s:
		s.get(User, factory.id).otp_secret = encrypt("JBSWY3DPEHPK3PXP")
	client_for(admin).post(f"/admin/users/{factory.id}/reset_2fa")
	assert db_get(User, factory.id).otp_secret is None


def test_demoting_yourself_as_the_last_admin_is_refused(admin, factory, client_for,
                                                        db_get):
	"""The factory admin disabled, the only other admin demoting themselves
	is refused in words and stays an admin."""
	c = client_for(admin)
	c.post(f"/admin/users/{factory.id}/disable")
	c.post(f"/admin/users/{admin.id}/demote")
	assert flashes(c) == [NO_ADMIN_LEFT]
	assert db_get(User, admin.id).role == "admin"


def test_the_last_admin_who_can_sign_in_is_kept_in_bulk(factory, make_user,
                                                        client_for, db_get,
                                                        session_scope):
	"""An admin from a directory whose LDAP server is off can't sign in, so
	doesn't count: their bulk disable of the factory admin (the last one who
	can) is refused as a whole - the operator ticked with it stays active
	too."""
	with session_scope() as s:
		server = LDAPServer(name="off", host="ldap.test", port=389, base_dn="dc=corp",
		                    bind_type="simple", is_active=False)
		s.add(server)
		s.flush()
		server_id = server.id
	directory_admin = make_user(role="admin", auth_type="ldap", ldap_server_id=server_id)
	op = make_user()
	c = client_for(directory_admin)
	c.post("/admin/users/bulk/disable", data={"user_ids": f"{factory.id},{op.id}"})
	assert flashes(c) == [NO_ADMIN_LEFT]
	assert db_get(User, factory.id).is_active is True
	assert db_get(User, op.id).is_active is True


def test_disabling_another_admin_is_fine_while_one_is_left(admin, factory, make_user,
                                                           client_for, db_get):
	"""With the acting admin left, disabling the factory admin and another
	admin is allowed."""
	other = make_user(role="admin")
	client_for(admin).post("/admin/users/bulk/disable",
	                       data={"user_ids": f"{factory.id},{other.id}"})
	assert db_get(User, factory.id).is_active is False
	assert db_get(User, other.id).is_active is False


def test_the_users_page_lets_the_factory_admin_be_ticked(admin, factory, client_for):
	"""The factory admin's row has a checkbox (Disable / Enable / Reset 2FA
	work on it) and its data marks it as the factory account."""
	page = client_for(admin).get("/admin/users").get_data(as_text=True)
	assert f'class="form-check-input row-cb" style="margin:0; cursor:pointer;" value="{factory.id}"' in page
	assert '"is_factory": true' in page
