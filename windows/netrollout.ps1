<#
NetRollout management (Windows). Run through netrollout.bat / install.bat,
or the "NetRollout Manager" shortcut (no command: a numbered menu).

  netrollout install | start | stop | status | open | logs [service] | help

The install folder is this script's parent folder. The script does what needs
this computer (checks, Docker, the shortcuts) and leaves the thinking to the
setup core inside the app image (python -m src.setup, docs/plans/stage-9.md).
PowerShell 5.1 compatible.
#>
param(
	[Parameter(Position = 0)][string]$Command = "",
	[Parameter(Position = 1)][string]$Service = "",
	[switch]$NoBrowser,
	[switch]$Yes,          # unattended: the licence accepted, defaults answered
	[switch]$NoShortcuts,  # install: no desktop / Start Menu shortcuts (scripted installs)
	[switch]$PauseAtEnd,   # set by install.bat: keep a double-clicked window open
	# install's answers, given (the Setup wizard asks them on its own pages)
	[string]$Hostname = "",
	[string]$HttpsPort = "",
	[string]$Monitoring = "",       # y / n
	[string]$OrgCertificate = "",   # y / n: fullchain.pem + privkey.pem already in certs\
	[string]$TimeZone = "",
	[string]$Out = "",              # defaults: the file to write them to
	[switch]$DeleteData,            # uninstall: delete the data too (no question)
	[switch]$KeepData               # uninstall: keep it (no question)
)

Set-StrictMode -Version 2
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
# (the Setup wizard runs some commands from a temporary copy, before
# NetRollout's files are in place: no VERSION there)
$VersionFile = Join-Path $Root "VERSION"
$Version = if (Test-Path $VersionFile) { (Get-Content -Raw $VersionFile).Trim() } else { "" }
# A different project name only for testing next to a running NetRollout
$Project = if ($env:NETROLLOUT_PROJECT) { $env:NETROLLOUT_PROJECT } else { "netrollout" }
$AppImage = "itamarweinstein/netrollout:$Version"
$EnvFile = Join-Path $Root ".env"
$Interactive = [Environment]::UserInteractive -and -not $Yes
$DockerDesktopExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"

# ── output ──────────────────────────────────────────────────────────────────

