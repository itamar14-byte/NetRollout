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
	# the script and dashboards come from the image; the only mount is the
	# app's connection, read only (the database data source follows a move)
	assert setup["volumes"] == ["./config:/data/config:ro"]
	assert setup["environment"]["GRAFANA_DB_PASSWORD"] == "${GRAFANA_DB_PASSWORD}"
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


def test_the_installers_identity_never_changes():
	# Windows finds the install (Settings -> Apps, updates) by this id: a new
	# one would orphan every installed NetRollout. Test builds (/DTestBuild)
	# have their own, so a test can't take over or update a real install.
	import re
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	real = re.search(r'#else\s+#define AppGuid "([0-9A-F-]+)"', text).group(1)
	test = re.search(r'#ifdef TestBuild\s+#define AppGuid "([0-9A-F-]+)"', text).group(1)
	assert real == "6C1F0E52-9B47-4E1B-A7D3-5E2C8F41B0A9" != test
	assert "AppId={{{#AppGuid}}" in text and "'{{#AppGuid}}_is1'" in text


def test_setup_over_an_install_updates_and_checks_first():
	# before any file is replaced: the direction and a backup (the copy in
	# Setup's temporary folder, pointed at the install); after: the update
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert "function PrepareToInstall(" in text
	assert "prepare-update -Yes -InstallDir" in text and r"{tmp}\manage.ps1" in text
	assert "Args := 'update -Yes -NoBrowser'" in text
	script = (ROOT / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '"prepare-update" { Invoke-PrepareUpdate; return 0 }' in script
	assert '"update" { Invoke-Update; return 0 }' in script
	assert '"check-update",' in script and '(Invoke-BackupCreate "before-update")' in script
	assert '(Invoke-Setup @("upgrade"))' in script


def test_an_update_closes_the_installs_manager_before_the_files():
	# the tray and the port helper (9.7) hold NetRollout Manager.exe: without
	# closing them a silent update aborts ("unable to automatically close all
	# applications") - every update failed so. Closed after the checks pass,
	# started again by [Run].
	import re
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	prepare = re.search(r"function PrepareToInstall\(.*?\nend;", text, re.S)[0]
	checks = prepare.index("prepare-update -Yes")
	closes = prepare.index("'\\bin\\NetRollout Manager.exe', '--exit'")
	assert checks < closes
	assert 'Parameters: "--helper"' in text and 'Parameters: "--tray"' in text


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


def test_the_repository_address_is_the_same_everywhere():
	# six places in five languages can't share one constant: if the repo
	# moves, all must follow (the footer's source link, the installer, the
	# scripts' "report it", the Manager's releases and updates, the image label)
	import re
	from src import runtime
	repo = runtime.SOURCE_REPO
	owner_repo = repo.removeprefix("https://github.com/")
	found = {
		"Dockerfile": re.search(r'image\.source="([^"]+)"', (ROOT / "Dockerfile").read_text(encoding="utf-8")).group(1),
		"netrollout.iss": re.search(r'#define Repo "([^"]+)"', (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")).group(1),
		"manage.ps1": re.search(r'\$IssuesUrl = "([^"]+)/issues"', (ROOT / "windows" / "manage.ps1").read_text(encoding="utf-8")).group(1),
		"NetRolloutManager.cs": re.search(r'Releases = "([^"]+)/releases"', (ROOT / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")).group(1),
		"Updates.cs": "https://github.com/" + re.search(r'api\.github\.com/repos/([^"]+)/releases/latest', (ROOT / "windows" / "manager" / "Updates.cs").read_text(encoding="utf-8")).group(1),
	}
	assert found == {name: repo for name in found}, owner_repo


def test_uninstalling_keeps_the_backups_unless_asked():
	# the backups are the last copy of the data: deleting the data asks about
	# them separately (kept by default), and only an explicit answer deletes them
	iss = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert "Keep the backups (the backups folder)?" in iss and "Data := Data + ' -DeleteBackups'" in iss
	ps1 = (ROOT / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '$gone = @(".env", "config", "certs", "logs")' in ps1 and 'if ($deleteBackups) { $gone += "backups" }' in ps1
	sh = (ROOT / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert 'rm -rf "$ENV_FILE" "$ROOT/config" "$ROOT/certs" "$ROOT/logs"\n' in sh
	assert 'if [ -n "$delete_backups" ]; then rm -rf "$ROOT/backups"; fi' in sh


def test_the_port_helper_runs_by_itself_on_windows():
	# saving a port in System Settings is applied without anyone at the
	# server: the headless helper starts at sign-in and with Setup, and goes
	# with the uninstaller (--exit closes it too)
	iss = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert r'Name: "{userstartup}\NetRollout port helper"; Filename: "{app}\bin\NetRollout Manager.exe"; Parameters: "--helper"' in iss
	assert 'Parameters: "--helper"; WorkingDir: "{app}"; Flags: nowait; Check: SetUpOk' in iss
	cs = (ROOT / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")
	assert 'if (args[i] == "--helper") helper = true;' in cs and 'return ExitRunning(name + ".Helper");' in cs
	ps1 = (ROOT / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '"apply" { Invoke-Apply; return 0 }' in ps1 and "Start-NetRollout; Start-PortHelper;" in ps1
	assert '"port-close", "--outcome", "rollback", "--id", $id, "--timed-out"' in ps1


def test_the_database_data_source_is_not_provisioned():
	# a provisioned data source is read-only: grafana-setup couldn't make it
	# follow a database move. The earlier versions' one is deleted by name.
	provisioning = yaml.safe_load((ROOT / "deploy/grafana/provisioning/datasources/netrollout.yml")
	                              .read_text(encoding="utf-8"))
	assert {d["type"] for d in provisioning["datasources"]} == {"prometheus", "loki"}
	assert provisioning["deleteDatasources"] == [{"name": "postgresql", "orgId": 1}]
	assert "GRAFANA_DB_PASSWORD" not in compose()["services"]["grafana"]["environment"]


def test_slow_first_starts_have_a_start_period():
	# failures inside it don't count, so `up --wait` doesn't give up on a slow
	# machine; Grafana migrates its own database on its first start
	services = compose()["services"]
	assert services["grafana"]["healthcheck"]["start_period"] == "180s"
	assert services["nginx"]["healthcheck"]["start_period"] == "60s"
	assert services["prometheus"]["healthcheck"]["start_period"] == "30s"
	assert services["postgres"]["healthcheck"]["start_period"] == "30s"


def test_a_test_build_names_everything_outside_its_folder_its_own_way():
	# a test uninstall deleted the real install's Start Menu folder, desktop
	# and Startup shortcuts and Win+R (same names); only the install record
	# was its own. Every such name now depends on the build.
	import re
	text = (ROOT / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	test, real = re.search(r"#ifdef TestBuild(.*?)#else(.*?)#endif", text, re.S).groups()
	for name in ("AppTitle", "NameSuffix", "RunName"):
		values = [re.search(rf'#define {name} "([^"]*)"', part)[1] for part in (test, real)]
		assert values[0] != values[1], name
	outside = [line for line in text.splitlines()
	           if re.search(r"\{(autoprograms|autodesktop|userstartup)\}|App Paths\\", line)
	           and not line.lstrip().startswith(";")]
	assert len(outside) >= 12
	for line in outside:
		assert re.search(r"\{#(AppTitle|NameSuffix|RunName)\}", line), line
