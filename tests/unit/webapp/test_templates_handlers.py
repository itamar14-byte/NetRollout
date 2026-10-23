"""Every function an inline handler calls (onclick="x(this)", also in HTML a
script builds) is one the page publishes: assigned to window, or declared at
the top level of a <script>. One declared inside a script's own function
(the pages wrap theirs in one) can't be reached from HTML - the handler
throws, and the button silently does nothing: Active Jobs' Cancel did, from
the change that gave it NetRollout's own question window until it was
published (window.confirmCancel)."""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.S)
SCRIPT = re.compile(r"<script\b[^>]*>(.*?)</script>", re.S | re.I)
HANDLER = re.compile(r"""\bon[a-z]+\s*=\s*(?:"([^"]*)"|'([^']*)')""")
CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
WINDOW_NAME = re.compile(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=(?!=)")
TOP_LEVEL = re.compile(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")
# what a handler may call without a page defining it: the language's and
# the browser's own
BUILT_IN = {"if", "return", "function", "typeof", "void", "new", "await", "event", "this",
            "Number", "String", "Boolean", "parseInt", "parseFloat", "encodeURIComponent",
            "setTimeout", "clearTimeout", "fetch", "history", "location", "navigator",
            # CSS values a handler assigns as strings stripped of quotes
            "var", "rgba", "rgb", "calc", "url"}


def top_level_names(script: str) -> set[str]:
	""":returns: the names declared at the top level of a script (brace depth
	 0), strings, template literals and comments skipped"""
	names, depth, i, n = set(), 0, 0, len(script)
	while i < n:
		ch = script[i]
		if script.startswith("//", i):
			i = script.find("\n", i)
			i = n if i == -1 else i
			continue
		if script.startswith("/*", i):
			i = script.find("*/", i + 2)
			i = n if i == -1 else i + 2
			continue
		if ch in "'\"`":
			end = i + 1
			while end < n and script[end] != ch:
				end += 2 if script[end] == "\\" else 1
			i = end + 1
			continue
		if ch == "{":
			depth += 1
		elif ch == "}":
			depth -= 1
		elif depth == 0 and (m := TOP_LEVEL.match(script, i)) and (i == 0 or not script[i - 1].isalnum()):
			names.add(m.group(1) or m.group(2))
			i = m.end()
			continue
		i += 1
	return names


def published() -> set[str]:
	""":returns: every name a page can reach from HTML, from all templates
	 (the skeletons and partials publish for the pages that include them)"""
	names: set[str] = set()
	for path in TEMPLATES.rglob("*.html"):
		text = JINJA.sub("", path.read_text(encoding="utf-8"))
		names |= set(WINDOW_NAME.findall(text))
		for script in SCRIPT.findall(text):
			names |= top_level_names(script)
	return names


def handler_calls(text: str) -> set[str]:
	""":returns: the functions the inline handlers in a template call"""
	calls = set()
	for m in HANDLER.finditer(JINJA.sub("", text)):
		code = QUOTED.sub("''", m.group(1) or m.group(2) or "")
		calls |= {name for name in CALL.findall(code) if name not in BUILT_IN}
	return calls


@pytest.mark.parametrize("script, names", [
	("function a() {}\nconst b = 1;", {"a", "b"}),
	("(() => {\n  function hidden() {}\n  window.shown = () => {};\n})();", set()),
	("const s = '{'; function after() {}", {"s", "after"}),
	("// } a comment\nasync function c() {}", {"c"}),
])
def test_top_level_names(script, names):
	"""Only depth-0 declarations count; braces in strings and comments don't."""
	assert top_level_names(script) == names


def test_every_inline_handler_calls_a_published_function():
	"""No template's inline handler calls a function its page can't reach -
	the old Active Jobs (confirmCancel inside the page's function) fails."""
	names = published()
	unreachable = {path.relative_to(TEMPLATES).as_posix(): sorted(calls - names)
	               for path in TEMPLATES.rglob("*.html")
	               if (calls := handler_calls(path.read_text(encoding="utf-8"))) - names}
	assert unreachable == {}
