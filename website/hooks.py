"""MkDocs hook for the documentation site (mkdocs.yml): docs/*.md link to the
repository's other files (CONTRIBUTING.md, SECURITY.md, ...) as ../<file>,
which works on GitHub; the site has only docs/, so those links point at the
file on GitHub instead."""
from __future__ import annotations

import re
from typing import Any

REPO_FILES = "https://github.com/itamar14-byte/NetRollout/blob/master/"
# ](../FILE) or ](../FILE#anchor) - a link out of docs/
OUTSIDE_DOCS = re.compile(r"\]\(\.\./(?!docs/)([^)#\s]+)((?:#[^)\s]*)?)\)")


def on_page_markdown(markdown: str, **kwargs: Any) -> str:
	""":returns: the page with links out of docs/ pointed at GitHub"""
	return OUTSIDE_DOCS.sub(lambda m: f"]({REPO_FILES}{m.group(1)}{m.group(2)})", markdown)
