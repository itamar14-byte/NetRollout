"""The shipped files say what stage 9.1 decided (docs/architecture.md §9): the
hostname reaches nginx only through site.env, .env carries no seeds, the
version is the VERSION file, Grafana's setup comes from the app image."""
import re

import yaml

from src import runtime


ROOT = runtime.REPO_ROOT


def compose(name="compose.yaml"):
	"""The parsed compose file `name` from the repo root."""
	return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def env(service):
	return compose()["services"][service].get("environment") or {}


def test_nginx_takes_the_hostname_and_port_only_from_site_env():
	"""compose.yaml passes nginx neither NETROLLOUT_HOSTNAME nor NETROLLOUT_HTTPS_PORT."""
	assert "NETROLLOUT_HOSTNAME" not in env("nginx")
	assert "NETROLLOUT_HTTPS_PORT" not in env("nginx")


def test_the_app_gets_no_seeds_but_the_published_port_and_the_server_ips():
	"""The app gets no hostname or worker seed, but the published HTTPS port (default 443)
	and NETROLLOUT_SERVER_IPS from .env."""
	app = env("app")
	assert "NETROLLOUT_PUBLIC_HOSTNAME" not in app      # site.env seeds it
	assert "ORCHESTRATOR_WORKERS" not in app            # System Settings
	assert app["NETROLLOUT_HTTPS_PORT"] == "${HTTPS_PORT:-443}"
	assert app["NETROLLOUT_SERVER_IPS"] == "${NETROLLOUT_SERVER_IPS:-}"


def test_the_app_writes_backups_and_reads_grafanas_data_only():
	"""Scheduled backups (src/backup/archive.py): the app mounts backups/ and Grafana's volume
	read only, readable through group 0 (its file is 640 472:0); Grafana keeps that
	volume at /var/lib/grafana."""
	app = compose()["services"]["app"]
	assert "./backups:/data/backups" in app["volumes"]
	assert "grafana:/data/grafana:ro" in app["volumes"]
	assert app["group_add"] == ["0"]
	grafana = compose()["services"]["grafana"]["volumes"]
	assert any(v.startswith("grafana:/var/lib/grafana") for v in grafana)


def test_the_windows_restore_hands_secrets_over_safely():
	"""manage.ps1's restore runs restore-key as root (the restored key is root's), and
	resets Grafana's password from stdin fed by printf from an env var - never piped
	from PowerShell 5.1 (a byte-order mark and CR LF become part of it) nor on a
	command line."""
	script = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '(Invoke-Setup @("restore-key") -AsRoot)' in script
	assert "reset-admin-password --password-from-stdin" in script
	assert 'printf %s "$NR_GRAFANA_PASSWORD"' in script
	assert '"-e", "NR_GRAFANA_PASSWORD", "grafana"' in script


def test_port_80_is_always_80_when_switched_on():
	"""compose.http.yaml publishes nginx's port 80 as 80, nothing else."""
	assert compose("compose.http.yaml")["services"]["nginx"]["ports"] == ["80:80"]


def test_grafana_setup_runs_from_the_app_image():
	"""grafana-setup runs setup.py and the dashboards copied into the app image (the
	Dockerfile and .dockerignore let them in); its only mount is the app's connection,
	read only (the database data source follows a move), and it gets GRAFANA_DB_PASSWORD."""
	setup = compose()["services"]["grafana-setup"]
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
	"""VERSION is copied into the image (allowed by .dockerignore) and bundled into the
	CLI .exe; the Dockerfile no longer rewrites code with sed."""
	assert "COPY LICENSE VERSION ./" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
	assert "!VERSION" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
	assert '("../VERSION", ".")' in (ROOT / "packaging" / "netrollout-cli.spec").read_text(encoding="utf-8")
	# nothing rewrites the code any more
	assert "sed -i" not in (ROOT / "Dockerfile").read_text(encoding="utf-8")


# ── the Windows installer (packaging/windows/installer/netrollout.iss) ──

