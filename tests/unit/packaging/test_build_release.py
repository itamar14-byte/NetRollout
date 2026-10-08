"""The release build (tools/build_release.py): the Linux zip to its contract,
one list of shipped files that the installer and compose agree with, the
sums, a feed the update code reads."""
import importlib.util
import re
import stat
import zipfile
from pathlib import Path

import pytest
import yaml

from src.setup import update


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
build = importlib.util.module_from_spec(spec)

spec.loader.exec_module(build)
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_the_linux_zip_keeps_the_contract(tmp_path):
	"""The Linux zip has the release's name, everything under netrollout/, all that an
	update replaces, exactly SHIPPED + the Linux scripts + VERSION + README.md, Unix
	entries (755 for .sh, 644 else) and LF line ends."""
	path = build.build_linux_zip(VERSION, tmp_path)
	assert path.name == update.zip_name(VERSION)
	with zipfile.ZipFile(path) as zf:
		infos = {i.filename: i for i in zf.infolist()}
		names = {n.removeprefix("netrollout/") for n in infos}
		assert all(n.startswith("netrollout/") for n in infos)
		# what an update replaces (release.REPLACED) is all there
		for top in update.REPLACED:
			assert any(n == top or n.startswith(top + "/") for n in names), top
		assert names == {*build.SHIPPED, *build.LINUX_BIN, "VERSION", "README.md"}
		assert zf.read("netrollout/VERSION").decode() == VERSION + "\n"
		for name, info in infos.items():
			assert info.create_system == 3, name                    # unzip applies modes only then
			mode = stat.S_IMODE(info.external_attr >> 16)
			assert mode == (0o755 if name.endswith(".sh") else 0o644), name
			assert b"\r\n" not in zf.read(name), name               # LF, whatever the checkout


def test_installers_ship_the_same_files():
	"""The Windows installer's {#Root} files go to the same relative folders and are
	exactly SHIPPED (plus VERSION)."""
	iss = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	from_root = set()
	for source, dest in re.findall(r'^Source: "\{#Root\}\\([^"]+)"; DestDir: "\{app\}([^"]*)"', iss, re.M):
		name = source.replace("\\", "/")
		parent = Path(name).parent.as_posix()
		assert dest.replace("\\", "/").lstrip("/") == ("" if parent == "." else parent), name
		from_root.add(name)
	assert from_root - {"VERSION"} == set(build.SHIPPED)


def test_every_file_compose_mounts_is_shipped():
	"""Every file under ./deploy/ that compose.yaml mounts is in SHIPPED (and there
	are some)."""
	compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
	mounted = set()
	for service in compose["services"].values():
		for volume in service.get("volumes", []):
			source = volume.split(":")[0]
			if source.startswith("./deploy/"):
				target = ROOT / source[2:]
				files = [target] if target.is_file() else [p for p in target.rglob("*") if p.is_file()]
				mounted |= {p.relative_to(ROOT).as_posix() for p in files}
	assert mounted and mounted <= set(build.SHIPPED)


def test_the_version_must_be_the_version_files():
	"""A --version other than the VERSION file's stops the build, naming VERSION."""
	with pytest.raises(SystemExit, match="VERSION"):
		build.main(["--version", "9.9.9", "--linux"])


def test_sums_and_a_feed_the_update_code_reads(tmp_path):
	"""The written SHA256SUMS gives the zip's sum as the update code reads it, and the
	written feed is found as the release: version, notes, zip and sums links."""
	zip_path = build.build_linux_zip(VERSION, tmp_path)
	sums = build.write_sums([zip_path], tmp_path)
	line = sums.read_text(encoding="ascii")
	assert update.expected_sum(line, zip_path.name) == build.sha256(zip_path)
	feed = build.write_feed(VERSION, [zip_path, sums], tmp_path.as_uri(), "notes", tmp_path)
	found = update.find(feed=str(feed))
	assert (found.version, found.notes) == (VERSION, "notes")
	assert found.zip_url == f"{tmp_path.as_uri()}/{zip_path.name}"
	assert found.sums_url == f"{tmp_path.as_uri()}/SHA256SUMS"
