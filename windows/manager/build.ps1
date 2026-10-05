<#
Builds "NetRollout Manager.exe" into windows\ with the C# compiler that ships
with Windows (.NET Framework 4.8) — no SDK. CI and the release build run it;
the .exe isn't committed.

  powershell -ExecutionPolicy Bypass -File windows\manager\build.ps1
#>
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$windows = Split-Path -Parent $here
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "The .NET Framework 4 C# compiler isn't here: $csc" }
$version = (Get-Content -Raw (Join-Path (Split-Path -Parent $windows) "VERSION")).Trim()
# the file version needs numbers: 1.0.0.dev0 -> 1.0.0.0
$numeric = (($version -replace "[^0-9.].*$", "").TrimEnd(".") -split "\.") + @("0", "0", "0", "0")
$fileVersion = ($numeric[0..3]) -join "."
$info = Join-Path $env:TEMP "NetRolloutManager.Version.cs"
Set-Content -Encoding UTF8 -Path $info -Value @(
	"[assembly: System.Reflection.AssemblyVersion(""$fileVersion"")]",
	"[assembly: System.Reflection.AssemblyFileVersion(""$fileVersion"")]",
	"[assembly: System.Reflection.AssemblyInformationalVersion(""$version"")]")
$out = Join-Path $windows "NetRollout Manager.exe"
& $csc /nologo /target:winexe /optimize+ "/win32icon:$(Join-Path $windows 'netrollout.ico')" `
	"/out:$out" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
	(Join-Path $here "NetRolloutManager.cs") $info
if ($LASTEXITCODE -ne 0) { throw "csc failed ($LASTEXITCODE)" }
Remove-Item $info
Write-Host "Built: $out ($version)"
