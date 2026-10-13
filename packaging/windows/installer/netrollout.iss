; NetRollout-Setup-<version>.exe — the Windows installer (Inno Setup 6), the
; only way NetRollout is installed on Windows.
;
;   iscc packaging\windows\installer\netrollout.iss      (from the repo root; build the
;                                               Manager first: packaging\windows\manager\build.ps1)
;
; The wizard asks; bin\manage.ps1 does the work (install -Yes with the
; answers as parameters). The installed folder: bin\ (the Manager, the engine,
; the command line, the icon), deploy\, the compose files, VERSION, LICENSE,
; the uninstaller; the install creates .env (hidden), config\, certs\, logs\,
; backups\. Overview: docs/architecture.md §9.
;
; Over an install it updates (9.6): only the review page ("Update X -> Y");
; before any file is replaced the script checks the direction (an older
; Setup is refused) and backs up; after the files, it downloads the new
; images while the old version runs, brings .env up to date and restarts.
; NetRollout Manager's Update runs it with /SILENT (and opens again after it).

#define Root AddBackslash(SourcePath) + "..\..\.."
#define AppVersion Trim(FileRead(FileOpen(Root + "\VERSION")))
#define Repo "https://github.com/itamar14-byte/NetRollout"
; The install's identity: Windows finds it by this id (Settings -> Apps, the
; install record Setup reads for an update) - never change it
#define AppGuid "6C1F0E52-9B47-4E1B-A7D3-5E2C8F41B0A9"
#define DefaultDir "C:\NetRollout"
; where the uninstaller records the folder whose data it kept (value KeptData);
; the Manager keeps its own settings in the Manager subkey
#define OurKey "Software\NetRollout"

