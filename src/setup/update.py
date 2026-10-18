"""A NetRollout release for Linux's `upgrade`: found (GitHub's newest on the
installed version's channel, a version, or a mirror's feed in the same
JSON), downloaded, checked against the release's SHA256SUMS and unpacked -
or a zip given by hand (offline). Windows upgrades through NetRollout Setup
instead (the Manager's Upgrade, Updates.cs: the same rule).

The channel follows what is installed:
  a release (1.0.0)        GitHub's releases/latest - never a pre-release
                           (GitHub's latest leaves them out); a 404 there is
                           "no release published yet"
  a pre-release (1.0.0rc1, GitHub's release list (releases?per_page=30, newest
  1.0.1.dev0, a/b)         first): drafts and tags that aren't a version
                           skipped, the newest by version (PEP 440: 1.0.0rc1
                           < 1.0.0rc2 < 1.0.0), pre-releases included
  --version X              releases/tags/vX, whatever is installed
  --feed                   one release object in GitHub's JSON, used as
                           given; or a list of them, picked as GitHub's list
                           (a release installed: the newest release in it)

The release contract (stage 10's pipeline makes it):
  netrollout-<version>-linux.zip   everything under netrollout/: bin/,
                                   compose.yaml, compose.http.yaml, deploy/,
                                   VERSION, LICENSE, README.md; entries
                                   marked as made on Unix with their modes
                                   (bin/*.sh 755) - `unzip` applies the modes
                                   only then, and a first install unzips it
  SHA256SUMS                       "<sha256>  <file name>" per asset
"""
import datetime
import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version

from src import runtime
from src.setup.env import env_read, env_path, UPGRADE_DEFAULTS


TOP = "netrollout"
TIMEOUT = 60


class ReleaseError(Exception):
	"""Why there's nothing to upgrade to, in words."""


@dataclass
class Release:
	"""A published release, as its feed describes it."""
	version: str
	notes: str
	zip_url: str | None
	sums_url: str | None
	page: str | None = None     # its web page (GitHub's html_url): the full notes


def zip_name(version: str) -> str:
	""":returns: the Linux release zip's file name"""
	return f"netrollout-{version}-linux.zip"


NOTHING_PUBLISHED = "No NetRollout release is published on GitHub yet."


def api(version: str | None = None) -> str:
	""":returns: GitHub's API address for that release (None: the latest)"""
	repo = runtime.SOURCE_REPO.removeprefix("https://github.com/")
	base = f"https://api.github.com/repos/{repo}/releases"
	return f"{base}/tags/v{version}" if version else f"{base}/latest"


def api_list() -> str:
	""":returns: GitHub's API address for the release list (newest first)"""
	return api().removesuffix("/latest") + "?per_page=30"


def is_prerelease(version: str) -> bool:
	""":returns: whether `version` is a pre-release (dev, a, b, rc) - one that
	can't be read counts as a release"""
	try:
		return Version(version).is_prerelease
	except InvalidVersion:
		return False


def newest(releases: list[Any], prereleases: bool) -> dict[str, Any] | None:
	"""The newest release in a list (GitHub's, or a feed's) by version: drafts
	and tags that aren't a version skipped, pre-releases only when asked for.

	:returns: that release's object, or None when the list has none"""
	best: tuple[Version, dict[str, Any]] | None = None
	for item in releases:
		if (not isinstance(item, dict) or item.get("draft") is True
				or not isinstance(item.get("tag_name"), str)):
			continue
		try:
			v = Version(item["tag_name"].removeprefix("v"))
		except InvalidVersion:
			continue
		if v.is_prerelease and not prereleases:
			continue
		if best is None or v > best[0]:
			best = (v, item)
	return best[1] if best else None


def _open(url: str) -> Any:
	""":returns: the open response (a context manager), GitHub's headers sent
	:raises OSError: no answer, or an HTTP error"""
	request = urllib.request.Request(url, headers={
		"User-Agent": f"NetRollout/{runtime.VERSION}",      # GitHub requires one
		"Accept": "application/vnd.github+json"})
	return urllib.request.urlopen(request, timeout=TIMEOUT)


