"""python -m src.backup restore's exit codes, without a database: 0 restored,
1 nothing changed, 3 the database restored but not every file - and the
key handed over whenever the database was restored."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from src import runtime
from src.backup import __main__ as cli
from src.backup import archive
from tests.unit.backup.test_archive import make_zip


@pytest.fixture
def steps():
	"""The restore's database step, recorded instead of run (home)."""
	return []


@pytest.fixture
def home(tmp_path, monkeypatch, steps):
	"""NETROLLOUT_HOME in tmp_path, no database: the restore's database step is
	recorded in `steps` instead of run."""
	monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path))
	# an engine never connected: the database step is replaced
	monkeypatch.setattr(cli, "_app_engine",
	                    lambda: create_engine("postgresql+psycopg2://x:y@127.0.0.1:1/x"))
	monkeypatch.setattr(archive, "_restore_database", lambda *a, **k: steps.append("database"))
	(tmp_path / "backups").mkdir()
	return tmp_path


def backup_with_files(home):
	"""A backup holding a certificate, a rollout log and the key (b"k")."""
	return make_zip(home / "backups", "netrollout-0.9.0-20261005-020000-manual.zip",
	                version="0.9.0", members={"db/users.csv": "id\n", archive.KEY_MEMBER: "k",
	                         "certs/fullchain.pem": "CERT", "logs/rollout_a.log": "x"})


def _fernet_ok(monkeypatch):
	monkeypatch.setattr(archive, "Fernet", lambda key: object())


def test_a_file_failing_after_the_database_is_exit_3_with_the_key_handed_over(
		home, steps, monkeypatch, capsys):
	"""The certificates' folder failing after the database was restored: exit 3,
	the key written to --key-out, the logs still restored, and stderr says the
	database was restored and names what wasn't - never "nothing changed"."""
	_fernet_ok(monkeypatch)
	path = backup_with_files(home)
	real = archive._restore_files

	def certs_fail(zf, prefix, *args, **kwargs):
		if prefix == "certs":
			raise PermissionError(13, "Permission denied", "certs/fullchain.pem")
		return real(zf, prefix, *args, **kwargs)
	monkeypatch.setattr(archive, "_restore_files", certs_fail)
	key_out = home / "backups" / ".restored-key"

	code = cli.main(["restore", str(path), "--key-out", str(key_out)])
	err = capsys.readouterr().err
	assert code == 3
	assert steps == ["database"]
	assert key_out.read_bytes() == b"k\n"
	assert (home / "logs" / "rollout_a.log").read_text() == "x"
	assert "database was restored" in err and "certificate" in err
	assert "Permission denied" in err and "nothing" not in err.lower()


def test_a_damaged_file_in_the_zip_after_the_database_is_exit_3(home, monkeypatch, capsys):
	"""A zip member that can't be read (a damaged entry) after the database was
	restored is exit 3 too, with the key handed over."""
	_fernet_ok(monkeypatch)
	path = backup_with_files(home)
	monkeypatch.setattr(archive, "_write_like_folder",
	                    lambda *a: (_ for _ in ()).throw(archive.zipfile.BadZipFile("Bad CRC-32")))
	key_out = home / "backups" / ".restored-key"
	assert cli.main(["restore", str(path), "--key-out", str(key_out)]) == 3
	assert key_out.exists()
	assert "Bad CRC-32" in capsys.readouterr().err


def test_a_restore_refused_before_the_database_is_exit_1_and_hands_nothing_over(
		home, monkeypatch, capsys):
	"""The database step failing (the key doesn't decrypt): exit 1, no key file
	left, no file written."""
	_fernet_ok(monkeypatch)
	path = backup_with_files(home)

	def refused(*a, **k):
		raise archive.BackupError("The backup's encryption key doesn't decrypt its saved "
		                          "credentials - the backup is damaged. Nothing was changed.")
	monkeypatch.setattr(archive, "_restore_database", refused)
	key_out = home / "backups" / ".restored-key"
	assert cli.main(["restore", str(path), "--key-out", str(key_out)]) == 1
	assert not key_out.exists()
	assert not (home / "certs").exists() and not (home / "logs").exists()
	assert "doesn't decrypt" in capsys.readouterr().err


def test_a_database_error_before_the_commit_is_exit_1_in_words(home, monkeypatch, capsys):
	"""A database error during the database step (rolled back) is exit 1 with a
	one-line reason saying nothing was changed, not a traceback."""
	_fernet_ok(monkeypatch)
	path = backup_with_files(home)

	def lost(*a, **k):
		raise OperationalError("COPY", {}, Exception("server closed the connection"))
	monkeypatch.setattr(archive, "_restore_database", lost)
	assert cli.main(["restore", str(path)]) == 1
	err = capsys.readouterr().err
	assert "server closed the connection" in err and "Nothing was changed" in err


def test_a_full_restore_is_exit_0_with_the_key(home, monkeypatch):
	"""Everything restored: exit 0, the key handed over, the files written."""
	_fernet_ok(monkeypatch)
	path = backup_with_files(home)
	key_out = home / "backups" / ".restored-key"
	assert cli.main(["restore", str(path), "--key-out", str(key_out)]) == 0
	assert key_out.read_bytes() == b"k\n"
	assert (home / "certs" / "fullchain.pem").read_text() == "CERT"
