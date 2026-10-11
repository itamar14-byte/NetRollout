<#
NetRollout's engine on Windows: NetRollout Setup, its uninstaller and
NetRollout Manager run it; admins can too (bin\netrollout.bat):

  netrollout start | stop | status | open | logs [service] | backup |
             restore <file> | apply | rollouts [-Json] | stop-now |
             reset-admin [-NewPassword] | help

Installing is NetRollout Setup's (it runs `install -Yes` with the answers of
its pages). The install folder is this script's parent folder. The script does
what needs this computer (checks, Docker) and leaves the thinking to the setup
core inside the app image (python -m src.setup, docs/architecture.md §9).
PowerShell 5.1 compatible.
#>
param(
	[Parameter(Position = 0)][string]$Command = "",
	[Parameter(Position = 1)][string]$Service = "",
	[switch]$NoBrowser,
	[switch]$Yes,          # run by NetRollout Setup: its pages asked everything
	# install's answers (the Setup wizard's pages)
	[string]$Hostname = "",
	[string]$HttpsPort = "",
	[string]$Monitoring = "",       # y / n
	[string]$OrgCertificate = "",   # y / n: fullchain.pem + privkey.pem already in certs\
	[string]$TimeZone = "",
	[string]$Out = "",              # defaults: the file to write them to
	[switch]$DeleteData,            # uninstall: delete the data too (no question)
	[switch]$KeepData,              # uninstall: keep it (no question); clear-damaged: .env, config, certs, logs stay
	[switch]$DeleteBackups,         # uninstall with -DeleteData: the backups too (else kept)
	[switch]$NoSafetyBackup,        # restore: without backing up the current state
	[switch]$NewPassword,           # reset-admin: also a temporary password, shown once
	[switch]$Json,                  # rollouts: one line of JSON (NetRollout Manager)
	# NetRollout Setup, updating: the installed folder (this copy runs from
	# Setup's temporary folder) and the version it brings
	[string]$InstallDir = "",
	[string]$NewVersion = "",
	# update over data an uninstall kept: back it up first (prepare-update
	# couldn't - the folder had no compose file yet)
	[switch]$BackupFirst,
	# forget-kept with no folder (it was deleted by hand): the project's
	# Docker volumes, containers and network only - no file is touched
	[switch]$VolumesOnly,
	# port: the new HTTPS port (netrollout port 8443, or -Port 8443)
	[Alias("Port")][string]$NewPort = ""
)

Set-StrictMode -Version 2
$ErrorActionPreference = "Stop"

$Root = if ($InstallDir) { $InstallDir } else { Split-Path -Parent $PSScriptRoot }
# (the Setup wizard runs some commands from a temporary copy, before
# NetRollout's files are in place: no VERSION there)
$VersionFile = Join-Path $Root "VERSION"
$Version = if (Test-Path $VersionFile) { (Get-Content -Raw $VersionFile).Trim() } else { "" }
# A different project name only for testing next to a running NetRollout
$Project = "netrollout"
$AppImage = "itamarweinstein/netrollout:$Version"
# compose's images of another version (a kept install's backup); "" = $Version
$ImageVersion = ""
$EnvFile = Join-Path $Root ".env"
# An update's second half (update) hasn't finished while this is here (and
# names this folder's VERSION): Setup, the Manager and start finish it
$MarkerFile = Join-Path $Root ".update-pending"
$Interactive = [Environment]::UserInteractive -and -not $Yes
# Docker Desktop's own folder: 64-bit Program Files even from a 32-bit caller
$DockerDir = Join-Path $(if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles }) "Docker\Docker"
$DockerDesktopExe = Join-Path $DockerDir "Docker Desktop.exe"
$DockerCliDir = Join-Path $DockerDir "resources\bin"
$IssuesUrl = "https://github.com/itamar14-byte/NetRollout/issues"

# ── output ──────────────────────────────────────────────────────────────────

function Say([string]$Text) { Write-Host $Text }
function Step([string]$Text) { Write-Host "-> $Text" -ForegroundColor Cyan }
function Good([string]$Text) { Write-Host "   $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }
# Exit codes: 1 failed (fix the cause, try again), 2 refused (already
# installed), 3 this release can't be set up anywhere (retrying won't help)
function Fail([string]$Text, [int]$Code = 1) {
	Write-Host ""
	Write-Host $Text -ForegroundColor Red
	throw [System.OperationCanceledException]::new("$Code")
}

# Native commands write progress to stderr; under "Stop" PowerShell 5.1 would
# turn that into an error. Returns @{Code; Output}.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
	$saved = $ErrorActionPreference
	$ErrorActionPreference = "Continue"
	try {
		$output = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
		return @{ Code = $LASTEXITCODE; Output = ($output -join "`n") }
	} finally {
		$ErrorActionPreference = $saved
	}
}

# From the install folder: compose reads .env's COMPOSE_FILE relative to the
# folder it runs in, not to --project-directory
function Compose([string[]]$Arguments) {
	$env:NETROLLOUT_VERSION = if ($script:ImageVersion) { $script:ImageVersion } else { $Version }
	Push-Location $Root
	try {
		return Invoke-Native "docker" (@("compose", "-p", $Project, "--project-directory", $Root,
			"--env-file", $EnvFile) + $Arguments)
	} finally {
		Pop-Location
	}
}

# The setup core in the app image, the install folder mounted. -OnNetwork:
# next to the running app (status asks its health over the compose network,
# when there is one). -AsRoot: for a file only root may read (the restored key).
function Invoke-Setup([string[]]$Arguments, [switch]$OnNetwork, [switch]$AsRoot) {
	$r = Invoke-Native "docker" (Get-SetupCommand $Arguments -OnNetwork:$OnNetwork -AsRoot:$AsRoot)
	if ($r.Output) { Write-Host $r.Output }
	return $r.Code
}

# The setup core's answer as text, not shown (the port helper acts on it)
function Get-SetupAnswer([string[]]$Arguments) {
	return Invoke-Native "docker" (Get-SetupCommand $Arguments)
}

# docker's arguments for the setup core (Invoke-Setup's switches)
function Get-SetupCommand([string[]]$Arguments, [switch]$OnNetwork, [switch]$AsRoot) {
	$run = @("run", "--rm", "-v", "${Root}:/install", "-e", "NETROLLOUT_HOME=/install")
	if ($AsRoot) { $run += @("--user", "0") }
	if ($OnNetwork -and (Invoke-Native "docker" @("network", "inspect", "${Project}_default")).Code -eq 0) {
		$run += @("--network", "${Project}_default")
	}
	# an option with an empty value as one "--option=" argument: Windows
	# PowerShell 5.1 drops an empty string passed to a program, and the next
	# argument then became the option's value (status with no containers:
	# "--containers: expected one argument")
	$joined = @()
	for ($i = 0; $i -lt $Arguments.Count; $i++) {
		if ($Arguments[$i] -like "--*" -and $i + 1 -lt $Arguments.Count -and $Arguments[$i + 1] -eq "") {
			$joined += "$($Arguments[$i])="
			$i++
		} else { $joined += $Arguments[$i] }
	}
	return $run + @($AppImage, "python", "-m", "src.setup") + $joined
}

# One of our images, pulled when this computer doesn't have it; a release
# whose image isn't on Docker Hub is incomplete - exit 3, "report it"
function Get-OurImage([string]$Image) {
	if ((Invoke-Native "docker" @("image", "inspect", $Image)).Code -eq 0) { return }
	$r = Invoke-Native "docker" @("pull", $Image)
	if ($r.Code -ne 0) {
		Write-Host $r.Output
		if ($r.Output -match "not found|manifest unknown") {
			Fail ("NetRollout $Version isn't published on Docker Hub ($Image doesn't exist there). " +
			      "This release is incomplete - not a problem with this computer. Please report it: $IssuesUrl") 3
		}
		Fail "Couldn't download $Image - check this computer's internet connection (and proxy, if any)."
	}
}

# An image on this computer, pulled when it isn't: $true when it's there now
function Test-Image([string]$Image) {
	if ((Invoke-Native "docker" @("image", "inspect", $Image)).Code -eq 0) { return $true }
	return (Invoke-Native "docker" @("pull", $Image)).Code -eq 0
}

# ── what this computer knows ──────────────────────────────────────────────────

function Get-BusyPorts([int[]]$Ours = @()) {
	# port=who, for the setup core; NetRollout's own published ports left out
	$names = @{}
	foreach ($c in Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue) {
		$port = [int]$c.LocalPort
		if ($Ours -contains $port -or $names.ContainsKey($port)) { continue }
		$who = ""
		if ($c.OwningProcess -eq 4) {
			$who = "Windows' HTTP service (IIS, or another program using it)"
		} else {
			$p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
			if ($p) {
				$who = switch -Wildcard ($p.ProcessName) {
					"com.docker*" { "Docker (another container)" }
					"wslrelay"    { "Docker (another container)" }
					"vpnkit*"     { "Docker (another container)" }
					default       { $p.ProcessName }
				}
			}
		}
		$names[$port] = ($who -replace "[,=]", " ")
	}
	# Docker's: the container publishing it, by name (asked only then -
	# before Docker is installed there is no docker command)
	if ($names.Values -contains "Docker (another container)") {
		$containers = Get-OtherContainerPorts
		foreach ($port in @($names.Keys)) {
			if ($names[$port] -eq "Docker (another container)" -and $containers.ContainsKey($port)) {
				$names[$port] = $containers[$port]
			}
		}
	}
	return ($names.Keys | Sort-Object | ForEach-Object { "$_=$($names[$_])" }) -join ","
}

# The host ports other containers publish (not this NetRollout's):
# port -> "<name> (another Docker container)" - every port of a published
# range too ("0.0.0.0:8000-8010->8000-8010/tcp")
function Get-OtherContainerPorts {
	$found = @{}
	$r = Invoke-Native "docker" @("ps", "--format", "{{.Names}}|{{.Labels}}|{{.Ports}}")
	if ($r.Code -ne 0) { return $found }
	$ours = "(^|,)com\.docker\.compose\.project=$([regex]::Escape($Project))(,|$)"
	foreach ($line in ($r.Output -split "`n")) {
		$parts = $line.Trim() -split "\|", 3
		if ($parts.Count -lt 3 -or $parts[1] -match $ours) { continue }
		foreach ($m in [regex]::Matches($parts[2], ":(\d+)(?:-(\d+))?->")) {
			$first = [int]$m.Groups[1].Value
			$last = if ($m.Groups[2].Success) { [int]$m.Groups[2].Value } else { $first }
			foreach ($port in $first..$last) {
				if (-not $found.ContainsKey($port)) {
					$found[$port] = "$($parts[0] -replace '[,=]', ' ') (another Docker container)"
				}
			}
		}
	}
	return $found
}