def _url(location: str) -> str:
	# a plain path is a file (a mirror on disk, a test)
	return location if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", location) \
		else Path(location).resolve().as_uri()


def rate_limited(error: urllib.error.HTTPError) -> str | None:
	"""Why GitHub refused, when it's its limit for requests without a sign-in
	(60 an hour per internet address, shared by everything behind it): it
	answers 403 or 429 with X-RateLimit-Remaining 0 and the reset time in
	X-RateLimit-Reset (seconds since the epoch).

	:returns: the words, with the reset time when GitHub gives one; None for
	 any other refusal"""
	headers = error.headers
	if error.code not in (403, 429) or headers is None or headers.get("X-RateLimit-Remaining") != "0":
		return None
	words = ("GitHub's limit for upgrade checks from this network (60 an hour per internet "
	         "address, shared by everything here that asks GitHub) is used up")
	try:
		reset = datetime.datetime.fromtimestamp(int(headers.get("X-RateLimit-Reset", "")))
	except (TypeError, ValueError, OverflowError, OSError):
		return f"{words}. Try again within the hour."
	return f"{words}. It resets at {reset:%H:%M}: try again then."


def find(version: str | None = None, feed: str | None = None) -> Release:
	"""The release: `feed` (a mirror's JSON) if given, else that version's, else
	the newest on the installed version's channel (the module's docstring).
	:raises ReleaseError: none, or unreachable"""
	prereleases = is_prerelease(runtime.VERSION)
	github = not feed and not version
	where = feed or (api_list() if github and prereleases else api(version))
	try:
		with _open(_url(where)) as r:
			data = json.loads(r.read().decode())
	except urllib.error.HTTPError as e:
		if e.code == 404:
			if github:           # GitHub's latest: no release (only pre-releases)
				raise ReleaseError(NOTHING_PUBLISHED) from None
			raise ReleaseError(f"No release {'v' + version if version else ''} "
			                   f"at {where}.".replace("  ", " ")) from None
		if (limited := rate_limited(e)) is not None:
			raise ReleaseError(limited) from None
		raise ReleaseError(f"{where} answered {e.code}.") from None
	except (OSError, ValueError) as e:
		raise ReleaseError(f"Couldn't reach {where} ({e}) - is this server online? "
		                   f"Offline: upgrade --from <zip>.") from None
	if isinstance(data, list):
		picked = newest(data, prereleases)
		if picked is None:
			raise ReleaseError(NOTHING_PUBLISHED if github else f"No NetRollout release in "
			                   f"{where}{'' if prereleases else ' (a pre-release is offered only to a pre-release installed)'}.")
		data = picked
	if not isinstance(data, dict) or not isinstance(data.get("tag_name"), str):
		raise ReleaseError(f"{where} didn't answer with a release.")
	found = data["tag_name"].removeprefix("v")
	assets = {a.get("name"): a.get("browser_download_url")
	          for a in data.get("assets") or [] if isinstance(a, dict)}
	page = data.get("html_url")
	return Release(found, data.get("body") or "", assets.get(zip_name(found)),
	               assets.get("SHA256SUMS"), page if isinstance(page, str) else None)


# what upgrade shows of a release's notes (its CHANGELOG section: long) - the
# rest is on its page
NOTES_LINES = 30
NOTES_CHARS = 3000
# the notes of a release `release` downloaded, in its --out folder (the
# script's .upgrade/), for the script's confirmation
NOTES_FILE = "release-notes.txt"


