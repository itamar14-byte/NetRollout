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


def ps1_function(text, name):
	"""manage.ps1's function `name`, up to the next top-level function."""
	start = text.index(f"function {name}")
	end = text.find("\nfunction ", start + 1)
	return text[start:end if end > 0 else len(text)]


def sh_function(text, name):
	"""netrollout.sh's function `name`, up to its closing brace."""
	return re.search(rf"\n{re.escape(name)}\(\) \{{(.*?)\n\}}", text, re.S)[1]


def test_an_unfinished_windows_update_is_finished_never_skipped():
	"""manage.ps1 keeps .update-pending (ASCII key=value, naming the version) from before
	an update's second half until its restart worked: prepare-update writes it once the
	files are about to be replaced (kept data: with the backup still owed), update
	rewrites it first, makes an owed backup (then drops the flag), removes it only after
	Start-NetRollout. `start` with it pending runs the update instead of starting the new
	version without `setup upgrade` (rc1: a failed update's second half was skipped by
	Setup's "same version" and by start); status names it before needing Docker; deleting
	the data deletes it."""
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '$MarkerFile = Join-Path $Root ".update-pending"' in ps1
	pending = ps1_function(ps1, "Get-PendingUpdate")
	assert 'if ($found.Version -ne $Version) { return $null }' in pending
	write = ps1_function(ps1, "Write-UpdateMarker")
	assert "Set-Content -Encoding ASCII -Path $MarkerFile" in write
	assert '"version=$Target"' in write and 'if ($BackupFirst) { $lines += "backup-first=yes" }' in write
	prepare = ps1_function(ps1, "Invoke-PrepareUpdate")
	assert prepare.index('(Invoke-BackupCreate "before-update")') < prepare.index("Write-UpdateMarker $NewVersion")
	assert ps1_function(ps1, "Invoke-PrepareKept").count("Write-UpdateMarker $NewVersion -BackupFirst") == 2
	update = ps1_function(ps1, "Invoke-Update")
	assert "$owed = $BackupFirst -or ($pending -and $pending.BackupFirst)" in update
	marked = update.index("Write-UpdateMarker $Version -BackupFirst:$owed")
	pull = update.index('Compose @("pull"')
	backup = update.index("Backup-KeptData")
	upgrade = update.index('(Invoke-Setup @("upgrade"))')
	started = update.index("Start-NetRollout")
	removed = update.index("Remove-UpdateMarker")
	assert marked < pull < backup < upgrade < started < removed
	assert "if ($owed) {\n\t\t\tBackup-KeptData\n\t\t\tWrite-UpdateMarker $Version " in update
	start = ps1_function(ps1, "Invoke-Start")
	assert "if ($pending) {" in start and "Invoke-Update" in start
	assert '"start" { Invoke-Start; return 0 }' in ps1
	status = ps1_function(ps1, "Invoke-Status")
	assert status.index("Get-PendingUpdate") < status.index("Confirm-Docker")
	assert "DIDN'T FINISH - run: netrollout start" in status and "if ($pending -and $code -eq 0) { return 1 }" in status
	uninstall = ps1_function(ps1, "Invoke-Uninstall")
	assert "Remove-UpdateMarker" in uninstall[uninstall.index("if ($delete) {\n\t\t$gone"):]