# The project's Docker volumes (the data): by compose's project label, and
# by name - compose names them <project>_<volume>, and one made or copied
# outside compose has only the name. Each once; an empty list when there are
# none, $null when Docker didn't answer. The name must start with the
# project and "_" exactly: netrollout-dev_* (the dev stack's) never matches.
function Get-ProjectVolumes {
	$labelled = Invoke-Native "docker" @("volume", "ls", "-q", "--filter", "label=com.docker.compose.project=$Project")
	$all = Invoke-Native "docker" @("volume", "ls", "-q")
	if ($labelled.Code -ne 0 -or $all.Code -ne 0) { return $null }
	$prefix = "^$([regex]::Escape($Project))_"
	$names = New-Object System.Collections.Generic.List[string]
	foreach ($name in @($labelled.Output -split "`n") + @(($all.Output -split "`n") | Where-Object { $_.Trim() -cmatch $prefix })) {
		$name = $name.Trim()
		if ($name -and -not $names.Contains($name)) { $names.Add($name) }
	}
	return ,$names.ToArray()
}

# Whether nginx holds the HTTPS port in use - Docker's own answer: its
# container running with 443/tcp published on HTTPS_PORT. A taken port
# leaves it Created (Docker refused to start it), or - seen once on Docker
# Desktop - running without the binding
function Test-HttpsPublished {
	$port = Read-EnvValue "HTTPS_PORT" "443"
	$ids = @((Compose @("ps", "-a", "-q", "nginx")).Output -split "`n" |
		ForEach-Object { $_.Trim() } | Where-Object { $_ -match "^[0-9a-f]{12,64}$" })
	if (-not $ids) { return $false }
	$r = Invoke-Native "docker" @("inspect", "--format", "{{.State.Running}}|{{json .NetworkSettings.Ports}}", $ids[0])
	if ($r.Code -ne 0) { return $false }
	$running, $json = ($r.Output.Trim() -split "\|", 2)
	if ($running -ne "true" -or -not $json) { return $false }
	try { $bound = ($json | ConvertFrom-Json).'443/tcp' } catch { return $false }
	foreach ($b in @($bound)) {
		if ($b -and "$($b.HostPort)" -eq $port) { return $true }
	}
	return $false
}

# nginx and its HTTPS port: @{Port; Published; Owner} - Owner: who holds it
# instead ("" when nothing else does)
function Get-PortCheck {
	$port = Read-EnvValue "HTTPS_PORT" "443"
	$published = Test-HttpsPublished
	$owner = ""
	if (-not $published) { $owner = Get-PortOwner $port (Get-BusyPorts (Get-OurPorts)) }
	return @{ Port = $port; Published = $published; Owner = $owner }
}

# Who holds $Port in Get-BusyPorts' list ("" when nobody does)
function Get-PortOwner([string]$Port, [string]$Busy) {
	foreach ($item in ($Busy -split ",")) {
		if ($item -match "^$Port=(.*)$") {
			if ($Matches[1].Trim()) { return $Matches[1].Trim() }
			return "another program"
		}
	}
	return ""
}

# The words for a taken HTTPS port (status says the same: src/setup/manage.py)
function Get-PortTakenText([string]$Port, [string]$Owner) {
	return "Port $Port is in use by $Owner - NetRollout can't publish it. Stop that, then: netrollout start - or move NetRollout to another port: netrollout port <N>"
}

function Get-ServerIPs {
	$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
		Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and
		               $_.InterfaceAlias -notlike "vEthernet*" -and $_.InterfaceAlias -notlike "*Loopback*" } |
		ForEach-Object { $_.IPAddress }
	return ($ips | Select-Object -Unique) -join ","
}

function Get-Facts {
	return @("--os", "windows", "--computer-name", $env:COMPUTERNAME,
		"--host-timezone", (Get-TimeZone).Id, "--server-ips", (Get-ServerIPs),
		"--account", "$env:USERDOMAIN\$env:USERNAME")
}

function Read-EnvValue([string]$Key, [string]$Default = "") {
	foreach ($line in Get-Content $EnvFile) {
		if ($line -match "^$Key=(.*)$") { return $Matches[1].Trim() }
	}
	return $Default
}

# The version .env was last set up by: its "# Updated to NetRollout X on ..."
# line (setup upgrade), else the installer's "# NetRollout X - written by the
# installer" header; "" when it has neither (an early install)
function Get-KeptVersion {
	$lines = @(Get-Content $EnvFile)
	foreach ($pattern in "^# Updated to NetRollout (\S+) ", "^# NetRollout (\S+) .*written by the installer") {
		$found = ""
		foreach ($line in $lines) { if ($line -match $pattern) { $found = $Matches[1] } }
		if ($found) { return $found }
	}
	return ""
}

# The update this folder's files are for, unfinished: @{Version; BackupFirst}
# - the marker naming the VERSION in place ($null: none, or a stale one: an
# update cancelled before its files were replaced names a version not here)
function Get-PendingUpdate {
	if (-not $Version -or -not (Test-Path $MarkerFile)) { return $null }
	$found = @{ Version = ""; BackupFirst = $false }
	foreach ($line in Get-Content $MarkerFile) {
		if ($line -match "^version=(.+)$") { $found.Version = $Matches[1].Trim() }
		if ($line -match "^backup-first=yes") { $found.BackupFirst = $true }
	}
	if ($found.Version -ne $Version) { return $null }
	return $found
}

# The marker, written before an update's second half can start: the version
# it brings, and whether the kept data's backup is still owed (-BackupFirst).
# ASCII key=value: Setup and the Manager read it (no BOM before the first key)
function Write-UpdateMarker([string]$Target, [switch]$BackupFirst) {
	$lines = @("# NetRollout's update to $Target hasn't finished while this file is here:",
	           "# netrollout start (or Setup, or NetRollout Manager's Finish update) finishes it",
	           "version=$Target", "started=$((Get-Date).ToString('yyyy-MM-dd HH:mm'))")
	if ($BackupFirst) { $lines += "backup-first=yes" }
	Set-Content -Encoding ASCII -Path $MarkerFile -Value $lines
}

function Remove-UpdateMarker { Remove-Item -Force -ErrorAction SilentlyContinue $MarkerFile }

function Get-Address {
	$hostName = "localhost"
	$site = Join-Path $Root "config\nginx\site.env"
	if (Test-Path $site) {
		foreach ($line in Get-Content $site) {
			if ($line -match "^NETROLLOUT_HOSTNAME=(.+)$") { $hostName = $Matches[1].Trim() }
		}
	}
	$port = Read-EnvValue "HTTPS_PORT" "443"
	if ($port -eq "443") { return "https://$hostName" }
	return "https://${hostName}:$port"
}

# Our running containers' published ports (so they don't count as "busy")
function Get-OurPorts {
	$r = Compose @("ps", "--format", "json")
	$ports = @()
	foreach ($line in ($r.Output -split "`n")) {
		$line = $line.Trim()
		if (-not $line.StartsWith("{") -and -not $line.StartsWith("[")) { continue }
		foreach ($c in @($line | ConvertFrom-Json)) {
			foreach ($pub in @($c.Publishers)) {
				if ($pub -and $pub.PublishedPort) { $ports += [int]$pub.PublishedPort }
			}
		}
	}
	return @($ports | Select-Object -Unique)
}

function Get-ContainerStates {
	$r = Compose @("ps", "-a", "--format", "{{.Service}}={{.State}}/{{.Health}}")
	$items = $r.Output -split "`n" | Where-Object { $_ -match "=" } |
		ForEach-Object { $_.Trim().TrimEnd("/") }
	return ($items -join ",")
}

# ── Docker ────────────────────────────────────────────────────────────────────

# PATH as Windows has it now (Machine + User), then whatever this session
# added: a process started by a program that was running before Docker was
# installed (NetRollout Setup launched from a browser) inherits that
# program's old PATH, without docker on it
function Update-SessionPath {
	$entries = @()
	foreach ($path in [Environment]::GetEnvironmentVariable("Path", "Machine"),
	                  [Environment]::GetEnvironmentVariable("Path", "User"), $env:Path) {
		foreach ($entry in "$path" -split ";") {
			if ($entry.Trim() -and $entries -notcontains $entry) { $entries += $entry }
		}
	}
	$env:Path = $entries -join ";"
}

# docker on PATH - else in Docker Desktop's folder, then put on this session's PATH
function Test-DockerCli {
	if (Get-Command docker -ErrorAction SilentlyContinue) { return $true }
	if (-not (Test-Path (Join-Path $DockerCliDir "docker.exe"))) { return $false }
	$env:Path = "$env:Path;$DockerCliDir"
	return $true
}
function Test-DockerRunning { return (Invoke-Native "docker" @("info")).Code -eq 0 }
function Test-DockerReady { return (Test-DockerCli) -and (Test-DockerRunning) }

function Wait-Docker([int]$Seconds, [string]$Waiting) {
	Write-Host "   $Waiting" -NoNewline
	$end = (Get-Date).AddSeconds($Seconds)
	while ((Get-Date) -lt $end) {
		if (Test-DockerReady) { Write-Host ""; return $true }
		if ((Test-DockerCli) -and (Test-Path $DockerDesktopExe) -and
		    -not (Get-Process "Docker Desktop" -ErrorAction SilentlyContinue)) {
			Start-Process $DockerDesktopExe | Out-Null
		}
		Write-Host "." -NoNewline
		Start-Sleep -Seconds 5
		# a fresh install puts docker on PATH for new windows only
		Update-SessionPath
	}
	Write-Host ""
	return $false
}

