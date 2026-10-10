"""GitHub's issue forms (.github/ISSUE_TEMPLATE): valid forms, and their
platform lists the platforms NetRollout supports - a vendor added to
src/rollout/platforms.py can't be left out of them."""
from pathlib import Path

import pytest
import yaml

from src.rollout.inputs import SUPPORTED_PLATFORMS


FORMS = Path(__file__).resolve().parents[3] / ".github" / "ISSUE_TEMPLATE"


def platform_options(form: str) -> list[str]:
	""":returns: the options of the form's "platform" dropdown"""
	body = yaml.safe_load((FORMS / form).read_text(encoding="utf-8"))["body"]
	(field,) = [f for f in body if f.get("id") == "platform"]
	return field["attributes"]["options"]


def test_the_bug_report_lists_every_supported_platform():
	"""The bug report's platforms: exactly the supported ones, sorted, after "Not
	about a device" and before "other"."""
	options = platform_options("bug_report.yml")
	assert options[0] == "Not about a device" and options[-1] == "other"
	assert options[1:-1] == sorted(SUPPORTED_PLATFORMS)


def test_a_platform_confirmation_names_a_supported_platform():
	"""A confirmation picks exactly one of the supported platforms, sorted."""
	assert platform_options("platform_confirmation.yml") == sorted(SUPPORTED_PLATFORMS)


@pytest.mark.parametrize("form", sorted(p.name for p in FORMS.glob("*.yml") if p.name != "config.yml"))
def test_every_form_is_a_valid_issue_form(form):
	"""Each form has a name, a description and a body; every field has a unique id
	(markdown blocks aside), and every dropdown at least one option."""
	data = yaml.safe_load((FORMS / form).read_text(encoding="utf-8"))
	assert data["name"] and data["description"] and data["body"]
	ids = [f["id"] for f in data["body"] if f["type"] != "markdown"]
	assert len(ids) == len(set(ids))
	for field in data["body"]:
		if field["type"] == "dropdown":
			assert field["attributes"]["options"]


def test_blank_issues_are_off_and_security_goes_privately():
	"""No blank issues (everything through a form), and the chooser links the
	private vulnerability report."""
	config = yaml.safe_load((FORMS / "config.yml").read_text(encoding="utf-8"))
	assert config["blank_issues_enabled"] is False
	assert any("security/advisories/new" in link["url"] for link in config["contact_links"])
