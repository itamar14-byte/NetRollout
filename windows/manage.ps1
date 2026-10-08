<#
NetRollout's engine on Windows: NetRollout Setup, its uninstaller and
NetRollout Manager run it; admins can too (bin\netrollout.bat):

  netrollout start | stop | status | open | logs [service] | backup |
             restore <file> | apply | rollouts [-Json] | stop-now | help

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
	[switch]$KeepData,              # uninstall: keep it (no question)
	[switch]$DeleteBackups,         # uninstall with -DeleteData: the backups too (else kept)
	[switch]$NoSafetyBackup,        # restore: without backing up the current state
	[switch]$Json,                  # rollouts: one line of JSON (NetRollout Manager)
	# NetRollout Setup, updating: the installed folder (this copy runs from
	# Setup's temporary folder) and the version it brings
	[string]$InstallDir = "",
	[string]$NewVersion = ""
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
$EnvFile = Join-Path $Root ".env"
$Interactive = [Environment]::UserInteractive -and -not $Yes
$DockerDesktopExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
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
	$env:NETROLLOUT_VERSION = $Version
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
	return $run + @($AppImage, "python", "-m", "src.setup") + $Arguments
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
	return ($names.Keys | Sort-Object | ForEach-Object { "$_=$($names[$_])" }) -join ","
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

function Test-DockerCli { return [bool](Get-Command docker -ErrorAction SilentlyContinue) }
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
		$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
			[Environment]::GetEnvironmentVariable("Path", "User")
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
	if ((Invoke-Native "docker" @("image", "inspect", $AppImage)).Code -eq 0) { Step "Starting NetRollout" }
	else { Step "Starting NetRollout (the first time takes a few minutes: the images are downloaded)" }
	$r = Compose @("up", "-d", "--wait", "--wait-timeout", "600")
	if ($r.Code -ne 0) {
		Write-Host $r.Output
		$logs = Compose @("logs", "--tail", "30", "app")
		Write-Host $logs.Output
		Fail "NetRollout didn't start - above: what Docker said and the app's last lines."
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
	Step "Getting NetRollout $Version"
	Get-OurImage $AppImage
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
	Confirm-Docker
	$reachable = if (Test-Reachable) { "yes" } else { "no" }
	$busy = Get-BusyPorts (Get-OurPorts)
	return Invoke-Setup (@("status", "--containers", (Get-ContainerStates), "--reachable", $reachable,
		"--busy-ports", $busy) + (Get-Facts)) -OnNetwork
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
	Assert-Installed
	if (-not (Test-DockerReady)) { Good "NetRollout isn't running (Docker isn't)."; return }
	if (-not (Confirm-Rollouts "stop")) { Good "Not stopped - NetRollout keeps running."; return }
	Step "Stopping NetRollout"
	$r = Compose @("stop")
	if ($r.Code -ne 0) { Write-Host $r.Output; Fail "Docker couldn't stop it - see above." }
	Good "Stopped. Start it again with: netrollout start"
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
		if ($r.Code -ne 0) {
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
		Good "Restored $shown. Everyone signs in again."
	} finally {
		if ($staged) { Remove-Item -Force -ErrorAction SilentlyContinue $staged }
	}
}

# ── updates (NetRollout Setup over an install) ───────────────────────────────

# Before Setup replaces any file - run from Setup's temporary folder with
# -InstallDir: the direction (the installed version's image decides), then
# a backup by the installed version. Exit 2: refused, nothing changed.
function Invoke-PrepareUpdate {
	Assert-Installed
	if (-not $NewVersion) { Fail "prepare-update needs -NewVersion" }
	Confirm-Docker
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
}

