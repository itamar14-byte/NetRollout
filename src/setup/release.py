"""A NetRollout release for Linux's `update`: found (GitHub's latest, a
version, or a mirror's feed in the same JSON), downloaded, checked against
the release's SHA256SUMS and unpacked - or a zip given by hand (offline).
Windows updates through NetRollout Setup instead (the Manager's Update).

The release contract (stage 10's pipeline makes it):
  netrollout-<version>-linux.zip   everything under netrollout/: bin/,
                                   compose.yaml, compose.http.yaml, deploy/,
                                   VERSION, LICENSE, README.md; entries
                                   marked as made on Unix with their modes
                                   (bin/*.sh 755) - `unzip` applies the modes
                                   only then, and a first install unzips it
  SHA256SUMS                       "<sha256>  <file name>" per asset
"""
import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from src import runtime

TOP = "netrollout"
# what an update replaces in the install folder - never .env, config/,
# certs/, logs/, backups/
REPLACED = ("bin", "compose.yaml", "compose.http.yaml", "deploy", "VERSION",
            "LICENSE", "README.md")
TIMEOUT = 60


class ReleaseError(Exception):
	"""Why there's nothing to update to, in words."""


@dataclass
class Release:
	version: str
	notes: str
	zip_url: str | None
	sums_url: str | None


def zip_name(version: str) -> str:
	return f"netrollout-{version}-linux.zip"


def api(version: str | None = None) -> str:
	repo = runtime.SOURCE_REPO.removeprefix("https://github.com/")
	base = f"https://api.github.com/repos/{repo}/releases"
	return f"{base}/tags/v{version}" if version else f"{base}/latest"


def _open(url: str):
	request = urllib.request.Request(url, headers={
		"User-Agent": f"NetRollout/{runtime.VERSION}",      # GitHub requires one
		"Accept": "application/vnd.github+json"})
	return urllib.request.urlopen(request, timeout=TIMEOUT)


def _url(location: str) -> str:
	# a plain path is a file (a mirror on disk, a test)
	return location if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", location) \
		else Path(location).resolve().as_uri()


def find(version: str | None = None, feed: str | None = None) -> Release:
	"""The release: `feed` (a mirror's JSON) if given, else GitHub's latest
	or that version's. :raises ReleaseError: none, or unreachable"""
	where = feed or api(version)
	try:
		with _open(_url(where)) as r:
			data = json.loads(r.read().decode())
	except urllib.error.HTTPError as e:
		if e.code == 404:
			raise ReleaseError(f"No release {'v' + version if version else ''} "
			                   f"at {where}.".replace("  ", " ")) from None
		raise ReleaseError(f"{where} answered {e.code}.") from None
	except (OSError, ValueError) as e:
		raise ReleaseError(f"Couldn't reach {where} ({e}) - is this server online? "
		                   f"Offline: update --from <zip>.") from None
	if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
		raise ReleaseError(f"{where} didn't answer with a release.")
	found = data["tag_name"].removeprefix("v")
	assets = {a.get("name"): a.get("browser_download_url")
	          for a in data.get("assets") or [] if isinstance(a, dict)}
	return Release(found, data.get("body") or "", assets.get(zip_name(found)),
	               assets.get("SHA256SUMS"))


def expected_sum(sums: str, name: str) -> str | None:
	for line in sums.splitlines():
		m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
		if m and m.group(2).strip() == name:
			return m.group(1).lower()
	return None


def download(release: Release, dest: Path) -> Path:
	"""The release's zip into `dest`, checked against its SHA256SUMS.
	:raises ReleaseError: missing assets, unreachable, or a mismatch (deleted)"""
	name = zip_name(release.version)
	if not release.zip_url or not release.sums_url:
		raise ReleaseError(f"The release {release.version} has no "
		                   f"{name if not release.zip_url else 'SHA256SUMS'} yet - "
		                   f"try again later.")
	try:
		with _open(_url(release.sums_url)) as r:
			sums = r.read().decode()
		expected = expected_sum(sums, name)
		if not expected:
			raise ReleaseError(f"SHA256SUMS doesn't list {name}.")
		dest.mkdir(parents=True, exist_ok=True)
		path = dest / name
		digest = hashlib.sha256()
		with _open(_url(release.zip_url)) as r, open(path, "wb") as out:
			while chunk := r.read(1 << 16):
				digest.update(chunk)
				out.write(chunk)
	except (OSError, ValueError) as e:
		raise ReleaseError(f"Couldn't download {name} ({e}).") from None
	if digest.hexdigest() != expected:
		path.unlink(missing_ok=True)
		raise ReleaseError(f"{name} doesn't match the release's checksum - the "
		                   f"download is damaged or was altered; it was deleted.")
	return path


def unpack(zip_path: Path, dest: Path) -> tuple[Path, str]:
	"""The release's files into `dest/netrollout`; returns that folder and the
	version it brings. A file given by hand is checked against a SHA256SUMS
	next to it when there is one.
	:raises ReleaseError: not a NetRollout release, or it doesn't match"""
	sums = zip_path.parent / "SHA256SUMS"
	if sums.exists():
		expected = expected_sum(sums.read_text(encoding="utf-8"), zip_path.name)
		if expected and hashlib.sha256(zip_path.read_bytes()).hexdigest() != expected:
			raise ReleaseError(f"{zip_path.name} doesn't match the SHA256SUMS next "
			                   f"to it - the file is damaged or was altered.")
	try:
		archive = zipfile.ZipFile(zip_path)
	except (zipfile.BadZipFile, OSError) as e:
		raise ReleaseError(f"{zip_path.name} isn't a zip ({e}).") from None
	target = dest / TOP
	if target.exists():
		shutil.rmtree(target)
	with archive:
		for info in archive.infolist():
			parts = PurePosixPath(info.filename).parts
			# only what's under netrollout/, never outside it
			if not parts or parts[0] != TOP or ".." in parts or info.filename.startswith("/"):
				continue
			out = dest.joinpath(*parts)
			if info.is_dir():
				out.mkdir(parents=True, exist_ok=True)
				continue
			out.parent.mkdir(parents=True, exist_ok=True)
			with archive.open(info) as src, open(out, "wb") as f:
				shutil.copyfileobj(src, f)
			mode = (info.external_attr >> 16) & 0o777
			if mode:
				out.chmod(mode)
	try:
		version = (target / "VERSION").read_text(encoding="utf-8").strip()
	except OSError:
		raise ReleaseError(f"{zip_path.name} isn't a NetRollout release "
		                   f"(no {TOP}/VERSION).") from None
	missing = [n for n in ("bin", "compose.yaml", "VERSION") if not (target / n).exists()]
	if missing:
		raise ReleaseError(f"{zip_path.name} is incomplete (no {', '.join(missing)}).")
	return target, version
