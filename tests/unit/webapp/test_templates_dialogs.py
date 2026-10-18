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


# ── a window over another (the stacking in _confirm_modal.html) ──────────────

BACKDROP_SIBLING = re.compile(r"\.modal-backdrop\s*~\s*\.modal-backdrop")
FIXED_LEVEL = re.compile(r"#\w+Modal\s*\{[^}]*z-index", re.S)


def test_no_template_raises_a_backdrop_or_window_by_css():
	"""rc4's rule `.modal-backdrop ~ .modal-backdrop { z-index: 1060 }` put
	every second backdrop over a window opened at Bootstrap's 1055 - Create
	New Profile over Edit device was dimmed and took no click (Q2.2). Levels
	are set when a window opens, never by a fixed CSS rule."""
	found = {path.name: [m.group(0) for m in (*BACKDROP_SIBLING.finditer(text), *FIXED_LEVEL.finditer(text))]
	         for path in TEMPLATES.glob("*.html")
	         if (text := JINJA_COMMENT.sub("", path.read_text(encoding="utf-8")))}
	assert {name: rules for name, rules in found.items() if rules} == {}


def test_every_page_stacks_windows_when_they_open_and_close():
	"""The stacking listens to every window's opening and closing, in the
	file both page skeletons include - so it covers every window, not only
	the confirm."""
	source = (TEMPLATES / "_confirm_modal.html").read_text(encoding="utf-8")
	assert "document.addEventListener('show.bs.modal'" in source
	assert "document.addEventListener('hidden.bs.modal'" in source
	for skeleton in ("operator_base.html", "admin.html"):
		assert "{% include '_confirm_modal.html' %}" in (TEMPLATES / skeleton).read_text(encoding="utf-8")
