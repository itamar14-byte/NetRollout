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
