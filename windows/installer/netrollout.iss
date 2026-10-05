; NetRollout-Setup-<version>.exe — the Windows installer (Inno Setup 6), the
; only way NetRollout is installed on Windows.
;
;   iscc windows\installer\netrollout.iss      (from the repo root; build the
;                                               Manager first: windows\manager\build.ps1)
;
; The wizard asks; bin\manage.ps1 does the work (install -Yes with the
; answers as parameters). The installed folder: bin\ (the Manager, the engine,
; the command line, the icon), deploy\, the compose files, VERSION, LICENSE,
; the uninstaller; the install creates .env (hidden), config\, certs\, logs\,
; backups\. docs/plans/stage-9.md, 9.3b / 9.4b.
;
; Over an install it updates (9.6): only the review page ("Update X -> Y");
; before any file is replaced the script checks the direction (an older
; Setup is refused) and backs up; after the files, it downloads the new
; images while the old version runs, brings .env up to date and restarts.
; NetRollout Manager's Update runs it with /SILENT (and opens again after it).

#define Root AddBackslash(SourcePath) + "..\.."
#define AppVersion Trim(FileRead(FileOpen(Root + "\VERSION")))
#define Repo "https://github.com/itamar14-byte/NetRollout"
; Windows keeps one install record per app identity and user: a test build
; (iscc /DTestBuild) has its own, so a test install can neither take over nor
; update the real one
#ifdef TestBuild
  #define AppGuid "8E0B3C71-6F2D-4C5A-9B1E-2D7F4A6C8E90"
  #define AppTitle "NetRollout (test)"
  #define OutputSuffix "-test"
#else
  #define AppGuid "6C1F0E52-9B47-4E1B-A7D3-5E2C8F41B0A9"
  #define AppTitle "NetRollout"
  #define OutputSuffix ""
#endif