function Install-DockerDesktop {
	Step "Installing Docker Desktop (Docker's official installer - it shows Docker's terms)"
	if (Get-Command winget -ErrorAction SilentlyContinue) {
		# unattended (the Setup wizard, -Yes): Docker's terms were shown and
		# accepted on the licence page; winget can't ask in a hidden window
		$accept = if ($Yes) { @("--accept-package-agreements", "--accept-source-agreements") } else { @() }
		& winget install --id Docker.DockerDesktop --exact @accept
		if ($LASTEXITCODE -eq 0) { return }
		Warn "winget didn't install it - trying Docker's installer directly."
	}
	$installer = Join-Path $env:TEMP "Docker Desktop Installer.exe"
	try {
		[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
		$ProgressPreference = "SilentlyContinue"
		Invoke-WebRequest -UseBasicParsing -OutFile $installer `
			"https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
		Start-Process -FilePath $installer -Verb RunAs -Wait
	} catch {
		Warn "Couldn't download it ($($_.Exception.Message)) - opening Docker's download page."
		Start-Process "https://www.docker.com/products/docker-desktop/"
	}
}

function Confirm-Docker([switch]$OfferInstall) {
	if (Test-DockerReady) { return }
	if (-not (Test-DockerCli)) {
		if (-not $OfferInstall) { Fail "Docker Desktop isn't installed - run install first." }
		if ($Interactive) {
			$answer = Read-Host "Docker Desktop isn't installed. NetRollout runs on it. Install it now? [Y]"
			if ($answer -and $answer -notmatch "^(y|yes)$") { Fail "Install Docker Desktop, then run install again." }
		}
		Install-DockerDesktop
		if (-not (Wait-Docker 900 "Waiting for Docker Desktop to be installed and to start (this window continues by itself; Ctrl+C stops)")) {
			Fail ("Docker Desktop isn't running yet. If its installer asked to restart Windows, " +
			      "restart, then run install again - nothing of NetRollout was installed yet.")
		}
		return
	}
	Step "Starting Docker Desktop"
	if (-not (Wait-Docker 180 "Waiting for Docker Desktop")) {
		Fail "Docker Desktop didn't start within 3 minutes. Start it from the Start menu, then try again."
	}
}

# ── NetRollout ────────────────────────────────────────────────────────────────

function Test-Installed { return Test-Path $EnvFile }

# Stop unless NetRollout is installed here (-Setup: say how to install it)
function Assert-Installed([switch]$Setup) {
	if (Test-Installed) { return }
	if ($Setup) { Fail "NetRollout isn't installed in $Root - run NetRollout Setup." }
	Fail "NetRollout isn't installed in $Root."
}

# NetRollout on this computer (https://127.0.0.1) usually has a self-signed
# certificate: accepted for 127.0.0.1 only, every other address is verified
# as usual. Compiled, not a script block: PowerShell 5.1 runs the callback on
# a thread without a runspace, where a script block can't run (every request
# then fails). TLS 1.2 isn't PowerShell 5.1's default either.
function Enable-LocalTls {
	if (-not ("NrLocalTls" -as [type])) {
		Add-Type -TypeDefinition @"
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class NrLocalTls {
	public static bool Check(object sender, X509Certificate cert, X509Chain chain, SslPolicyErrors errors) {
		HttpWebRequest request = sender as HttpWebRequest;
		if (request != null && request.RequestUri.Host == "127.0.0.1") { return true; }
		return errors == SslPolicyErrors.None;
	}
	public static void Install() {
		ServicePointManager.ServerCertificateValidationCallback = Check;
	}
}
"@
	}
	[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
	[NrLocalTls]::Install()
}

# NetRollout's health on this computer (the port in use)
function Get-HealthUrl { return "https://127.0.0.1:$(Read-EnvValue 'HTTPS_PORT' '443')/_netrollout/health" }

function Test-Reachable {
	Enable-LocalTls
	try {
		$r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 (Get-HealthUrl)
		return $r.StatusCode -eq 200
	} catch {
		return $false
	}
}

function Open-Browser {
	if ($NoBrowser -or -not $Interactive) { return }
	Start-Process (Get-Address) | Out-Null
}

function Start-NetRollout {
	Confirm-Docker
	# every start: an install from before this rule (or a folder whose
	# permissions were changed) is put right
	Restrict $Root
	Step "Checking the ports"
	$busy = Get-BusyPorts (Get-OurPorts)
	Invoke-Setup (@("prepare-start", "--busy-ports", $busy) + (Get-Facts)) | Out-Null
	# something else on the HTTPS port: Docker would refuse nginx (or, on
	# Docker Desktop once, run it without the port) - say who, start nothing
	$port = Read-EnvValue "HTTPS_PORT" "443"
	$owner = Get-PortOwner $port $busy
	if ($owner) { Fail (Get-PortTakenText $port $owner) }
	if ((Invoke-Native "docker" @("image", "inspect", $AppImage)).Code -eq 0) { Step "Starting NetRollout" }
	else { Step "Starting NetRollout (the first time takes a few minutes: the images are downloaded)" }
	$r = Compose @("up", "-d", "--wait", "--wait-timeout", "600")
	if ($r.Code -ne 0) {
		Write-Host $r.Output
		$logs = Compose @("logs", "--tail", "30", "app")
		Write-Host $logs.Output
		$owner = Get-PortOwner $port (Get-BusyPorts (Get-OurPorts))
		if ($owner) { Fail (Get-PortTakenText $port $owner) }
		Fail "NetRollout didn't start - above: what Docker said and the app's last lines."
	}
	# a container whose start failed on a taken port runs without the
	# binding when it's started again, even once the port is free (Docker
	# 29: start / restart keep it so) - only a new container gets it
	if (-not (Test-HttpsPublished)) {
		$owner = Get-PortOwner $port (Get-BusyPorts (Get-OurPorts))
		if ($owner) { Fail (Get-PortTakenText $port $owner) }
		Step "nginx runs without port $port - recreating it"
		$r = Compose @("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "120", "nginx")
		if ($r.Code -ne 0) { Write-Host $r.Output }
		if (-not (Test-HttpsPublished)) {
			$owner = Get-PortOwner $port (Get-BusyPorts (Get-OurPorts))
			if ($owner) { Fail (Get-PortTakenText $port $owner) }
			Fail "NetRollout's nginx runs without port $port (Docker didn't publish it) - above: what Docker said."
		}
	}
	if (Test-Reachable) { Good "NetRollout is running: $(Get-Address)" }
	else { Warn "NetRollout runs, but $(Get-Address) didn't answer from this computer yet - see: netrollout status" }
}

function Restrict([string]$Path) {
	# Administrators, SYSTEM and the installing account only (a folder: also
	# what's created in it)
	$f = if (Test-Path $Path -PathType Container) { "(OI)(CI)F" } else { "F" }
	$r = Invoke-Native "icacls" @($Path, "/inheritance:r", "/grant:r", "*S-1-5-32-544:$f",
		"*S-1-5-18:$f", "${env:USERDOMAIN}\${env:USERNAME}:$f")
	if ($r.Code -ne 0) { Warn "Couldn't restrict $Path ($($r.Output.Trim()))" }
}

# Why NetRollout can't run on this computer ("" when it can): NetRollout Setup
# asks before its first page
function Get-HostProblem {
	if ((Get-CimInstance Win32_OperatingSystem).ProductType -ne 1) {
		return ("This is Windows Server, which Docker Desktop doesn't support. Install NetRollout " +
		        "on Windows 10/11 (a virtual machine is fine), or on a Linux server.")
	}
	if (-not (Test-DockerReady)) {
		$hypervisor = (Get-CimInstance Win32_ComputerSystem).HypervisorPresent
		$firmware = @(Get-CimInstance Win32_Processor | Where-Object { $_.VirtualizationFirmwareEnabled }).Count -gt 0
		if (-not ($hypervisor -or $firmware)) {
			return ("Virtualization is turned off, and Docker needs it. On a PC: turn on Intel VT-x / " +
			        "AMD-V in the BIOS/UEFI. On a virtual machine: enable nested virtualization (Hyper-V: " +
			        "Set-VMProcessor -ExposeVirtualizationExtensions `$true; VMware: 'Virtualize Intel " +
			        "VT-x/EPT'). Then run NetRollout Setup again.")
		}
	}
	return ""
}

function Invoke-Install {
	if (-not $Yes) { Fail "Install NetRollout with NetRollout Setup (NetRollout-Setup-<version>.exe)." }
	if (Test-Installed) {
		Fail "NetRollout is already installed in $Root - see: netrollout status." 2
	}
	$problem = Get-HostProblem
	if ($problem) { Fail $problem }
	Confirm-Docker -OfferInstall
	# a fresh install on volumes an earlier install left (Setup asks about
	# them; it can't when Docker wasn't running at its start) would put new
	# passwords and a new key on the old database
	$volumes = Get-ProjectVolumes
	if ($volumes) {
		Fail ("NetRollout's data from an earlier install is still in Docker ($($volumes -join ', ')), and " +
		      "$Root doesn't hold its .env. Nothing was installed: run NetRollout Setup again and choose " +
		      "the folder that holds its .env - or, if that folder is gone, let Setup delete the data.") 2
	}
	Step "Getting NetRollout $Version"
	foreach ($image in "netrollout", "netrollout-nginx") { Get-OurImage "itamarweinstein/${image}:$Version" }
	Step "Setting up"
	$setup = @("init", "--licence-accepted", "--busy-ports", (Get-BusyPorts)) + (Get-Facts)
	foreach ($given in @(@("--hostname", $Hostname), @("--https-port", $HttpsPort),
	                     @("--monitoring", $Monitoring), @("--org-certificate", $OrgCertificate),
	                     @("--timezone", $TimeZone))) {
		if ($given[1]) { $setup += $given }
	}
	$code = Invoke-Setup ($setup + "--defaults")
	if ($code -ne 0) { Fail "Setup stopped - nothing was started." $code }
	# the folder holds the TLS key, the scripts NetRollout runs, and (after a
	# database move) runtime.env's passwords: no other account of this
	# computer may read or change it - inherited by everything in it
	Restrict $Root
	Restrict $EnvFile
	Restrict (Join-Path $Root "backups")
	# settings live in System Settings: .env is for Docker, not for people
	(Get-Item -Force $EnvFile).Attributes += "Hidden"
	Start-NetRollout
	Start-PortHelper
	Say ""
	Good "Installed. Open $(Get-Address) and sign in as admin / admin - you'll set a new password."
}

function Invoke-Status {
	Assert-Installed -Setup
	# first: the new version's image may not be here yet (the setup core's report needs it)
	$pending = Get-PendingUpdate
	if ($pending) {
		Write-Host ("Update:       the update to NetRollout $($pending.Version) DIDN'T FINISH - run: netrollout start " +
		            "(it finishes the update, then starts; NetRollout Manager: Finish update)") -ForegroundColor Yellow
	}
	Confirm-Docker
	$reachable = if (Test-Reachable) { "yes" } else { "no" }
	$busy = Get-BusyPorts (Get-OurPorts)
	$published = if (Test-HttpsPublished) { "yes" } else { "no" }
	$code = Invoke-Setup (@("status", "--containers", (Get-ContainerStates), "--reachable", $reachable,
		"--https-published", $published, "--busy-ports", $busy) + (Get-Facts)) -OnNetwork
	if ($pending -and $code -eq 0) { return 1 }
	return $code
}

