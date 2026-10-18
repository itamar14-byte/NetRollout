"""The pages ask and tell in NetRollout's own windows (nrConfirm / nrAlert,
templates/_confirm_modal.html), never the browser's confirm() / alert() /
prompt(): a browser lets people switch those off for a page, and then
confirm() answers No without asking - Restart or Cancel rollout would do
nothing, silently."""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
# a call of the browser's own (window.confirm( too), not nrConfirm( or x.confirm(
NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(")
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def native_calls(source):
	""":returns: the lines calling a native dialog (Jinja comments left out)"""
	code = JINJA_COMMENT.sub("", source)
	return [line.strip() for line in code.splitlines() if NATIVE.search(line)]


@pytest.mark.parametrize("line, found", [
	("if (!confirm('Delete?')) return;", True),
	("alert(data.message);", True),
	("window.confirm('x')", True),
	("const n = prompt ('name');", True),
	("if (!await nrConfirm({title: 'x'})) return;", False),
	("nrAlert(message, 'Not cancelled');", False),
	("form.confirm(x); this.alert (y)", False),
	("{# instead of the browser's confirm() #}", False),
])
def test_the_check_finds_native_calls_only(line, found):
	"""The pattern: a bare (or window.) confirm / alert / prompt call counts;
	ours, a method of something else and a Jinja comment don't."""
	assert bool(native_calls(line)) is found


def test_no_template_uses_the_browsers_dialogs():
	"""Every template: none calls confirm(), alert() or prompt()."""
	found = {path.name: native_calls(path.read_text(encoding="utf-8"))
	         for path in TEMPLATES.glob("*.html")}
	assert {name: lines for name, lines in found.items() if lines} == {}