[Setup]
AppId={{{#AppGuid}}
AppName=NetRollout
AppVersion={#AppVersion}
AppVerName=NetRollout {#AppVersion}
AppPublisher=Itamar Weinstein
AppPublisherURL={#Repo}
AppSupportURL={#Repo}/issues
AppUpdatesURL={#Repo}/releases
AppCopyright=GNU AGPL v3
VersionInfoDescription=NetRollout Setup
DefaultDirName={#DefaultDir}
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
UsePreviousAppDir=yes
; a folder holding NetRollout's kept data is announced in our own words
; (CheckKept); any other existing folder is fine to install into
DirExistsWarning=no
; per user: no UAC prompt for NetRollout itself (Docker Desktop's installer
; asks for its own)
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; windows11: Inno draws the controls itself (the native check boxes are clipped at 200% DPI); dynamic: follows Windows' light / dark mode
WizardStyle=modern dynamic windows11 includetitlebar
WizardImageFile=wizard.bmp,wizard-200.bmp
WizardSmallImageFile=wizard-small.png,wizard-small-200.png
; dark mode uses its own images: the same ones (they are dark already)
WizardImageFileDynamicDark=wizard.bmp,wizard-200.bmp
WizardSmallImageFileDynamicDark=wizard-small.png,wizard-small-200.png
SetupIconFile=..\netrollout.ico
UninstallDisplayIcon={app}\bin\netrollout.ico
UninstallDisplayName=NetRollout
LicenseFile=licence-notice.txt
OutputDir={#Root}\dist
OutputBaseFilename=NetRollout-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
; PATH changes reach new terminals without signing out
ChangesEnvironment=yes

[Messages]
WelcomeLabel2=This installs NetRollout {#AppVersion} — push configuration to many network devices at once, from your browser.%n%nNetRollout runs on Docker Desktop: if it isn't on this computer yet, Setup installs it.
SelectDirDesc=Where should NetRollout be installed?
SelectDirLabel3=NetRollout and its data (settings, certificates, logs, backups) will be kept in this folder.
FinishedLabel=NetRollout is installed and running.%n%nSign in as admin / admin — you'll set a new password at once. NetRollout Manager (Start Menu, desktop, tray) starts, stops and checks it.

[Tasks]
Name: desktopicons; Description: "Desktop shortcuts (NetRollout, NetRollout Manager)"
Name: trayatsignin; Description: "Start NetRollout Manager in the tray when I sign in (shows whether NetRollout is running)"
Name: addtopath; Description: "Add the netrollout command to PATH (for terminals: netrollout status, start, stop, logs)"

[Registry]
; Win+R -> netrollout opens NetRollout Manager (Windows' App Paths, per user)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\netrollout.exe"; ValueType: string; ValueName: ""; ValueData: "{app}\bin\NetRollout Manager.exe"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\netrollout.exe"; ValueType: string; ValueName: "Path"; ValueData: "{app}\bin"

[Files]
Source: "..\NetRollout Manager.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\manage.ps1"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\netrollout.bat"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\netrollout.ico"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "{#Root}\compose.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\compose.http.yaml"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\VERSION"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\deploy\initdb\10-netrollout.sh"; DestDir: "{app}\deploy\initdb"; Flags: ignoreversion
Source: "{#Root}\deploy\prometheus\prometheus.yml"; DestDir: "{app}\deploy\prometheus"; Flags: ignoreversion
Source: "{#Root}\deploy\loki\loki-config.yml"; DestDir: "{app}\deploy\loki"; Flags: ignoreversion
Source: "{#Root}\deploy\alloy\config.alloy"; DestDir: "{app}\deploy\alloy"; Flags: ignoreversion
Source: "{#Root}\deploy\grafana\provisioning\datasources\netrollout.yml"; DestDir: "{app}\deploy\grafana\provisioning\datasources"; Flags: ignoreversion
; for the wizard, before anything is installed (the checks, the defaults)
Source: "..\manage.ps1"; Flags: dontcopy
; the licence page: the notice, then the full licence (the repo's one file)
Source: "{#Root}\LICENSE"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\NetRollout\NetRollout Manager"; Filename: "{app}\bin\NetRollout Manager.exe"; WorkingDir: "{app}"; Comment: "Start, stop and check NetRollout"
Name: "{autodesktop}\NetRollout Manager"; Filename: "{app}\bin\NetRollout Manager.exe"; WorkingDir: "{app}"; Comment: "Start, stop and check NetRollout"; Tasks: desktopicons
Name: "{userstartup}\NetRollout Manager"; Filename: "{app}\bin\NetRollout Manager.exe"; Parameters: "--tray"; WorkingDir: "{app}"; Tasks: trayatsignin
; the port helper (headless): an HTTPS port saved in System Settings is applied by itself
Name: "{userstartup}\NetRollout port helper"; Filename: "{app}\bin\NetRollout Manager.exe"; Parameters: "--helper"; WorkingDir: "{app}"; Comment: "Applies an HTTPS port saved in NetRollout's System Settings"

[INI]
Filename: "{autoprograms}\NetRollout\NetRollout.url"; Section: "InternetShortcut"; Key: "URL"; String: "{code:Address}"
Filename: "{autoprograms}\NetRollout\NetRollout.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\bin\netrollout.ico"
Filename: "{autoprograms}\NetRollout\NetRollout.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"
Filename: "{autodesktop}\NetRollout.url"; Section: "InternetShortcut"; Key: "URL"; String: "{code:Address}"; Tasks: desktopicons
Filename: "{autodesktop}\NetRollout.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\bin\netrollout.ico"; Tasks: desktopicons
Filename: "{autodesktop}\NetRollout.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"; Tasks: desktopicons

[UninstallDelete]
Type: files; Name: "{autoprograms}\NetRollout\NetRollout.url"
Type: files; Name: "{autodesktop}\NetRollout.url"
Type: dirifempty; Name: "{autoprograms}\NetRollout"
Type: dirifempty; Name: "{app}\bin"
Type: dirifempty; Name: "{app}"

[Run]
; the port helper now, not only from the next sign-in (also after an update closed it)
Filename: "{app}\bin\NetRollout Manager.exe"; Parameters: "--helper"; WorkingDir: "{app}"; Flags: nowait; Check: SetUpOk
; the tray now, not only from the next sign-in (also after an update closed it)
Filename: "{app}\bin\NetRollout Manager.exe"; Parameters: "--tray"; WorkingDir: "{app}"; Flags: nowait; Tasks: trayatsignin; Check: SetUpOk
; the finished page's "Open NetRollout in the browser" / "Open NetRollout
; Manager" are our own check boxes (FinishBoxes): Inno's list of [Run]
; entries for the finished page draws half-themed in dark mode

[Code]
var
	DockerPage, SettingsPage: TWizardPage;
	DockerState, DockerHint, PortHint: TNewStaticText;
	HostnameEdit, PortEdit, CertEdit, KeyEdit: TNewEdit;
	TimezoneBox: TNewComboBox;
	SetUpFailed: Boolean;
	SetUpCode: Integer;
	SetUpLog: String;
	TimezoneIds: TArrayOfString;
	MonitoringBox, OrgCertBox: TNewCheckBox;
	{ the finished page's (FinishBoxes) }
	BrowserBox, ManagerBox: TNewCheckBox;
	CertButton, KeyButton: TNewButton;
	DefaultsFile: String;
	Reinstall: Boolean;
	{ over an installed NetRollout (Windows' record of it + its VERSION) }
	UpdateMode: Boolean;
	{ over the data an uninstall kept (.env; no record, no VERSION) of an
	  older version - or one that doesn't say: updated, a backup first }
	KeptUpdate: Boolean;
	InstalledDir, InstalledVersion: String;
	{ the same version installed, its update unfinished (.update-pending):
	  Setup runs only the update's second half }
	Resume: Boolean;
	{ the folder holding kept data found at Setup's start (pre-selected), and
	  the last folder CheckKept announced (not announced twice) }
	KeptDir, AnnouncedDir: String;
	{ the folder page over kept data: its note, "Start fresh instead", and
	  whether that was chosen (and confirmed): the kept data is deleted first }
	KeptNote, FreshHint: TNewStaticText;
	FreshBox: TNewCheckBox;
	FreshStart: Boolean;
	{ the project's Docker volumes with no folder holding their .env (it was
	  deleted by hand after an uninstall that kept the data): asked about
	  (AskLeftVolumes); Yes deletes them first (PrepareToInstall) }
	LeftVolumes: Boolean;
	{ those volumes are in a folder Setup doesn't know (AskLeftVolumes: No):
	  the folder page asks for it (LeftFolderPage) }
	LeftSearch: Boolean;
	{ Inno's own Browse (LeftFolderPage replaces it while looking for a folder) }
	InstallBrowse: TNotifyEvent;
	{ Setup closes without its "Exit Setup?" question (No to the volumes) }
	QuietClose: Boolean;
	{ the update's preparation, shown while it runs (the Preparing page has no
	  progress bar of its own) }
	PreparePage: TOutputProgressWizardPage;
	{ a damaged install (FindDamaged) Setup clears before installing again
	  (AskDamaged: OK), its folder, what's missing there in words, and whether
	  its data is kept (AskDamaged: Yes) }
	Damaged, DamagedKeep: Boolean;
	DamagedDir, DamageWords: String;

{ Windows PowerShell by its full path: Setup may be started with a PATH that
  lacks its folder (a program launched before an install changed PATH, or a
  stripped one) - a bare powershell.exe then isn't found and no check runs
  (rc2 test: Docker reported missing). 64-bit mode: the sys constant is System32. }
function PowerShellExe: String;
begin
	Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

{ powershell.exe's arguments: run Script with CommandLine }
function PsArgs(const Script, CommandLine: String): String;
begin
	Result := '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" ' + CommandLine;
end;

function Ps(const Command, Extra: String): String;
begin
	Result := PsArgs(ExpandConstant('{tmp}\manage.ps1'), Command + ' ' + Extra);
end;

{ The script's facts about this computer: defaults, busy ports, Docker, the
  timezones, and whether NetRollout can run here at all }
procedure LoadDefaults;
var Code: Integer;
begin
	ExtractTemporaryFile('manage.ps1');
	DefaultsFile := ExpandConstant('{tmp}\defaults.ini');
	Exec(PowerShellExe, Ps('defaults', '-Out "' + DefaultsFile + '"'), '', SW_HIDE,
		ewWaitUntilTerminated, Code);
end;

function GetDefault(const Key, Fallback: String): String;
begin
	Result := GetIniString('defaults', Key, Fallback, DefaultsFile);
end;

{ Windows' record of the install (this AppId's): where it is, its uninstaller }
function InstallRecord: String;
begin
	Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + '{{#AppGuid}}_is1';
end;

{ Words as a list: "a", "a and b", "a, b and c" }
function JoinWords(const Words: TArrayOfString): String;
var I, N: Integer;
begin
	Result := '';
	N := GetArrayLength(Words);
	for I := 0 to N - 1 do
		if I = 0 then Result := Words[I]
		else if I = N - 1 then Result := Result + ' and ' + Words[I]
		else Result := Result + ', ' + Words[I];
end;

procedure AddWord(var Words: TArrayOfString; const Word: String);
begin
	SetArrayLength(Words, GetArrayLength(Words) + 1);
	Words[GetArrayLength(Words) - 1] := Word;
end;

{ An install Windows has a record of, its folder damaged - files deleted by
  hand (the rc2 test: only bin\ was left, held by the running Manager): no
  uninstaller, no script, so it could be neither updated nor removed. What a
  healthy install always has: its folder, the uninstaller the record names,
  VERSION and compose.yaml - and .env once it's set up: without it while
  Docker holds the project's volumes (kept_volumes, known only when Docker
  answers) the data lost its passwords and key. No .env and no volumes is a
  set-up that never finished: installed again as usual. DamagedDir and
  DamageWords (what's missing, in words) say which. }
function FindDamaged: Boolean;
var Dir, Uninstaller: String; Missing: TArrayOfString; EnvLost: Boolean;
begin
	Result := False;
	if not RegQueryStringValue(HKCU, InstallRecord, 'InstallLocation', Dir) then exit;
	Dir := RemoveBackslashUnlessRoot(Dir);
	if Dir = '' then exit;
	if not DirExists(Dir) then
		DamageWords := 'its folder is gone'
	else begin
		if not RegQueryStringValue(HKCU, InstallRecord, 'UninstallString', Uninstaller) then
			Uninstaller := AddBackslash(Dir) + 'unins000.exe';
		Uninstaller := RemoveQuotes(Uninstaller);
		if not FileExists(Uninstaller) then AddWord(Missing, 'its uninstaller');
		if not FileExists(AddBackslash(Dir) + 'VERSION') then AddWord(Missing, 'its VERSION file');
		if not FileExists(AddBackslash(Dir) + 'compose.yaml') then AddWord(Missing, 'its compose.yaml');
		EnvLost := not FileExists(AddBackslash(Dir) + '.env') and (GetDefault('kept_volumes', '') <> '');
		if EnvLost then AddWord(Missing, 'its settings file (.env)');
		if GetArrayLength(Missing) = 0 then exit;
		if GetArrayLength(Missing) = 1 then DamageWords := Missing[0] + ' is missing'
		else DamageWords := JoinWords(Missing) + ' are missing';
		if EnvLost then DamageWords := DamageWords + ', while its data is still in Docker';
	end;
	DamagedDir := Dir;
	Result := True;
end;

{ An installed NetRollout: where (Windows' record of the install) and which
  version (its VERSION file) }
procedure FindInstalled;
var Dir: String; Version: AnsiString;
begin
	UpdateMode := False;
	if not RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
			'{{#AppGuid}}_is1', 'InstallLocation', Dir) then exit;
	Dir := RemoveBackslashUnlessRoot(Dir);
	if not (FileExists(Dir + '\.env') and LoadStringFromFile(Dir + '\VERSION', Version)) then exit;
	InstalledDir := Dir;
	InstalledVersion := Trim(String(Version));
	UpdateMode := True;
	Reinstall := True;
end;

{ An update to Version that didn't finish in Dir: the marker manage.ps1
  writes before an update's second half (removed once it worked) names it
  ("version=X"; another version: a stale marker, nothing pending) }
function UpdatePending(const Dir, Version: String): Boolean;
var Lines: TArrayOfString; I: Integer;
begin
	Result := False;
	if LoadStringsFromFile(AddBackslash(Dir) + '.update-pending', Lines) then
		for I := 0 to GetArrayLength(Lines) - 1 do
			if Lines[I] = 'version=' + Version then Result := True;
end;

{ The digits at the start of S, as N (removed from S); False: none }
function TakeNumber(var S: String; var N: Integer): Boolean;
var I: Integer;
begin
	I := 1;
	while (I <= Length(S)) and (S[I] >= '0') and (S[I] <= '9') do I := I + 1;
	Result := I > 1;
	if Result then begin
		N := StrToInt(Copy(S, 1, I - 1));
		Delete(S, 1, I - 1);
	end;
end;

{ A version as a key that sorts like the versions do (PEP 440, as the setup
  core and the Manager order them: 1.0.0.dev2 < 1.0.0rc1 < 1.0.0 < 1.0.1);
  '' when it isn't one of ours (then the setup core decides, later) }
function VersionKey(const Version: String): String;
var S: String; A, B, C, Stage, N: Integer;
begin
	Result := '';
	S := Lowercase(Trim(Version));
	if Copy(S, 1, 1) = 'v' then Delete(S, 1, 1);
	if not TakeNumber(S, A) or (Copy(S, 1, 1) <> '.') then exit;
	Delete(S, 1, 1);
	if not TakeNumber(S, B) or (Copy(S, 1, 1) <> '.') then exit;
	Delete(S, 1, 1);
	if not TakeNumber(S, C) then exit;
	Stage := 4; N := 0;             { a release }
	if S <> '' then begin
		if Copy(S, 1, 1) = '.' then Delete(S, 1, 1);
		if Copy(S, 1, 3) = 'dev' then begin Stage := 0; Delete(S, 1, 3); end
		else if Copy(S, 1, 2) = 'rc' then begin Stage := 3; Delete(S, 1, 2); end
		else if Copy(S, 1, 1) = 'a' then begin Stage := 1; Delete(S, 1, 1); end
		else if Copy(S, 1, 1) = 'b' then begin Stage := 2; Delete(S, 1, 1); end
		else exit;
		if not TakeNumber(S, N) or (S <> '') then exit;
	end;
	Result := Format('%.6d.%.6d.%.6d.%d.%.6d', [A, B, C, Stage, N]);
end;

{ A version newer than this Setup: no downgrades (the data may be upgraded) }
procedure SayDowngrade(const Installed, Dir: String);
begin
	SuppressibleMsgBox('NetRollout ' + Installed + ' is installed in ' + Dir + ' - newer than this Setup ' +
		'({#AppVersion}). Downgrades aren''t supported.' + #13#10#13#10 +
		'To run an earlier version: uninstall NetRollout, then run that version''s Setup - with your ' +
		'data from a backup made by that version (the current data may already be upgraded).',
		mbError, MB_OK, IDOK);
end;

{ Over an installed NetRollout only a newer Setup goes on (the update): the
  same version or an older one says so before the first page. False: stop.
  The same version whose update didn't finish goes on too: Setup finishes
  it (Resume). }
function NewerThanInstalled: Boolean;
var Installed, This: String;
begin
	Result := True;
	Installed := VersionKey(InstalledVersion);
	This := VersionKey('{#AppVersion}');
	if (Installed = '') or (This = '') then exit;
	if (This = Installed) and UpdatePending(InstalledDir, InstalledVersion) then
		Resume := True
	else if This = Installed then begin
		SuppressibleMsgBox('NetRollout ' + InstalledVersion + ' is already installed in ' + InstalledDir +
			' - this Setup is the same version. Nothing to update.', mbInformation, MB_OK, IDOK);
		Result := False;
	end else if This < Installed then begin
		SayDowngrade(InstalledVersion, InstalledDir);
		Result := False;
	end;
end;

{ S up to its first space }
function FirstWord(const S: String): String;
begin
	Result := S;
	if Pos(' ', Result) > 0 then Result := Copy(Result, 1, Pos(' ', Result) - 1);
end;

{ The version a kept .env was last set up by: its "# Updated to NetRollout X
  on ..." line (an update), else the installer's "# NetRollout X - written by
  the installer" header; '' when it has neither (an early install) }
function KeptVersion(const Dir: String): String;
var Lines: TArrayOfString; I: Integer; Updated, Header: String;
begin
	Updated := ''; Header := '';
	if LoadStringsFromFile(AddBackslash(Dir) + '.env', Lines) then
		for I := 0 to GetArrayLength(Lines) - 1 do begin
			if Pos('# Updated to NetRollout ', Lines[I]) = 1 then
				Updated := FirstWord(Copy(Lines[I], 25, Length(Lines[I])))
			else if (Pos('# NetRollout ', Lines[I]) = 1) and (Pos('written by the installer', Lines[I]) > 0) then
				Header := FirstWord(Copy(Lines[I], 14, Length(Lines[I])));
		end;
	if Updated <> '' then Result := Updated else Result := Header;
end;

{ A folder holding the data an uninstall kept (.env) - or a damaged
  install's, kept (AskDamaged: already said, AnnouncedDir) -, announced in words
  naming the folder (once per folder: AnnouncedDir): the same version is
  started as it is; an older one (or one .env doesn't name) is updated -
  KeptUpdate; a newer one is refused, nothing changed. False: refused. }
function CheckKept(const Dir: String): Boolean;
var Kept, Key, This, Held: String;
begin
	Result := True;
	KeptUpdate := False;
	Kept := KeptVersion(Dir);
	Key := VersionKey(Kept);
	This := VersionKey('{#AppVersion}');
	if Kept <> '' then Held := Dir + ' holds NetRollout ' + Kept + ' data kept by an uninstall'
	else Held := Dir + ' holds NetRollout data kept by an uninstall (an early ' +
		'version - it doesn''t say which)';
	if (Kept = '{#AppVersion}') or ((Key <> '') and (Key = This)) then begin
		if CompareText(Dir, AnnouncedDir) <> 0 then
			SuppressibleMsgBox(Held + '. It will be started as it was.', mbInformation, MB_OK, IDOK);
		AnnouncedDir := Dir;
		exit;
	end;
	{ newer: said without "kept by an uninstall" - a damaged install's data
	  kept (AskDamaged) is refused here too }
	if (Key <> '') and (This <> '') and (Key > This) then begin
		SuppressibleMsgBox(Dir + ' holds data of NetRollout ' + Kept + ', newer than this Setup ' +
			'({#AppVersion}). Downgrades aren''t supported.' + #13#10#13#10 +
			'To start it again, run the Setup of NetRollout ' + Kept + ' (or a newer one).', mbError, MB_OK, IDOK);
		Result := False;
		exit;
	end;
	{ older, or not one of ours: the script's check-update decides (refused there
	  before any file is replaced) }
	if CompareText(Dir, AnnouncedDir) <> 0 then
		SuppressibleMsgBox(Held + '. It will be updated to {#AppVersion} - a backup is made first.',
			mbInformation, MB_OK, IDOK);
	AnnouncedDir := Dir;
	KeptUpdate := True;
	InstalledDir := Dir;
	if Kept <> '' then InstalledVersion := Kept else InstalledVersion := 'an earlier version';
end;

{ The data an uninstall kept: the folder the uninstaller recorded, wherever
  it is, else the default folder; '' when neither holds it (.env) }
function FindKept: String;
var Dir: String;
begin
	Result := '';
	if RegQueryStringValue(HKCU, '{#OurKey}', 'KeptData', Dir) then begin
		Dir := RemoveBackslashUnlessRoot(Dir);
		if (Dir <> '') and FileExists(AddBackslash(Dir) + '.env') then begin
			Result := Dir;
			exit;
		end;
	end;
	{ /DIR= naming it (an rc1 uninstall recorded no folder: a silent Setup
	  finds kept data in a custom folder only so) }
	Dir := RemoveBackslashUnlessRoot(ExpandConstant('{param:DIR|}'));
	if (Dir <> '') and FileExists(AddBackslash(Dir) + '.env') then begin
		Result := Dir;
		exit;
	end;
	if FileExists(AddBackslash('{#DefaultDir}') + '.env') then Result := '{#DefaultDir}';
end;

{ The record of kept data in Dir is dropped: Dir is installed again (the
  install record takes over) or its data was deleted }
procedure ForgetKept(const Dir: String);
var Recorded: String;
begin
	if RegQueryStringValue(HKCU, '{#OurKey}', 'KeptData', Recorded) and
			(CompareText(RemoveBackslashUnlessRoot(Recorded), RemoveBackslashUnlessRoot(Dir)) = 0) then
		RegDeleteValue(HKCU, '{#OurKey}', 'KeptData');
end;

{ NetRollout's volumes in Docker (the script's defaults: kept_volumes, only
  when Docker answered) while Setup knows no folder holding their .env -
  the record and the default folder have none. Never assumed gone: an rc1
  uninstall recorded no folder, so a custom one is found only by the user.
  Three ways (No, the safe one, is the default):
  - No, "it's in another folder": LeftSearch - the folder page asks for it
    (a folder with .env is kept data; one without is refused unless "Start
    fresh instead" is ticked there);
  - Yes, "that folder is gone": LeftVolumes - deleted before anything is
    installed (PrepareToInstall), then a fresh install;
  - Cancel: Setup closes, nothing changed (False).
  A silent run never deletes: refused (False) - /DIR= naming the folder
  that holds the .env finds it as kept data instead (FindKept). }
function AskLeftVolumes: Boolean;
var Recorded, Default, Where, Said: String; Answer: Integer;
begin
	Result := True;
	{ a damaged install's volumes: its own (AskDamaged decided about them) }
	if Damaged then exit;
	if UpdateMode or (KeptDir <> '') or LeftVolumes or LeftSearch or (GetDefault('kept_volumes', '') = '') then exit;
	Default := '{#DefaultDir}';
	if RegQueryStringValue(HKCU, '{#OurKey}', 'KeptData', Recorded) then
		Recorded := RemoveBackslashUnlessRoot(Recorded)
	else Recorded := '';
	if (Recorded = '') or (CompareText(Recorded, Default) = 0) then
		Where := Default + ' has none, and no folder was recorded'
	else Where := 'neither ' + Recorded + ' nor ' + Default + ' has its .env';
	Said := 'NetRollout''s data from an earlier install is still in Docker, but Setup can''t find its folder (' +
		Where + ').';
	if WizardSilent then begin
		SuppressibleMsgBox(Said + ' A silent Setup never deletes it - nothing was installed. Run Setup without ' +
			'/SILENT, or with /DIR=<the folder that holds its .env>.', mbError, MB_OK, IDOK);
		Result := False;
		exit;
	end;
	{ one question, the buttons its answers; Yes (keep looking) the default }
	Answer := SuppressibleMsgBox(Said + #13#10#13#10 + 'Do you still have that folder?' + #13#10#13#10 +
		'Yes - choose it on the folder page: NetRollout is updated or started there with its data.' + #13#10#13#10 +
		'No - it''s gone: Setup deletes the data in Docker and installs fresh (its passwords ' +
		'and key were in that folder, so the data can''t be used without it). This can''t be undone - ' +
		'backups in a folder you still have are kept.' + #13#10#13#10 +
		'Cancel - close Setup, nothing is changed.',
		mbConfirmation, MB_YESNOCANCEL or MB_DEFBUTTON1, IDYES);
	LeftSearch := Answer = IDYES;
	LeftVolumes := Answer = IDNO;
	Result := Answer <> IDCANCEL;
end;

{ A damaged install (FindDamaged): one question the buttons answer - clear
  what's left and install again (OK) or close Setup (Cancel, the default) -
  then, while its .env is there (the data can still be used), a second: keep
  its data (Yes, the default: installed again with it - CheckKept, so a
  newer one is still refused) or delete it (No); without .env the data
  can't be used (its passwords and key were in it): said, and deleted. The
  backups always stay. Nothing is done here: PrepareToInstall clears it,
  after the review page. A silent run never deletes: refused, nothing
  changed. False: Setup closes. }
function AskDamaged: Boolean;
var Said, Backups, Data: String; HasData: Boolean; Answer: Integer;
begin
	Result := False;
	Said := 'NetRollout''s installation in ' + DamagedDir + ' is damaged (' + DamageWords + ').';
	Backups := 'Backups in ' + AddBackslash(DamagedDir) + 'backups are kept.';
	if WizardSilent then begin
		SuppressibleMsgBox(Said + ' A silent Setup never deletes anything - nothing was changed. Run Setup ' +
			'without /SILENT: it clears what''s left of that install and installs NetRollout {#AppVersion} again.',
			mbError, MB_OK, IDOK);
		exit;
	end;
	HasData := FileExists(AddBackslash(DamagedDir) + '.env');
	if HasData then
		Data := 'Its data can be kept - Setup asks next.'
	else
		Data := 'Its settings file (.env) is gone - it held the database''s passwords and the key to the saved ' +
			'credentials - so its data can''t be used and is deleted too (the database and the other volumes ' +
			'in Docker). This can''t be undone.';
	if SuppressibleMsgBox(Said + #13#10#13#10 + 'Clear what''s left of it and install NetRollout {#AppVersion} again?' + #13#10#13#10 +
			'Setup closes its NetRollout Manager and removes its containers, files, shortcuts and ' +
			'Windows'' record of it - once you click Install, nothing before. ' + Data + ' ' + Backups + #13#10#13#10 +
			'OK - clear it and install again.' + #13#10 + 'Cancel - close Setup, nothing is changed.',
			mbConfirmation, MB_OKCANCEL or MB_DEFBUTTON2, IDCANCEL) <> IDOK then exit;
	Damaged := True;
	if HasData then begin
		Answer := SuppressibleMsgBox('Keep its data?' + #13#10 + 'NetRollout''s database, settings, certificates and ' +
			'logs (in ' + DamagedDir + ' and in Docker).' + #13#10#13#10 +
			'Yes - keep it: NetRollout {#AppVersion} is installed again in ' + DamagedDir + ' with it (data of an ' +
			'earlier version is updated - a backup first).' + #13#10 +
			'No - delete it. This can''t be undone.' + #13#10 +
			'Cancel - close Setup, nothing is changed.' + #13#10#13#10 + Backups,
			mbConfirmation, MB_YESNOCANCEL or MB_DEFBUTTON1, IDCANCEL);
		if Answer = IDCANCEL then exit;
		DamagedKeep := Answer = IDYES;
		if DamagedKeep then begin
			{ the folder then holds kept data (the folder page locks it); its
			  version decides, as for an uninstall's - already said, not
			  announced as "kept by an uninstall" }
			KeptDir := DamagedDir;
			AnnouncedDir := DamagedDir;
			Result := CheckKept(DamagedDir);
			exit;
		end;
	end;
	Result := True;
end;

{ The Docker page, Docker answering only now: a damaged install whose .env
  is gone while Docker holds its data (FindDamaged needs Docker to see
  that), asked as at the start. True: Setup closes (Cancel, a silent run) }
function DamagedCancelled: Boolean;
begin
	Result := False;
	if Damaged or UpdateMode or (KeptDir <> '') then exit;
	if FindDamaged then Result := not AskDamaged;
end;

{ Before the first page: Windows Server, or virtualization off -> say so and
  stop; a damaged install is asked about (AskDamaged); over an install, only
  a newer Setup goes on; over the data an uninstall kept (wherever it is) it
  is announced, its folder pre-selected (InitializeWizard), and never an
  older Setup goes on }
function InitializeSetup: Boolean;
var Problem: String;
begin
	LoadDefaults;
	Problem := GetDefault('problem', '');
	Result := Problem = '';
	if not Result then
		SuppressibleMsgBox('NetRollout can''t run on this computer.' + #13#10#13#10 + Problem, mbCriticalError, MB_OK, IDOK)
	else if FindDamaged then
		Result := AskDamaged
	else begin
		FindInstalled;
		if UpdateMode then Result := NewerThanInstalled
		else begin
			KeptDir := FindKept;
			if KeptDir <> '' then Result := CheckKept(KeptDir)
			else Result := AskLeftVolumes;
		end;
	end;
end;

function MakeLabel(Page: TWizardPage; const Caption: String; Top: Integer; Bold: Boolean): TNewStaticText;
begin
	Result := TNewStaticText.Create(Page);
	Result.Parent := Page.Surface;
	Result.Caption := Caption;
	Result.Top := ScaleY(Top);
	Result.Width := Page.SurfaceWidth;
	Result.AutoSize := True;
	Result.WordWrap := True;
	if Bold then Result.Font.Style := [fsBold];
end;

function MakeEdit(Page: TWizardPage; const Text: String; Top, Width: Integer): TNewEdit;
begin
	Result := TNewEdit.Create(Page);
	Result.Parent := Page.Surface;
	Result.Text := Text;
	Result.Top := ScaleY(Top);
	Result.Width := Width;
end;

function MakeCheck(Page: TWizardPage; const Caption: String; Top: Integer): TNewCheckBox;
begin
	Result := TNewCheckBox.Create(Page);
	Result.Parent := Page.Surface;
	Result.Top := ScaleY(Top);
	Result.Width := Page.SurfaceWidth;
	Result.Height := ScaleY(20);
	Result.Caption := Caption;
end;

function MakeBrowse(Page: TWizardPage; Top: Integer): TNewButton;
begin
	Result := TNewButton.Create(Page);
	Result.Parent := Page.Surface;
	Result.Caption := SetupMessage(msgButtonWizardBrowse);
	Result.Top := ScaleY(Top) - ScaleY(1);
	Result.Width := ScaleX(80);
	Result.Height := WizardForm.NextButton.Height;
	Result.Left := Page.SurfaceWidth - Result.Width;
end;

procedure ShowDocker;
var State: String;
begin
	State := GetDefault('docker', 'missing');
	if State = 'running' then begin
		DockerState.Caption := 'Docker Desktop is installed and running.';
		DockerHint.Caption := 'Click Next.';
	end else if State = 'installed' then begin
		DockerState.Caption := 'Docker Desktop is installed but not running.';
		DockerHint.Caption := 'Click Next to start it (this can take a minute or two).';
	end else begin
		DockerState.Caption := 'Docker Desktop isn''t installed.';
		DockerHint.Caption := 'NetRollout runs on it. Click Next to install it with Docker''s ' +
			'official installer (Windows asks for permission). If it asks to restart Windows, ' +
			'restart, then run this Setup again.';
	end;
end;

procedure BrowseFile(Sender: TObject);
var Name: String;
begin
	if GetOpenFileName('', Name, '', 'Certificates and keys (*.pem;*.crt;*.cer;*.key)|*.pem;*.crt;*.cer;*.key|All files|*.*', 'pem') then begin
		if Sender = CertButton then CertEdit.Text := Name else KeyEdit.Text := Name;
	end;
end;

procedure OrgCertClick(Sender: TObject);
begin
	CertEdit.Enabled := OrgCertBox.Checked; CertButton.Enabled := OrgCertBox.Checked;
	KeyEdit.Enabled := OrgCertBox.Checked; KeyButton.Enabled := OrgCertBox.Checked;
end;

{ Windows' own timezone list, this computer's selected }
procedure FillTimezones;
var I, P: Integer; Entry, Current: String;
begin
	Current := GetDefault('timezone', 'UTC');
	SetArrayLength(TimezoneIds, 0);
	I := 0;
	while True do begin
		Entry := GetIniString('timezones', IntToStr(I), '', DefaultsFile);
		if Entry = '' then break;
		P := Pos('|', Entry);
		SetArrayLength(TimezoneIds, I + 1);
		TimezoneIds[I] := Copy(Entry, 1, P - 1);
		TimezoneBox.Items.Add(Copy(Entry, P + 1, Length(Entry)));
		if TimezoneIds[I] = Current then TimezoneBox.ItemIndex := I;
		I := I + 1;
	end;
	if TimezoneBox.Items.Count = 0 then begin     { no list: UTC }
		SetArrayLength(TimezoneIds, 1);
		TimezoneIds[0] := 'UTC';
		TimezoneBox.Items.Add('(UTC) Coordinated Universal Time');
	end;
	if TimezoneBox.ItemIndex < 0 then TimezoneBox.ItemIndex := 0;
end;

{ The licence page: the notice (what people read: the AGPL in short, Docker
  Desktop's terms), then the full licence below a line }
procedure ShowFullLicence;
var Lines: TArrayOfString; I: Integer;
begin
	ExtractTemporaryFile('LICENSE');
	if not LoadStringsFromFile(ExpandConstant('{tmp}\LICENSE'), Lines) then exit;
	{ added to the notice (LicenseFile, already on the page) line by line, so
	  the lines keep the page's text colour (light / dark) }
	WizardForm.LicenseMemo.Lines.Add(StringOfChar('_', 60));
	WizardForm.LicenseMemo.Lines.Add('');
	for I := 0 to GetArrayLength(Lines) - 1 do WizardForm.LicenseMemo.Lines.Add(Lines[I]);
	WizardForm.LicenseMemo.SelStart := 0;
end;

{ The folder page over kept data: the folder locked, unless "Start fresh
  instead" is ticked (then any folder; the kept one again when unticked) }
procedure FreshClick(Sender: TObject);
begin
	{ the volumes' folder unknown (LeftFolderPage): the folder is always free }
	if KeptDir = '' then exit;
	WizardForm.DirEdit.Enabled := FreshBox.Checked;
	WizardForm.DirBrowseButton.Enabled := FreshBox.Checked;
	if not FreshBox.Checked then WizardForm.DirEdit.Text := KeptDir;
end;

{ The kept data's folder, version and what happens to it, below the folder;
  "Start fresh instead" below that }
procedure KeptFolderPage;
var Version, What: String;
begin
	Version := KeptVersion(KeptDir);
	if Version = '' then Version := 'an early version';
	if KeptUpdate then What := 'your data is updated to {#AppVersion} (a backup is made first).'
	else What := 'it''s started as it was.';
	KeptNote := TNewStaticText.Create(WizardForm);
	KeptNote.Parent := WizardForm.SelectDirPage;
	KeptNote.AutoSize := False;
	KeptNote.WordWrap := True;
	KeptNote.Caption := 'NetRollout''s data is kept in ' + KeptDir + ' (version ' + Version + '), so NetRollout is ' +
		'installed there - ' + What;
	KeptNote.SetBounds(WizardForm.DirEdit.Left, WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(16),
		WizardForm.DirBrowseButton.Left + WizardForm.DirBrowseButton.Width - WizardForm.DirEdit.Left, ScaleY(30));
	WizardForm.AdjustLabelHeight(KeptNote);
	{ a check box's caption is one line: what is deleted, and kept, below it }
	FreshBox := TNewCheckBox.Create(WizardForm);
	FreshBox.Parent := WizardForm.SelectDirPage;
	FreshBox.Caption := 'Start fresh instead - delete the kept data';
	FreshBox.SetBounds(KeptNote.Left, KeptNote.Top + KeptNote.Height + ScaleY(12), KeptNote.Width, ScaleY(20));
	FreshBox.OnClick := @FreshClick;
	FreshHint := TNewStaticText.Create(WizardForm);
	FreshHint.Parent := WizardForm.SelectDirPage;
	FreshHint.AutoSize := False;
	FreshHint.WordWrap := True;
	FreshHint.Caption := '(the database, settings, certificates and logs; your backups in ' +
		AddBackslash(KeptDir) + 'backups are kept)';
	FreshHint.SetBounds(KeptNote.Left + ScaleX(20), FreshBox.Top + FreshBox.Height + ScaleY(2),
		KeptNote.Width - ScaleX(20), ScaleY(30));
	WizardForm.AdjustLabelHeight(FreshHint);
	{ "at least N MB of free space": room for the two, and not what matters here }
	WizardForm.DiskSpaceLabel.Visible := False;
	FreshClick(nil);
end;

{ Browse while looking for the folder that holds the .env: that folder
  itself - Inno's own Browse appends "\NetRollout", right for a new install
  (and again when "Start fresh instead" is ticked: a new install) }
procedure BrowseExisting(Sender: TObject);
var Dir: String;
begin
	if FreshBox.Checked then begin
		InstallBrowse(Sender);
		exit;
	end;
	Dir := WizardForm.DirEdit.Text;
	if BrowseForFolder('Choose the folder that holds NetRollout''s .env:', Dir, False) then
		WizardForm.DirEdit.Text := Dir;
end;

{ The volumes' folder unknown (LeftSearch): below the folder, what to choose
  - and "Start fresh instead", which deletes the data in Docker (round 2's
  box; NextButtonClick confirms it). Built once, at the start or when the
  Docker page asks. }
procedure LeftFolderPage;
begin
	if KeptNote <> nil then exit;
	KeptNote := TNewStaticText.Create(WizardForm);
	KeptNote.Parent := WizardForm.SelectDirPage;
	KeptNote.AutoSize := False;
	KeptNote.WordWrap := True;
	KeptNote.Caption := 'NetRollout''s data from an earlier install is still in Docker. Choose the folder ' +
		'that holds its .env - NetRollout is updated or started there with its data.';
	KeptNote.SetBounds(WizardForm.DirEdit.Left, WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(16),
		WizardForm.DirBrowseButton.Left + WizardForm.DirBrowseButton.Width - WizardForm.DirEdit.Left, ScaleY(30));
	WizardForm.AdjustLabelHeight(KeptNote);
	FreshBox := TNewCheckBox.Create(WizardForm);
	FreshBox.Parent := WizardForm.SelectDirPage;
	FreshBox.Caption := 'Start fresh instead - delete the data left in Docker';
	FreshBox.SetBounds(KeptNote.Left, KeptNote.Top + KeptNote.Height + ScaleY(12), KeptNote.Width, ScaleY(20));
	FreshHint := TNewStaticText.Create(WizardForm);
	FreshHint.Parent := WizardForm.SelectDirPage;
	FreshHint.AutoSize := False;
	FreshHint.WordWrap := True;
	FreshHint.Caption := '(only if its folder is gone: the database can''t be used without it; backups in a ' +
		'folder you still have are kept)';
	FreshHint.SetBounds(KeptNote.Left + ScaleX(20), FreshBox.Top + FreshBox.Height + ScaleY(2),
		KeptNote.Width - ScaleX(20), ScaleY(30));
	WizardForm.AdjustLabelHeight(FreshHint);
	WizardForm.DiskSpaceLabel.Visible := False;
	InstallBrowse := WizardForm.DirBrowseButton.OnClick;
	WizardForm.DirBrowseButton.OnClick := @BrowseExisting;
end;

procedure InitializeWizard;
var Busy80: String;
begin
	{ kept data found at the start: the install is built there - even with
	  /DIR= (a silent install elsewhere would start fresh on the kept
	  database volumes, with new passwords: broken); another folder only by
	  "Start fresh instead", which deletes the kept data first }
	if KeptDir <> '' then begin
		WizardForm.DirEdit.Text := KeptDir;
		KeptFolderPage;
	end;
	if LeftSearch then LeftFolderPage;
	ShowFullLicence;
	DockerPage := CreateCustomPage(wpLicense, 'Docker Desktop', 'NetRollout runs in Docker containers.');
	DockerState := MakeLabel(DockerPage, '', 0, True);
	DockerHint := MakeLabel(DockerPage, '', 26, False);
	ShowDocker;

	SettingsPage := CreateCustomPage(wpSelectDir, 'Settings',
		'How people will reach NetRollout. All of these can be changed later in System Settings.');
	MakeLabel(SettingsPage, 'Hostname people will use (the certificate is made for it):', 0, False);
	HostnameEdit := MakeEdit(SettingsPage, GetDefault('hostname', 'netrollout'), 16, ScaleX(260));
	MakeLabel(SettingsPage, 'HTTPS port:', 46, False);
	PortEdit := MakeEdit(SettingsPage, GetDefault('https_port', '443'), 62, ScaleX(70));
	PortHint := MakeLabel(SettingsPage, '', 65, False);
	PortHint.Left := ScaleX(82);
	PortHint.Width := SettingsPage.SurfaceWidth - ScaleX(82);
	Busy80 := GetDefault('port80_busy', '');
	if Busy80 <> '' then
		PortHint.Caption := 'Port 80 is used by ' + Busy80 + ', so typing http:// won''t redirect.';
	MakeLabel(SettingsPage, 'Timezone (log times; the nightly clean-up at 03:00):', 92, False);
	TimezoneBox := TNewComboBox.Create(SettingsPage);
	TimezoneBox.Parent := SettingsPage.Surface;
	TimezoneBox.Style := csDropDownList;
	TimezoneBox.Top := ScaleY(108);
	TimezoneBox.Width := SettingsPage.SurfaceWidth;
	FillTimezones;
	MonitoringBox := MakeCheck(SettingsPage, 'Monitoring (Prometheus, Loki and Grafana dashboards for admins)', 140);
	MonitoringBox.Checked := True;
	OrgCertBox := MakeCheck(SettingsPage, 'Use my organisation''s certificate (otherwise a self-signed one is made)', 164);
	OrgCertBox.OnClick := @OrgCertClick;
	MakeLabel(SettingsPage, 'Certificate (yours first, then each issuer):', 188, False);
	CertEdit := MakeEdit(SettingsPage, '', 204, SettingsPage.SurfaceWidth - ScaleX(88));
	CertButton := MakeBrowse(SettingsPage, 204);
	CertButton.OnClick := @BrowseFile;
	MakeLabel(SettingsPage, 'Its private key (without a password):', 230, False);
	KeyEdit := MakeEdit(SettingsPage, '', 246, SettingsPage.SurfaceWidth - ScaleX(88));
	KeyButton := MakeBrowse(SettingsPage, 246);
	KeyButton.OnClick := @BrowseFile;
	OrgCertClick(nil);

	PreparePage := CreateOutputProgressPage('Preparing', 'Nothing is installed or replaced until this is done.');

	{ the finished page's choices: shown and placed by FinishBoxes }
	BrowserBox := TNewCheckBox.Create(WizardForm);
	BrowserBox.Parent := WizardForm.FinishedPage;
	BrowserBox.Caption := 'Open NetRollout in the browser';
	ManagerBox := TNewCheckBox.Create(WizardForm);
	ManagerBox.Parent := WizardForm.FinishedPage;
end;

{ .env is written by the script's init: with it NetRollout is set up (Start
  works), without it nothing is yet (Start refuses, Setup has to run again) }
function HasSettings: Boolean;
begin
	Result := FileExists(ExpandConstant('{app}\.env'));
end;

{ The script's exit code 3: this release can't be set up (retrying won't help) }
function ReleaseBroken: Boolean;
begin
	Result := SetUpFailed and (SetUpCode = 3);
end;

function ManagerCanStart: Boolean;
begin
	{ an update that didn't finish too: its marker (.update-pending) turns the
	  Manager's Start into Finish update - never the new version without the
	  update's steps }
	Result := SetUpFailed and not ReleaseBroken and HasSettings;
end;

{ An update (over an install or kept data) failed after its files: finished
  later by this Setup again, NetRollout Manager's Finish update or netrollout
  start - its marker keeps it pending }
function FinishLater: String;
begin
	Result := 'run this Setup again (the same folder) - it finishes the update - or click Finish update ' +
		'in NetRollout Manager.';
end;

{ After a failed set-up and Cancel: what to do next }
function NextStep: String;
begin
	if ReleaseBroken then
		Result := 'This release can''t be set up - please report it: {#Repo}/issues' + #13#10 +
			'To remove it: Settings -> Apps -> NetRollout -> Uninstall.'
	else if UpdateMode and not Resume then
		Result := 'Fix it, then ' + FinishLater + ' The backup made before the update is in the backups ' +
			'folder (...-before-update.zip): to go back to NetRollout ' + InstalledVersion + ', install it and ' +
			'restore that backup.'
	else if UpdateMode or KeptUpdate then
		Result := 'Fix it, then ' + FinishLater
	else if HasSettings then
		Result := 'Fix it, then click Start in NetRollout Manager.'
	else
		Result := 'Fix it, then run NetRollout Setup again (the same folder) - nothing is set up yet.';
end;

{ The finished page's check boxes (our own: Inno's list of [Run] entries
  draws half-themed in dark mode), below the text: set up -> open it in the
  browser (ticked) or NetRollout Manager; set up but not started -> the
  Manager, ticked (its Start is the next step); else none }
procedure FinishBoxes;
var Top: Integer;
begin
	WizardForm.AdjustLabelHeight(WizardForm.FinishedLabel);
	Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(12);
	BrowserBox.Visible := not SetUpFailed;
	BrowserBox.Checked := True;
	ManagerBox.Visible := (not SetUpFailed) or ManagerCanStart;
	if SetUpFailed and (UpdateMode or KeptUpdate) then begin
		ManagerBox.Caption := 'Open NetRollout Manager (to finish the update once it''s fixed)';
		ManagerBox.Checked := True;
	end else if SetUpFailed then begin
		ManagerBox.Caption := 'Open NetRollout Manager (to start NetRollout once it''s fixed)';
		ManagerBox.Checked := True;
	end else begin
		ManagerBox.Caption := 'Open NetRollout Manager';
		ManagerBox.Checked := False;
	end;
	if BrowserBox.Visible then begin
		BrowserBox.SetBounds(WizardForm.FinishedLabel.Left, Top, WizardForm.FinishedLabel.Width, ScaleY(20));
		Top := Top + ScaleY(24);
	end;
	ManagerBox.SetBounds(WizardForm.FinishedLabel.Left, Top, WizardForm.FinishedLabel.Width, ScaleY(20));
end;

{ The install-location page: the default selected, so typing replaces it
  (not while it's locked to the kept data's folder: a disabled control
  can't take the focus) }
procedure CurPageChanged(CurPageID: Integer);
begin
	if (CurPageID = wpSelectDir) and WizardForm.DirEdit.Enabled then begin
		WizardForm.ActiveControl := WizardForm.DirEdit;
		WizardForm.DirEdit.SelectAll;
	end;
	if (CurPageID = wpReady) and Resume then begin
		WizardForm.PageNameLabel.Caption := 'Ready to finish the update';
		WizardForm.PageDescriptionLabel.Caption := 'The update to NetRollout {#AppVersion} didn''t finish';
		WizardForm.ReadyLabel.Caption := 'Setup finishes it now: click Finish update. Its last steps run ' +
			'again - the new version''s images, the settings, the restart (about a minute).';
		WizardForm.NextButton.Caption := 'Finish update';
	end else if (CurPageID = wpReady) and UpdateMode then begin
		WizardForm.PageNameLabel.Caption := 'Ready to update';
		WizardForm.PageDescriptionLabel.Caption := 'NetRollout ' + InstalledVersion + ' → {#AppVersion}';
		WizardForm.ReadyLabel.Caption := 'Click Update. NetRollout keeps running while the new version ' +
			'downloads; then it restarts (about a minute).';
		WizardForm.NextButton.Caption := 'Update';
	end;
	if CurPageID <> wpFinished then exit;
	if SetUpFailed then begin
		WizardForm.FinishedHeadingLabel.Caption := 'NetRollout is installed, but not running';
		WizardForm.FinishedLabel.Caption := 'Setting it up didn''t finish. The reason is at the end of the log:' + #13#10#13#10 +
			SetUpLog + #13#10#13#10 + NextStep;
	end else if UpdateMode or KeptUpdate then begin
		WizardForm.FinishedHeadingLabel.Caption := 'NetRollout is updated';
		WizardForm.FinishedLabel.Caption := 'NetRollout {#AppVersion} is running. Everyone signs in again ' +
			'(a restart signs everyone out).';
	end else begin
		WizardForm.FinishedHeadingLabel.Caption := 'NetRollout is installed';
		{ the data an uninstall kept, the same version: started - the factory
		  admin / admin of FinishedLabel doesn't apply }
		if Reinstall then
			WizardForm.FinishedLabel.Caption := 'NetRollout {#AppVersion} is running again with the data kept in ' +
				RemoveBackslashUnlessRoot(WizardDirValue) + '. Sign in as before.';
	end;
	FinishBoxes;
end;

function SetUpOk: Boolean;
begin
	Result := not SetUpFailed;
end;

function ValidHostname(const S: String): Boolean;
var I: Integer; C: Char;
begin
	Result := (Length(S) > 0) and (Length(S) <= 253) and (S[1] <> '-') and (S[1] <> '.');
	for I := 1 to Length(S) do begin
		C := S[I];
		if not (((C >= 'a') and (C <= 'z')) or ((C >= 'A') and (C <= 'Z')) or
			((C >= '0') and (C <= '9')) or (C = '-') or (C = '.')) then Result := False;
	end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var Code, Port: Integer; Who: String;
begin
	Result := True;
	if CurPageID = wpSelectDir then begin
		FreshStart := False;
		{ "Start fresh instead": asked once more, then nothing of the kept data
		  counts - a new install (Settings page); the data is deleted first
		  (PrepareToInstall). Never into another folder with kept data. }
		if (KeptDir <> '') and FreshBox.Checked then begin
			if (CompareText(RemoveBackslashUnlessRoot(WizardDirValue), KeptDir) <> 0) and
					FileExists(AddBackslash(WizardDirValue) + '.env') then begin
				SuppressibleMsgBox(RemoveBackslashUnlessRoot(WizardDirValue) + ' holds NetRollout data too. Choose ' +
					'another folder.', mbError, MB_OK, IDOK);
				Result := False; exit;
			end;
			if SuppressibleMsgBox('Delete the data NetRollout kept in ' + KeptDir + ' - the database, settings, ' +
					'certificates and logs - and install NetRollout {#AppVersion} fresh in ' +
					RemoveBackslashUnlessRoot(WizardDirValue) + '?' + #13#10#13#10 + 'This can''t be undone. The ' +
					'backups in ' + AddBackslash(KeptDir) + 'backups are kept (a new install can restore one).',
					mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) <> IDYES then begin
				Result := False; exit;
			end;
			FreshStart := True;
			Reinstall := False;
			KeptUpdate := False;
			exit;
		end;
		{ NetRollout's data already there (an uninstall that kept it): its
		  settings stay, the Settings page is skipped; its version decides
		  (the same: started; older: updated; newer: refused) - announced
		  unless it's the folder announced at the start }
		Reinstall := FileExists(AddBackslash(WizardDirValue) + '.env');
		KeptUpdate := False;
		{ a damaged install whose data goes (AskDamaged): its .env is deleted
		  first - a new install there; never into another folder holding
		  NetRollout's data (the database deleted with the damaged install is
		  the one that data needs) }
		if Damaged and not DamagedKeep then begin
			if CompareText(RemoveBackslashUnlessRoot(WizardDirValue), DamagedDir) = 0 then
				Reinstall := False
			else if Reinstall then begin
				SuppressibleMsgBox(RemoveBackslashUnlessRoot(WizardDirValue) + ' holds NetRollout data too - its ' +
					'database in Docker is the one deleted with the damaged install. Choose another folder.',
					mbError, MB_OK, IDOK);
				Result := False; exit;
			end;
		end;
		{ the folder the volumes Setup was about to delete belong to: kept data
		  after all (its .env is here) - used, not deleted }
		if Reinstall then LeftVolumes := False;
		{ the volumes' folder unknown (AskLeftVolumes: No): a folder without
		  .env is refused - a fresh install there would land on that data -
		  unless "Start fresh instead" (confirmed) deletes the data first }
		if LeftSearch and not Reinstall then begin
			LeftVolumes := False;
			if not FreshBox.Checked then begin
				SuppressibleMsgBox(RemoveBackslashUnlessRoot(WizardDirValue) + ' doesn''t hold NetRollout''s ' +
					'.env, and NetRollout''s data from an earlier install is still in Docker. Choose the folder ' +
					'that holds its .env (NetRollout is updated or started there) - or, if that folder is gone, ' +
					'tick "Start fresh instead" to delete that data and install fresh.', mbError, MB_OK, IDOK);
				Result := False; exit;
			end;
			if SuppressibleMsgBox('Delete NetRollout''s data left in Docker by an earlier install - its database ' +
					'and the other volumes - and install NetRollout {#AppVersion} fresh in ' +
					RemoveBackslashUnlessRoot(WizardDirValue) + '?' + #13#10#13#10 + 'This can''t be undone. ' +
					'Backups in a folder you still have are kept (a new install can restore one).',
					mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) <> IDYES then begin
				Result := False; exit;
			end;
			LeftVolumes := True;
			exit;
		end;
		if Reinstall then Result := CheckKept(RemoveBackslashUnlessRoot(WizardDirValue));
	end else if CurPageID = DockerPage.ID then begin
		if GetDefault('docker', 'missing') <> 'running' then begin
			DockerHint.Caption := 'Getting Docker Desktop ready - this window waits (up to 15 minutes)...';
			WizardForm.NextButton.Enabled := False;
			Exec(PowerShellExe, Ps('ensure-docker', '-Yes'), '', SW_HIDE, ewWaitUntilTerminated, Code);
			WizardForm.NextButton.Enabled := True;
			LoadDefaults;
			ShowDocker;
			if Code <> 0 then begin
				SuppressibleMsgBox('Docker Desktop isn''t running yet. If its installer asked to restart Windows, ' +
					'restart, then run this Setup again. Otherwise start Docker Desktop and click Next.',
					mbError, MB_OK, IDOK);
				Result := False;
			end else if DamagedCancelled then begin
				{ Docker answers only now: a damaged install's data in Docker, asked
				  as at the start; Cancel closes Setup, nothing changed }
				Result := False;
				if not WizardSilent then begin
					QuietClose := True;
					WizardForm.Close;
				end;
			end else if not AskLeftVolumes then begin
				{ Docker answers only now: the volumes Setup's start couldn't see,
				  the same three ways. Cancel: Setup closes, as at the start (a
				  silent run exits on False) }
				Result := False;
				if not WizardSilent then begin
					QuietClose := True;
					WizardForm.Close;
				end;
			end else if LeftSearch then
				LeftFolderPage;
		end;
	end else if CurPageID = SettingsPage.ID then begin
		if not ValidHostname(HostnameEdit.Text) then begin
			SuppressibleMsgBox('The hostname may contain letters, digits, dots and hyphens only (no https://, port or path).', mbError, MB_OK, IDOK);
			Result := False; exit;
		end;
		Port := StrToIntDef(PortEdit.Text, -1);
		if (Port < 1) or (Port > 65535) or (Port = 80) then begin
			SuppressibleMsgBox('Choose an HTTPS port between 1 and 65535 (not 80, which is for the http -> https redirect).', mbError, MB_OK, IDOK);
			Result := False; exit;
		end;
		Who := GetIniString('busy', IntToStr(Port), '', DefaultsFile);
		if Who <> '' then begin
			SuppressibleMsgBox('Port ' + IntToStr(Port) + ' is in use on this computer (by ' + Who + '). Choose another, e.g. 8443.', mbError, MB_OK, IDOK);
			Result := False; exit;
		end;
		if OrgCertBox.Checked and (not FileExists(CertEdit.Text) or not FileExists(KeyEdit.Text)) then begin
			SuppressibleMsgBox('Choose your certificate and its private key, or untick "Use my organisation''s certificate".', mbError, MB_OK, IDOK);
			Result := False; exit;
		end;
	end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
	if QuietClose then Confirm := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
	Result := (PageID = SettingsPage.ID) and Reinstall;
	{ an update: only the review page (and Docker's, if it isn't running) }
	if UpdateMode then
		Result := Result or (PageID = wpWelcome) or (PageID = wpLicense) or (PageID = wpSelectDir) or
			(PageID = wpSelectTasks) or ((PageID = DockerPage.ID) and (GetDefault('docker', '') = 'running'));
end;

function YesNo(B: Boolean): String;
begin
	if B then Result := 'y' else Result := 'n';
end;

function OnOff(B: Boolean): String;
begin
	if B then Result := 'on' else Result := 'off';
end;

{ The last value of Key= in a file; Default when there's none (or no file) }
function FileValue(const FileName, Key, Default: String): String;
var Lines: TArrayOfString; I: Integer;
begin
	Result := Default;
	if LoadStringsFromFile(FileName, Lines) then
		for I := 0 to GetArrayLength(Lines) - 1 do
			if Pos(Key + '=', Lines[I]) = 1 then Result := Copy(Lines[I], Length(Key) + 2, Length(Lines[I]));
end;

function Address(Param: String): String;
var Host, Port: String;
begin
	Host := FileValue(ExpandConstant('{app}\config\nginx\site.env'), 'NETROLLOUT_HOSTNAME', HostnameEdit.Text);
	{ none set (NETROLLOUT_HOSTNAME= blank): nginx serves any name - this computer's }
	if Trim(Host) = '' then Host := HostnameEdit.Text;
	if Trim(Host) = '' then Host := GetComputerNameString;
	Port := FileValue(ExpandConstant('{app}\.env'), 'HTTPS_PORT', PortEdit.Text);
	Result := 'https://' + Lowercase(Host);
	if Port <> '443' then Result := Result + ':' + Port;
end;

{ The review page (Ready to install): every choice, before anything is done }
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
	MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var Lines, Certificate, Shortcuts: String;
begin
	if Resume then begin
		Result := 'Finish the update:' + NewLine + Space + 'NetRollout {#AppVersion} - its files are in place, ' +
			'its last steps didn''t run' + NewLine + NewLine +
			'Folder:' + NewLine + Space + InstalledDir + NewLine + NewLine +
			'Runs again:' + NewLine + Space + 'the new images, the settings, the restart (and the backup, ' +
			'if it wasn''t made yet)' + NewLine + NewLine +
			'Downtime:' + NewLine + Space + 'about a minute (everyone signs in again)';
		exit;
	end;
	if UpdateMode then begin
		Result := 'Update:' + NewLine + Space + 'NetRollout ' + InstalledVersion + ' → {#AppVersion}' + NewLine + NewLine +
			'Folder:' + NewLine + Space + InstalledDir + NewLine + NewLine +
			'Kept:' + NewLine + Space + 'the data, the settings, the certificate, the backups' + NewLine + NewLine +
			'First:' + NewLine + Space + 'a backup (before-update), by the installed version' + NewLine + NewLine +
			'Downtime:' + NewLine + Space + 'about a minute (running rollouts finish first; everyone signs in again)';
		exit;
	end;
	Lines := 'Install folder:' + NewLine + Space + WizardDirValue + NewLine + NewLine;
	if Damaged then begin
		Lines := Lines + 'First:' + NewLine + Space + 'the damaged install in ' + DamagedDir + ' is cleared - its ' +
			'NetRollout Manager closed, its containers, files, shortcuts and Windows'' record removed; ';
		if DamagedKeep and not FreshStart then Lines := Lines + 'its data kept'
		else Lines := Lines + 'its data deleted (the database too)';
		Lines := Lines + '; the backups stay' + NewLine + NewLine;
	end;
	if FreshStart and not Damaged then
		Lines := Lines + 'First:' + NewLine + Space + 'the data kept in ' + KeptDir + ' is deleted (the backups ' +
			'there are kept)' + NewLine + NewLine;
	if LeftVolumes then
		Lines := Lines + 'First:' + NewLine + Space + 'NetRollout''s database left in Docker by an earlier install ' +
			'is deleted' + NewLine + NewLine;
	if Reinstall then begin
		Lines := Lines + 'Settings:' + NewLine + Space + 'kept from the NetRollout already in this folder' + NewLine + NewLine;
		if KeptUpdate then
			Lines := Lines + 'Update:' + NewLine + Space + 'NetRollout ' + InstalledVersion + ' → {#AppVersion} ' +
				'(the data is backed up first, then updated)' + NewLine + NewLine;
	end else begin
		if OrgCertBox.Checked then Certificate := 'your organisation''s (' + ExtractFileName(CertEdit.Text) + ')'
		else Certificate := 'self-signed, for ' + Lowercase(HostnameEdit.Text);
		Lines := Lines + 'Address:' + NewLine + Space + Address('') + NewLine + NewLine +
			'Timezone:' + NewLine + Space + TimezoneBox.Text + NewLine + NewLine +
			'Monitoring:' + NewLine + Space + OnOff(MonitoringBox.Checked) + NewLine + NewLine +
			'Certificate:' + NewLine + Space + Certificate + NewLine + NewLine;
	end;
	Shortcuts := 'Start Menu';
	if WizardIsTaskSelected('desktopicons') then Shortcuts := Shortcuts + ', desktop';
	if WizardIsTaskSelected('trayatsignin') then Shortcuts := Shortcuts + ', NetRollout Manager in the tray at sign-in';
	Shortcuts := Shortcuts + ', Win+R -> netrollout';
	Result := Lines + 'Shortcuts:' + NewLine + Space + Shortcuts + NewLine + NewLine;
	if WizardIsTaskSelected('addtopath') then
		Result := Result + 'Command line:' + NewLine + Space + 'netrollout (status, start, stop, logs) in any terminal' + NewLine + NewLine;
	Result := Result + 'Docker Desktop:' + NewLine + Space + 'running';
end;

{ The user's PATH: our bin folder added (the task) or removed (uninstall),
  nothing else touched }
function PathEntries(const Path: String): TArrayOfString;
var Rest: String; P, N: Integer;
begin
	Rest := Path; N := 0;
	SetArrayLength(Result, 0);
	while Rest <> '' do begin
		P := Pos(';', Rest);
		if P = 0 then P := Length(Rest) + 1;
		if Trim(Copy(Rest, 1, P - 1)) <> '' then begin
			SetArrayLength(Result, N + 1);
			Result[N] := Copy(Rest, 1, P - 1);
			N := N + 1;
		end;
		Rest := Copy(Rest, P + 1, Length(Rest));
	end;
end;

procedure SetPathEntry(const Bin: String; Add: Boolean);
var Path, NewPath: String; Entries: TArrayOfString; I: Integer; Found: Boolean;
begin
	if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then Path := '';
	Entries := PathEntries(Path);
	NewPath := ''; Found := False;
	for I := 0 to GetArrayLength(Entries) - 1 do begin
		if CompareText(RemoveBackslashUnlessRoot(Entries[I]), Bin) = 0 then begin
			Found := True;
			if not Add then continue;
		end;
		if NewPath <> '' then NewPath := NewPath + ';';
		NewPath := NewPath + Entries[I];
	end;
	if Add and not Found then begin
		if NewPath <> '' then NewPath := NewPath + ';';
		NewPath := NewPath + Bin;
	end;
	if NewPath <> Path then
		RegWriteExpandStringValue(HKCU, 'Environment', 'Path', NewPath);
end;

procedure SetOurPath(Add: Boolean);
begin
	SetPathEntry(ExpandConstant('{app}\bin'), Add);
end;

{ What Setup put outside a damaged install's folder, once clear-damaged
  cleared the folder: Windows' record of the install (this AppId's only),
  Win+R's App Paths entry when it names that folder, the shortcuts (the
  Icons and INI sections'), the PATH entry - nothing points at a folder that
  isn't there any more; the install that follows writes its own. Its data
  kept (Keep): recorded as an uninstall that kept it records it, so a later
  Setup finds it should this one stop before installing (an older version's
  update refused); deleted: the record goes }
procedure ForgetDamaged(const Dir: String; Keep: Boolean);
var AppPaths, Named: String;
begin
	RegDeleteKeyIncludingSubkeys(HKCU, InstallRecord);
	AppPaths := 'Software\Microsoft\Windows\CurrentVersion\App Paths\netrollout.exe';
	if RegQueryStringValue(HKCU, AppPaths, 'Path', Named) and
			(CompareText(RemoveBackslashUnlessRoot(Named), AddBackslash(Dir) + 'bin') = 0) then
		RegDeleteKeyIncludingSubkeys(HKCU, AppPaths);
	DeleteFile(ExpandConstant('{userstartup}\NetRollout Manager.lnk'));
	DeleteFile(ExpandConstant('{userstartup}\NetRollout port helper.lnk'));
	DeleteFile(ExpandConstant('{autodesktop}\NetRollout Manager.lnk'));
	DeleteFile(ExpandConstant('{autodesktop}\NetRollout.url'));
	DeleteFile(ExpandConstant('{autoprograms}\NetRollout\NetRollout Manager.lnk'));
	DeleteFile(ExpandConstant('{autoprograms}\NetRollout\NetRollout.url'));
	RemoveDir(ExpandConstant('{autoprograms}\NetRollout'));
	SetPathEntry(AddBackslash(Dir) + 'bin', False);
	if Keep then RegWriteStringValue(HKCU, '{#OurKey}', 'KeptData', Dir)
	else ForgetKept(Dir);
end;

{ The current step, live under the progress bar: while the script runs (Exec
  waits, and keeps the wizard's messages flowing), a timer shows its newest
  step line ("-> ...", manage.ps1's Step) written to the log after line
  WatchFrom. Display only: the script and its log are as without it. }
function SetTimer(Wnd, IDEvent, Elapse, TimerFunc: LongWord): LongWord;
	external 'SetTimer@user32.dll stdcall';
function KillTimer(Wnd, IDEvent: LongWord): BOOL;
	external 'KillTimer@user32.dll stdcall';

var
	WatchLog: String;
	WatchFrom: Integer;
	{ the preparation runs (PreparePage shows the step, not the status label) }
	Preparing: Boolean;

procedure ShowStep(Wnd, Msg, IDEvent, Time: LongWord);
var Lines: TArrayOfString; I: Integer; Line: String;
begin
	{ the script holds the log open for writing; unreadable now: the last step stays }
	if not LoadStringsFromLockedFile(WatchLog, Lines) then exit;
	for I := GetArrayLength(Lines) - 1 downto WatchFrom do
		if Pos('-> ', Lines[I]) = 1 then begin
			Line := Copy(Lines[I], 4, Length(Lines[I])) + '...';
			if Preparing then PreparePage.Msg2Label.Caption := Line
			else WizardForm.StatusLabel.Caption := Line;
			exit;
		end;
end;

{ manage.ps1 Args, its output into Log (appended: Append), the steps shown
  live; the exit code }
function RunScript(const Args, Log: String; Append: Boolean): Integer;
var Redirect: String; Lines: TArrayOfString; Timer: LongWord;
begin
	WatchLog := Log;
	WatchFrom := 0;
	if Append then begin
		Redirect := ' >> "';
		{ only this run's steps: the preparation's are in the same file }
		if LoadStringsFromLockedFile(Log, Lines) then WatchFrom := GetArrayLength(Lines);
	end else begin
		Redirect := ' > "';
		{ a new log: an earlier attempt's steps can't show }
		DeleteFile(Log);
	end;
	WizardForm.ProgressGauge.Style := npbstMarquee;
	Timer := SetTimer(0, 0, 500, CreateCallback(@ShowStep));
	try
		Exec(ExpandConstant('{cmd}'), '/C ' + PowerShellExe + ' ' +
			PsArgs(ExpandConstant('{app}\bin\manage.ps1'), Args) + Redirect + Log + '" 2>&1',
			ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Result);
	finally
		KillTimer(0, Timer);
		WizardForm.ProgressGauge.Style := npbstNormal;
	end;
end;

{ Before any file (PrepareToInstall): Setup's temporary manage.ps1 Args, run
  in Dir, its output into Log (a new one: an earlier run's steps can't
  show). The Preparing page has no progress bar, so PreparePage shows What,
  a marquee and the newest step line live, as RunScript does; the exit code }
function RunPreparation(const Args, Dir, Log, What: String): Integer;
var Timer: LongWord;
begin
	WatchLog := Log;
	WatchFrom := 0;
	DeleteFile(Log);
	PreparePage.SetText(What, '');
	PreparePage.SetProgress(0, 100);
	PreparePage.Show;
	PreparePage.ProgressBar.Style := npbstMarquee;
	Preparing := True;
	Timer := SetTimer(0, 0, 500, CreateCallback(@ShowStep));
	try
		Exec(ExpandConstant('{cmd}'), '/C ' + PowerShellExe + ' ' +
			PsArgs(ExpandConstant('{tmp}\manage.ps1'), Args) + ' > "' + Log + '" 2>&1',
			Dir, SW_HIDE, ewWaitUntilTerminated, Result);
	finally
		KillTimer(0, Timer);
		Preparing := False;
		PreparePage.Hide;
	end;
end;

{ One attempt: set NetRollout up, update it, or (set up already) start it;
  the exit code }
function RunSetUp(const Log: String): Integer;
var Args: String;
begin
	if UpdateMode or KeptUpdate then begin
		Args := 'update -Yes -NoBrowser';
		{ over kept data: the backup prepare-update couldn't make (no files yet) }
		if KeptUpdate then Args := Args + ' -BackupFirst';
		if Resume then
			WizardForm.StatusLabel.Caption := 'Finishing the update - the new version, then a restart (about a minute)...'
		else
			WizardForm.StatusLabel.Caption := 'Updating NetRollout - downloading the new version, then a restart (about a minute)...';
		{ appended: the log of the preparation is in the same file }
		Result := RunScript(Args, Log, True);
		exit;
	end;
	if Reinstall or HasSettings then
		Args := 'start -Yes -NoBrowser'
	else begin
		if OrgCertBox.Checked then begin
			ForceDirectories(ExpandConstant('{app}\certs'));
			CopyFile(CertEdit.Text, ExpandConstant('{app}\certs\fullchain.pem'), False);
			CopyFile(KeyEdit.Text, ExpandConstant('{app}\certs\privkey.pem'), False);
		end;
		Args := 'install -Yes -NoBrowser -Hostname "' + Lowercase(HostnameEdit.Text) +
			'" -HttpsPort ' + PortEdit.Text + ' -Monitoring ' + YesNo(MonitoringBox.Checked) +
			' -OrgCertificate ' + YesNo(OrgCertBox.Checked) + ' -TimeZone "' +
			TimezoneIds[TimezoneBox.ItemIndex] + '"';
	end;
	WizardForm.StatusLabel.Caption := 'Setting up and starting NetRollout - the first time downloads it (a few minutes)...';
	Result := RunScript(Args, Log, False);
end;

{ The last lines of the log: the script's own explanation and advice }
function LogTail(const Log: String): String;
var I, From: Integer; Lines: TArrayOfString;
begin
	Result := '';
	if LoadStringsFromFile(Log, Lines) then begin
		From := GetArrayLength(Lines) - 12;
		if From < 0 then From := 0;
		for I := From to GetArrayLength(Lines) - 1 do Result := Result + Lines[I] + #13#10;
	end;
end;

{ An update, before any file is replaced: the direction (an older Setup is
  refused) and a backup by the installed version - not when finishing an
  unfinished one (Resume: checked and backed up already; its marker says
  whether the backup is still owed); or the kept data / the volumes left in
  Docker deleted first (Start fresh / AskLeftVolumes). Each shown live
  (RunPreparation). Non-empty: why Setup stops. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer; Log, What: String;
begin
	Result := '';
	{ a damaged install (AskDamaged; never in a silent run - refused at the
	  start): cleared first by Setup's temporary manage.ps1 (it needs none
	  of the folder's files) - its data kept or deleted as answered ("Start
	  fresh instead" deletes it too), the backups kept - then what Setup put
	  outside the folder (ForgetDamaged); kept data then goes the kept-data
	  way below (KeptUpdate) or is started after the files }
	if Damaged and not WizardSilent then begin
		Log := ExpandConstant('{tmp}\clear-damaged.log');
		What := 'clear-damaged -Yes -InstallDir "' + DamagedDir + '"';
		if DamagedKeep and not FreshStart then What := What + ' -KeepData';
		Code := RunPreparation(What, ExpandConstant('{tmp}'), Log, 'Clearing the damaged install in ' + DamagedDir);
		if Code <> 0 then begin
			Result := 'The damaged install in ' + DamagedDir + ' wasn''t cleared (or not all of it) - nothing was ' +
				'installed.' + #13#10#13#10 + LogTail(Log);
			exit;
		end;
		ForgetDamaged(DamagedDir, DamagedKeep and not FreshStart);
		if FreshStart then exit;
	end;
	{ "Start fresh instead" (confirmed; never in a silent run): the kept data
	  goes as the uninstaller's "delete the data" - the folder has no compose
	  files any more, so by the Docker project's label (forget-kept) }
	if FreshStart and not WizardSilent then begin
		Log := ExpandConstant('{tmp}\forget-kept.log');
		Code := RunPreparation('forget-kept -Yes -InstallDir "' + KeptDir + '"', KeptDir, Log,
			'Deleting the data kept in ' + KeptDir + ' (the backups stay)');
		if Code <> 0 then begin
			Result := 'The data kept in ' + KeptDir + ' wasn''t deleted (or not all of it) - nothing was ' +
				'installed.' + #13#10#13#10 + LogTail(Log);
			exit;
		end;
		{ deleted: no record of it any more (the uninstaller drops it the same way) }
		ForgetKept(KeptDir);
		exit;
	end;
	{ NetRollout's volumes with no folder (AskLeftVolumes: Yes): the Docker
	  part only - there are no files (never in a silent run: it refuses first) }
	if LeftVolumes and not WizardSilent then begin
		Log := ExpandConstant('{tmp}\forget-kept.log');
		Code := RunPreparation('forget-kept -Yes -VolumesOnly', ExpandConstant('{tmp}'), Log,
			'Deleting NetRollout''s database left in Docker');
		if Code <> 0 then
			Result := 'NetRollout''s database left in Docker wasn''t deleted (or not all of it) - nothing was ' +
				'installed.' + #13#10#13#10 + LogTail(Log);
		exit;
	end;
	if not (UpdateMode or KeptUpdate) then exit;
	ForceDirectories(InstalledDir + '\logs');
	Log := InstalledDir + '\logs\update.log';
	{ kept data (no VERSION there): the script checks its version by its image;
	  the backup follows once the files are in place (update -BackupFirst) }
	if KeptUpdate then What := 'Checking the kept data''s version'
	else What := 'Backing up NetRollout ' + InstalledVersion + ' before the update - nothing is replaced yet';
	if not Resume then
		Code := RunPreparation('prepare-update -Yes -InstallDir "' + InstalledDir + '" -NewVersion {#AppVersion}',
			InstalledDir, Log, What)
	else
		Code := 0;
	if Code <> 0 then begin
		{ a damaged install was cleared before: not "nothing was changed" }
		if Damaged then Result := 'The damaged install in ' + DamagedDir + ' was cleared - its data is kept ' +
			'there - but the update didn''t start. Fix it (below), then run Setup again.'
		else if KeptUpdate then Result := 'Nothing was changed.'
		else Result := 'Nothing was changed - NetRollout ' + InstalledVersion + ' keeps running.';
		Result := Result + #13#10#13#10 + LogTail(Log) + #13#10 + 'The whole log: ' + Log;
		exit;
	end;
	{ this install's Manager - the tray and the port helper - holds its .exe:
	  closed now (as the uninstall does), else no file can be replaced and a
	  silent update aborts; [Run] starts them again at the end }
	if FileExists(InstalledDir + '\bin\NetRollout Manager.exe') then
		Exec(InstalledDir + '\bin\NetRollout Manager.exe', '--exit', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ After the files: the script sets NetRollout up and starts it. A failure
  that can be fixed here offers Retry (a silent install cancels) }
procedure CurStepChanged(CurStep: TSetupStep);
var Log, Text: String; Code: Integer;
begin
	{ Finish clicked: what the finished page's boxes ask (a silent run opens nothing) }
	if (CurStep = ssDone) and not WizardSilent then begin
		if BrowserBox.Visible and BrowserBox.Checked then
			ShellExec('open', Address(''), '', '', SW_SHOWNORMAL, ewNoWait, Code);
		if ManagerBox.Visible and ManagerBox.Checked then
			Exec(ExpandConstant('{app}\bin\NetRollout Manager.exe'), '', ExpandConstant('{app}'),
				SW_SHOWNORMAL, ewNoWait, Code);
	end;
	if CurStep <> ssPostInstall then exit;
	{ an install again: Windows' install record takes over from the kept-data record }
	ForgetKept(ExpandConstant('{app}'));
	if WizardIsTaskSelected('addtopath') then SetOurPath(True);
	ForceDirectories(ExpandConstant('{app}\logs'));
	if UpdateMode or KeptUpdate then Log := ExpandConstant('{app}\logs\update.log')
	else Log := ExpandConstant('{app}\logs\install.log');
	SetUpLog := Log;
	while True do begin
		SetUpCode := RunSetUp(Log);
		SetUpFailed := SetUpCode <> 0;
		if not SetUpFailed then break;
		if UpdateMode or KeptUpdate then Text := 'NetRollout''s files are updated, but the update didn''t finish:'
		else Text := 'NetRollout was installed, but setting it up didn''t finish:';
		Text := Text + #13#10#13#10 + LogTail(Log) + #13#10 + 'The whole log: ' + Log;
		if ReleaseBroken then begin
			SuppressibleMsgBox(Text, mbError, MB_OK, IDOK);
			break;
		end;
		if UpdateMode or KeptUpdate then
			Text := Text + #13#10#13#10 + 'Fix it and click Retry - or Cancel, and later ' + FinishLater
		else
			Text := Text + #13#10#13#10 + 'Fix it and click Retry, or Cancel to finish without it.';
		if SuppressibleMsgBox(Text, mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then break;
	end;
end;

{ Uninstall: the containers go; the data only if asked }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Code: Integer; Data: String;
begin
	if CurUninstallStep = usPostUninstall then SetOurPath(False);
	if CurUninstallStep <> usUninstall then exit;
	{ this install's Manager (the tray) closes first: its files can go }
	Exec(ExpandConstant('{app}\bin\NetRollout Manager.exe'), '--exit', '', SW_HIDE,
		ewWaitUntilTerminated, Code);
	{ a silent uninstall keeps everything }
	Data := '-KeepData';
	{ both questions: Yes always deletes, each answer spelled out, No the default }
	if not UninstallSilent and (MsgBox('Delete NetRollout''s data too?' + #13#10 +
		'The database, settings, certificates and logs.' + #13#10#13#10 +
		'Yes - delete it. This can''t be undone.' + #13#10 +
		'No - keep it, to install again later with everything as it was.',
		mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then begin
		Data := '-DeleteData';
		{ the backups are the last copy of the data: kept unless asked }
		if MsgBox('Delete the backups too?' + #13#10 + 'They''re the last copy of NetRollout''s data - ' +
			'a new install can restore one.' + #13#10#13#10 + 'Yes - delete them as well.' + #13#10 +
			'No - keep them in ' + AddBackslash(ExpandConstant('{app}')) + 'backups (recommended). They ' +
			'hold the key to the saved credentials: keep them as safe as the server.',
			mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
			Data := Data + ' -DeleteBackups';
	end;
	Exec(PowerShellExe, PsArgs(ExpandConstant('{app}\bin\manage.ps1'), 'uninstall -Yes ' + Data),
		ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
	{ the data kept (whatever was asked - .env decides): recorded, so the next
	  Setup finds it wherever this folder is; deleted: the record goes }
	if FileExists(ExpandConstant('{app}\.env')) then
		RegWriteStringValue(HKCU, '{#OurKey}', 'KeptData', ExpandConstant('{app}'))
	else
		ForgetKept(ExpandConstant('{app}'));
end;
