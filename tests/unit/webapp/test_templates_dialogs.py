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


def test_enter_clicks_a_windows_main_button_but_never_a_red_one():
	"""Enter in a window clicks its main button (data-nr-enter, else its blue
	one), as Escape closes it - never a red one, nor from a text area, a
	button, a link or a list's search."""
	source = (TEMPLATES / "_confirm_modal.html").read_text(encoding="utf-8")
	handler = source[source.index("document.addEventListener('keydown'"):]
	handler = handler[:handler.index("\n});") + 4]
	assert "e.key !== 'Enter'" in handler
	assert "!btn.classList.contains('btn-danger')" in handler
	assert "t.closest('textarea, button, a, select, [contenteditable], .nr-select-panel')" in handler
	assert "querySelectorAll('[data-nr-enter]')" in handler
	inventory = (TEMPLATES / "inventory.html").read_text(encoding="utf-8")
	assert 'id="addTestBtn" class="btn btn-secondary btn-sm" data-nr-enter' in inventory


def test_server_management_cards_close_like_a_window():
	"""An open card (PostgreSQL's or Redis' form, an LDAP server's panel)
	closes on Escape or a click outside it - never from inside a window over
	the page (Bootstrap closes that window on Escape first) - and its own
	button closes it again."""
	source = (TEMPLATES / "server_management.html").read_text(encoding="utf-8")
	closer = source[source.index("function closeOpenCards"):source.index("function expandServer")]
	assert "e.target.closest('.modal')" in closer
	assert "document.addEventListener('click'" in closer and "e.key === 'Escape'" in closer
	assert "#row-${expandedId}, #panel-${expandedId}" in closer
	assert "!(s === 'postgres' && moving)" in closer
	assert "style.display === 'block') svc = null;" in source


def test_a_connection_tests_result_is_never_cut_off():
	"""Add / Edit Device: the result takes its own line above the buttons and
	wraps - it was cut at 240 px next to them ("unreachable on 19")."""
	source = (TEMPLATES / "inventory.html").read_text(encoding="utf-8")
	style = source[source.index(".conn-status {"):source.index(".conn-status.conn-ok")]
	assert "flex: 1 0 100%;" in style and "max-width" not in style and "ellipsis" not in style
	for status in ("addTestStatus", "editTestStatus"):
		footer = source[:source.index(f'<div id="{status}"')].rsplit('<div class="modal-footer"', 1)[1]
		assert "flex-wrap:wrap;" in footer and "<button" not in footer    # the footer's first item


def test_a_window_closes_as_it_opens():
	"""Bootstrap fades a window in 0.15 s but slides it 0.3 s, so a window
	closing (Escape, a click outside) was gone before the slide showed. The
	window's fade takes the slide's 0.3 s, in the styles every skeleton loads;
	the backdrop keeps Bootstrap's (opening waits for it)."""
	core = (TEMPLATES / "_theme_core_css.html").read_text(encoding="utf-8")
	assert ".modal.fade { transition: opacity 0.3s ease-out; }" in core
	assert ".modal-backdrop" not in core.split(".modal.fade {")[1].split("}")[0]
	for skeleton in ("operator_base.html", "admin.html", "base.html"):
		assert "_theme_core_css.html" in (TEMPLATES / skeleton).read_text(encoding="utf-8")


def test_the_inventory_tooltip_shows_a_hostnames_address():
	"""Hovering a device saved by name shows "name (address)" - the page's
	reachability check's address, else the stored one (resolved_ip); by IP,
	or a name that didn't resolve, the address alone; the line wraps rather
	than cuts."""
	source = (TEMPLATES / "inventory.html").read_text(encoding="utf-8")
	assert '"resolved_ip":           device.resolved_ip or "",' in source
	assert "const live = REACH[device.id], resolved = (live && live.address) || device.resolved_ip;" in source
	assert "resolved ? device.ip + ' (' + resolved + ')' : device.ip;" in source
	assert "#ttIp { white-space: normal; overflow-wrap: anywhere; }" in source


def test_a_name_that_doesnt_resolve_says_so_where_reachability_shows():
	"""Not reachable because the name doesn't resolve reads "Not reachable
	(can't resolve)": the inventory's dot and tooltip, New Rollout's rows."""
	widgets = (TEMPLATES / "_op_widgets_js.html").read_text(encoding="utf-8")
	assert "return status && !status.reachable && status.unresolved ? \" (can't resolve)\" : '';" in widgets
	assert "unknown: 'Reachability unknown'}[state] + nrReachWhy(status);" in widgets
	assert "(reach.reachable ? 'Reachable' : 'Not reachable') + nrReachWhy(reach)" in 		(TEMPLATES / "inventory.html").read_text(encoding="utf-8")
	assert '"Not reachable from NetRollout" + nrReachWhy(status)' in 		(TEMPLATES / "new_rollout.html").read_text(encoding="utf-8")