def notes_shown(release: Release) -> list[str]:
	"""The release's notes as upgrade shows them: a heading, the notes' first
	lines (up to NOTES_LINES lines / NOTES_CHARS characters, whole lines, then
	how many more), and its page for the full notes when the feed gives one.

	:returns: the lines; [] when it has neither notes nor a page"""
	lines = release.notes.replace("\r\n", "\n").strip().splitlines()
	shown: list[str] = []
	chars = 0
	for line in lines:
		chars += len(line)
		if len(shown) == NOTES_LINES or (shown and chars > NOTES_CHARS):
			break
		shown.append(line)
	if more := len(lines) - len(shown):
		shown.append(f"... ({more} more line{'s' if more != 1 else ''})")
	if release.page:
		shown.append(f"Full notes: {release.page}")
	return [f"What's new in NetRollout {release.version}:", *shown] if shown else []


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
		listed = sums.read_text(encoding="utf-8")
		expected = expected_sum(listed, zip_path.name)
		if not expected:
			# a renamed zip would otherwise go unchecked
			names = sorted({m.group(1) for m in re.finditer(
				r"^[0-9a-fA-F]{64}\s+\*?(netrollout-\S+-linux\.zip)\s*$", listed, re.M)})
			back = (f"rename it back to {' or '.join(names)}" if names
			        else f"rename it back to its release name ({zip_name('<version>')})")
			raise ReleaseError(f"{zip_path.name} isn't listed in the SHA256SUMS next to it, "
			                   f"so it can't be checked - if it was renamed, {back}.")
		if hashlib.sha256(zip_path.read_bytes()).hexdigest() != expected:
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


# ── an upgrade ──

def update_kind(installed: str, new: str) -> str:
	"""Whether `new` may be installed over `installed`: only a newer version
	(the same one has nothing to upgrade; an older one is a downgrade - never:
	the database may be upgraded already).

	:returns: "update"
	:raises ValueError: the same version, an older one, or a version that
	 can't be read - the reason, for the person"""
	try:
		old_v, new_v = Version(installed), Version(new)
	except InvalidVersion as e:
		raise ValueError(f"Can't compare the versions ({e}).") from None
	if new_v == old_v:
		raise ValueError(f"NetRollout {installed} is already installed - the same version. "
		                 f"Nothing to upgrade.")
	if new_v < old_v:
		raise ValueError(f"NetRollout {installed} is installed - newer than {new}. "
		                 f"Downgrades aren't supported. Nothing was changed. To run an "
		                 f"earlier version: uninstall NetRollout, then install that version - "
		                 f"with your data from a backup made by that version (the current "
		                 f"data may already be upgraded).")
	return "update"


def upgrade(version: str = runtime.VERSION,
            now: datetime.datetime | None = None) -> list[str]:
	"""After an upgrade's files are in place: .env gets what this version needs
	(files.UPGRADE_DEFAULTS) - existing values and comments untouched - and a
	line saying when it was upgraded (UTC: the setup core runs in a container
	on UTC). Returns what to say.
	:raises ValueError: a key the data depends on is missing (nothing written)"""
	now = now or datetime.datetime.now(datetime.timezone.utc)
	path = env_path()
	text = path.read_text(encoding="utf-8")
	present = set(env_read())
	missing = [k for k in UPGRADE_DEFAULTS if k not in present]
	lost = [k for k in missing if UPGRADE_DEFAULTS[k] is None]
	if lost:
		raise ValueError(f"{path} is missing {', '.join(lost)} - the data depends on "
		                 f"{'it' if len(lost) == 1 else 'them'}, so NetRollout can't be "
		                 f"upgraded or started. Put {'it' if len(lost) == 1 else 'them'} "
		                 f"back (a copy of .env), then upgrade again.")
	lines = text.splitlines()
	stamp = f"# Upgraded to NetRollout {version} on {now:%Y-%m-%d %H:%M} UTC."
	# BRIDGE(rc4): remove in rc6 - "# Updated to NetRollout " (rc4 and earlier's stamp)
	stamped = [i for i, line in enumerate(lines)
	           if line.startswith(("# Upgraded to NetRollout ", "# Updated to NetRollout "))]
	if stamped:
		lines[stamped[0]] = stamp
	else:
		lines.insert(1 if lines and lines[0].startswith("#") else 0, stamp)
	said = []
	if missing:
		lines += ["", f"# Added by the upgrade to NetRollout {version}"]
		for k in missing:
			make = UPGRADE_DEFAULTS[k]
			assert make is not None   # those were refused above
			lines.append(f"{k}={make()}")
		said.append(f".env: added {', '.join(missing)}")
	with open(path, "w", encoding="utf-8", newline="\n") as f:
		f.write("\n".join(lines) + "\n")
	return said