function Show-RunningRollouts {
	try {
		Enable-LocalTls
		$h = Invoke-RestMethod -TimeoutSec 5 (Get-HealthUrl)
		if ($h.rollouts.running -gt 0) {
			Warn "$($h.rollouts.running) rollout(s) running - they finish and are recorded first (up to 10 minutes)."
		}
	} catch { }
}

# The running app's rollouts, asked inside its container (python -m src.jobs:
# its Redis and database settings are there): the table ("" when none), or
# with -Json one line {"rollouts": [...]}. $null when it can't say (not
# running, or a version without the command).
function Get-Rollouts([switch]$Json) {
	$a = @("exec", "-T", "app", "python", "-m", "src.jobs", "rollouts")
	if ($Json) { $a += "--json" }
	$r = Compose $a
	if ($r.Code -ne 0) { return $null }
	$lines = @($r.Output -split "`n" | Where-Object { $_.Trim() -ne "System.Management.Automation.RemoteException" })
	if ($Json) { return ($lines | Where-Object { $_.TrimStart().StartsWith("{") } | Select-Object -Last 1) }
	# the table only, from its heading: compose's and Python's warnings (stderr) left out
	$start = -1
	for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "^\d+ rollouts? running or queued:$") { $start = $i; break } }
	if ($start -lt 0) { return "" }
	return (($lines[$start..($lines.Count - 1)] | Where-Object { $_.Trim() -and ($_ -match "^(\d+ rollouts? running|   )") }) -join "`n")
}

# The stop under way cancels the running rollouts at once (the app's drain
# looks for the mark) - devices being configured finish, results recorded
function Request-StopNow {
	$r = Compose @("exec", "-T", "app", "python", "-m", "src.jobs", "stop-now")
	if ($r.Code -eq 0) { Show-Output $r.Output; return $true }
	Warn "Couldn't ask NetRollout to cancel them - they finish first."
	return $false
}

# The stop or update that may have asked for a stop now isn't happening: a
# later stop waits for the rollouts again (nothing asked: nothing changes)
function Clear-StopNow {
	try { Compose @("exec", "-T", "app", "python", "-m", "src.jobs", "stop-now", "--clear") | Out-Null } catch { }
}

# Run a stop / update; when it fails, the stop now asked for (here, by the
# Manager, or by prepare-update before Setup ran this) is cleared first
function Invoke-ClearingStopNow([scriptblock]$Body) {
	try { & $Body } catch { Clear-StopNow; throw }
}

# Before a stop or an update's restart: the rollouts running or queued, and -
# asked here, not with -Yes - wait for them (up to the drain deadline),
# cancel them all now, or don't. $false: don't stop.
function Confirm-Rollouts([string]$What) {
	$list = Get-Rollouts
	if ($null -eq $list) { Show-RunningRollouts; return $true }    # an older version: the count
	if (-not $list) { return $true }
	Warn (($list -split "`n" | Select-Object -First 1))
	Write-Host (($list -split "`n" | Select-Object -Skip 1) -join "`n")     # indented already
	if (-not $Interactive) {
		Warn "They finish and are recorded first (up to 10 minutes)."
		return $true
	}
	while ($true) {
		$a = (Read-Host "   [W]ait for them (up to 10 minutes), [C]ancel them all now, or [D]on't $($What)? [W]").Trim()
		if ($a -match "^(w|wait)?$") { Warn "They finish and are recorded first."; return $true }
		if ($a -match "^c") { Request-StopNow | Out-Null; return $true }
		if ($a -match "^d") { return $false }
	}
}

function Invoke-Stop {
	Invoke-ClearingStopNow {
		Assert-Installed
		if (-not (Test-DockerReady)) { Good "NetRollout isn't running (Docker isn't)."; return }
		if ((Get-ContainerStates) -notmatch "=running") { Good "NetRollout isn't running - nothing to stop."; return }
		if (-not (Confirm-Rollouts "stop")) { Good "Not stopped - NetRollout keeps running."; return }
		Step "Stopping NetRollout"
		$r = Compose @("stop")
		if ($r.Code -ne 0) { Write-Host $r.Output; Fail "Docker couldn't stop it - see above." }
		Good "Stopped. Start it again with: netrollout start"
	}
}

# ── backups (the engine: python -m src.backup, in the app image) ─────────────

# compose run / exec's own progress lines left out of what people read
function Show-Output([string]$Text) {
	# (PowerShell 5.1 shows an empty line of a native command's stderr as
	# "System.Management.Automation.RemoteException")
	$lines = $Text -split "`n" | Where-Object { $_.Trim() -and $_ -notmatch "^\s*(Container|Network|Volume) " -and
		$_.Trim() -ne "System.Management.Automation.RemoteException" }
	if ($lines) { Write-Host (($lines | ForEach-Object { "   $($_.TrimEnd())" }) -join "`n") }
}

function Test-AppRunning { return (Get-ContainerStates) -match "(^|,)app=running" }

# In the running app; when it isn't running, a one-off app container next to
# the database (the same settings, folders and Grafana's data)
function Invoke-BackupCreate([string]$Kind) {
	if (Test-AppRunning) {
		$r = Compose @("exec", "-T", "app", "python", "-m", "src.backup", "create", "--kind", $Kind)
	} else {
		$up = Compose @("up", "-d", "--wait", "postgres")
		if ($up.Code -ne 0) { Write-Host $up.Output; Fail "The database didn't start - see above." }
		$r = Compose @("run", "--rm", "--no-deps", "app", "python", "-m", "src.backup", "create", "--kind", $Kind)
	}
	Show-Output $r.Output
	return $r.Code
}

function Invoke-Backup {
	Assert-Installed
	Confirm-Docker
	Step "Backing up"
	if ((Invoke-BackupCreate "manual") -ne 0) { Fail "Not backed up - see above." }
	Good "In $(Join-Path $Root 'backups'). Keep a copy somewhere else too: it holds the key to the saved credentials."
}

function Test-Monitoring { return (Read-EnvValue "COMPOSE_PROFILES") -match "monitoring" }

# Grafana's admin password back to this installation's (.env): the restored
# Grafana database has the backup's, and grafana-setup signs in with ours.
# Grafana must run, grafana-setup not yet (it would sign in with the wrong one).
function Reset-GrafanaAdmin {
	# Not piped from PowerShell: 5.1 adds a byte-order mark and CR LF, and
	# Grafana takes them as part of the password. Passed through the
	# environment (never on a command line), printf hands over its exact bytes.
	$env:NR_GRAFANA_PASSWORD = Read-EnvValue "GRAFANA_ADMIN_PASSWORD"
	try {
		$r = Compose @("exec", "-T", "-e", "NR_GRAFANA_PASSWORD", "grafana", "sh", "-c",
			'printf %s "$NR_GRAFANA_PASSWORD" | grafana cli --homepath /usr/share/grafana admin reset-admin-password --password-from-stdin')
	} finally {
		Remove-Item Env:NR_GRAFANA_PASSWORD -ErrorAction SilentlyContinue
	}
	if ($r.Code -ne 0) {
		Show-Output $r.Output
		Warn "Grafana's admin password couldn't be reset - Grafana's dashboards may not update until it is (netrollout logs grafana-setup)."
	}
}