def iss_sources():
	"""The path of every `Source:` the installer packs, resolved against the repo or
	the installer folder."""
	installer = ROOT / "packaging" / "windows" / "installer"
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
	"""Every file the installer packs exists (but the built Manager .exe) - a rename would
	otherwise surface only when CI compiles the installer."""
	missing = [str(p) for p in iss_sources()
	           if p.name != "NetRollout Manager.exe" and not p.exists()]   # built
	assert missing == []


def test_the_installer_installs_the_version_file():
	"""The installer copies VERSION into the install folder (the scripts, `check-update`
	and the Manager read it there). Every other file it ships is SHIPPED, checked
	against packaging/build_release.py by tests/unit/packaging/test_build_release.py, which
	leaves VERSION out of its comparison; the bin\\ tools are checked below."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert re.search(r'^Source: "\{#Root\}\\VERSION"; DestDir: "\{app\}";', text, re.M)


def test_the_installed_layout_is_bin_and_the_licence_page_has_its_text():
	"""The Manager, manage.ps1, netrollout.bat and the icon install to {app}\\bin, the
	LicenseFile exists, and there is no packaging\\windows\\install.bat (Setup is the one way)."""
	installer = ROOT / "packaging" / "windows" / "installer"
	text = (installer / "netrollout.iss").read_text(encoding="utf-8")
	for name in ("NetRollout Manager.exe", "manage.ps1", "netrollout.bat", "netrollout.ico"):
		assert re.search(r'Source: "\.\.\\' + re.escape(name) + r'"; DestDir: "\{app\}\\bin"', text), name
	licence = re.search(r"^LicenseFile=(.+)$", text, re.M).group(1).strip()
	assert (installer / licence).exists()
	# one install method on Windows: the installer (no console install)
	assert not (ROOT / "packaging" / "windows" / "install.bat").exists()


def test_the_netrollout_command_on_path_is_only_the_bat():
	"""bin\\ goes on PATH (the addtopath task) holding no netrollout.* but the .bat and the
	icon - PowerShell would run a netrollout.ps1 there first and Windows' default policy
	refuses scripts; Win+R's App Paths name is netrollout.exe."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	installed = re.findall(r'^Source: "\.\.\\([^"]+)"; DestDir: "\{app\}\\bin"', text, re.M)
	assert [n for n in installed if n.lower().startswith("netrollout.")] == ["netrollout.bat", "netrollout.ico"]
	assert re.search(r"^Name: addtopath;", text, re.M)
	assert "App Paths\\netrollout.exe" in text


def test_the_licence_page_shows_the_full_licence_from_the_one_file():
	"""The licence page shows the notice, then the repo's LICENSE (packed for the page
	with dontcopy and extracted at run time, not a copy)."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert re.search(r'^Source: "\{#Root\}\\LICENSE"; Flags: dontcopy$', text, re.M)
	assert re.search(r"^\tShowFullLicence;$", text, re.M)
	assert "ExtractTemporaryFile('LICENSE')" in text


def test_the_installers_identity_never_changes():
	"""The AppGuid is the fixed one; AppId and the install record's key use it. Windows
	finds the install (Settings -> Apps, updates) by this id: a new one would orphan every
	installed NetRollout."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert re.findall(r'#define AppGuid "([0-9A-F-]+)"', text) == ["6C1F0E52-9B47-4E1B-A7D3-5E2C8F41B0A9"]
	assert "AppId={{{#AppGuid}}" in text and "'{{#AppGuid}}_is1'" in text


