"""Build NetRollout's release files - what stage 10's release job publishes
and what a person can verify first (stage 9.9). From the repo root:

  python packaging/build_release.py --version 1.0.0 [--out DIR] [--linux] [--windows]
                                [--feed-base URL] [--notes FILE]

  netrollout-<v>-linux.zip      the Linux install / update (src/setup/update.py's contract)
  NetRollout-Setup-<v>.exe      the Windows install / update (Windows only: Inno Setup)
  netrollout-cli-<v>.exe        the headless CLI (Windows only: PyInstaller)
  SHA256SUMS                    "<sha256>  <name>" for each
  release-feed.json             with --feed-base: the release as GitHub's API describes it, the
                                links under that base (http(s) or file) - a mirror, or a test of
                                NetRollout Manager -> Update (UpdateFeed) and `netrollout update --feed`

The version must be the VERSION file's: the images, Setup and the footer all
take it from there. The Docker images aren't built here: the release job builds and pushes them
(docker build --build-arg VERSION=...).

SHIPPED is the one list of the files an install gets besides the images;
tests/unit/packaging/test_build_release.py checks that the Windows installer ships the
same and that every file compose mounts is in it.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/itamar14-byte/NetRollout"

# install folder path -> repo path (both platforms; bin/ differs, below)
SHIPPED = {
	"compose.yaml": "compose.yaml",
	"compose.http.yaml": "compose.http.yaml",
	"LICENSE": "LICENSE",
	"deploy/initdb/10-netrollout.sh": "deploy/initdb/10-netrollout.sh",
	"deploy/prometheus/prometheus.yml": "deploy/prometheus/prometheus.yml",
	"deploy/loki/loki-config.yml": "deploy/loki/loki-config.yml",
	"deploy/alloy/config.alloy": "deploy/alloy/config.alloy",
	"deploy/grafana/provisioning/datasources/netrollout.yml":
		"deploy/grafana/provisioning/datasources/netrollout.yml",
}
LINUX_BIN = {"bin/install.sh": "packaging/linux/install.sh",
             "bin/netrollout.sh": "packaging/linux/netrollout.sh"}


def linux_zip_name(version: str) -> str:
	return f"netrollout-{version}-linux.zip"


def setup_name(version: str) -> str:
	return f"NetRollout-Setup-{version}.exe"


def cli_name(version: str) -> str:
	return f"netrollout-cli-{version}.exe"


def build_linux_zip(version: str, out: Path) -> Path:
	"""The Linux release zip: everything under netrollout/, entries made on
	Unix with their modes (unzip applies them only then): scripts 755, the
	rest 644; text with LF line ends.

	:param out: the folder it's written to
	:returns: its path"""
	path = out / linux_zip_name(version)
	readme = ROOT / "README.md"
	members = {**{k: (ROOT / v).read_bytes() for k, v in {**SHIPPED, **LINUX_BIN}.items()},
	           "VERSION": f"{version}\n".encode(),
	           "README.md": readme.read_bytes() if readme.exists() else
	           f"NetRollout {version}\n\nInstall: sudo ./bin/install.sh\nMore: {REPO_URL}\n".encode()}
	with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
		for name in sorted(members):
			info = zipfile.ZipInfo(f"netrollout/{name}", date_time=(2026, 1, 1, 0, 0, 0))
			info.create_system = 3
			info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
			info.compress_type = zipfile.ZIP_DEFLATED
			# all text: LF whatever the checkout (a Windows one has CRLF files)
			zf.writestr(info, members[name].replace(b"\r\n", b"\n"))
	return path


def _run(*args: str | Path, **kw: Any) -> None:
	"""Run a command from the repo root, printed first.

	:raises subprocess.CalledProcessError: it failed"""
	print("  $ " + " ".join(str(a) for a in args), flush=True)
	subprocess.run([str(a) for a in args], check=True, cwd=ROOT, **kw)


def _iscc() -> None:
	"""Compile NetRollout Setup with Inno Setup: a local ISCC.exe (CI's
	Windows runner), else its container."""
	for candidate in (shutil.which("iscc"), r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"):
		if candidate and Path(candidate).exists():
			_run(candidate, ROOT / "packaging" / "windows" / "installer" / "netrollout.iss")
			return
	repo = subprocess.run(["cmd", "/c", "cd"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
	_run("docker", "run", "--rm", "-v", f"{repo}:/work", "amake/innosetup",
	     "packaging/windows/installer/netrollout.iss")


def build_windows(version: str, out: Path) -> list[Path]:
	"""NetRollout Manager, NetRollout Setup and the CLI .exe, into `out`.

	:returns: Setup's path and the CLI's
	:raises SystemExit: not on Windows"""
	if sys.platform != "win32":
		raise SystemExit("The Windows files are built on Windows (the Manager uses Windows' C# compiler).")
	_run("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
	     ROOT / "packaging" / "windows" / "manager" / "build.ps1")
	_iscc()
	built = ROOT / "dist" / f"NetRollout-Setup-{version}.exe"
	setup = out / setup_name(version)
	shutil.move(built, setup)
	_run(sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
	     ROOT / "packaging" / "netrollout-cli.spec")
	cli = out / cli_name(version)
	shutil.copy2(ROOT / "dist" / "netrollout-cli.exe", cli)
	return [setup, cli]


def sha256(path: Path) -> str:
	""":returns: the file's SHA-256, in hex"""
	h = hashlib.sha256()
	with path.open("rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			h.update(chunk)
	return h.hexdigest()


def write_sums(files: list[Path], out: Path) -> Path:
	"""SHA256SUMS for the files, as `sha256sum` writes it.

	:returns: its path"""
	path = out / "SHA256SUMS"
	path.write_text("".join(f"{sha256(f)}  {f.name}\n" for f in sorted(files)),
	                encoding="ascii", newline="\n")
	return path


def write_feed(version: str, files: list[Path], base: str, notes: str, out: Path) -> Path:
	"""A release feed, as GitHub's releases API answers (what the Manager and
	`update` read) - for a mirror or a test.

	:param base: the address the files are served from
	:param notes: the release notes
	:returns: its path"""
	base = base.rstrip("/")
	feed = {"tag_name": f"v{version}", "name": f"NetRollout {version}", "body": notes,
	        "html_url": f"{REPO_URL}/releases/tag/v{version}",
	        "assets": [{"name": f.name, "browser_download_url": f"{base}/{f.name}"}
	                   for f in sorted(files)]}
	path = out / "release-feed.json"
	path.write_text(json.dumps(feed, indent=2), encoding="utf-8")
	return path


def main(argv: list[str] | None = None) -> int:
	"""Build the release files, their checksums and (with --feed-base) a feed.

	:param argv: the arguments; sys.argv's when None
	:returns: 0
	:raises SystemExit: --version isn't the VERSION file's, or a build failed"""
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--version", required=True)
	parser.add_argument("--out", type=Path)
	parser.add_argument("--linux", action="store_true", help="only the Linux zip")
	parser.add_argument("--windows", action="store_true", help="only the Windows files")
	parser.add_argument("--feed-base")
	parser.add_argument("--notes", type=Path)
	args = parser.parse_args(argv)

	in_repo = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
	if args.version != in_repo:
		raise SystemExit(f"--version {args.version} isn't the VERSION file's {in_repo}: "
		                 f"set VERSION first (the images and Setup take it from there).")
	both = not (args.linux or args.windows)
	out = args.out or ROOT / "dist" / f"release-{args.version}"
	out.mkdir(parents=True, exist_ok=True)
	files: list[Path] = []
	if args.linux or both:
		print("-> the Linux zip", flush=True)
		files.append(build_linux_zip(args.version, out))
	if args.windows or both:
		print("-> the Windows files", flush=True)
		files += build_windows(args.version, out)
	files = sorted({*files, *(f for f in out.iterdir() if f.name in (
		linux_zip_name(args.version), setup_name(args.version), cli_name(args.version)))})
	write_sums(files, out)
	if args.feed_base:
		notes = args.notes.read_text(encoding="utf-8") if args.notes else f"NetRollout {args.version}"
		write_feed(args.version, [*files, out / "SHA256SUMS"], args.feed_base, notes, out)
	print(f"-> {out}", flush=True)
	for f in sorted(out.iterdir()):
		print(f"   {f.name}  {f.stat().st_size:,} bytes")
	return 0


if __name__ == "__main__":
	sys.exit(main())