function Invoke-Restore {
	Assert-Installed
	if (-not $Service) { Fail "Which backup? netrollout restore <file>  (they're in $(Join-Path $Root 'backups'))" }
	$backups = Join-Path $Root "backups"
	$file = if (Test-Path -PathType Leaf $Service) { (Resolve-Path $Service).Path }
	        elseif (Test-Path -PathType Leaf (Join-Path $backups $Service)) { Join-Path $backups $Service }
	        else { Fail "No such file: $Service" }
	$shown = Split-Path -Leaf $file
	$name = $shown
	# the app sees only the backups folder: a file from elsewhere is staged
	# there under a hidden name and removed afterwards (the original stays)
	$staged = $null
	if ((Split-Path -Parent $file) -ne $backups) {
		if (-not (Test-Path $backups)) { New-Item -ItemType Directory $backups | Out-Null }
		$name = ".restoring-$shown"
		$staged = Join-Path $backups $name
		Copy-Item -Force $file $staged
	}
	try {
		Confirm-Docker
		Step "Checking $shown"
		$check = Compose @("run", "--rm", "--no-deps", "app", "python", "-m", "src.backup", "check", $name)
		Show-Output $check.Output
		if ($check.Code -ne 0) { Fail "This backup can't be restored - nothing was changed." }
		if ($Interactive) {
			$a = Read-Host ("Restore it? Everything in NetRollout since then is replaced, and everyone signs in " +
			                "again (the current state is backed up first). [y/N]")
			if ($a -notmatch "^(y|yes)$") { Fail "Nothing was changed." 2 }
		}
		if ($NoSafetyBackup) {
			Warn "Without a backup of the current state (-NoSafetyBackup)."
		} else {
			Step "Backing up the current state first"
			if ((Invoke-BackupCreate "before-restore") -ne 0) {
				Fail ("Couldn't back up the current state - nothing was changed. To restore anyway " +
				      "(e.g. the database is damaged): netrollout restore `"$file`" -NoSafetyBackup")
			}
		}
		$monitoring = Test-Monitoring
		Step "Stopping NetRollout (running rollouts finish first)"
		$stop = @("stop", "app")
		if ($monitoring) { $stop += @("grafana-setup", "grafana") }
		$r = Compose $stop
		if ($r.Code -ne 0) { Write-Host $r.Output; Fail "Docker couldn't stop it - nothing was changed." }
		$r = Compose @("up", "-d", "--wait", "postgres")
		if ($r.Code -ne 0) { Write-Host $r.Output; Start-NetRollout; Fail "The database didn't start - nothing was changed." }
		Step "Restoring $shown"
		# as root: the files get their folder's owner (Grafana's volume is Grafana's)
		$run = @("run", "--rm", "--no-deps", "--user", "0")
		if ($monitoring) { $run += @("-v", "${Project}_grafana:/data/grafana-restore") }
		$run += @("app", "python", "-m", "src.backup", "restore", $name,
			"--https-port", (Read-EnvValue "HTTPS_PORT" "443"), "--key-out", "/data/backups/.restored-key")
		if ($monitoring) { $run += @("--grafana-dir", "/data/grafana-restore") }
		$r = Compose $run
		Show-Output $r.Output
		# 3: the database restored (its key handed over), not every file
		$partial = $r.Code -eq 3
		if ($r.Code -ne 0 -and -not $partial) {
			Step "Starting NetRollout again, as it was"
			Start-NetRollout
			Fail "Not restored - see above. Nothing was changed."
		}
		if ((Invoke-Setup @("restore-key") -AsRoot) -ne 0) {
			Fail ("Restored, but the backup's encryption key couldn't be put into .env - NetRollout " +
			      "isn't started (it couldn't decrypt the saved credentials). See above.")
		}
		if ($monitoring) {
			# Grafana alone first (not grafana-setup), its password reset, then the rest
			$r = Compose @("up", "-d", "--wait", "grafana")
			if ($r.Code -eq 0) { Reset-GrafanaAdmin }
			else { Show-Output $r.Output; Warn "Grafana didn't start - its admin password wasn't reset (netrollout logs grafana)." }
		}
		Start-NetRollout
		if ($partial) {
			# exit 3: a script calling this can tell it from a full restore
			Fail ("Restored $shown's database, but not all of its files - see above for which. NetRollout " +
			      "runs on the restored data (everyone signs in again); put the missing files back by hand, " +
			      "or fix the cause and restore again.") 3
		} else {
			Good "Restored $shown. Everyone signs in again."
		}
	} finally {
		if ($staged) { Remove-Item -Force -ErrorAction SilentlyContinue $staged }
	}
}

# ── netrollout reset-admin: the factory admin back (python -m src.accounts) ──
# When nobody can sign in as an administrator: the factory admin enabled, its
# two-factor sign-in off, its failed sign-ins and lock cleared, signed out
# everywhere - in the running app's container, else a one-off one next to the
# database. -NewPassword: also a temporary password, shown once. No button in
# NetRollout Manager: rare, and for whoever may run commands on this computer.
function Invoke-ResetAdmin {
	Assert-Installed
	Confirm-Docker
	if ($Interactive) {
		$extra = if ($NewPassword) { "; it gets a temporary password, shown once" } else { "" }
		$a = Read-Host ("Reset the factory admin account (admin)? It's enabled, its two-factor sign-in " +
		                "turned off and its failed sign-ins cleared$extra. [y/N]")
		if ($a -notmatch "^(y|yes)$") { Say "Nothing was changed."; return }
	}
	$cli = @("python", "-m", "src.accounts", "reset-admin")
	if ($NewPassword) { $cli += "--new-password" }
	Step "Resetting the factory admin"
	if (Test-AppRunning) {
		$r = Compose (@("exec", "-T", "app") + $cli)
	} else {
		$up = Compose @("up", "-d", "--wait", "postgres", "redis")
		if ($up.Code -ne 0) { Show-Output $up.Output; Fail "The database didn't start - see above. Nothing was changed." }
		$r = Compose (@("run", "--rm", "--no-deps", "-T", "app") + $cli)
	}
	Show-Output $r.Output
	if ($r.Code -ne 0) { Fail "The factory admin wasn't reset - see above." }
}

# ── updates (NetRollout Setup over an install) ───────────────────────────────

# Before Setup replaces any file - run from Setup's temporary folder with
# -InstallDir: the direction (the installed version's image decides), then
# a backup by the installed version. Exit 2: refused, nothing changed.
function Invoke-PrepareUpdate {
	Invoke-ClearingStopNow {
		Assert-Installed
		if (-not $NewVersion) { Fail "prepare-update needs -NewVersion" }
		Confirm-Docker
		if (-not (Test-Path $VersionFile)) { Invoke-PrepareKept; return }
		Step "Checking the update: NetRollout $Version -> $NewVersion"
		$r = Invoke-Native "docker" @("run", "--rm", $AppImage, "python", "-m", "src.setup", "check-update",
			"--installed", $Version, "--new", $NewVersion)
		if ($r.Code -eq 2) { Show-Output $r.Output; Fail "Nothing was changed." 2 }
		if ($r.Code -ne 0) {
			Warn "Couldn't compare the versions ($AppImage didn't run) - continuing."
		}
		if (-not (Confirm-Rollouts "update")) { Fail "Nothing was changed." 2 }
		Step "Backing up first"
		if ((Invoke-BackupCreate "before-update") -ne 0) {
			Fail "Couldn't back up - nothing was changed. Fix it (above), then run Setup again."
		}
		# from here on the files are replaced: the update counts as unfinished
		# until its second half (update) has run
		Write-UpdateMarker $NewVersion
	}
}

# prepare-update over data an uninstall kept (.env, config\, the volumes - no
# NetRollout files, so no VERSION): the version .env was last set up by
# decides, by its own image (gone: the new one - versions compare the same
# way). A newer one is refused (exit 2); an older one needs its image for the
# backup, made by Invoke-Update -BackupFirst once the files are in place
# (there's no compose file here yet). No version line: an older one.
function Invoke-PrepareKept {
	$kept = Get-KeptVersion
	if (-not $kept) {
		Warn ("$EnvFile doesn't say which version set it up (an early install): taken as an older version and " +
		      "updated. The backup before the update is made by NetRollout $NewVersion - it restores into " +
		      "$NewVersion, not into the earlier version.")
		Say "   The backup is made once NetRollout $NewVersion's files are in place, before anything starts."
		Write-UpdateMarker $NewVersion -BackupFirst
		return
	}
	$old = "itamarweinstein/netrollout:$kept"
	$haveOld = Test-Image $old
	$judge = if ($haveOld) { $old } else { "itamarweinstein/netrollout:$NewVersion" }
	Step "Checking the update: NetRollout $kept (its settings and data, kept in $Root) -> $NewVersion"
	$r = Invoke-Native "docker" @("run", "--rm", $judge, "python", "-m", "src.setup", "check-update",
		"--installed", $kept, "--new", $NewVersion)
	if ($r.Code -eq 2 -and $r.Output -match "the same version") {
		Good "The same version - its settings and data are used as they are."
		return
	}
	if ($r.Code -eq 2) { Show-Output $r.Output; Fail "Nothing was changed." 2 }
	if ($r.Code -ne 0) { Show-Output $r.Output; Fail "Couldn't compare the versions ($judge didn't run) - nothing was changed." }
	if (-not $haveOld) {
		Fail ("NetRollout $kept's image ($old) isn't on this computer and couldn't be downloaded - it makes the " +
		      "backup before the update (one NetRollout $kept can restore). Nothing was changed. Check this " +
		      "computer's internet connection, then run Setup again.")
	}
	Say "   The backup (by NetRollout $kept) is made once NetRollout $NewVersion's files are in place, before anything starts."
	# the backup is owed until the update's second half has made it
	Write-UpdateMarker $NewVersion -BackupFirst
}

# Invoke-Update -BackupFirst: the backup prepare-update couldn't make for
# kept data - by the version that wrote it (no version line: this one)
function Backup-KeptData {
	$kept = Get-KeptVersion
	if ($kept) {
		$old = "itamarweinstein/netrollout:$kept"
		if (-not (Test-Image $old)) {
			Fail ("NetRollout $kept's image ($old) isn't on this computer and couldn't be downloaded - it makes " +
			      "the backup before the update. NetRollout wasn't started; its data is as NetRollout $kept left it. " +
			      "Check this computer's internet connection, then Retry - or later: " +
			      "run Setup again, or netrollout start (both finish the update).")
		}
	} else {
		Warn "No version line in .env: the backup is made by NetRollout $Version (it restores into $Version, not into the earlier version)."
	}
	Step "Backing up first (the data NetRollout $(if ($kept) { $kept } else { 'had' }) left here)"
	$script:ImageVersion = $kept
	try { $code = Invoke-BackupCreate "before-update" } finally { $script:ImageVersion = "" }
	if ($code -ne 0) {
		Fail ("Couldn't back up - NetRollout wasn't started and its data wasn't changed. Fix it (above), " +
		      "then Retry - or later: run Setup again, or netrollout start (both finish the update).")
	}
}

# After Setup replaced the files (this is the new script): the new images
# while the old version keeps running, .env brought up to date, the restart.
# Resumable: the marker says the update is unfinished until the restart
# worked, and whether the kept data's backup is still owed - so an update
# that failed halfway is finished by running this again (Setup, the
# Manager's Finish update, start), never skipped.
function Invoke-Update {
	Invoke-ClearingStopNow {
		Assert-Installed
		Confirm-Docker
		$pending = Get-PendingUpdate
		$owed = $BackupFirst -or ($pending -and $pending.BackupFirst)
		Write-UpdateMarker $Version -BackupFirst:$owed
		Step "Downloading NetRollout $Version (it keeps running meanwhile)"
		# failures of others' images surface at the start; ours are checked here
		# (a local build isn't on Docker Hub)
		Compose @("pull", "--quiet", "--ignore-pull-failures") | Out-Null
		foreach ($image in "netrollout", "netrollout-nginx") { Get-OurImage "itamarweinstein/${image}:$Version" }
		if ($owed) {
			Backup-KeptData
			Write-UpdateMarker $Version      # the backup is made: not again on a resume
		}
		Step "Updating the settings"
		if ((Invoke-Setup @("upgrade")) -ne 0) { Fail "The update stopped before the restart - see above." }
		Confirm-Rollouts "update" | Out-Null      # the files are in place: shown, never refused
		Step "Restarting on the new version (about a minute; running rollouts finish first)"
		Start-NetRollout
		Remove-UpdateMarker
		Start-PortHelper
		Good "Updated to NetRollout $Version. Everyone signs in again."
	}
}

# start with an unfinished update here: the update's second half first (its
# restart is the start) - never the new version without setup upgrade
function Invoke-Start {
	Assert-Installed -Setup
	$pending = Get-PendingUpdate
	if ($pending) {
		Step "The update to NetRollout $($pending.Version) didn't finish - finishing it first"
		Invoke-Update; Open-Browser; return
	}
	if ((Test-DockerReady) -and (Test-AllRunning)) {
		Good "NetRollout is already running: $(Get-Address)"
		Start-PortHelper; Open-Browser; return
	}
	Start-NetRollout; Start-PortHelper; Open-Browser
}

# Everything up, healthy and on its port: start has nothing to do. Anything
# else (a container down or unhealthy, nginx without its port, no answer)
# gets the full start - that is start's repair.
function Test-AllRunning {
	$states = @((Get-ContainerStates) -split "," | Where-Object { $_ })
	if (-not $states) { return $false }
	foreach ($s in $states) { if ($s -notmatch "=running(/healthy)?$") { return $false } }
	return (Test-HttpsPublished) -and (Test-Reachable)
}

# ── the port helper (System Settings -> HTTPS port) ──────────────────────────
# The setup core decides (src/setup/port.py), this does the Docker part: a
# trial file adds the new port to nginx, nginx is recreated, the trial ends
# kept or rolled back. Run by the headless NetRollout Manager --helper when
# site.env changes, or by hand: netrollout apply.

$HelperExe = Join-Path $PSScriptRoot "NetRollout Manager.exe"

# The helper itself (headless; one per install - a second start adds nothing),
# announced to the page (else it says to run netrollout apply by hand)
function Start-PortHelper {
	if (-not (Test-Path $HelperExe)) { return }
	Start-Process $HelperExe -ArgumentList "--helper" | Out-Null
	Get-SetupAnswer @("port-ready") | Out-Null
}

function Update-Nginx {
	$r = Compose @("up", "-d", "--no-deps", "--wait", "--wait-timeout", "120", "nginx")
	return $r
}

# One port-* step of the setup core; when it fails, what it said and then
# $Why - the state it leaves (the helper's log; the person's terminal)
function Invoke-PortStep([string]$Why, [string[]]$Arguments) {
	$r = Get-SetupAnswer $Arguments
	if ($r.Code -ne 0) { Show-Output $r.Output; Fail $Why }
}

# A trial runs: its confirmation (site.env) within the trial's 120 s, timed
# here by a stopwatch - Docker Desktop's VM clock can lag Windows' by
# minutes, so a deadline written in the VM can't be compared with this
# computer's time. $true: confirmed; $false: time's up.
function Wait-PortTrial([string]$Id, [int]$Seconds = 120) {
	$site = Join-Path $Root "config\nginx\site.env"
	$clock = [Diagnostics.Stopwatch]::StartNew()
	while ($clock.Elapsed.TotalSeconds -lt $Seconds) {
		if (Get-Content $site -ErrorAction SilentlyContinue | Where-Object { $_ -eq "NETROLLOUT_PORT_CONFIRMED=$Id" }) {
			return $true
		}
		Start-Sleep -Seconds 1
	}
	return $false
}

function Invoke-Apply {
	Assert-Installed
	if (-not (Test-DockerReady)) { Warn "Docker isn't running - nothing applied."; return }
	# one at a time (the helper, and a hand-run apply): a second one waits its
	# turn, then handles what's still pending
	$lockPath = Join-Path $Root "config\.port-helper.lock"
	$lock = $null
	$clock = [Diagnostics.Stopwatch]::StartNew()
	while (-not $lock) {
		try { $lock = [IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None") }
		catch {
			if ($clock.Elapsed.TotalSeconds -gt 300) { Say "A port change is still being applied - try again in a few minutes."; return }
			Start-Sleep -Seconds 2
		}
	}
	try {
		while ($true) {
			$busy = Get-BusyPorts (Get-OurPorts)
			$r = Get-SetupAnswer @("port-next", "--busy-ports", $busy)
			if ($r.Code -ne 0) { Write-Host $r.Output; Fail "The port helper couldn't decide - see above." }
			$lines = @($r.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -and $_ -notmatch "RemoteException" })
			$action, $port, $id = ($lines[0] -split " ")
			$message = if ($lines.Count -gt 1) { $lines[1] } else { "" }
			$current = Read-EnvValue "HTTPS_PORT" "443"
			switch ($action) {
				"none" { if ($message) { Warn "Port $port not applied: $message" } else { Say "No port change to apply." }; return }
				"wait" {
					if (-not (Wait-PortTrial $id)) {
						Invoke-PortStep ("Port $port wasn't confirmed within 2 minutes, and ending its trial failed (above) - " +
						                 "nginx wasn't changed: it still answers on port $port too. Run: netrollout apply") `
							@("port-close", "--outcome", "rollback", "--id", $id, "--timed-out")
						Update-Nginx | Out-Null
						Warn "Port $port wasn't confirmed within 2 minutes (it didn't open from a browser - a firewall?) - NetRollout stays on port $current."
						return
					}
				}
				"try" {
					Step "Opening port $port next to port $current"
					$open = Get-SetupAnswer @("port-open", "--port", $port)
					if ($open.Code -ne 0) {
						Show-Output $open.Output
						# recorded as failed (the page shows it; not retried by itself)
						$closed = Get-SetupAnswer @("port-close", "--outcome", "failed", "--id", $id, "--message",
							"port ${port}: its trial couldn't be prepared (see the port helper's log)")
						if ($closed.Code -ne 0) { Warn "Recording that failed too - the page may still be waiting." }
						Fail "Port $port wasn't opened: preparing its trial failed (above). nginx wasn't changed - NetRollout stays on port $current."
					}
					$up = Update-Nginx
					if ($up.Code -ne 0) {
						Show-Output $up.Output     # what Docker said (the helper's log)
						$why = (($up.Output -split "`n" | Where-Object { $_ -match "error|failed|allocated" }) | Select-Object -First 1)
						if (-not $why) { $why = "nginx didn't start with it" }
						Invoke-PortStep ("Port $port couldn't be opened ($($why.Trim())), and ending its trial failed (above) - " +
						                 "nginx may still be set up with port $port. Run: netrollout apply") `
							@("port-close", "--outcome", "failed", "--id", $id, "--message", "port ${port}: $($why.Trim())")
						Update-Nginx | Out-Null
						Warn "Port $port couldn't be opened: $($why.Trim()) - port $current stays."
						return
					}
					$trying = Get-SetupAnswer @("port-trying", "--port", $port, "--id", $id)
					if ($trying.Code -ne 0) {
						Show-Output $trying.Output
						# nginx answers on both ports, but no trial is running: closed again
						Invoke-PortStep ("Port $port opened, but its trial couldn't be started (above), nor ended - nginx " +
						                 "still answers on port $port too. Run: netrollout apply") `
							@("port-close", "--outcome", "failed", "--id", $id, "--message",
							  "port ${port}: its trial couldn't be started (see the port helper's log)")
						Update-Nginx | Out-Null
						Fail "Port $port opened, but its trial couldn't be started (above) - closed again: NetRollout stays on port $current."
					}
					Good "Port $port is open next to port $current. Open NetRollout on port $port within 2 minutes to keep it - else port $current stays."
				}
				"keep" {
					Invoke-PortStep ("Port $port was confirmed, but keeping it failed (above) - nginx wasn't changed: it " +
					                 "answers on ports $current and $port. Run: netrollout apply") `
						@("port-close", "--outcome", "keep", "--id", $id)
					Update-Nginx | Out-Null
					Good "Port $port kept - NetRollout is at $(Get-Address)."   # then: anything newer?
				}
				"rollback" {
					Invoke-PortStep ("Rolling the port change back failed (above) - nginx wasn't changed: it may still " +
					                 "answer on the trial's port too. Run: netrollout apply") `
						@("port-close", "--outcome", "rollback", "--id", $id, "--message", $message)
					Update-Nginx | Out-Null
					Warn "Port change rolled back ($message) - NetRollout stays on port $(Read-EnvValue 'HTTPS_PORT' '443')."
				}
				default { Write-Host $r.Output; Fail "Unexpected answer from the port helper: $($lines[0])" }
			}
		}
	} finally {
		$lock.Dispose()
	}
}

