"""The pages run under a Content-Security-Policy without 'unsafe-inline' for
scripts (hooks.content_security_policy): no inline <script> (only JSON data
blocks), no on...= handler attribute - in a template or in HTML a page's
script builds -, no javascript: URL. A handler is named in
data-nr-<event>="name" (static/js/events.js) and registered with
nrHandlers({name: ...}); a name nothing registers would do nothing."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "templates"
STATIC_JS = ROOT / "src" / "static" / "js"
SCRIPT_TAG = re.compile(r"<script\b([^>]*)>", re.I)
# an event handler attribute; a script's own el.onclick = ... (after a dot) isn't one
HANDLER_ATTR = re.compile(
	r"(?<![\w.$-])on(?:abort|blur|change|click|contextmenu|dblclick|drag\w*|drop|error|focus\w*|input|"
	r"key(?:down|up|press)|load|mouse\w+|paste|reset|scroll|select|submit|toggle|wheel|pointer\w+|"
	r"touch\w+|animation\w+|transition\w+)\s*=\s*[\"'\\]", re.I)
NAMED = re.compile(r"""data-nr-(?:click|dblclick|change|input|submit|keydown|keyup|mouseenter|mouseleave|"""
                   r"""focus|blur|error|load)="([A-Za-z_$][\w$]*)\"""")
REGISTERED_BLOCK = re.compile(r"nrHandlers\(\{\n(.*?)\n\s*\}\);", re.S)
REGISTERED_NAME = re.compile(r"^\s{4,8}([A-Za-z_$][\w$]*)\s*:", re.M)


def sources() -> list[Path]:
	""":returns: the templates and the pages' scripts"""
	return sorted(TEMPLATES.glob("*.html")) + sorted(STATIC_JS.glob("*.js"))


@pytest.mark.parametrize("text, found", [
	('<button onclick="go()">', True),
	("<img src=x onerror='hide(this)'>", True),
	('html += `<a onmouseenter=\\"x()\\">`', True),
	("btn.onclick = testAddConnection;", False),
	("es.onerror = function() {}", False),
	('<button data-nr-click="go">', False),
	('aria-controls="x" content="y"', False),
])
def test_the_check_finds_handler_attributes_only(text, found):
	"""The pattern: an on...= attribute counts; a property a script sets, a
	data-nr-* name and other attributes ending in "on" don't."""
	assert bool(HANDLER_ATTR.search(text)) is found


def test_no_page_has_an_inline_event_handler():
	"""No template and no page script writes an on...= attribute."""
	found = {p.name: [m.group(0) for m in HANDLER_ATTR.finditer(p.read_text(encoding="utf-8"))]
	         for p in sources()}
	assert {name: hits for name, hits in found.items() if hits} == {}


def test_no_template_has_an_inline_script():
	"""Every <script> loads a file (src) or is a JSON data block."""
	found = {}
	for t in sorted(TEMPLATES.glob("*.html")):
		inline = [m.group(0) for m in SCRIPT_TAG.finditer(t.read_text(encoding="utf-8"))
		          if "src=" not in m.group(1) and 'type="application/json"' not in m.group(1)]
		if inline:
			found[t.name] = inline
	assert found == {}


def test_no_javascript_urls():
	"""No link or button runs code from a javascript: URL."""
	assert [p.name for p in sources() if re.search(r"javascript:", p.read_text(encoding="utf-8"), re.I)] == []


def test_every_named_handler_is_registered():
	"""Each data-nr-<event>="name" in a template or a page script names a
	handler some script registers with nrHandlers."""
	registered = set()
	for js in STATIC_JS.glob("*.js"):
		for block in REGISTERED_BLOCK.findall(js.read_text(encoding="utf-8")):
			registered |= set(REGISTERED_NAME.findall(block))
	named = {name for p in sources() for name in NAMED.findall(p.read_text(encoding="utf-8"))}
	assert named and named - registered == set()