# After Setup replaced the files (this is the new script): the new images
# while the old version keeps running, .env brought up to date, the restart.
function Invoke-Update {
	Assert-Installed
	Confirm-Docker
	Step "Downloading NetRollout $Version (it keeps running meanwhile)"
	# failures of others' images surface at the start; ours are checked here
	# (a local build isn't on Docker Hub)
	Compose @("pull", "--quiet", "--ignore-pull-failures") | Out-Null
	foreach ($image in "netrollout", "netrollout-nginx") { Get-OurImage "itamarweinstein/${image}:$Version" }
	Step "Updating the settings"
	if ((Invoke-Setup @("upgrade")) -ne 0) { Fail "The update stopped before the restart - see above." }
	Confirm-Rollouts "update" | Out-Null      # the files are in place: shown, never refused
	Step "Restarting on the new version (about a minute; running rollouts finish first)"
	Start-NetRollout
	Start-PortHelper
	Good "Updated to NetRollout $Version. Everyone signs in again."
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
						Get-SetupAnswer @("port-close", "--outcome", "rollback", "--id", $id, "--timed-out") | Out-Null
						Update-Nginx | Out-Null
						Warn "Port $port wasn't confirmed within 2 minutes (it didn't open from a browser - a firewall?) - NetRollout stays on port $current."
						return
					}
				}
				"try" {
					Step "Opening port $port next to port $current"
					Get-SetupAnswer @("port-open", "--port", $port) | Out-Null
					$up = Update-Nginx
					if ($up.Code -ne 0) {
						Show-Output $up.Output     # what Docker said (the helper's log)
						$why = (($up.Output -split "`n" | Where-Object { $_ -match "error|failed|allocated" }) | Select-Object -First 1)
						if (-not $why) { $why = "nginx didn't start with it" }
						Get-SetupAnswer @("port-close", "--outcome", "failed", "--id", $id, "--message", "port ${port}: $($why.Trim())") | Out-Null
						Update-Nginx | Out-Null
						Warn "Port $port couldn't be opened: $($why.Trim()) - port $current stays."
						return
					}
					Get-SetupAnswer @("port-trying", "--port", $port, "--id", $id) | Out-Null
					Good "Port $port is open next to port $current. Open NetRollout on port $port within 2 minutes to keep it - else port $current stays."
				}
				"keep" {
					Get-SetupAnswer @("port-close", "--outcome", "keep", "--id", $id) | Out-Null
					Update-Nginx | Out-Null
					Good "Port $port kept - NetRollout is at $(Get-Address)."   # then: anything newer?
				}
				"rollback" {
					Get-SetupAnswer @("port-close", "--outcome", "rollback", "--id", $id, "--message", $message) | Out-Null
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
	$lines = @("[defaults]", "hostname=$name", "https_port=$port",
		"timezone=$((Get-TimeZone).Id)", "port80_busy=$(if ($busy.ContainsKey(80)) { $busy[80] } else { '' })",
		"docker=$(if (Test-DockerReady) { 'running' } elseif (Test-DockerCli) { 'installed' } else { 'missing' })",
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
		$a = Read-Host ("Also delete NetRollout's data - the database, settings, certificates " +
		                "and logs? This can't be undone. [y/N]")
		$delete = $a -match "^(y|yes)$"
		if ($delete) {
			$a = Read-Host "Keep the backups (the backups folder)? They're the last copy of the data. [Y/n]"
			$deleteBackups = $a -match "^(n|no)$"
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
	Say "  netrollout rollouts         the rollouts running or queued (-Json: as JSON)"
	Say "  netrollout stop-now         a stop under way (or the next) cancels them at once"
	Say ""
	Say "  -NoBrowser   don't open the browser"
	Say "  Installing and uninstalling: NetRollout Setup / Settings -> Apps."
	Say "  Day to day: NetRollout Manager (Start Menu, desktop, tray)."
}

function Invoke-NrCommand([string]$Name) {
	switch ($Name) {
		"install" { Invoke-Install; return 0 }
		"start" {
			Assert-Installed -Setup
			Start-NetRollout; Start-PortHelper; Open-Browser; return 0
		}
		"stop" { Invoke-Stop; return 0 }
		"backup" { Invoke-Backup; return 0 }
		"restore" { Invoke-Restore; return 0 }
		"prepare-update" { Invoke-PrepareUpdate; return 0 }
		"update" { Invoke-Update; return 0 }
		"apply" { Invoke-Apply; return 0 }
		"rollouts" {
			Assert-Installed
			if (-not (Test-DockerReady)) { return 1 }
			$list = Get-Rollouts -Json:$Json
			if ($null -eq $list) { return 1 }
			if ($Json -and -not $list) { return 1 }
			if ($list) { Write-Host $list } elseif (-not $Json) { Say "No rollout running or queued." }
			return 0
		}
		"stop-now" {
			Assert-Installed
			if (-not (Test-DockerReady) -or -not (Request-StopNow)) { return 1 }
			return 0
		}
		"uninstall" { Invoke-Uninstall; return 0 }
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
	if ($Command) { $exit = Invoke-NrCommand $Command } else { Show-Help }
} catch [System.OperationCanceledException] {
	$exit = [int]$_.Exception.Message
} catch {
	Write-Host ""
	Write-Host "Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
	$exit = 1
}
exit $exit