# What the Setup wizard pre-fills (an ini file: it reads those natively)
# ── netrollout port N: the HTTPS port by hand ────────────────────────────────
# For when the port in use is blocked (another program holds it) and System
# Settings can't be reached. .env, site.env and the System Settings HTTPS
# port all name the new port afterwards (so neither the port helper nor the
# next start undoes it); any step failing puts the previous port back in
# all three. Not while a System Settings port change is applied (its lock).

# System Settings' HTTPS port, through the app's own settings rules: in the
# running app's container, else a one-off one next to the database
function Set-PortSetting([string]$Port) {
	if (Test-AppRunning) {
		$r = Compose @("exec", "-T", "app", "python", "-m", "src.setup", "port-setting", "--wanted", $Port)
	} else {
		$up = Compose @("up", "-d", "--wait", "postgres")
		if ($up.Code -ne 0) { Show-Output $up.Output; return $false }
		$r = Compose @("run", "--rm", "--no-deps", "-T", "app", "python", "-m", "src.setup", "port-setting", "--wanted", $Port)
	}
	if ($r.Code -ne 0) { Show-Output $r.Output }
	return $r.Code -eq 0
}

# The previous port back in all three, NetRollout started on it (it may be
# blocked still - then start says by whom)
function Restore-PreviousPort([string]$Previous) {
	Warn "Putting port $Previous back"
	$r = Get-SetupAnswer @("port-set", "--wanted", $Previous, "--previous")
	if ($r.Code -ne 0) { Show-Output $r.Output }
	if (-not (Set-PortSetting $Previous)) { Warn "The System Settings HTTPS port couldn't be put back - set it to $Previous there." }
	try { Start-NetRollout } catch [System.OperationCanceledException] { }
}

# Where Restore-PreviousPort left NetRollout, in words (after "... - "):
# back on the port; or put back, but another program holds it (named); or
# put back, but it didn't start there for another reason
function Get-PortBackText([string]$Previous) {
	if (Test-HttpsPublished) { return "NetRollout is back on port $Previous." }
	$owner = Get-PortOwner $Previous (Get-BusyPorts (Get-OurPorts))
	if ($owner) { return "it's put back to port $Previous - but $Previous is taken by ${owner}: stop that, then netrollout start." }
	return "it's put back to port $Previous - but it didn't start there (above): see netrollout status."
}

