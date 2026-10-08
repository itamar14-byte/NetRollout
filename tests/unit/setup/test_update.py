"""Linux's update source (src/setup/update.py): a release found through a
feed in GitHub's JSON, downloaded, checked against SHA256SUMS and unpacked -
all on local files (file:// links, as a mirror on disk would be)."""
import hashlib
import io
import json
import urllib.error
import zipfile

import pytest

from src import runtime
from src.setup import __main__ as cli, update


def make_zip(path, version="1.0.1", extra=None, top=update.TOP):
	"""A minimal release zip under `top/` (scripts 755), plus `extra` entries as named."""
	with zipfile.ZipFile(path, "w") as zf:
		files = {"bin/netrollout.sh": "#!/usr/bin/env bash\n", "bin/install.sh": "#!/usr/bin/env bash\n",
		         "compose.yaml": "services: {}\n", "VERSION": version + "\n", "LICENSE": "AGPL"}
		files.update(extra or {})
		for name, data in files.items():
			info = zipfile.ZipInfo(f"{top}/{name}" if not name.startswith("/") and ".." not in name else name)
			if name.endswith(".sh"):
				info.external_attr = 0o755 << 16
			zf.writestr(info, data)
	return path


def publish(folder, version="1.0.1", wrong_sum=False, with_zip=True):
	"""A release as a mirror would serve it: the zip, SHA256SUMS, the JSON."""
	folder.mkdir(parents=True, exist_ok=True)
	name = update.zip_name(version)
	assets = []
	if with_zip:
		digest = hashlib.sha256(make_zip(folder / name, version).read_bytes()).hexdigest()
		if wrong_sum:
			digest = "0" * 64
		(folder / "SHA256SUMS").write_text(f"{digest}  {name}\n")
		assets = [{"name": name, "browser_download_url": (folder / name).as_uri()},
		          {"name": "SHA256SUMS", "browser_download_url": (folder / "SHA256SUMS").as_uri()}]
	feed = folder / "release.json"
	feed.write_text(json.dumps({"tag_name": f"v{version}", "body": "notes", "assets": assets}))
	return feed


def test_the_release_comes_from_github_unless_a_feed_is_given():
	"""The default source is GitHub's API: releases/latest, or releases/tags/vX for
	a version."""
	repo = runtime.SOURCE_REPO.removeprefix("https://github.com/")
	assert update.api() == f"https://api.github.com/repos/{repo}/releases/latest"
	assert update.api("1.0.1") == f"https://api.github.com/repos/{repo}/releases/tags/v1.0.1"


class FakeGitHub:
	"""Stands in for urllib's urlopen: answers each URL with the JSON (or the HTTP
	error code) it was given, and records every request."""
	def __init__(self, answers):
		self.answers, self.requests = answers, []

	def __call__(self, request, timeout):
		self.requests.append((request, timeout))
		answer = self.answers[request.full_url]
		if isinstance(answer, int):
			raise urllib.error.HTTPError(request.full_url, answer, "error", {}, io.BytesIO(b"{}"))
		return io.BytesIO(json.dumps(answer).encode())


def test_github_is_asked_as_it_requires_and_the_version_by_its_tag(monkeypatch):
	"""find() asks GitHub's API with a NetRollout User-Agent (GitHub requires one) and
	its JSON media type, within update.TIMEOUT; a version is looked up by its tag
	(releases/tags/vX), and the release found is that tag's, its zip and SHA256SUMS links
	taken by their names."""
	monkeypatch.setattr(runtime, "VERSION", "1.0.0")
	fake = FakeGitHub({update.api("1.0.1"): {"tag_name": "v1.0.1", "body": "the notes", "assets": [
		{"name": "NetRollout-Setup-1.0.1.exe", "browser_download_url": "https://x/setup.exe"},
		{"name": update.zip_name("1.0.1"), "browser_download_url": "https://x/linux.zip"},
		{"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS"}]}})
	monkeypatch.setattr(update.urllib.request, "urlopen", fake)
	found = update.find("1.0.1")
	assert found == update.Release("1.0.1", "the notes", "https://x/linux.zip", "https://x/SHA256SUMS")
	((request, timeout),) = fake.requests
	assert request.full_url == update.api("1.0.1") and timeout == update.TIMEOUT
	assert request.get_header("User-agent") == "NetRollout/1.0.0"
	assert request.get_header("Accept") == "application/vnd.github+json"


