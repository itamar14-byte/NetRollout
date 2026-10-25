"""Every check box looks like NetRollout's: the shared .nr-check (the theme's,
in all three skeletons), Bootstrap's themed .form-check-input, or a switch
(.nr-toggle). A box with none of them is the browser's own white square -
the bulk review's "Also decide the other devices" was - and a box with its
own accent-color / appearance is a one-off look. Boxes a page's script
builds count too."""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
BOX = re.compile(r"""<input\b[^>]*?type\s*=\s*["']checkbox["'][^>]*>""", re.S | re.I)
CLASS = re.compile(r"""\bclass\s*=\s*["']([^"']*)["']""")
STYLE = re.compile(r"""\bstyle\s*=\s*["']([^"']*)["']""", re.S)
# a box a script builds: <name>.type = 'checkbox'
JS_BOX = re.compile(r"""\b([A-Za-z_$][\w$]*)\.type\s*=\s*["']checkbox["']""")
STYLED = {"nr-check", "form-check-input"}


def boxes() -> list[tuple[str, int, str, str]]:
	""":returns: (template, line, the tag, the text before it) for every
	 check box written as HTML"""
	found = []
	for path in sorted(TEMPLATES.glob("*.html")):
		text = path.read_text(encoding="utf-8")
		for m in BOX.finditer(text):
			found.append((path.name, text.count("\n", 0, m.start()) + 1, m.group(0),
			              text[max(0, m.start() - 400):m.start()]))
	return found


def in_switch(before: str) -> bool:
	""":returns: whether the box sits in a .nr-toggle: the element opened
	 last before it (a label or span) is one"""
	opened = max(before.rfind("<label"), before.rfind("<span"))
	return opened != -1 and "nr-toggle" in before[opened:]


def script_boxes() -> list[tuple[str, int, str, str]]:
	""":returns: (template, line, variable, the script around it) for every
	 check box a script builds"""
	found = []
	for path in sorted(TEMPLATES.glob("*.html")):
		text = path.read_text(encoding="utf-8")
		for m in JS_BOX.finditer(text):
			found.append((path.name, text.count("\n", 0, m.start()) + 1, m.group(1),
			              text[max(0, m.start() - 600):m.end() + 600]))
	return found


def cases(found: list[tuple[str, int, str, str]]) -> list:
	""":returns: the boxes as test cases named template:line"""
	return [pytest.param(*f, id=f"{f[0]}:{f[1]}") for f in found]


@pytest.mark.parametrize("name, line, tag, before", cases(boxes()))
def test_every_check_box_has_netrollouts_look(name, line, tag, before):
	"""A check box written in a template is .nr-check, Bootstrap's
	.form-check-input, or inside a .nr-toggle switch."""
	classes = set((CLASS.search(tag).group(1) if CLASS.search(tag) else "").split())
	assert classes & STYLED or in_switch(before), \
		f"{name}:{line}: a check box without NetRollout's look - give it class nr-check: {tag}"


@pytest.mark.parametrize("name, line, tag, before", cases(boxes()))
def test_no_check_box_styles_itself(name, line, tag, before):
	"""No check box carries its own accent-color or appearance: its look is
	the shared one (a danger box is .nr-check-danger)."""
	style = (STYLE.search(tag).group(1) if STYLE.search(tag) else "").lower()
	assert "accent-color" not in style and "appearance" not in style, \
		f"{name}:{line}: a check box styling itself - use nr-check (nr-check-danger): {tag}"


@pytest.mark.parametrize("name, line, var, around", cases(script_boxes()))
def test_every_check_box_a_script_builds_has_netrollouts_look(name, line, var, around):
	"""A box a script builds is made with the nr-check class (_el('input',
	'nr-check'), or className), or goes into a .nr-toggle switch."""
	made_styled = re.search(rf"""\b{re.escape(var)}\s*=\s*_el\(\s*["']input["']\s*,\s*["'][^"']*\bnr-check\b""", around) \
		or re.search(rf"""\b{re.escape(var)}\.className\s*=\s*["'][^"']*\bnr-check\b""", around)
	switched = re.search(rf"""\.append\(\s*{re.escape(var)}\s*,\s*_el\(\s*["']span["']\s*,\s*["']nr-toggle-slider""", around)
	assert made_styled or switched, \
		f"{name}:{line}: a check box built in a script without NetRollout's look ({var})"


def test_the_shared_check_box_is_in_the_theme():
	"""The theme every skeleton includes defines .nr-check - its box, the tick
	when checked, the dash when some are, disabled - and the danger variant."""
	theme = (TEMPLATES / "_theme_core_css.html").read_text(encoding="utf-8")
	for rule in (".nr-check {", ".nr-check:checked", ".nr-check:checked::after",
	             ".nr-check:indeterminate", ".nr-check:disabled", ".nr-check-danger:checked"):
		assert rule in theme, rule
	for skeleton in ("base.html", "operator_base.html", "admin.html"):
		assert "_theme_core_css.html" in (TEMPLATES / skeleton).read_text(encoding="utf-8"), skeleton