def test_setup_over_an_install_updates_and_checks_first():
	"""Setup over an install runs, before any file is replaced, the temporary copy's
	prepare-update (check-update + a before-update backup, pointed at the install), and
	after the files `update` (which runs `setup upgrade`)."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert "function PrepareToInstall(" in text
	assert "prepare-update -Yes -InstallDir" in text and r"{tmp}\manage.ps1" in text
	assert "Args := 'update -Yes -NoBrowser'" in text
	script = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '"prepare-update" { Invoke-PrepareUpdate; return 0 }' in script
	assert '"update" { Invoke-Update; return 0 }' in script
	assert '"check-update",' in script and '(Invoke-BackupCreate "before-update")' in script
	assert '(Invoke-Setup @("upgrade"))' in script


def test_an_update_closes_the_installs_manager_before_the_files():
	"""PrepareToInstall closes the install's Manager (`--exit`) after the prepare-update
	checks, and [Run] starts the helper and the tray again. The tray and the port helper
	(9.7) hold NetRollout Manager.exe: without closing them a silent update aborts
	("unable to automatically close all applications") - every update failed so."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	prepare = re.search(r"function PrepareToInstall\(.*?\nend;", text, re.S)[0]
	checks = prepare.index("prepare-update -Yes")
	closes = prepare.index("'\\bin\\NetRollout Manager.exe', '--exit'")
	assert checks < closes
	assert 'Parameters: "--helper"' in text and 'Parameters: "--tray"' in text


def test_the_installer_ends_honestly():
	"""On success the portal opens on Finish (ticked, only when set up); set up but not
	started, NetRollout Manager is offered, ticked (its Start is the next step); on failure Retry
	is offered unless the script says retrying can't help (exit 3: the release's image
	isn't published)."""
	text = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	# the finished page's own check boxes (rc2: they replace [Run]'s postinstall entries,
	# which Inno 6.7 draws half-themed in dark mode): the browser box only when set up,
	# ticked; Finish opens what's ticked, never in a silent run
	assert "postinstall" not in text
	boxes = pascal(text, "procedure FinishBoxes;")
	assert "BrowserBox.Visible := not SetUpFailed;" in boxes and "BrowserBox.Checked := True;" in boxes
	assert "ManagerBox.Visible := (not SetUpFailed) or ManagerCanStart;" in boxes
	done = pascal(text, "procedure CurStepChanged(")
	assert "if (CurStep = ssDone) and not WizardSilent then begin" in done
	assert "if BrowserBox.Visible and BrowserBox.Checked then\n\t\t\tShellExec('open', Address('')" in done
	assert "MB_RETRYCANCEL" in text and "(SetUpCode = 3)" in text
	script = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert re.search(r"isn't published on Docker Hub.*\) 3$", script, re.M | re.S)


def test_the_linux_scripts_have_unix_line_endings():
	"""Every packaging/linux/*.sh has no carriage return and starts with the bash shebang. A CR
	breaks bash ("$'\\r': command not found"); .gitattributes keeps them LF in checkouts,
	this catches an editor that didn't."""
	for script in (ROOT / "packaging" / "linux").glob("*.sh"):
		assert b"\r" not in script.read_bytes(), script.name
		assert script.read_bytes().startswith(b"#!/usr/bin/env bash\n"), script.name


def test_the_repository_address_is_the_same_everywhere():
	"""The Dockerfile's label, the installer, manage.ps1's issues link and the Manager's
	releases and updates all name runtime.SOURCE_REPO. Six places in five languages can't
	share one constant: if the repo moves, all must follow."""
	repo = runtime.SOURCE_REPO
	owner_repo = repo.removeprefix("https://github.com/")
	found = {
		"Dockerfile": re.search(r'image\.source="([^"]+)"', (ROOT / "Dockerfile").read_text(encoding="utf-8")).group(1),
		"netrollout.iss": re.search(r'#define Repo "([^"]+)"', (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")).group(1),
		"manage.ps1": re.search(r'\$IssuesUrl = "([^"]+)/issues"', (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")).group(1),
		"NetRolloutManager.cs": re.search(r'Releases = "([^"]+)/releases"', (ROOT / "packaging" / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")).group(1),
		"Updates.cs": "https://github.com/" + re.search(r'api\.github\.com/repos/([^"]+)/releases/latest', (ROOT / "packaging" / "windows" / "manager" / "Updates.cs").read_text(encoding="utf-8")).group(1),
	}
	assert found == {name: repo for name in found}, owner_repo


def test_uninstalling_keeps_the_backups_unless_asked():
	"""Deleting the data on uninstall (Setup's uninstaller, manage.ps1, netrollout.sh)
	leaves backups/ unless deleting them was asked for separately - they are the last
	copy of the data."""
	iss = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert "Keep the backups (the backups folder)?" in iss and "Data := Data + ' -DeleteBackups'" in iss
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '$gone = @(".env", "config", "certs", "logs")' in ps1 and 'if ($deleteBackups) { $gone += "backups" }' in ps1
	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert 'rm -rf "$ENV_FILE" "$ROOT/config" "$ROOT/certs" "$ROOT/logs"\n' in sh
	assert 'if [ -n "$delete_backups" ]; then rm -rf "$ROOT/backups"; fi' in sh


def test_the_port_helper_runs_by_itself_on_windows():
	"""A port saved in System Settings is applied without anyone at the server: the
	headless `--helper` starts at sign-in (Startup entry), at Setup's end and with
	`start`, has its own single-instance lock, and rolls back a timed-out trial
	(the uninstaller's --exit closes it too)."""
	iss = (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")
	assert r'Name: "{userstartup}\NetRollout port helper"; Filename: "{app}\bin\NetRollout Manager.exe"; Parameters: "--helper"' in iss
	assert 'Parameters: "--helper"; WorkingDir: "{app}"; Flags: nowait; Check: SetUpOk' in iss
	cs = (ROOT / "packaging" / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")
	assert 'if (args[i] == "--helper") helper = true;' in cs and 'return ExitRunning(name + ".Helper");' in cs
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '"apply" { Invoke-Apply; return 0 }' in ps1 and "Start-NetRollout; Start-PortHelper;" in ps1
	assert '"port-close", "--outcome", "rollback", "--id", $id, "--timed-out"' in ps1


def test_the_database_data_source_is_not_provisioned():
	"""Only Prometheus and Loki are provisioned, the earlier versions' `postgresql` is
	deleted by name, and Grafana gets no GRAFANA_DB_PASSWORD. A provisioned data source
	is read-only: grafana-setup couldn't make it follow a database move."""
	provisioning = yaml.safe_load((ROOT / "deploy/grafana/provisioning/datasources/netrollout.yml")
	                              .read_text(encoding="utf-8"))
	assert {d["type"] for d in provisioning["datasources"]} == {"prometheus", "loki"}
	assert provisioning["deleteDatasources"] == [{"name": "postgresql", "orgId": 1}]
	assert "GRAFANA_DB_PASSWORD" not in compose()["services"]["grafana"]["environment"]


def test_slow_first_starts_have_a_start_period():
	"""Grafana (180 s), nginx (60 s), Prometheus and Postgres (30 s) have a health-check
	start period: failures inside it don't count, so `up --wait` doesn't give up on a
	slow machine; Grafana migrates its own database on its first start."""
	services = compose()["services"]
	assert services["grafana"]["healthcheck"]["start_period"] == "180s"
	assert services["nginx"]["healthcheck"]["start_period"] == "60s"
	assert services["prometheus"]["healthcheck"]["start_period"] == "30s"
	assert services["postgres"]["healthcheck"]["start_period"] == "30s"


def test_installing_again_over_a_kept_linux_install_starts_it():
	"""do_install, before asking anything, starts an install whose .env was kept and
	returns, never saying "already installed" - as `uninstall --keep-data` promises
	("installing again in this folder picks it up"). install.sh used to refuse
	(exit 2) - found by 9.9e; it now starts as Setup does on Windows. The same
	version starts at once; an older one goes through the update (install_over_kept)."""
	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	install = re.search(r"\ndo_install\(\) \{(.*?)\n\}", sh, re.S)[1]
	kept = install[:install.index("if [ -z \"$YES\" ]")]
	assert 'if [ -f "$ENV_FILE" ]; then' in kept and "install_over_kept" in kept and "return" in kept
	over = re.search(r"\ninstall_over_kept\(\) \{(.*?)\n\}", sh, re.S)[1]
	same = over[:over.index('step "Getting NetRollout $VERSION"')]
	assert 'if [ "$kept" = "$VERSION" ]; then' in same and "do_start" in same and "return" in same
	assert "do_update_finish" in over
	assert "already installed" not in kept + over
	assert "installing again in this folder picks it up" in sh


def test_the_windows_install_folder_is_the_installing_accounts_only():
	"""The whole install folder (the TLS key, the scripts NetRollout runs, compose,
	runtime.env after a database move) is restricted to Administrators, SYSTEM and the
	installing account - at install and at every start, so an older install heals;
	not only .env and backups (other local accounts could read the key and change the
	scripts)."""
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	start = ps1[ps1.index("function Start-NetRollout {"):ps1.index("function Restrict(")]
	install = ps1[ps1.index("function Invoke-Install {"):]
	install = install[:install.index("\nfunction ", 1)]
	assert "Restrict $Root" in start
	assert "Restrict $Root" in install and "Restrict $EnvFile" in install
	# Restrict: inheritance off, then Administrators, SYSTEM and the installing account
	assert re.search(r'"/inheritance:r", "/grant:r", "\*S-1-5-32-544:\$f",\s*"\*S-1-5-18:\$f", '
	                 r'"\$\{env:USERDOMAIN\}\\\$\{env:USERNAME\}:\$f"', ps1)


def test_the_windows_script_finds_docker_whoever_started_it():
	"""manage.ps1 refreshes PATH from the registry (Machine + User, the session's own
	entries kept) before any command runs, and Wait-Docker uses the same helper; the
	Docker CLI check falls back to Docker Desktop's resources\\bin (then put on PATH).
	Setup launched from a browser that was open before Docker was installed inherited
	the browser's PATH, so the Docker page said "isn't installed" over a running Docker
	(rc1) - and Next would have installed it again."""
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	refresh = ps1[ps1.index("function Update-SessionPath {"):ps1.index("function Test-DockerCli {")]
	assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in refresh
	assert '[Environment]::GetEnvironmentVariable("Path", "User"), $env:Path' in refresh
	main = ps1[ps1.index("# ── main ──"):]
	assert main.index("Update-SessionPath") < main.index("Invoke-NrCommand $Command")
	wait = ps1[ps1.index("function Wait-Docker("):ps1.index("function Install-DockerDesktop")]
	assert "Update-SessionPath" in wait and "GetEnvironmentVariable" not in wait
	cli = ps1[ps1.index("function Test-DockerCli {"):ps1.index("function Test-DockerRunning")]
	assert 'Join-Path $DockerCliDir "docker.exe"' in cli and '$env:Path = "$env:Path;$DockerCliDir"' in cli
	assert r'$DockerCliDir = Join-Path $DockerDir "resources\bin"' in ps1


def iss_text():
	return (ROOT / "packaging" / "windows" / "installer" / "netrollout.iss").read_text(encoding="utf-8")


def pascal(text, header):
	"""The [Code] routine starting with `header`, up to its closing `end;`."""
	return re.search(re.escape(header) + r".*?\nend;", text, re.S)[0]


def test_kept_data_is_recorded_by_the_uninstaller_and_found_by_setup():
	"""The uninstaller records the folder whose data it kept (.env still there after
	manage.ps1 uninstall - a silent uninstall keeps it too) in HKCU\\Software\\NetRollout
	KeptData and drops the record when the data was deleted; Setup's start reads it
	(the folder must still hold .env), else the default folder, announces it and
	pre-selects it; an install into that folder drops the record (the install record
	takes over). The Manager's subkey under the same key is never deleted."""
	text = iss_text()
	assert r'#define OurKey "Software\NetRollout"' in text
	uninstall = pascal(text, "procedure CurUninstallStepChanged(")
	run = uninstall.index("'uninstall -Yes ' + Data")
	record = uninstall.index("RegWriteStringValue(HKCU, '{#OurKey}', 'KeptData', ExpandConstant('{app}'))")
	assert run < record
	assert r"if FileExists(ExpandConstant('{app}\.env')) then" in uninstall[run:record]
	assert "ForgetKept(ExpandConstant('{app}'))" in uninstall[record:]
	forget = pascal(text, "procedure ForgetKept(")
	assert "RegDeleteValue(HKCU, '{#OurKey}', 'KeptData')" in forget
	assert "RegDeleteKey" not in text
	find = pascal(text, "function FindKept:")
	assert "RegQueryStringValue(HKCU, '{#OurKey}', 'KeptData', Dir)" in find
	assert find.index("'KeptData'") < find.index("'{#DefaultDir}'")
	assert "FileExists(AddBackslash(Dir) + '.env')" in find
	start = pascal(text, "function InitializeSetup:")
	assert "KeptDir := FindKept;" in start and "CheckKept(KeptDir)" in start
	assert start.index("if UpdateMode then Result := NewerThanInstalled") < start.index("FindKept")
	assert "WizardForm.DirEdit.Text := KeptDir" in pascal(text, "procedure InitializeWizard;")
	post = pascal(text, "procedure CurStepChanged(")
	assert "ForgetKept(ExpandConstant('{app}'));" in post


def test_kept_data_is_announced_in_setups_own_words_naming_the_folder():
	"""Inno's generic "folder exists" warning is off; CheckKept announces the kept data
	with the folder it is in - updated (a backup first), started as it was, or refused
	(newer) - once per folder, at the start and on the folder page. rc1 said nothing
	(it only announced a downgrade), so the developer got Inno's warning instead."""
	text = iss_text()
	assert re.search(r"^DirExistsWarning=no$", text, re.M)
	check = pascal(text, "function CheckKept(")
	assert "Held := Dir + ' holds NetRollout ' + Kept + ' data kept by an uninstall'" in check
	assert "'. It will be updated to {#AppVersion} - a backup is made first.'" in check
	assert "'. It will be started as it was.'" in check
	assert "Downgrades aren''t supported." in check
	assert check.count("if CompareText(Dir, AnnouncedDir) <> 0 then") == 2
	assert "CheckKept(RemoveBackslashUnlessRoot(WizardDirValue))" in pascal(text, "function NextButtonClick(")


def test_no_setup_message_assumes_the_default_folder():
	"""C:\\NetRollout appears only in the DefaultDir define: every message names the
	folder actually involved (kept data can be anywhere)."""
	text = iss_text()
	assert [line for line in text.splitlines() if r"C:\NetRollout" in line] == [r'#define DefaultDir "C:\NetRollout"']
	code = text[text.index("[Code]"):]
	for message in re.findall(r"SuppressibleMsgBox\((.*?)\);", code, re.S):
		assert "{#DefaultDir}" not in message


def test_setup_shows_the_scripts_current_step_live():
	"""Install, start and update run manage.ps1 through RunScript: Exec still waits for
	the exit code (Retry, exit 3 and the log tail unchanged), while a timer (killed in a
	finally) shows the newest step line - manage.ps1's Step writes "-> " - of this run
	under the progress bar; the update appends to the preparation's log, so only the
	lines after it count; the install starts a new log."""
	text = iss_text()
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert 'function Step([string]$Text) { Write-Host "-> $Text"' in ps1
	show = pascal(text, "procedure ShowStep(")
	assert "LoadStringsFromLockedFile(WatchLog, Lines)" in show
	assert "downto WatchFrom do" in show and "if Pos('-> ', Lines[I]) = 1 then" in show
	assert "WizardForm.StatusLabel.Caption :=" in show
	run = pascal(text, "function RunScript(")
	start = run.index("Timer := SetTimer(0, 0, 500, CreateCallback(@ShowStep));")
	waits = run.index("SW_HIDE, ewWaitUntilTerminated, Result);")
	stop = run.index("finally\n\t\tKillTimer(0, Timer);")
	assert start < waits < stop
	assert "WatchFrom := GetArrayLength(Lines)" in run and "DeleteFile(Log);" in run
	setup = pascal(text, "function RunSetUp(")
	assert setup.count("RunScript(") == 2 and "ewWaitUntilTerminated" not in setup
	assert "Result := RunScript(Args, Log, True);" in setup and "Result := RunScript(Args, Log, False);" in setup


def test_every_finished_page_has_its_own_heading():
	"""A fresh install's finished page says "NetRollout is installed" (not Inno's
	"Completing the NetRollout Setup Wizard"), next to the update's and the
	not-running one's; kept data started as it was says so instead of admin / admin."""
	page = pascal(iss_text(), "procedure CurPageChanged(")
	for heading in ("NetRollout is installed, but not running", "NetRollout is updated", "NetRollout is installed"):
		assert f"WizardForm.FinishedHeadingLabel.Caption := '{heading}';" in page
	assert "is running again with the data kept in" in page