def test_what_github_answers_instead_of_a_release_is_said_in_words(monkeypatch):
	"""A 404 is "no release" - naming the version when one was asked for, the address
	either way; another HTTP error gives its code."""
	latest, tagged = update.api(), update.api("9.9.9")
	monkeypatch.setattr(update.urllib.request, "urlopen",
	                    FakeGitHub({latest: 404, tagged: 404, update.api("1.0.2"): 500}))
	with pytest.raises(update.ReleaseError) as e:
		update.find()
	assert str(e.value) == f"No release at {latest}."
	with pytest.raises(update.ReleaseError) as e:
		update.find("9.9.9")
	assert str(e.value) == f"No release v9.9.9 at {tagged}."
	with pytest.raises(update.ReleaseError) as e:
		update.find("1.0.2")
	assert str(e.value) == f"{update.api('1.0.2')} answered 500."


def test_a_release_with_odd_assets_or_no_notes(monkeypatch):
	"""Assets that aren't objects are passed over, a release without a body has empty
	notes, and one whose assets don't include the Linux zip has no zip link (its
	download then says so)."""
	monkeypatch.setattr(update.urllib.request, "urlopen", FakeGitHub({update.api(): {
		"tag_name": "v1.0.1", "body": None,
		"assets": ["junk", None, {"name": "SHA256SUMS", "browser_download_url": "https://x/S"}]}}))
	found = update.find()
	assert found == update.Release("1.0.1", "", None, "https://x/S")


def test_sums_that_dont_list_the_zip_are_refused(tmp_path):
	"""A SHA256SUMS without a line for the release's zip refuses the download and
	writes nothing."""
	feed = publish(tmp_path / "mirror")
	(tmp_path / "mirror" / "SHA256SUMS").write_text(f"{'a' * 64}  something-else.zip\n")
	found = update.find(feed=str(feed))
	with pytest.raises(update.ReleaseError, match=f"SHA256SUMS doesn't list {update.zip_name('1.0.1')}"):
		update.download(found, tmp_path / "update")
	assert not (tmp_path / "update").exists()


def test_a_sums_line_is_read_as_sha256sum_writes_it():
	"""expected_sum finds the file's line in text or binary mode ("*name"), lower-cases
	the digest, skips lines that aren't sums, and gives None for a file not listed."""
	digest = "AB" * 32
	sums = f"not a sum line\n{'c' * 64}  other.zip\n{digest} *netrollout-1.0.1-linux.zip\n"
	assert update.expected_sum(sums, "netrollout-1.0.1-linux.zip") == digest.lower()
	assert update.expected_sum(sums, "other.zip") == "c" * 64
	assert update.expected_sum(sums, "missing.zip") is None


def test_found_downloaded_checked_and_unpacked(tmp_path):
	"""A release found through a feed gives its version and notes; downloaded and unpacked,
	its files land in update/netrollout and the version is the zip's."""
	found = update.find(feed=str(publish(tmp_path / "mirror")))
	assert (found.version, found.notes) == ("1.0.1", "notes")
	folder, version = update.unpack(update.download(found, tmp_path / "update"), tmp_path / "update")
	assert version == "1.0.1" and folder == tmp_path / "update" / "netrollout"
	assert (folder / "compose.yaml").read_text() == "services: {}\n"
	assert (folder / "bin" / "netrollout.sh").exists()


def test_a_download_that_doesnt_match_its_checksum_is_deleted(tmp_path):
	"""A zip whose SHA256SUMS entry is wrong is refused and nothing is left in the folder."""
	found = update.find(feed=str(publish(tmp_path / "mirror", wrong_sum=True)))
	with pytest.raises(update.ReleaseError, match="doesn't match the release's checksum"):
		update.download(found, tmp_path / "update")
	assert not list((tmp_path / "update").iterdir())


