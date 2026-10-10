"""A page that isn't there (hooks.register_handlers, templates/404.html): a
person gets NetRollout's own 404 page - a router rejecting the command -, a
script gets JSON."""
import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def test_a_missing_page_is_answered_like_a_router(client_for, make_user):
	"""Signed in: 404, the page shows the path as the rejected command with the
	router's error, and links back to the dashboard."""
	resp = client_for(make_user()).get("/admin/setings")
	page = resp.get_data(as_text=True)
	assert resp.status_code == 404
	assert "show page /admin/setings" in page
	assert "% Invalid input detected at &#39;^&#39; marker." in page
	assert 'href="/dashboard"' in page


def test_signed_out_the_404_page_offers_the_sign_in(client_for):
	"""Not signed in: the same page, its link goes to the sign-in."""
	resp = client_for().get("/no/such/page")
	assert resp.status_code == 404
	assert "show page /no/such/page" in resp.get_data(as_text=True)
	assert 'href="/"' in resp.get_data(as_text=True)


def test_the_path_is_escaped(client_for):
	"""A path with markup in it is shown as text, never as markup."""
	page = client_for().get("/<script>alert(1)</script>").get_data(as_text=True)
	assert "<script>alert(1)</script>" not in page
	assert "&lt;script&gt;" in page


def test_a_script_gets_json(client_for, make_user):
	"""A page's script (the XHR header) gets {"status": "error", "message":
	"Not found"} with 404, not the page."""
	resp = client_for(make_user(), xhr=True).get("/inventory/no-such-endpoint")
	assert resp.status_code == 404
	assert resp.get_json() == {"status": "error", "message": "Not found"}