[Setup]
AppId={{{#AppGuid}}
AppName={#AppTitle}
AppVersion={#AppVersion}
AppVerName=NetRollout {#AppVersion}
AppPublisher=Itamar Weinstein
AppPublisherURL={#Repo}
AppSupportURL={#Repo}/issues
AppUpdatesURL={#Repo}/releases
AppCopyright=GNU AGPL v3
VersionInfoDescription=NetRollout Setup
DefaultDirName=C:\NetRollout
DisableDirPage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
UsePreviousAppDir=yes
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
UninstallDisplayName={#AppTitle}
LicenseFile=licence-notice.txt
OutputDir={#Root}\dist
OutputBaseFilename=NetRollout-Setup-{#AppVersion}{#OutputSuffix}
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
Filename: "{code:Address}"; Description: "Open NetRollout in the browser"; Flags: postinstall shellexec nowait skipifsilent; Check: SetUpOk
Filename: "{app}\bin\NetRollout Manager.exe"; Description: "Open NetRollout Manager"; Flags: postinstall nowait skipifsilent unchecked; Check: SetUpOk
; set up but not started: the Manager's Start is the next step, so it's ticked
Filename: "{app}\bin\NetRollout Manager.exe"; Description: "Open NetRollout Manager (to start NetRollout once it's fixed)"; Flags: postinstall nowait skipifsilent; Check: ManagerCanStart

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
	CertButton, KeyButton: TNewButton;
	DefaultsFile: String;
	Reinstall: Boolean;
	{ over an installed NetRollout (Windows' record of it + its VERSION) }
	UpdateMode: Boolean;
	InstalledDir, InstalledVersion: String;

function Ps(const Command, Extra: String): String;
begin
	Result := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\manage.ps1') +
		'" ' + Command + ' ' + Extra;
end;

{ The script's facts about this computer: defaults, busy ports, Docker, the
  timezones, and whether NetRollout can run here at all }
procedure LoadDefaults;
var Code: Integer;
begin
	ExtractTemporaryFile('manage.ps1');
	DefaultsFile := ExpandConstant('{tmp}\defaults.ini');
	Exec('powershell.exe', Ps('defaults', '-Out "' + DefaultsFile + '"'), '', SW_HIDE,
		ewWaitUntilTerminated, Code);
end;

function GetDefault(const Key, Fallback: String): String;
begin
	Result := GetIniString('defaults', Key, Fallback, DefaultsFile);
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

{ Before the first page: Windows Server, or virtualization off -> say so and stop }
function InitializeSetup: Boolean;
var Problem: String;
begin
	LoadDefaults;
	Problem := GetDefault('problem', '');
	Result := Problem = '';
	if not Result then
		SuppressibleMsgBox('NetRollout can''t run on this computer.' + #13#10#13#10 + Problem, mbCriticalError, MB_OK, IDOK)
	else
		FindInstalled;
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

procedure InitializeWizard;
var Busy80: String;
begin
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
	MonitoringBox := TNewCheckBox.Create(SettingsPage);
	MonitoringBox.Parent := SettingsPage.Surface;
	MonitoringBox.Top := ScaleY(140);
	MonitoringBox.Width := SettingsPage.SurfaceWidth;
	MonitoringBox.Height := ScaleY(20);
	MonitoringBox.Caption := 'Monitoring (Prometheus, Loki and Grafana dashboards for admins)';
	MonitoringBox.Checked := True;
	OrgCertBox := TNewCheckBox.Create(SettingsPage);
	OrgCertBox.Parent := SettingsPage.Surface;
	OrgCertBox.Top := ScaleY(164);
	OrgCertBox.Width := SettingsPage.SurfaceWidth;
	OrgCertBox.Height := ScaleY(20);
	OrgCertBox.Caption := 'Use my organisation''s certificate (otherwise a self-signed one is made)';
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
	Result := SetUpFailed and not ReleaseBroken and HasSettings;
end;

{ After a failed set-up and Cancel: what to do next }
function NextStep: String;
begin
	if ReleaseBroken then
		Result := 'This release can''t be set up - please report it: {#Repo}/issues' + #13#10 +
			'To remove it: Settings -> Apps -> NetRollout -> Uninstall.'
	else if UpdateMode then
		Result := 'Fix it, then click Retry - or Start in NetRollout Manager. The backup made before the ' +
			'update is in the backups folder (...-before-update.zip): to go back to NetRollout ' +
			InstalledVersion + ', install it and restore that backup.'
	else if HasSettings then
		Result := 'Fix it, then click Start in NetRollout Manager.'
	else
		Result := 'Fix it, then run NetRollout Setup again (the same folder) - nothing is set up yet.';
end;

{ The install-location page: the default selected, so typing replaces it }
procedure CurPageChanged(CurPageID: Integer);
begin
	if CurPageID = wpSelectDir then begin
		WizardForm.ActiveControl := WizardForm.DirEdit;
		WizardForm.DirEdit.SelectAll;
	end;
	if (CurPageID = wpReady) and UpdateMode then begin
		WizardForm.PageNameLabel.Caption := 'Ready to update';
		WizardForm.PageDescriptionLabel.Caption := 'NetRollout ' + InstalledVersion + ' → {#AppVersion}';
		WizardForm.ReadyLabel.Caption := 'Click Update. NetRollout keeps running while the new version ' +
			'downloads; then it restarts (about a minute).';
		WizardForm.NextButton.Caption := 'Update';
	end;
	if (CurPageID = wpFinished) and UpdateMode and not SetUpFailed then begin
		WizardForm.FinishedHeadingLabel.Caption := 'NetRollout is updated';
		WizardForm.FinishedLabel.Caption := 'NetRollout {#AppVersion} is running. Everyone signs in again ' +
			'(a restart signs everyone out).';
	end;
	if (CurPageID = wpFinished) and SetUpFailed then begin
		WizardForm.FinishedHeadingLabel.Caption := 'NetRollout is installed, but not running';
		WizardForm.FinishedLabel.Caption := 'Setting it up didn''t finish. The reason is at the end of the log:' + #13#10#13#10 +
			SetUpLog + #13#10#13#10 + NextStep;
	end;
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
	if CurPageID = wpSelectDir then
		{ NetRollout's data already there (an uninstall that kept it): its
		  settings stay, the Settings page is skipped }
		Reinstall := FileExists(AddBackslash(WizardDirValue) + '.env')
	else if CurPageID = DockerPage.ID then begin
		if GetDefault('docker', 'missing') <> 'running' then begin
			DockerHint.Caption := 'Getting Docker Desktop ready - this window waits (up to 15 minutes)...';
			WizardForm.NextButton.Enabled := False;
			Exec('powershell.exe', Ps('ensure-docker', '-Yes'), '', SW_HIDE, ewWaitUntilTerminated, Code);
			WizardForm.NextButton.Enabled := True;
			LoadDefaults;
			ShowDocker;
			if Code <> 0 then begin
				SuppressibleMsgBox('Docker Desktop isn''t running yet. If its installer asked to restart Windows, ' +
					'restart, then run this Setup again. Otherwise start Docker Desktop and click Next.',
					mbError, MB_OK, IDOK);
				Result := False;
			end;
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

function Address(Param: String): String;
var Lines: TArrayOfString; I: Integer; Host, Port: String;
begin
	Host := HostnameEdit.Text; Port := PortEdit.Text;
	if LoadStringsFromFile(ExpandConstant('{app}\config\nginx\site.env'), Lines) then
		for I := 0 to GetArrayLength(Lines) - 1 do
			if Pos('NETROLLOUT_HOSTNAME=', Lines[I]) = 1 then Host := Copy(Lines[I], 21, Length(Lines[I]));
	if LoadStringsFromFile(ExpandConstant('{app}\.env'), Lines) then
		for I := 0 to GetArrayLength(Lines) - 1 do
			if Pos('HTTPS_PORT=', Lines[I]) = 1 then Port := Copy(Lines[I], 12, Length(Lines[I]));
	Result := 'https://' + Lowercase(Host);
	if Port <> '443' then Result := Result + ':' + Port;
end;

{ The review page (Ready to install): every choice, before anything is done }
function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
	MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
var Lines, Certificate, Shortcuts: String;
begin
	if UpdateMode then begin
		Result := 'Update:' + NewLine + Space + 'NetRollout ' + InstalledVersion + ' → {#AppVersion}' + NewLine + NewLine +
			'Folder:' + NewLine + Space + InstalledDir + NewLine + NewLine +
			'Kept:' + NewLine + Space + 'the data, the settings, the certificate, the backups' + NewLine + NewLine +
			'First:' + NewLine + Space + 'a backup (before-update), by the installed version' + NewLine + NewLine +
			'Downtime:' + NewLine + Space + 'about a minute (running rollouts finish first; everyone signs in again)';
		exit;
	end;
	Lines := 'Install folder:' + NewLine + Space + WizardDirValue + NewLine + NewLine;
	if Reinstall then
		Lines := Lines + 'Settings:' + NewLine + Space + 'kept from the NetRollout already in this folder' + NewLine + NewLine
	else begin
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

procedure SetOurPath(Add: Boolean);
var Path, Bin, NewPath: String; Entries: TArrayOfString; I: Integer; Found: Boolean;
begin
	Bin := ExpandConstant('{app}\bin');
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

{ One attempt: set NetRollout up, update it, or (set up already) start it;
  the exit code }
function RunSetUp(const Log: String): Integer;
var Args: String;
begin
	if UpdateMode then begin
		Args := 'update -Yes -NoBrowser';
		WizardForm.StatusLabel.Caption := 'Updating NetRollout - downloading the new version, then a restart (about a minute)...';
		WizardForm.ProgressGauge.Style := npbstMarquee;
		{ appended: the log of the preparation is in the same file }
		Exec(ExpandConstant('{cmd}'), '/C powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' +
			ExpandConstant('{app}\bin\manage.ps1') + '" ' + Args + ' >> "' + Log + '" 2>&1',
			ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Result);
		WizardForm.ProgressGauge.Style := npbstNormal;
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
	WizardForm.ProgressGauge.Style := npbstMarquee;
	Exec(ExpandConstant('{cmd}'), '/C powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' +
		ExpandConstant('{app}\bin\manage.ps1') + '" ' + Args + ' > "' + Log + '" 2>&1',
		ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Result);
	WizardForm.ProgressGauge.Style := npbstNormal;
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
  refused) and a backup by the installed version. Non-empty: why Setup stops. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer; Log: String;
begin
	Result := '';
	if not UpdateMode then exit;
	ForceDirectories(InstalledDir + '\logs');
	Log := InstalledDir + '\logs\update.log';
	Exec(ExpandConstant('{cmd}'), '/C powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' +
		ExpandConstant('{tmp}\manage.ps1') + '" prepare-update -Yes -InstallDir "' + InstalledDir +
		'" -NewVersion {#AppVersion} > "' + Log + '" 2>&1', InstalledDir, SW_HIDE, ewWaitUntilTerminated, Code);
	if Code <> 0 then
		Result := 'Nothing was changed - NetRollout ' + InstalledVersion + ' keeps running.' + #13#10#13#10 +
			LogTail(Log) + #13#10 + 'The whole log: ' + Log;
end;

{ After the files: the script sets NetRollout up and starts it. A failure
  that can be fixed here offers Retry (a silent install cancels) }
procedure CurStepChanged(CurStep: TSetupStep);
var Log, Text: String;
begin
	if CurStep <> ssPostInstall then exit;
	if WizardIsTaskSelected('addtopath') then SetOurPath(True);
	ForceDirectories(ExpandConstant('{app}\logs'));
	if UpdateMode then Log := ExpandConstant('{app}\logs\update.log')
	else Log := ExpandConstant('{app}\logs\install.log');
	SetUpLog := Log;
	while True do begin
		SetUpCode := RunSetUp(Log);
		SetUpFailed := SetUpCode <> 0;
		if not SetUpFailed then break;
		if UpdateMode then Text := 'NetRollout''s files are updated, but the update didn''t finish:'
		else Text := 'NetRollout was installed, but setting it up didn''t finish:';
		Text := Text + #13#10#13#10 + LogTail(Log) + #13#10 + 'The whole log: ' + Log;
		if ReleaseBroken then begin
			SuppressibleMsgBox(Text, mbError, MB_OK, IDOK);
			break;
		end;
		if SuppressibleMsgBox(Text + #13#10#13#10 + 'Fix it and click Retry, or Cancel to finish without it.',
				mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then break;
	end;
end;

{ Uninstall: the containers go; the data only if asked }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Code: Integer; Data: String;
begin
	if CurUninstallStep = usPostUninstall then SetOurPath(False);
	if CurUninstallStep <> usUninstall then exit;
	Data := '-KeepData';
	if not UninstallSilent and (MsgBox('Also delete NetRollout''s data - the database, settings, ' +
		'certificates, logs and backups?' + #13#10#13#10 + 'This can''t be undone. Keep it to install ' +
		'again later with everything as it was.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
		Data := '-DeleteData';
	Exec('powershell.exe', '-NoProfile -ExecutionPolicy Bypass -File "' +
		ExpandConstant('{app}\bin\manage.ps1') + '" uninstall -Yes ' + Data,
		ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
end;