function Invoke-PortByHand {
	Assert-Installed -Setup
	$wanted = if ($NewPort) { $NewPort } else { $Service }
	if (-not $wanted) { Fail "Which port? netrollout port <N>  (e.g. netrollout port 8443)" }
	Confirm-Docker
	$lockPath = Join-Path $Root "config\.port-helper.lock"
	try { $lock = [IO.File]::Open($lockPath, "OpenOrCreate", "ReadWrite", "None") }
	catch { Fail "A port change from System Settings is being applied - try again in a few minutes. Nothing was changed." }
	try {
		$current = Read-EnvValue "HTTPS_PORT" "443"
		# checked before the question: an unusable, current or taken port is
		# said at once, never "Change the port from 7443 to 7443?"
		Step "Checking port $wanted"
		$check = Get-SetupAnswer @("port-set", "--wanted", $wanted, "--busy-ports", (Get-BusyPorts (Get-OurPorts)), "--check")
		if ($check.Code -ne 0) {
			Show-Output $check.Output
			Fail "Nothing was changed - NetRollout stays on port $current."
		}
		if ($Interactive) {
			$a = Read-Host ("NetRollout restarts on port $wanted (running rollouts finish first; everyone signs " +
			                "in again). Change the port from $current to ${wanted}? [y/N]")
			if ($a -notmatch "^(y|yes)$") { Say "Nothing was changed."; return }
		}
		$r = Get-SetupAnswer @("port-set", "--wanted", $wanted, "--busy-ports", (Get-BusyPorts (Get-OurPorts)))
		$found = @($r.Output -split "`n" | Where-Object { $_ -match "^previous=\d+" })
		if ($r.Code -ne 0 -or -not $found) {
			Show-Output $r.Output
			Fail "Nothing was changed - NetRollout stays on port $current."
		}
		$previous = ($found[0].Trim() -split "=")[1]
		Step "Saving port $wanted in System Settings"
		if (-not (Set-PortSetting $wanted)) {
			Restore-PreviousPort $previous
			Fail "The System Settings HTTPS port couldn't be saved (above) - $(Get-PortBackText $previous)"
		}
		$started = $false
		try { Start-NetRollout; $started = $true } catch [System.OperationCanceledException] { }
		$clock = [Diagnostics.Stopwatch]::StartNew()
		while ($started -and -not (Test-Reachable) -and $clock.Elapsed.TotalSeconds -lt 60) { Start-Sleep -Seconds 3 }
		if (-not $started -or -not (Test-Reachable)) {
			Restore-PreviousPort $previous
			Fail "NetRollout didn't answer on port $wanted (above) - $(Get-PortBackText $previous)"
		}
		Start-PortHelper
		Good "NetRollout now uses port $wanted - $(Get-Address)"
	} finally {
		$lock.Dispose()
	}
}

function Write-Defaults {
	if (-not $Out) { Fail "defaults needs -Out <file>" }
	$busy = @{}
	foreach ($item in ((Get-BusyPorts) -split ",")) {
		$port, $who = $item -split "=", 2
		if ($port) { $busy[[int]$port] = $who }
	}
	$port = 443
	foreach ($p in 443, 8443, 9443, 10443, 11443) { if (-not $busy.ContainsKey($p)) { $port = $p; break } }
	$name = $env:COMPUTERNAME.ToLower()
	if ($name -notmatch "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$") { $name = "netrollout" }
	$docker = if (Test-DockerReady) { "running" } elseif (Test-DockerCli) { "installed" } else { "missing" }
	# the project's volumes (only when Docker answers): Setup asks about them
	# when no folder holds their .env any more
	$volumes = if ($docker -eq "running") { Get-ProjectVolumes } else { $null }
	$lines = @("[defaults]", "hostname=$name", "https_port=$port",
		"timezone=$((Get-TimeZone).Id)", "port80_busy=$(if ($busy.ContainsKey(80)) { $busy[80] } else { '' })",
		"docker=$docker", "kept_volumes=$(if ($volumes) { $volumes -join ' ' } else { '' })",
		"problem=$((Get-HostProblem) -replace '[\r\n]', ' ')",
		"", "[busy]")
	foreach ($p in ($busy.Keys | Sort-Object)) { $lines += "$p=$(if ($busy[$p]) { $busy[$p] } else { 'another program' })" }
	# Windows' own timezone list, as Windows shows it: n=Id|(UTC+02:00) Jerusalem
	$lines += @("", "[timezones]")
	$i = 0
	foreach ($tz in Get-TimeZone -ListAvailable) { $lines += "$i=$($tz.Id)|$($tz.DisplayName)"; $i++ }
	Set-Content -Encoding ASCII -Path $Out -Value $lines
}

function Invoke-Uninstall {
	if (-not (Test-Installed)) { Good "NetRollout isn't installed in $Root - nothing to remove." }
	$delete = [bool]$DeleteData
	$deleteBackups = [bool]$DeleteBackups
	if (-not $DeleteData -and -not $KeepData -and $Interactive -and (Test-Installed)) {
		# both questions: y always deletes, No the default (as Setup's uninstaller)
		$a = Read-Host ("Delete NetRollout's data too - the database, settings, certificates and logs? " +
		                "y: delete it (can't be undone), N: keep it to install again later. [y/N]")
		$delete = $a -match "^(y|yes)$"
		if ($delete) {
			$a = Read-Host ("Delete the backups too ($(Join-Path $Root 'backups'))? They're the last copy of the data - " +
			                "a new install can restore one. y: delete them, N: keep them (recommended). [y/N]")
			$deleteBackups = $a -match "^(y|yes)$"
		}
	}
	if ((Test-Installed) -and (Test-DockerReady)) {
		Step "Removing NetRollout's containers$(if ($delete) { ' and its data' })"
		$down = @("down", "--remove-orphans")
		if ($delete) { $down += "-v" }
		$r = Compose $down
		if ($r.Code -ne 0) { Write-Host $r.Output; Warn "Docker couldn't remove everything - see above." }
	} elseif ($delete) {
		Warn "Docker isn't running: the database volumes stay (Docker Desktop -> Volumes: netrollout_*)."
	}
	if ($delete) {
		$gone = @(".env", "config", "certs", "logs")
		if ($deleteBackups) { $gone += "backups" }
		foreach ($name in $gone) {
			Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Root $name)
		}
		Remove-UpdateMarker
		if ($deleteBackups -or -not (Test-Path (Join-Path $Root "backups"))) {
			Good "NetRollout and its data are removed."
		} else {
			Good "NetRollout and its data are removed - the backups stay in $(Join-Path $Root 'backups')."
			Good "Restore one into a new install: NetRollout Manager -> Restore..."
		}
	} else {
		Good "NetRollout is removed. Its data stays (the database volumes, and .env, config, certs,"
		Good "logs, backups in $Root): installing again in this folder picks it up."
	}
}

# NetRollout Setup's "Start fresh instead" over the data an uninstall kept
# (from Setup's temporary copy, -InstallDir the kept folder; Setup asked):
# what the uninstaller's "delete the data" deletes, without its compose
# files (the uninstall removed them) - the project's containers and network
# by compose's project label, its volumes by the label or their name
# (Get-ProjectVolumes), then .env, config, certs, logs and the update marker.
# The backups stay. Refused, nothing deleted, while NetRollout is installed
# there (VERSION) or any of the project's containers runs.
# -VolumesOnly instead of -InstallDir: volumes left in Docker whose folder
# was deleted by hand - the Docker part only, no file anywhere ($Root is only
# where this copy runs: Setup's temporary folder)
function Invoke-ForgetKept {
	if (-not $Yes -or -not ($InstallDir -or $VolumesOnly)) { Fail "forget-kept is NetRollout Setup's (its folder page asks first)." }
	if ($VolumesOnly) {
		if ($InstallDir -or (Test-Installed) -or (Test-Path $VersionFile)) {
			Fail "forget-kept -VolumesOnly is for volumes whose folder is gone - NetRollout's files are in $Root. Nothing was deleted."
		}
	} else {
		Assert-Installed
		if (Test-Path $VersionFile) {
			Fail "NetRollout is installed in $Root (not only its kept data) - nothing was deleted. Uninstall it in Settings -> Apps instead."
		}
	}
	Confirm-Docker
	$label = "label=com.docker.compose.project=$Project"
	$running = Invoke-Native "docker" @("container", "ls", "-q", "--filter", $label)
	if ($running.Code -ne 0) { Write-Host $running.Output; Fail "Docker didn't answer - nothing was deleted." }
	if ($running.Output.Trim()) {
		Fail "NetRollout's containers are running (Docker project $Project) - nothing was deleted. Stop that NetRollout first."
	}
	if ($VolumesOnly) { Step "Deleting NetRollout's database left in Docker (the database and the other volumes)" }
	else { Step "Deleting the data NetRollout kept in $Root (the database and the other volumes)" }
	foreach ($kind in "container", "volume", "network") {
		if ($kind -eq "volume") {
			$ids = Get-ProjectVolumes
			if ($null -eq $ids) { Fail "Docker didn't list NetRollout's volumes - nothing more was deleted." }
		} else {
			$list = @("ls", "-q", "--filter", $label)
			if ($kind -eq "container") { $list += "-a" }
			$found = Invoke-Native "docker" (@($kind) + $list)
			if ($found.Code -ne 0) { Write-Host $found.Output; Fail "Docker didn't list NetRollout's ${kind}s - nothing more was deleted." }
			$ids = @($found.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
		}
		if (-not $ids) { continue }
		$remove = if ($kind -eq "container") { @("rm", "-f") } else { @("rm") }
		$r = Invoke-Native "docker" (@($kind) + $remove + $ids)
		if ($r.Code -ne 0) { Write-Host $r.Output }
	}
	# the volumes are the data: still there -> the files stay too (a fresh
	# install's new passwords on the old database would break it)
	$left = Get-ProjectVolumes
	if ($null -eq $left -or $left.Count) {
		if ($left) { Write-Host ($left -join "`n") }
		if ($VolumesOnly) { Fail "Docker didn't delete all of NetRollout's volumes (above). Close what uses them (Docker Desktop -> Volumes), then try again." }
		Fail "Docker didn't delete all of NetRollout's volumes (above) - its settings in $Root were kept. Close what uses them (Docker Desktop -> Volumes), then try again."
	}
	if ($VolumesOnly) { Good "NetRollout's old database is deleted from Docker."; return }
	foreach ($name in ".env", "config", "certs", "logs") {
		Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Root $name)
	}
	Remove-UpdateMarker
	if (Test-Installed) { Fail "$EnvFile couldn't be deleted - delete it by hand, then try again." }
	Good "The kept data is deleted - the backups stay in $(Join-Path $Root 'backups')."
}

# NetRollout's own files in an install folder, by name - what a clearing
# deletes there, never anything else in the folder (-Data: .env, config,
# certs and logs too; the backups never). The uninstaller and its data file
# (unins000.exe, .dat: Inno numbers them) by their pattern.
function Get-InstallItems([switch]$Data) {
	$names = @("bin", "deploy", "compose.yaml", "compose.http.yaml", "VERSION", "LICENSE", "README.md",
		".update-pending", ".update")
	if ($Data) { $names += @(".env", "config", "certs", "logs") }
	$items = @($names | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path -LiteralPath $_ })
	if (Test-Path -LiteralPath $Root) {
		$items += @(Get-ChildItem -LiteralPath $Root -Force -File -Filter "unins*" |
			Where-Object { $_.Name -match "^unins\d+\.(exe|dat|msg)$" } | ForEach-Object { $_.FullName })
	}
	return ,$items
}

