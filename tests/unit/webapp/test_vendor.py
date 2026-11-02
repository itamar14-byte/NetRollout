"""The third-party files the web app serves from the image (src/static/vendor):
each is the file as its project published it - its sha256 recorded in
vendor/SHA256SUMS when it was checked (a line-ending conversion or an edit
shows up here) - and each library carries its licence."""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "src" / "static" / "vendor"
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