def test_an_unfinished_linux_update_is_finished_never_skipped():
	"""netrollout.sh keeps the same .update-pending as manage.ps1: update writes it before
	the files are replaced, install over older kept data writes it with the backup owed,
	update-finish rewrites it first, makes an owed backup by the kept version (then drops
	the flag) and removes it only after do_start; start (and the menu's Start, and install
	again) finish a pending update instead of starting the new version without `setup
	upgrade`; status names it before needing Docker; deleting the data deletes it."""
	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert 'MARKER="$ROOT/.update-pending"' in sh
	assert '[ "$v" = "$VERSION" ]' in sh_function(sh, "pending_update")
	write = sh_function(sh, "write_marker")
	assert "printf 'version=%s\\nstarted=%s\\n'" in write and "backup-first=yes" in write
	update = sh_function(sh, "do_update")
	assert update.index('write_marker "$new"') < update.index('rm -rf "${ROOT:?}/$item"')
	over = sh_function(sh, "install_over_kept")
	assert over.index('if [ -n "$(pending_update)" ]; then') < over.index('if [ "$kept" = "$VERSION" ]; then')
	assert 'write_marker "$VERSION" backup-first\n\tdo_update_finish' in over
	finish = sh_function(sh, "do_update_finish")
	marked = finish.index('write_marker "$VERSION" "$owed"')
	pull = finish.index("compose pull")
	backup = finish.index("backup_create before-update")
	upgrade = finish.index("setup_core upgrade")
	started = finish.index("do_start")
	removed = finish.index('rm -f "$MARKER"')
	assert marked < pull < backup < upgrade < started < removed
	assert 'IMAGE_VERSION="$kept"' in finish
	choose = sh_function(sh, "start_or_finish")
	assert 'if [ -n "$(pending_update)" ]; then' in choose and "do_update_finish" in choose
	assert "start) need_installed; start_or_finish; open_browser ;;" in sh
	assert "3) ( COMMAND=start; need_installed; start_or_finish ) || true ;;" in sh
	status = sh_function(sh, "do_status")
	assert status.index('pending="$(pending_update)"') < status.index("need_docker")
	assert "DIDN'T FINISH - run: sudo $BIN/netrollout.sh start" in status
	uninstall = sh_function(sh, "do_uninstall")
	assert 'rm -rf "$ENV_FILE" "$ROOT/config" "$ROOT/certs" "$ROOT/logs"\n\t\trm -f "$MARKER"' in uninstall


