"""The shipped files say what stage 9.1 decided (docs/plans/stage-9.md): the
hostname reaches nginx only through site.env, .env carries no seeds, the
version is the VERSION file, Grafana's setup comes from the app image."""
import yaml

from src import runtime

ROOT = runtime.REPO_ROOT


def compose(name="compose.yaml"):
	return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def env(service):
	return compose()["services"][service].get("environment") or {}


def test_nginx_takes_the_hostname_and_port_only_from_site_env():
	assert "NETROLLOUT_HOSTNAME" not in env("nginx")
	assert "NETROLLOUT_HTTPS_PORT" not in env("nginx")


def test_the_app_gets_no_seeds_but_the_published_port_and_the_server_ips():
	app = env("app")
	assert "NETROLLOUT_PUBLIC_HOSTNAME" not in app      # site.env seeds it
	assert "ORCHESTRATOR_WORKERS" not in app            # System Settings
	assert app["NETROLLOUT_HTTPS_PORT"] == "${HTTPS_PORT:-443}"
	assert app["NETROLLOUT_SERVER_IPS"] == "${NETROLLOUT_SERVER_IPS:-}"


def test_the_app_writes_backups_and_reads_grafanas_data_only():
	# scheduled backups (src/backup.py): into backups/, with Grafana's
	# database — read only, readable through group 0 (its file is 640 472:0)
	app = compose()["services"]["app"]
	assert "./backups:/data/backups" in app["volumes"]
	assert "grafana:/data/grafana:ro" in app["volumes"]
	assert app["group_add"] == ["0"]
	grafana = compose()["services"]["grafana"]["volumes"]
	assert any(v.startswith("grafana:/var/lib/grafana") for v in grafana)


def test_the_windows_restore_hands_secrets_over_safely():
	# the restored key is root's (the restore runs as root): restore-key too;
	# Grafana's password never piped from PowerShell 5.1 (a byte-order mark
	# and CR LF become part of it) nor on a command line
	script = (ROOT / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '(Invoke-Setup @("restore-key") -AsRoot)' in script
	assert "reset-admin-password --password-from-stdin" in script
	assert 'printf %s "$NR_GRAFANA_PASSWORD"' in script
	assert '"-e", "NR_GRAFANA_PASSWORD", "grafana"' in script


def test_port_80_is_always_80_when_switched_on():
	assert compose("compose.http.yaml")["services"]["nginx"]["ports"] == ["80:80"]


def test_grafana_setup_runs_from_the_app_image():
	setup = compose()["services"]["grafana-setup"]
	assert "volumes" not in setup
	assert setup["command"] == ["python", "/app/grafana/setup.py"]
	assert setup["environment"]["DASHBOARDS_DIR"] == "/app/grafana/dashboards"
	dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
	assert "COPY deploy/grafana/setup.py grafana/setup.py" in dockerfile
	assert "COPY deploy/grafana/dashboards/ grafana/dashboards/" in dockerfile
	ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
	assert "!deploy/grafana/setup.py" in ignore and "!deploy/grafana/dashboards/" in ignore


def test_the_version_file_travels_into_the_image_and_the_exe():
	assert "COPY LICENSE VERSION ./" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
	assert "!VERSION" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
	assert '("VERSION", ".")' in (ROOT / "netrollout-cli.spec").read_text(encoding="utf-8")
	# nothing rewrites the code any more
	assert "sed -i" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


# ── the Windows installer (windows/installer/netrollout.iss) ──

def iss_sources():
	import re
	installer = ROOT / "windows" / "installer"
	text = (installer / "netrollout.iss").read_text(encoding="utf-8")
	for line in text.splitlines():
		m = re.match(r'Source: "([^"]+)"', line)
		if m:
			source = m.group(1).replace("\\", "/")
			if source.startswith("{#Root}/"):
				yield ROOT / source[len("{#Root}/"):]
			else:
				yield (installer / source).resolve()


def test_every_file_the_installer_packs_exists():
	# a rename would otherwise surface only when CI compiles the installer
	missing = [str(p) for p in iss_sources()
	           if p.name != "NetRollout Manager.exe" and not p.exists()]   # built
	assert missing == []


def test_the_installer_ships_what_the_zip_ships():
	names = {p.name for p in iss_sources()}
	for needed in ("compose.yaml", "compose.http.yaml", "VERSION", "LICENSE",
	               "manage.ps1", "netrollout.bat", "netrollout.ico",
	               "NetRollout Manager.exe", "prometheus.yml",
	               "loki-config.yml", "config.alloy", "netrollout.yml"):
		assert needed in names, needed


def test_the_installed_layout_is_bin_and_the_licence_page_has_its_text():
	import re
	installer = ROOT / "windows" / "installer"
	text = (installer / "netrollout.iss").read_text(encoding="utf-8")
	for name in ("NetRollout Manager.exe", "manage.ps1", "netrollout.bat", "netrollout.ico"):
		assert re.search(r'Source: "\.\.\\' + re.escape(name) + r'"; DestDir: "\{app\}\\bin"', text), name
	licence = re.search(r"^LicenseFile=(.+)$", text, re.M).group(1).strip()
	assert (installer / licence).exists()
	# one install method on Windows: the installer (no console install)
	assert not (ROOT / "windows" / "install.bat").exists()


def test_the_netrollout_command_on_path_is_only_the_bat():
	# bin\ goes on PATH (the addtopath task); PowerShell runs a netrollout.ps1
	# there before netrollout.bat, and Windows' default policy refuses scripts
	import re
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	installed = re.findall(r'^Source: "\.\.\\([^"]+)"; DestDir: "\{app\}\\bin"', text, re.M)
	assert [n for n in installed if n.lower().startswith("netrollout.")] == ["netrollout.bat", "netrollout.ico"]
	assert re.search(r"^Name: addtopath;", text, re.M)
	assert "App Paths\\netrollout.exe" in text


def test_the_licence_page_shows_the_full_licence_from_the_one_file():
	# the notice on top, then the repo's LICENSE (packed for the page, not a copy)
	import re
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert re.search(r'^Source: "\{#Root\}\\LICENSE"; Flags: dontcopy$', text, re.M)
	assert re.search(r"^\tShowFullLicence;$", text, re.M)
	assert "ExtractTemporaryFile('LICENSE')" in text


def test_the_installer_ends_honestly():
	# success: the portal opens on Finish (ticked); failure: Retry unless the
	# script says retrying can't help (exit 3: the release's image is missing)
	import re
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	browser = re.search(r'^Filename: "\{code:Address\}";.*$', text, re.M).group(0)
	assert "postinstall" in browser and "unchecked" not in browser and "Check: SetUpOk" in browser
	assert "MB_RETRYCANCEL" in text and "(SetUpCode = 3)" in text
	script = (ROOT / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert re.search(r"isn't published on Docker Hub.*\) 3$", script, re.M | re.S)


def test_the_linux_scripts_have_unix_line_endings():
	# a carriage return breaks bash ("$'\r': command not found"); .gitattributes
	# keeps them LF in checkouts, this catches an editor that didn't
	for script in (ROOT / "linux").glob("*.sh"):
		assert b"\r" not in script.read_bytes(), script.name
		assert script.read_bytes().startswith(b"#!/usr/bin/env bash\n"), script.name