# The processes running from the install folder (NetRollout Manager: the
# window, the tray, the port helper) - by their path under "$Root\", never
# by name: another install's Manager (or a test's) is left alone
function Get-FolderProcesses {
	$prefix = $Root.TrimEnd("\") + "\"
	return ,@(Get-Process | Where-Object {
		$path = $null
		try { $path = $_.Path } catch { }
		$path -and $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
	})
}

# The install's NetRollout Manager closed: asked first (--exit, its tray icon
# goes properly), then whatever still runs from the folder ended
function Stop-FolderProcesses {
	if (-not (Get-FolderProcesses).Count) { return }
	Step "Closing NetRollout Manager (the tray and the port helper) of $Root"
	$manager = Join-Path $Root "bin\NetRollout Manager.exe"
	if (Test-Path -LiteralPath $manager) { Invoke-Native $manager @("--exit") | Out-Null }
	$left = Get-FolderProcesses
	if ($left.Count) {
		$left | Stop-Process -Force -ErrorAction SilentlyContinue
		$left | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
	}
	$left = Get-FolderProcesses
	if ($left.Count) {
		Write-Host (($left | ForEach-Object { "$($_.Path) (process $($_.Id))" }) -join "`n")
		Fail "These programs from $Root couldn't be closed (above) - nothing more was changed. End them in Task Manager, then run Setup again."
	}
}

# NetRollout Setup over a damaged install: Windows' record of it names $Root,
# but the folder lacks the uninstaller, VERSION or compose.yaml - or .env
# while Docker holds the data (Setup asked). From Setup's temporary copy
# (-InstallDir the folder): nothing here needs NetRollout's own files, they
# may be gone. What's left is cleared so Setup can install again: the
# folder's NetRollout Manager closed (Stop-FolderProcesses), the project's
# containers removed - running ones too - and its network, by compose's
# project label; without -KeepData its volumes too (Get-ProjectVolumes:
# netrollout_* exactly, never the dev stack's), all of them or no file is
# touched; then NetRollout's files in the folder, by name
# (Get-InstallItems) - -KeepData keeps .env, config, certs and logs: the
# folder then holds kept data, as after an uninstall that kept it. The
# backups always stay. Refused, nothing changed, over a healthy install.
function Invoke-ClearDamaged {
	if (-not $Yes -or -not $InstallDir) { Fail "clear-damaged is NetRollout Setup's (it asks first)." }
	$uninstaller = @((Get-InstallItems) | Where-Object { $_ -match "\\unins\d+\.exe$" })
	if ((Test-Installed) -and (Test-Path $VersionFile) -and (Test-Path (Join-Path $Root "compose.yaml")) -and $uninstaller.Count) {
		Fail "NetRollout in $Root isn't damaged - nothing was changed. Uninstall it in Settings -> Apps instead."
	}
	Stop-FolderProcesses
	Confirm-Docker
	Step "Removing NetRollout's containers (Docker project $Project)"
	$label = "label=com.docker.compose.project=$Project"
	foreach ($kind in "container", "network") {
		$list = @($kind, "ls", "-q", "--filter", $label)
		if ($kind -eq "container") { $list += "-a" }
		$found = Invoke-Native "docker" $list
		if ($found.Code -ne 0) { Write-Host $found.Output; Fail "Docker didn't list NetRollout's ${kind}s - nothing more was changed." }
		$ids = @($found.Output -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
		if (-not $ids) { continue }
		$remove = if ($kind -eq "container") { @("rm", "-f") } else { @("rm") }
		$r = Invoke-Native "docker" (@($kind) + $remove + $ids)
		if ($r.Code -ne 0) { Write-Host $r.Output; Fail "Docker couldn't remove NetRollout's ${kind}s (above) - nothing more was changed." }
	}
	if (-not $KeepData) {
		Step "Deleting NetRollout's data in Docker (the database and the other volumes)"
		$ids = Get-ProjectVolumes
		if ($null -eq $ids) { Fail "Docker didn't list NetRollout's volumes - nothing more was changed." }
		if ($ids) {
			$r = Invoke-Native "docker" (@("volume", "rm") + $ids)
			if ($r.Code -ne 0) { Write-Host $r.Output }
		}
		# the volumes are the data: still there -> the files stay too
		$left = Get-ProjectVolumes
		if ($null -eq $left -or $left.Count) {
			if ($left) { Write-Host ($left -join "`n") }
			Fail "Docker didn't delete all of NetRollout's volumes (above) - no file in $Root was deleted. Close what uses them (Docker Desktop -> Volumes), then run Setup again."
		}
	}
	Step "Deleting NetRollout's files in $Root$(if ($KeepData) { ' (its data stays)' })"
	$failed = @()
	foreach ($item in (Get-InstallItems -Data:(-not $KeepData))) {
		Remove-Item -LiteralPath $item -Recurse -Force -ErrorAction SilentlyContinue
		if (Test-Path -LiteralPath $item) { $failed += $item }
	}
	if ($failed) {
		Write-Host ($failed -join "`n")
		Fail "These couldn't be deleted (above) - close what uses them, then run Setup again."
	}
	if ((Test-Path -LiteralPath $Root) -and -not (Get-ChildItem -LiteralPath $Root -Force)) {
		Remove-Item -LiteralPath $Root -Force -ErrorAction SilentlyContinue
	}
	if ($KeepData) { Good "The damaged install is cleared - its data stays in $Root." }
	elseif (Test-Path -LiteralPath (Join-Path $Root "backups")) { Good "The damaged install is cleared - the backups stay in $(Join-Path $Root 'backups')." }
	else { Good "The damaged install is cleared." }
}

function Show-Help {
	Say "NetRollout $Version - $Root"
	Say ""
	Say "  netrollout start            start it (and open it in the browser)"
	Say "  netrollout stop             stop it (running rollouts finish first)"
	Say "  netrollout status           is everything well? what to do if not"
	Say "  netrollout open             open it in the browser"
	Say "  netrollout logs [service]   recent log lines (app, nginx, postgres, ...)"
	Say "  netrollout backup           back up now (into the backups folder)"
	Say "  netrollout restore <file>   put NetRollout back to a backup (asks first)"
	Say "  netrollout apply            apply an HTTPS port saved in System Settings (the helper does it by itself)"
	Say "  netrollout port <N>         move NetRollout to port N when its port is taken (else: System Settings)"
	Say "  netrollout rollouts         the rollouts running or queued (-Json: as JSON)"
	Say "  netrollout port-check       does nginx hold its HTTPS port? if not, who does (-Json: as JSON)"
	Say "  netrollout stop-now         a stop under way (or one within 15 minutes) cancels them at once"
	Say "  netrollout reset-admin      no administrator can sign in: the factory admin enabled, its 2FA off,"
	Say "                              its failed sign-ins cleared (asks first; -NewPassword: a temporary one)"
	Say ""
	Say "  -NoBrowser   don't open the browser"
	Say "  Installing and uninstalling: NetRollout Setup / Settings -> Apps."
	Say "  Day to day: NetRollout Manager (Start Menu, desktop, tray)."
}

function Invoke-NrCommand([string]$Name) {
	switch ($Name) {
		"install" { Invoke-Install; return 0 }
		"start" { Invoke-Start; return 0 }
		"stop" { Invoke-Stop; return 0 }
		"backup" { Invoke-Backup; return 0 }
		"restore" { Invoke-Restore; return 0 }
		"prepare-update" { Invoke-PrepareUpdate; return 0 }
		"update" { Invoke-Update; return 0 }
		"apply" { Invoke-Apply; return 0 }
		"port" { Invoke-PortByHand; return 0 }
		"reset-admin" { Invoke-ResetAdmin; return 0 }
		"rollouts" {
			Assert-Installed
			if (-not (Test-DockerReady)) { return 1 }
			$list = Get-Rollouts -Json:$Json
			if ($null -eq $list) { return 1 }
			if ($Json -and -not $list) { return 1 }
			if ($list) { Write-Host $list } elseif (-not $Json) { Say "No rollout running or queued." }
			return 0
		}
		"port-check" {
			# NetRollout Manager (-Json): whether nginx holds its HTTPS port, else who does
			Assert-Installed
			if (-not (Test-DockerReady)) { return 1 }
			$check = Get-PortCheck
			if ($Json) {
				Write-Host (ConvertTo-Json -Compress @{ port = $check.Port; published = $check.Published; owner = $check.Owner })
			} elseif ($check.Published) {
				Good "Port $($check.Port): published by NetRollout's nginx."
			} elseif ($check.Owner) {
				Warn (Get-PortTakenText $check.Port $check.Owner)
			} else {
				Warn "Port $($check.Port): not published (NetRollout isn't running)."
			}
			return 0
		}
		"stop-now" {
			Assert-Installed
			if (-not (Test-DockerReady) -or -not (Request-StopNow)) { return 1 }
			return 0
		}
		"uninstall" { Invoke-Uninstall; return 0 }
		"forget-kept" { Invoke-ForgetKept; return 0 }
		"clear-damaged" { Invoke-ClearDamaged; return 0 }
		"ensure-docker" { Confirm-Docker -OfferInstall; Good "Docker is running."; return 0 }
		"defaults" { Write-Defaults; return 0 }
		"status" { return (Invoke-Status) }
		"open" { Assert-Installed; Start-Process (Get-Address) | Out-Null; return 0 }
		"logs" {
			Assert-Installed
			Confirm-Docker
			$name = if ($Service) { $Service } else { "app" }
			Write-Host (Compose @("logs", "--tail", "100", "--no-log-prefix", $name)).Output
			return 0
		}
		{ $_ -in "help", "-h", "--help", "/?" } { Show-Help; return 0 }
		default { Show-Help; Fail "Unknown command: $Name" }
	}
}

# ── main ── (dot-sourced: only the functions are loaded, e.g. for tests)
if ($MyInvocation.InvocationName -eq ".") { return }
$exit = 0
try {
	Update-SessionPath
	if ($Command) { $exit = Invoke-NrCommand $Command } else { Show-Help }
} catch [System.OperationCanceledException] {
	$exit = [int]$_.Exception.Message
} catch {
	Write-Host ""
	Write-Host "Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
	$exit = 1
}
exit $exit
