"""The instance route behind the startup reverse-proxy check."""
import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def test_instance_route_returns_this_processes_token(app, client_for):
	client = client_for()   # anonymous: nginx fetches it without a session
	resp = client.get("/_netrollout/instance")
	assert resp.status_code == 200
	assert resp.json == {"instance": app.config["INSTANCE_TOKEN"]}
	assert len(app.config["INSTANCE_TOKEN"]) == 32
	# no session is written for it
	assert "Set-Cookie" not in resp.headers


@pytest.mark.parametrize("path,role", [
	("/", None),                  # base.html (the login page)
	("/dashboard", "operator"),   # operator_base.html
	("/admin/users", "admin"),    # admin.html
])
def test_every_page_skeleton_shows_the_version_and_its_source(
		client_for, make_user, path, role):
	# AGPL-3.0 §13: network users are offered the source of this version
	from src.runtime import VERSION, source_url
	user = make_user(role=role) if role else None
	html = client_for(user).get(path).get_data(as_text=True)
	assert f"NETROLLOUT V{VERSION.upper()}" in html
	assert f'href="{source_url()}"' in html
	assert "LICENSED UNDER GNU AGPL V3" in html