def test_setup_and_the_manager_finish_an_unfinished_update():
	"""Setup over the same version whose update didn't finish (.update-pending naming
	it) doesn't say "nothing to update": it goes on (Resume) to a "Finish update" review
	page, skips prepare-update (its check refuses the same version) but still closes the
	install's Manager before the files, and runs `update` - the second half. The Manager
	reads the same marker: the status says the update didn't finish and Start becomes
	Finish update (`update -Yes`), in the window and the tray."""
	text = iss_text()
	pending = pascal(text, "function UpdatePending(")
	assert "'.update-pending'" in pending and "Lines[I] = 'version=' + Version" in pending
	newer = pascal(text, "function NewerThanInstalled:")
	assert "if (This = Installed) and UpdatePending(InstalledDir, InstalledVersion) then\n\t\tResume := True" in newer
	assert newer.index("Resume := True") < newer.index("is already installed in")
	prepare = pascal(text, "function PrepareToInstall(")
	assert prepare.index("if not Resume then") < prepare.index("prepare-update -Yes")
	assert "'\\bin\\NetRollout Manager.exe', '--exit'" in prepare
	page = pascal(text, "procedure CurPageChanged(")
	assert "WizardForm.NextButton.Caption := 'Finish update';" in page
	assert "if Resume then begin\n\t\tResult := 'Finish the update:'" in pascal(text, "function UpdateReadyMemo(")
	assert "if UpdateMode or KeptUpdate then begin\n\t\tArgs := 'update -Yes -NoBrowser';" in pascal(text, "function RunSetUp(")
	cs = (ROOT / "packaging" / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")
	assert 'Value(Path.Combine(Root, ".update-pending"), "version", "")' in cs
	assert "pending.Length > 0 && pending == Version ? pending : null" in cs
	assert "\" didn't finish - click Finish update\")" in cs
	assert 'Run("update -Yes", "Finishing the update to NetRollout "' in cs
	assert 'startButton = AddButton("Start", delegate { StartOrFinish(); }, false);' in cs
	assert 'startItem = menu.Items.Add("Start", null, delegate { ShowWindow(); StartOrFinish(); });' in cs
	assert 'Install.PendingUpdate != null ? "Finish update" : "Start"' in cs


def test_a_failed_update_is_told_retry_now_or_setup_again_later():
	"""After an update's files, a failure offers Retry in its message box, which says so -
	and that Cancel leaves it for later: this Setup again or the Manager's Finish update
	(both finish it, item C). The finished page comes only after Cancel, so it never says
	"click Retry" (rc1 did); it says how to finish it later, and the Manager is offered
	for an update too: its Start became Finish update. The script's own advice agrees."""
	text = iss_text()
	later = pascal(text, "function FinishLater:")
	assert "run this Setup again (the same folder) - it finishes the update" in later
	assert "click Finish update ' +\n\t\t'in NetRollout Manager." in later
	assert "Retry" not in pascal(text, "function NextStep:")
	assert pascal(text, "function NextStep:").count("'Fix it, then ' + FinishLater") == 2
	assert "KeptUpdate" not in pascal(text, "function ManagerCanStart:")
	assert "'Open NetRollout Manager (to finish the update once it''s fixed)'" in pascal(text, "procedure FinishBoxes;")
	post = pascal(text, "procedure CurStepChanged(")
	assert "'Fix it and click Retry - or Cancel, and later ' + FinishLater" in post
	assert "MB_RETRYCANCEL" in post
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	backup = ps1_function(ps1, "Backup-KeptData")
	assert backup.count("run Setup again, or netrollout start (both finish the update)") == 2
	assert "then Retry (or run Setup again)" not in ps1


def test_kept_data_locks_the_folder_unless_starting_fresh():
	"""Kept data found at Setup's start (the KeptData record or the default folder) locks
	the folder page to its folder - even over /DIR= - and says why, naming the folder and
	the version: updated (a backup first) or started as it was. Before rc2 another folder
	could be picked: a fresh install on the kept Docker volumes (they belong to the
	project, not the folder), with new passwords on the old database - broken. "Start
	fresh instead" unlocks it after one explicit confirmation ("can't be undone", the
	backups kept), then it is a new install (Settings page, `install`), never into
	another folder holding kept data; the review page says the data goes first."""
	text = iss_text()
	start = pascal(text, "procedure InitializeWizard;")
	assert "if KeptDir <> '' then begin\n\t\tWizardForm.DirEdit.Text := KeptDir;\n\t\tKeptFolderPage;" in start
	assert "{param:DIR}" not in start
	page = pascal(text, "procedure KeptFolderPage;")
	assert ("'NetRollout''s data is kept in ' + KeptDir + ' (version ' + Version + '), so NetRollout is ' +\n"
	        "\t\t'installed there - ' + What") in page
	assert "'your data is updated to {#AppVersion} (a backup is made first).'" in page
	assert "'it''s started as it was.'" in page
	assert "FreshBox.Caption := 'Start fresh instead - delete the kept data';" in page
	assert "your backups in ' +\n\t\tAddBackslash(KeptDir) + 'backups are kept)'" in page
	click = pascal(text, "procedure FreshClick(")
	assert "WizardForm.DirEdit.Enabled := FreshBox.Checked;" in click
	assert "WizardForm.DirBrowseButton.Enabled := FreshBox.Checked;" in click
	assert "if not FreshBox.Checked then WizardForm.DirEdit.Text := KeptDir;" in click
	assert "(CurPageID = wpSelectDir) and WizardForm.DirEdit.Enabled" in pascal(text, "procedure CurPageChanged(")
	nxt = pascal(text, "function NextButtonClick(")
	fresh = nxt[nxt.index("if (KeptDir <> '') and FreshBox.Checked then begin"):nxt.index("Reinstall := FileExists(")]
	assert "holds NetRollout data too" in fresh
	assert "'This can''t be undone." in fresh and "MB_YESNO or MB_DEFBUTTON2, IDNO) <> IDYES" in fresh
	assert "FreshStart := True;\n\t\t\tReinstall := False;\n\t\t\tKeptUpdate := False;" in fresh
	assert "the data kept in ' + KeptDir + ' is deleted" in pascal(text, "function UpdateReadyMemo(")


def test_starting_fresh_deletes_the_kept_data_first_never_silently():
	"""Before any file: Setup's temporary manage.ps1 forget-kept (never in a silent run;
	a failure installs nothing) deletes what the uninstaller's "delete the data" deletes
	- the kept folder has no compose files any more, so the containers, volumes and
	network go by the Docker project's label - then .env, config, certs, logs and the
	update marker, never the backups; the files only once no volume is left (the volumes
	are the data). Refused, nothing deleted, without -Yes, over an install (VERSION) or
	while any of the project's containers runs. Then the KeptData record goes."""
	text = iss_text()
	prepare = pascal(text, "function PrepareToInstall(")
	fresh = prepare[prepare.index("if FreshStart and not WizardSilent then begin"):prepare.index("if not (UpdateMode or KeptUpdate) then exit;")]
	assert "'forget-kept -Yes -InstallDir \"' + KeptDir + '\"'" in fresh
	assert "nothing was ' +\n\t\t\t\t'installed.'" in fresh
	assert fresh.index("forget-kept") < fresh.index("ForgetKept(KeptDir);")
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '"forget-kept" { Invoke-ForgetKept; return 0 }' in ps1
	forget = ps1_function(ps1, "Invoke-ForgetKept")
	assert 'if (-not $Yes -or -not $InstallDir) { Fail' in forget
	assert "if (Test-Path $VersionFile) {" in forget
	assert '$label = "label=com.docker.compose.project=$Project"' in forget
	running = forget.index('@("container", "ls", "-q", "--filter", $label)')
	removes = forget.index('foreach ($kind in "container", "volume", "network") {')
	left = forget.index('$left = Invoke-Native "docker" @("volume", "ls", "-q", "--filter", $label)')
	files = forget.index('foreach ($name in ".env", "config", "certs", "logs") {')
	assert running < removes < left < files
	assert "backups" not in forget[files:forget.index("Remove-UpdateMarker")]
	assert "Remove-UpdateMarker" in forget[files:]


def test_start_and_status_check_that_nginx_holds_its_https_port():
	"""Seen on rc1: another container held 443 and the installed nginx ran without its
	port; status blamed DNS and the firewall. Both scripts ask Docker (the nginx
	container running, 443/tcp bound on HTTPS_PORT) and tell status (--https-published);
	another container on a port is named (docker ps, this project left out). start
	refuses before `up` while something else holds the port, naming it in status's
	words, and recreates an nginx running without its binding (Docker keeps a container
	whose start failed on a taken port so, even once the port is free)."""
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	published = ps1_function(ps1, "Test-HttpsPublished")
	assert '"{{.State.Running}}|{{json .NetworkSettings.Ports}}"' in published
	assert "'443/tcp'" in published and '"$($b.HostPort)" -eq $port' in published
	others = ps1_function(ps1, "Get-OtherContainerPorts")
	assert '"{{.Names}}|{{.Labels}}|{{.Ports}}"' in others
	assert "com\\.docker\\.compose\\.project=$([regex]::Escape($Project))" in others
	assert "(another Docker container)" in others
	assert "$containers = Get-OtherContainerPorts" in ps1_function(ps1, "Get-BusyPorts")
	words = "Port $Port is in use by $Owner - NetRollout can't publish it. Stop that, then: netrollout start"
	assert words in ps1_function(ps1, "Get-PortTakenText")
	start = ps1_function(ps1, "Start-NetRollout")
	refuse = start.index("if ($owner) { Fail (Get-PortTakenText $port $owner) }")
	assert start.index("prepare-start") < refuse < start.index('Compose @("up", "-d", "--wait"')
	assert '@("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", "nginx")' in start
	assert start.index("--force-recreate") < start.index("Test-Reachable")
	assert '"--https-published", $published' in ps1_function(ps1, "Invoke-Status")
	assert '"port-check" {' in ps1 and "published = $check.Published; owner = $check.Owner" in ps1

	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert '{{if eq $p "443/tcp"}}{{range $b}}{{.HostPort}} {{end}}' in sh_function(sh, "https_published")
	assert '*",com.docker.compose.project=$PROJECT,"*) continue' in sh_function(sh, "other_container_ports")
	busy = sh_function(sh, "busy_ports")
	assert 'docker-proxy|dockerd) who="${named[$port]:-Docker (another container)}"' in busy
	assert 'for port in "${!named[@]}"; do' in busy        # no listener: userland proxy off
	assert "is in use by %s - NetRollout can't publish it. Stop that, then:" in sh_function(sh, "port_taken")
	start = sh_function(sh, "do_start")
	refuse = start.index('[ -z "$owner" ] || fail "$(port_taken "$port" "$owner")"')
	assert start.index("prepare-start") < refuse < start.index("compose up -d --wait")
	assert "compose up -d --no-deps --force-recreate --wait --wait-timeout 120 nginx" in start
	assert '--https-published "$published"' in sh_function(sh, "do_status")


def test_linux_kept_data_is_recorded_and_locks_other_folders():
	"""Before rc2 a Linux install from another folder over data an uninstall kept was a
	fresh install on the project's old Docker volumes - new passwords and key on the old
	database. Now `uninstall` keeping the data records its folder (/etc/netrollout/
	kept-data; deleting it drops the record, a start in that folder clears it), and
	`install` - before the licence, before anything is created - refuses (exit 2) while
	the record's folder still holds .env, or while the project's volumes exist (an rc1
	uninstall left no record), naming the folder and the way out."""
	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert "KEPT_RECORD=/etc/netrollout/kept-data" in sh
	uninstall = sh_function(sh, "do_uninstall")
	assert uninstall.index("clear_record") < uninstall.index("write_record")   # delete / keep branches
	assert 'if [ "$(record_value folder)" = "$ROOT" ]; then rm -f "$KEPT_RECORD"; fi' in sh_function(sh, "clear_record")
	assert "clear_record" in sh_function(sh, "do_start")
	install = sh_function(sh, "do_install")
	fresh, kept = install.index("start_fresh"), install.index("install_over_kept")
	refuse = install.index("refuse_over_kept")
	assert fresh < kept < refuse < install.index("Type yes to accept")
	assert install.index("need_docker") < refuse
	check = sh_function(sh, "refuse_over_kept")
	assert ('fail "NetRollout\'s data is kept in $folder${version:+ (version $version)}. Run the install '
	        'there to update it, or start fresh: sudo $BIN/install.sh --start-fresh (deletes the kept '
	        'data; its backups are kept)." 2') in check
	assert 'NetRollout\'s data is kept in Docker volumes from an earlier install' in check
	assert '[ -n "$(project_volumes)" ]' in check
	assert 'docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT"' in sh


def test_linux_start_fresh_deletes_the_kept_data_only_when_asked():
	"""install --start-fresh asks "can't be undone" (unless --yes; --yes alone never
	deletes - refuse_over_kept stops it), refuses while the project's containers run,
	removes its containers, volumes and network by compose's label, then - only once no
	volume is left - .env, config, certs, logs and the update marker of the kept folders,
	never backups/; the record goes."""
	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert "--start-fresh) START_FRESH=1 ;;" in sh
	fresh = sh_function(sh, "start_fresh")
	running = fresh.index('docker container ls -q --filter "$label"')
	asked = fresh.index("This can't be undone. Delete it? [y/N]")
	assert 'if [ -z "$YES" ]; then' in fresh[running:asked]
	removed = fresh.index('docker container rm -f "${ids[@]}"')
	left = fresh.index('if [ -n "$(project_volumes)" ]; then', removed)
	files = fresh.index('rm -rf "${dir:?}/.env" "$dir/config" "$dir/certs" "$dir/logs" "$dir/.update-pending"')
	assert running < asked < removed < fresh.index('docker volume rm "${ids[@]}"') < left < files
	assert "backups" not in fresh[files:fresh.index('rm -f "$KEPT_RECORD"')]


def test_the_manager_names_what_holds_the_https_port():
	"""NetRollout Manager asks manage.ps1 port-check -Json (the one lookup) through the
	same hidden runner as the rollouts list - when its state changes, else every minute
	(5 while running) - shows "Port 443 is taken by X. Close it and click Start - or
	click Change port..." whatever answers there, and notifies from the tray once per
	appearance; the build compiles it."""
	manager = ROOT / "packaging" / "windows" / "manager"
	check = (manager / "PortCheck.cs").read_text(encoding="utf-8")
	assert 'Rollouts.RunScript("port-check -Json", out output)' in check
	assert ('"Port " + port + " is taken by " + owner + ". Close it and click Start - " +\n'
	        '\t\t\t\t"or click Change port... to use another port for NetRollout."') in check
	assert "internal static int RunScript(" in (manager / "Rollouts.cs").read_text(encoding="utf-8")
	form = (manager / "NetRolloutManager.cs").read_text(encoding="utf-8")
	assert "string blocker = PortBlocker(health.Item1);" in form
	assert "health = Tuple.Create(State.Attention, PortCheck.Words(Install.Port, blocker));" in form
	assert "TimeSpan.FromMinutes(now == State.Running ? 5 : 1)" in form
	assert "if (blocker == portAnnounced) return;" in form
	assert '(Join-Path $here "PortCheck.cs")' in (manager / "build.ps1").read_text(encoding="utf-8")


def test_the_port_can_be_changed_by_hand_while_it_is_taken():
	"""netrollout port N (Windows `port -Port N`, Linux `port N` + menu 9): under the
	port helper's lock (refused, not waited for, while a System Settings change is
	applied), port-set checks N (free, named otherwise) and writes .env + site.env,
	port-setting saves System Settings' port inside the app's container (exec, else
	run next to the database), then a start on N that must answer - any failure puts
	the previous port back in all three. The Manager's Change port... is enabled only
	while the port is taken (a hint where to change it otherwise) and runs it with
	-Yes after asking."""
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	assert '[Alias("Port")][string]$NewPort = ""' in ps1 and '"port" { Invoke-PortByHand; return 0 }' in ps1
	by_hand = ps1_function(ps1, "Invoke-PortByHand")
	lock = by_hand.index('[IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None")')
	files = by_hand.index('@("port-set", "--wanted", $wanted, "--busy-ports", (Get-BusyPorts (Get-OurPorts)))')
	setting = by_hand.index("if (-not (Set-PortSetting $wanted)) {")
	start = by_hand.index("try { Start-NetRollout; $started = $true }")
	assert lock < files < setting < start
	assert by_hand.count("Restore-PreviousPort $previous") == 2
	assert "(Test-Reachable)" in by_hand[start:]
	back = ps1_function(ps1, "Restore-PreviousPort")
	assert '@("port-set", "--wanted", $Previous, "--previous")' in back and "Set-PortSetting $Previous" in back
	setting_fn = ps1_function(ps1, "Set-PortSetting")
	assert '"exec", "-T", "app", "python", "-m", "src.setup", "port-setting", "--wanted", $Port' in setting_fn
	assert '"run", "--rm", "--no-deps", "-T", "app", "python", "-m", "src.setup", "port-setting"' in setting_fn

	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	assert "port) do_port_by_hand ;;" in sh and "9  Change the HTTPS port" in sh
	by_hand = sh_function(sh, "do_port_by_hand")
	lock = by_hand.index('flock -n 9 || fail "A port change from System Settings is being applied')
	files = by_hand.index('setup_core port-set --wanted "$wanted" --busy-ports "$(busy_ports)"')
	setting = by_hand.index('if ! set_port_setting "$wanted"; then')
	start = by_hand.index("if ( do_start ); then")
	assert lock < files < setting < start
	assert by_hand.count('restore_previous_port "$previous"') == 2
	assert 'setup_core port-set --wanted "$1" --previous' in sh_function(sh, "restore_previous_port")
	assert "python -m src.setup port-setting --wanted" in sh_function(sh, "set_port_setting")

	form = (ROOT / "packaging" / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")
	assert "changePortButton.Enabled = running == null && portBlocker.Length > 0;" in form
	assert "hint.Show(PortCheck.Hint, buttons, e.X, e.Y + 22);" in form
	assert 'Run("port -Port " + port + " -Yes"' in form
	check = (ROOT / "packaging" / "windows" / "manager" / "PortCheck.cs").read_text(encoding="utf-8")
	assert 'Hint = "Change the port in System Settings -> HTTPS port"' in check


def test_the_manager_forgets_the_ports_owner_when_docker_does_not_answer():
	"""rc2 round 3 kept the last owner when port-check couldn't answer (Docker quit): the
	"taken by X" line stayed and Change port... stayed enabled. No answer now clears it -
	the status shows Docker's state, the button greys - and the cleared state re-arms the
	tray notification for the next time the port is found taken."""
	form = (ROOT / "packaging" / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")
	blocker = form[form.index("string PortBlocker(State now)"):form.index("void AnnouncePortBlocker(")]
	assert "portBlocker = found ?? \"\";" in blocker
	assert "if (found != null) portBlocker = found;" not in blocker
	assert 'lock (portLock) portBlocker = "";' in blocker          # not installed / update pending
	assert 'if (blocker.Length == 0) { portAnnounced = ""; return; }' in form


def test_the_manager_forgets_the_ports_owner_when_docker_does_not_answer():
	"""rc2 round 3 kept the last owner when port-check couldn't answer (Docker quit): the
	"taken by X" line stayed and Change port... stayed enabled. No answer now clears it -
	the status shows Docker's state, the button greys - and the cleared state re-arms the
	tray notification for the next time the port is found taken."""
	form = (ROOT / "packaging" / "windows" / "manager" / "NetRolloutManager.cs").read_text(encoding="utf-8")
	blocker = form[form.index("string PortBlocker(State now)"):form.index("void AnnouncePortBlocker(")]
	assert "portBlocker = found ?? \"\";" in blocker
	assert "if (found != null) portBlocker = found;" not in blocker
	assert 'lock (portLock) portBlocker = "";' in blocker          # not installed / update pending
	assert 'if (blocker.Length == 0) { portAnnounced = ""; return; }' in form


def test_a_failed_port_change_says_truthfully_where_it_left_netrollout():
	"""After netrollout port N failed and the previous port P was put back, both scripts
	said "back on port P" even when P was still taken. Now: back on P only when nginx
	holds it (Docker's answer), else who holds it ("stop that, then ... start"), else that
	it didn't start."""
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	back = ps1_function(ps1, "Get-PortBackText")
	assert back.index("if (Test-HttpsPublished)") < back.index("Get-PortOwner $Previous")
	assert '"NetRollout is back on port $Previous."' in back
	assert "but $Previous is taken by ${owner}: stop that, then netrollout start." in back
	by_hand = ps1_function(ps1, "Invoke-PortByHand")
	assert by_hand.count("(above) - $(Get-PortBackText $previous)\"") == 2
	assert "back on port $previous" not in by_hand

	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	back = sh_function(sh, "port_back_text")
	assert back.index("if https_published; then") < back.index('port_owner "$1" "$(busy_ports)"')
	assert "but %s is taken by %s: stop that, then sudo %s/netrollout.sh start." in back
	by_hand = sh_function(sh, "do_port_by_hand")
	assert by_hand.count('(above) - $(port_back_text "$previous")"') == 2
	assert "back on port $previous" not in by_hand


def test_another_container_publishing_a_port_range_is_named():
	"""docker ps shows a published range as "0.0.0.0:8000-8010->8000-8010/tcp"; the owner
	lookup read only single ports, so a port inside a range wasn't named. Both scripts
	now take every port of a range."""
	ports = "0.0.0.0:18440-18450->18440-18450/tcp, [::]:18440-18450->18440-18450/tcp, 0.0.0.0:8443->443/tcp"
	ps1 = (ROOT / "packaging" / "windows" / "manage.ps1").read_text(encoding="utf-8")
	others = ps1_function(ps1, "Get-OtherContainerPorts")
	pattern = re.search(r'\[regex\]::Matches\(\$parts\[2\], "(.*?)"\)', others)[1]
	assert pattern == r":(\d+)(?:-(\d+))?->"
	found = set()
	for m in re.finditer(pattern, ports):
		found.update(range(int(m[1]), int(m[2] or m[1]) + 1))
	assert found == set(range(18440, 18451)) | {8443}
	assert "foreach ($port in $first..$last) {" in others

	sh = (ROOT / "packaging" / "linux" / "netrollout.sh").read_text(encoding="utf-8")
	others = sh_function(sh, "other_container_ports")
	assert "grep -oE ':[0-9]+(-[0-9]+)?->' | sed 's/^://; s/->$//'" in others
	assert 'for port in $(seq "${range%-*}" "${range#*-}"); do' in others
	ranges = {m.lstrip(":").removesuffix("->") for m in re.findall(r":[0-9]+(?:-[0-9]+)?->", ports)}
	assert ranges == {"18440-18450", "8443"}