def test_a_release_without_its_zip_yet_says_so(tmp_path):
	"""A release with no assets yet is refused naming the missing Linux zip."""
	found = update.find(feed=str(publish(tmp_path / "mirror", with_zip=False)))
	with pytest.raises(update.ReleaseError, match="has no netrollout-1.0.1-linux.zip yet"):
		update.download(found, tmp_path / "update")


def test_unreachable_or_not_a_release(tmp_path):
	"""An unreachable feed points to `update --from <zip>`; JSON that isn't a release
	(GitHub's rate-limit message) is refused as such."""
	with pytest.raises(update.ReleaseError, match="Offline: update --from <zip>"):
		update.find(feed=str(tmp_path / "missing.json"))
	(tmp_path / "x.json").write_text('{"message": "rate limited"}')
	with pytest.raises(update.ReleaseError, match="didn't answer with a release"):
		update.find(feed=str(tmp_path / "x.json"))


def test_unpack_keeps_inside_its_folder_and_needs_a_release(tmp_path):
	"""Entries with `..` or an absolute path are skipped (only netrollout/ is written);
	a zip without netrollout/ isn't a release, and a file that isn't a zip is refused."""
	z = make_zip(tmp_path / "evil.zip", extra={"../../escape.txt": "x", "/abs.txt": "y"})
	folder, _ = update.unpack(z, tmp_path / "out")
	assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "out" / "escape.txt").exists()
	assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["netrollout"]
	with pytest.raises(update.ReleaseError, match="isn't a NetRollout release"):
		update.unpack(make_zip(tmp_path / "other.zip", top="something"), tmp_path / "out2")
	(tmp_path / "plain.zip").write_text("not a zip")
	with pytest.raises(update.ReleaseError, match="isn't a zip"):
		update.unpack(tmp_path / "plain.zip", tmp_path / "out3")


def test_a_zip_given_by_hand_is_checked_against_sums_next_to_it(tmp_path):
	"""A `--from` zip is refused when the SHA256SUMS beside it doesn't match, and
	unpacked as given when there is no SHA256SUMS."""
	z = make_zip(tmp_path / update.zip_name("1.0.1"))
	(tmp_path / "SHA256SUMS").write_text(f"{'1' * 64}  {z.name}\n")
	with pytest.raises(update.ReleaseError, match="doesn't match the SHA256SUMS next to it"):
		update.unpack(z, tmp_path / "out")
	(tmp_path / "SHA256SUMS").unlink()                     # none: taken as given
	assert update.unpack(z, tmp_path / "out")[1] == "1.0.1"


def run(argv):
	"""The setup CLI run with `argv`: (exit code, the lines it wrote)."""
	out = []
	return cli.main(argv, write=out.append), out


def test_the_cli_checks_and_fetches(tmp_path, monkeypatch):
	"""`release --check` says a newer version is available or that this one is the latest;
	`release` prints version= and folder= of the unpacked release; a bad `--from-zip`
	exits 1 saying it isn't a zip."""
	monkeypatch.setattr(runtime, "VERSION", "1.0.0")
	feed = str(publish(tmp_path / "mirror"))
	assert run(["release", "--check", "--feed", feed]) == (0, [
		"NetRollout 1.0.1 is available (you have 1.0.0): sudo bin/netrollout.sh update"])
	monkeypatch.setattr(runtime, "VERSION", "1.0.1")
	assert run(["release", "--check", "--feed", feed]) == (0, ["You have the latest version (1.0.1)."])
	code, out = run(["release", "--feed", feed, "--out", str(tmp_path / "up")])
	assert code == 0 and out == ["version=1.0.1", f"folder={tmp_path / 'up' / 'netrollout'}"]
	code, out = run(["release", "--from-zip", str(tmp_path / "nothing.zip"), "--out", str(tmp_path / "up")])
	assert code == 1 and "isn't a zip" in out[0]
