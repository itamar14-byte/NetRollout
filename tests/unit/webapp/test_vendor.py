"""The third-party files the web app serves from the image (src/static/vendor):
each is the file as its project published it - its sha256 recorded in
vendor/SHA256SUMS when it was checked (a line-ending conversion or an edit
shows up here) - and each library carries its licence."""
import hashlib
import re
from pathlib import Path

from src.webapp.build import VENDOR_LOGOS

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "src" / "static" / "vendor"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "src" / "static"
LICENCE = re.compile(r"LICENSE|OFL")


def recorded() -> dict[str, str]:
	""":returns: path under vendor/ → the sha256 SHA256SUMS records"""
	sums = {}
	for line in (VENDOR / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
		digest, path = line.split("  ", 1)
		sums[path] = digest
	return sums


def test_every_vendored_file_is_as_published():
	"""Each file has the sha256 recorded, and nothing is there unrecorded."""
	files = {p.relative_to(VENDOR).as_posix() for p in VENDOR.rglob("*") if p.is_file()} - {"SHA256SUMS"}
	sums = recorded()
	assert files == set(sums)
	assert {path: hashlib.sha256((VENDOR / path).read_bytes()).hexdigest() for path in files} == sums


def test_every_vendored_library_carries_its_licence():
	"""Each library folder (vendor/<library>/<version>/, the fonts', the
	logos') holds a licence file."""
	folders = {p.parent for p in VENDOR.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
	top = {f.relative_to(VENDOR).parts[0] for f in folders}
	for name in top:
		licences = [p for p in (VENDOR / name).rglob("*") if p.is_file() and LICENCE.search(p.name)]
		assert licences, f"vendor/{name} has no licence file"


def test_no_page_loads_anything_from_another_site():
	"""No template loads a script, stylesheet or image from another site, and
	the vendor logos are NetRollout's own files: the web app works without
	internet access (links people follow, like the source code's, aside)."""
	remote = re.compile(r"""<(?:script|link|img)\b[^>]*?\b(?:src|href)\s*=\s*["'](https?:)?//[^"']*""", re.I | re.S)
	found = {t.name: [m.group(0) for m in remote.finditer(t.read_text(encoding="utf-8"))]
	         for t in TEMPLATES.glob("*.html")}
	assert {name: hits for name, hits in found.items() if hits} == {}
	assert [url for url in VENDOR_LOGOS.values() if not url.startswith("/static/vendor/logos/")] == []
	assert [url for url in VENDOR_LOGOS.values()
	        if not (STATIC / url[len("/static/"):].split("?")[0]).is_file()] == []
	css_remote = re.compile(r"""url\(\s*["']?(https?:)?//""")
	assert [p.name for p in (STATIC / "css").glob("*.css") if css_remote.search(p.read_text(encoding="utf-8"))] == []