function Say([string]$Text) { Write-Host $Text }
function Step([string]$Text) { Write-Host "-> $Text" -ForegroundColor Cyan }
function Good([string]$Text) { Write-Host "   $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "   $Text" -ForegroundColor Yellow }
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

function Compose([string[]]$Arguments) {
	$env:NETROLLOUT_VERSION = $Version
	return Invoke-Native "docker" (@("compose", "-p", $Project, "--project-directory", $Root,
		"--env-file", $EnvFile) + $Arguments)
}

# The setup core in the app image, the install folder mounted. -Talk: on
# this console (questions and answers); -OnNetwork: next to the running app
# (status asks its health over the compose network, when there is one).
function Invoke-Setup([string[]]$Arguments, [switch]$Talk, [switch]$OnNetwork) {
	$run = @("run", "--rm", "-v", "${Root}:/install", "-e", "NETROLLOUT_HOME=/install")
	if ($OnNetwork -and (Invoke-Native "docker" @("network", "inspect", "${Project}_default")).Code -eq 0) {
		$run += @("--network", "${Project}_default")
	}
	$all = $run + @($AppImage, "python", "-m", "src.setup") + $Arguments
	if ($Talk) {
		# Start-Process: the console is shared, nothing captured (a function's
		# output would swallow the questions); PowerShell 5.1 doesn't quote
		$quoted = ($all[0..1] + "-it" + $all[2..($all.Count - 1)]) |
			ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }
		$p = Start-Process -FilePath "docker" -ArgumentList ($quoted -join " ") -NoNewWindow -Wait -PassThru
		return $p.ExitCode
	}
	$r = Invoke-Native "docker" $all
	if ($r.Output) { Write-Host $r.Output }
	return $r.Code
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

function Wait-Docker([int]$Seconds, [string]$Waiting) {
	Write-Host "   $Waiting" -NoNewline
	$end = (Get-Date).AddSeconds($Seconds)
	while ((Get-Date) -lt $end) {
		if ((Test-DockerCli) -and (Test-DockerRunning)) { Write-Host ""; return $true }
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
	if ((Test-DockerCli) -and (Test-DockerRunning)) { return }
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

function Test-Reachable([string]$Url) {
	Enable-LocalTls
	$port = Read-EnvValue "HTTPS_PORT" "443"
	try {
		$r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "https://127.0.0.1:$port/_netrollout/health"
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
	Step "Checking the ports"
	$busy = Get-BusyPorts (Get-OurPorts)
	Invoke-Setup (@("prepare-start", "--busy-ports", $busy) + (Get-Facts)) | Out-Null
	Step "Starting NetRollout (the first time takes a few minutes: the images are downloaded)"
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

# The desktop and the Start Menu ("NetRollout" folder): "NetRollout" opens the
# web app, "NetRollout Manager" the menu; both with NetRollout's icon
function Get-ShortcutFolders {
	return @([Environment]::GetFolderPath("Desktop"),
		(Join-Path ([Environment]::GetFolderPath("Programs")) "NetRollout"))
}

function New-Shortcuts {
	$icon = Join-Path $PSScriptRoot "netrollout.ico"
	$shell = New-Object -ComObject WScript.Shell
	foreach ($folder in Get-ShortcutFolders) {
		New-Item -ItemType Directory -Force -Path $folder | Out-Null
		Set-Content -Encoding ASCII -Path (Join-Path $folder "NetRollout.url") -Value @(
			"[InternetShortcut]", "URL=$(Get-Address)", "IconFile=$icon", "IconIndex=0")
		$link = $shell.CreateShortcut((Join-Path $folder "NetRollout Manager.lnk"))
		$link.TargetPath = Join-Path $PSScriptRoot "netrollout.bat"
		$link.WorkingDirectory = $Root
		$link.IconLocation = "$icon,0"
		$link.Description = "Start, stop and check NetRollout"
		$link.Save()
	}
}

# The licence notice: one text for this script and the Setup wizard's page
$LicenceNoticeFile = Join-Path $PSScriptRoot "licence-notice.txt"

function Invoke-Install {
	if (Test-Installed) {
		Fail "NetRollout is already installed in $Root - see: netrollout status (or netrollout update)." 2
	}
	Step "Checking this computer"
	if ((Get-CimInstance Win32_OperatingSystem).ProductType -ne 1) {
		Fail ("This is Windows Server, which Docker Desktop doesn't support. Use Windows 10/11 " +
		      "(a VM is fine), or a Linux server with linux/install.sh.")
	}
	if (-not ((Test-DockerCli) -and (Test-DockerRunning))) {
		$hypervisor = (Get-CimInstance Win32_ComputerSystem).HypervisorPresent
		$firmware = @(Get-CimInstance Win32_Processor | Where-Object { $_.VirtualizationFirmwareEnabled }).Count -gt 0
		if (-not ($hypervisor -or $firmware)) {
			Fail ("Virtualization is off, and Docker needs it. On a PC: turn on Intel VT-x / AMD-V " +
			      "in the BIOS/UEFI. On a VM: enable nested virtualization (Hyper-V: " +
			      "Set-VMProcessor -ExposeVirtualizationExtensions `$true; VMware: 'Virtualize " +
			      "Intel VT-x/EPT'). Then run install again.")
		}
	}
	if (-not $Yes) {
		Write-Host ""
		Write-Host (Get-Content -Raw $LicenceNoticeFile)
		if ((Read-Host "Type yes to accept and continue") -ne "yes") {
			Fail "The licence terms weren't accepted - nothing was installed."
		}
	}
	Confirm-Docker -OfferInstall
	Step "Getting NetRollout $Version"
	if ((Invoke-Native "docker" @("image", "inspect", $AppImage)).Code -ne 0) {
		$r = Invoke-Native "docker" @("pull", $AppImage)
		if ($r.Code -ne 0) { Write-Host $r.Output; Fail "Couldn't download $AppImage - check the internet connection." }
	}
	Step "Setting up"
	$setup = @("init", "--licence-accepted", "--busy-ports", (Get-BusyPorts)) + (Get-Facts)
	foreach ($given in @(@("--hostname", $Hostname), @("--https-port", $HttpsPort),
	                     @("--monitoring", $Monitoring), @("--org-certificate", $OrgCertificate),
	                     @("--timezone", $TimeZone))) {
		if ($given[1]) { $setup += $given }
	}
	if ($Yes) { $setup += "--defaults"; $code = Invoke-Setup $setup }
	else { $code = Invoke-Setup $setup -Talk }
	if ($code -ne 0) { Fail "Setup stopped - nothing was started." $code }
	Restrict $EnvFile
	Restrict (Join-Path $Root "backups")
	Start-NetRollout
	if (-not $NoShortcuts) { New-Shortcuts }
	Say ""
	Good "Installed. Open $(Get-Address) and sign in as admin / admin - you'll set a new password."
	if (-not $NoShortcuts) {
		Say "   Desktop: 'NetRollout' opens it, 'NetRollout Manager' starts, stops and checks it."
	}
	Say "   NetRollout starts with Docker Desktop when someone signs in to Windows: on a server,"
	Say "   set up automatic sign-in for the account that runs it (README: 'After a reboot')."
	Open-Browser
}

function Invoke-Status {
	if (-not (Test-Installed)) { Fail "NetRollout isn't installed in $Root - run install." }
	Confirm-Docker
	$reachable = if (Test-Reachable) { "yes" } else { "no" }
	$busy = Get-BusyPorts (Get-OurPorts)
	return Invoke-Setup (@("status", "--containers", (Get-ContainerStates), "--reachable", $reachable,
		"--busy-ports", $busy) + (Get-Facts)) -OnNetwork
}

function Invoke-Stop {
	if (-not (Test-Installed)) { Fail "NetRollout isn't installed in $Root." }
	if (-not ((Test-DockerCli) -and (Test-DockerRunning))) { Good "NetRollout isn't running (Docker isn't)."; return }
	try {
		Enable-LocalTls
		$port = Read-EnvValue "HTTPS_PORT" "443"
		$h = Invoke-RestMethod -TimeoutSec 5 "https://127.0.0.1:$port/_netrollout/health"
		if ($h.rollouts.running -gt 0) {
			Warn "$($h.rollouts.running) rollout(s) running - they finish and are recorded first (up to 10 minutes)."
		}
	} catch { }
	Step "Stopping NetRollout"
	$r = Compose @("stop")
	if ($r.Code -ne 0) { Write-Host $r.Output; Fail "Docker couldn't stop it - see above." }
	Good "Stopped. Start it again with: netrollout start"
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
		"docker=$(if ((Test-DockerCli) -and (Test-DockerRunning)) { 'running' } elseif (Test-DockerCli) { 'installed' } else { 'missing' })",
		"", "[busy]")
	foreach ($p in ($busy.Keys | Sort-Object)) { $lines += "$p=$(if ($busy[$p]) { $busy[$p] } else { 'another program' })" }
	Set-Content -Encoding ASCII -Path $Out -Value $lines
}

function Invoke-Uninstall {
	if (-not (Test-Installed)) { Good "NetRollout isn't installed in $Root - nothing to remove." }
	$delete = [bool]$DeleteData
	if (-not $DeleteData -and -not $KeepData -and $Interactive -and (Test-Installed)) {
		$a = Read-Host ("Also delete NetRollout's data - the database, settings, certificates, " +
		                "logs and backups? This can't be undone. [y/N]")
		$delete = $a -match "^(y|yes)$"
	}
	if ((Test-Installed) -and (Test-DockerCli) -and (Test-DockerRunning)) {
		Step "Removing NetRollout's containers$(if ($delete) { ' and its data' })"
		$down = @("down", "--remove-orphans")
		if ($delete) { $down += "-v" }
		$r = Compose $down
		if ($r.Code -ne 0) { Write-Host $r.Output; Warn "Docker couldn't remove everything - see above." }
	} elseif ($delete) {
		Warn "Docker isn't running: the database volumes stay (Docker Desktop -> Volumes: netrollout_*)."
	}
	foreach ($folder in Get-ShortcutFolders) {
		foreach ($name in "NetRollout.url", "NetRollout Manager.lnk") {
			Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $folder $name)
		}
	}
	$menu = (Get-ShortcutFolders)[1]
	if ((Test-Path $menu) -and -not (Get-ChildItem $menu)) { Remove-Item -Force $menu }
	if ($delete) {
		foreach ($name in ".env", "config", "certs", "logs", "backups") {
			Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $Root $name)
		}
		Good "NetRollout and its data are removed."
	} else {
		Good "NetRollout is removed. Its data stays (the database volumes, and .env, config, certs,"
		Good "logs, backups in $Root): installing again in this folder picks it up."
	}
}

function Show-Help {
	Say "NetRollout $Version - $Root"
	Say ""
	Say "  netrollout install          install NetRollout in this folder"
	Say "  netrollout start            start it (and open it in the browser)"
	Say "  netrollout stop             stop it (running rollouts finish first)"
	Say "  netrollout status           is everything well? what to do if not"
	Say "  netrollout open             open it in the browser"
	Say "  netrollout logs [service]   recent log lines (app, nginx, postgres, ...)"
	Say "  netrollout uninstall        remove it (asks whether to delete the data too)"
	Say "  netrollout                  the menu"
	Say ""
	Say "  -NoBrowser   don't open the browser    -Yes   unattended (accepts the licence, defaults)"
	Say "  -NoShortcuts install without the desktop / Start Menu shortcuts"
}

$MenuItems = [ordered]@{
	"1" = @("Open NetRollout in the browser", "open")
	"2" = @("Status", "status")
	"3" = @("Start", "start")
	"4" = @("Stop", "stop")
	"5" = @("Logs", "logs")
	"0" = @("Exit", "")
}

function Show-Menu {
	$Host.UI.RawUI.WindowTitle = "NetRollout Manager"
	while ($true) {
		Clear-Host
		$state = "not installed"
		if (Test-Installed) {
			$state = if ((Test-DockerCli) -and (Test-DockerRunning) -and (Test-Reachable)) { "running" } else { "not running" }
			Say " NetRollout $Version  -  $(Get-Address)   [$state]"
		} else {
			Say " NetRollout $Version   [$state - run install.bat]"
		}
		Say ""
		foreach ($key in $MenuItems.Keys) { Say "  $key  $($MenuItems[$key][0])" }
		Say ""
		$choice = (Read-Host " Choose a number").Trim()
		if ($choice -eq "0") { return }
		if (-not $MenuItems.Contains($choice)) { continue }
		Say ""
		try { Invoke-NrCommand $MenuItems[$choice][1] | Out-Null }
		catch [System.OperationCanceledException] { }
		Say ""
		Read-Host " Press Enter to return to the menu" | Out-Null
	}
}

function Invoke-NrCommand([string]$Name) {
	switch ($Name) {
		"install" { Invoke-Install; return 0 }
		"start" {
			if (-not (Test-Installed)) { Fail "NetRollout isn't installed in $Root - run install." }
			Start-NetRollout; Open-Browser; return 0
		}
		"stop" { Invoke-Stop; return 0 }
		"uninstall" { Invoke-Uninstall; return 0 }
		"ensure-docker" { Confirm-Docker -OfferInstall; Good "Docker is running."; return 0 }
		"defaults" { Write-Defaults; return 0 }
		"status" { return (Invoke-Status) }
		"open" { if (-not (Test-Installed)) { Fail "NetRollout isn't installed in $Root." }; Start-Process (Get-Address) | Out-Null; return 0 }
		"logs" {
			if (-not (Test-Installed)) { Fail "NetRollout isn't installed in $Root." }
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
	if ($Command) { $exit = Invoke-NrCommand $Command } else { Show-Menu }
} catch [System.OperationCanceledException] {
	$exit = [int]$_.Exception.Message
} catch {
	Write-Host ""
	Write-Host "Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
	$exit = 1
}
if ($PauseAtEnd) { Write-Host ""; Read-Host "Press Enter to close" | Out-Null }
exit $exit
