"""The repository's text files are written with " - ", never an em dash."""
import subprocess

from src import runtime


BINARY = (".png", ".ico", ".exe", ".woff", ".woff2", ".zip", ".bmp")
EM_DASH = "\u2014".encode("utf-8")   # written as a code: this file holds none either


def test_no_tracked_text_file_has_an_em_dash():
	"""No file git tracks (binary ones left out by extension) contains an em dash
	(U+2014, UTF-8 E2 80 94); the offending files are listed."""
	root = runtime.REPO_ROOT
	tracked = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
	                         check=True).stdout.decode("utf-8").split("\0")
	files = [name for name in tracked if name and not name.lower().endswith(BINARY)]
	assert len(files) > 100                                       # git answered with the repo
	offending = [name for name in files
	             if (root / name).is_file() and EM_DASH in (root / name).read_bytes()]
	assert offending == []
