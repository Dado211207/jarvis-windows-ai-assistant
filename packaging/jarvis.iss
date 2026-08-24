; JARVIS Windows installer — Inno Setup script.
;
; Pinned/documented target: Inno Setup 6.7.1. The actual latest stable
; 6.x release upstream is 6.7.3 (jrsoftware.org/isdl.php) — but this
; project's build pipeline installs Inno Setup via Chocolatey
; (scripts/build-installer.ps1 and
; .github/workflows/windows-installer.yml), and Chocolatey's community
; package repository lags upstream: 6.7.1 is the newest version actually
; published there (verified directly against
; community.chocolatey.org/api/v2/FindPackagesById()?id='innosetup', not
; assumed — an earlier pin at 6.7.3 failed CI for exactly this reason,
; "the package was not found with the source(s) listed"). 6.x was chosen
; deliberately over 7.0.2: it is the long-established, CI-tooling-mature
; line versus a very recent major-version release with less-proven
; automation support; a two-patch-release gap behind upstream within the
; 6.x line has no bearing on that reasoning. Inno Setup itself is a
; modified zlib/libpng license
; (permissive, free for any use including commercial — verified via
; jrsoftware.org/files/is/license.txt, not assumed from the website's
; separate, non-binding "please consider a commercial license" donation
; request) — not bundled into JARVIS's own distribution either way,
; same category as PyInstaller: a build tool, not a runtime dependency.
;
; Per-user install by default (PrivilegesRequired=lowest — no UAC, no
; admin rights needed or requested). Compiled from the onedir
; PyInstaller output at packaging\dist\JARVIS (see
; scripts\build-installer.ps1, which runs PyInstaller first, then ISCC
; against this script).
;
; Deliberately does NOT: add a firewall rule, bind the server
; externally, install a Windows service, add browser extensions, change
; file associations, add startup persistence without explicit consent
; (see the unchecked-by-default "startupicon" task below), or install
; unrelated runtimes/software.
;
; No SignTool= directive: this build is unsigned (no code-signing
; certificate is available). The installer output will trigger Windows
; SmartScreen on first run — expected and documented in the packaging
; report, not hidden. Do not add a self-signed certificate here and
; present it as trusted; that would be worse than staying unsigned.

#define MyAppName "JARVIS"
#define MyAppVersion "0.2.0-rc1"
; VersionInfoVersion (below) sets the Setup.exe/uninstaller's numeric
; Windows FILEVERSION/PRODUCTVERSION resource fields, which Inno Setup
; requires in strict X.X.X.X numeric form — "0.2.0-rc1" is rejected at
; compile time ("Value of [Setup] section directive "VersionInfoVersion"
; is invalid", caught for real on windows-latest CI). Matches
; packaging/version_info.txt's identical (0, 2, 0, 1) encoding for
; JARVIS.exe itself: 0.2.0, build 1 ("rc1"). VersionInfoTextVersion /
; VersionInfoProductTextVersion below carry the real "0.2.0-rc1" string
; into the same file's human-readable version fields, same split as
; version_info.txt's numeric filevers/prodvers vs. its FileVersion/
; ProductVersion StringStructs.
#define MyAppVersionInfo "0.2.0.1"
#define MyAppPublisher "Dado211207"
#define MyAppURL "https://github.com/Dado211207/jarvis-windows-ai-assistant"
#define MyAppExeName "JARVIS.exe"
; Fixed once, never regenerated across versions — this is what lets
; Inno Setup recognize "this is an upgrade of the same product" (so
; Programs & Features shows one entry with repair/upgrade/uninstall,
; not a duplicate) rather than an unrelated fresh install each time.
;
; No {curly-brace} GUID wrapper, deliberately: two earlier attempts at
; combining ISPP's {#MyAppId} substitution with Inno Setup's own {{
; literal-brace escape both failed — braces in both places doubled up
; into a malformed GUID (caught in review); braces in neither place plus
; a {{ escape left the value with no closing brace at all (caught for
; real on windows-latest CI, "A "}" is missing at the end of the
; constant"). Both failures came from the same root cause: ISPP
; substitution runs before Inno Setup's own {-escaping, so a { adjacent
; to the ISPP token gets consumed as part of {#MyAppId} instead of
; pairing the way it would in a plain, non-preprocessed value. Per
; Inno Setup's own AppId documentation (jrsoftware.org/ishelp/
; topic_setup_appid.htm), the braces were never required in the first
; place — its own example is the bare string "MyProgram", and "AppId is
; not used for display anywhere" — the {GUID} bracketed form is a
; Windows/COM styling convention, not an Inno Setup parsing requirement.
; Dropping it removes the escaping interaction entirely rather than
; attempting a third variation of it: AppId below is plain, unescaped
; {#MyAppId} substitution, resolving to the bare GUID text with no
; wrapping braces — still fixed, still unique, still under 127 chars.
#define MyAppId "B4E6D9A1-7C2E-4F3A-9B1D-6A8E3F0C5D42"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
AppComments=Local-first personal Windows AI assistant. Runs entirely on your machine; the local API binds to 127.0.0.1 only and is never exposed to your network.
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; No admin/UAC requirement — a real, deliberate choice, not an
; oversight. This is strictly a per-user install: do not allow an
; administrative/all-users override. The executable, mutable data,
; Credential Manager entries and Startup shortcut all belong to one
; Windows profile; exposing an all-users Start Menu entry that points
; into that profile would be both misleading and unusable by other users.
PrivilegesRequired=lowest
OutputDir=dist\installer
OutputBaseFilename=JARVIS-Setup-v{#MyAppVersion}-x64
SetupIconFile=..\app\ui\static\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion={#MyAppVersionInfo}
VersionInfoTextVersion={#MyAppVersion}
VersionInfoProductTextVersion={#MyAppVersion}
; Detects a running JARVIS.exe (via Windows Restart Manager) before
; install/upgrade/uninstall and prompts to close it — covers "detect a
; running process" and "prevent locked-file failures" without hand-
; rolled process-kill Pascal Script. RestartApplications=no: JARVIS is
; not silently relaunched after a Restart-Manager-driven close: the
; user (or, for a fresh install, the optional "launch when finished"
; task below) is what starts it again, not this mechanism.
CloseApplications=yes
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
; Two copies of Setup running at once would race on the same {app}
; directory. A double-click on the downloaded installer while the first
; one is still extracting is the ordinary way that happens; the second
; is told to wait rather than corrupting the first one's output.
SetupMutex=JARVIS-Setup-{#MyAppId}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startupicon"; Description: "Start {#MyAppName} automatically when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Checked by default (no Flags: unchecked) — "Launch JARVIS when setup
; finishes" is enabled by default per this pass's requirements.
; skipifsilent: a silent/unattended install (CI) never auto-launches
; the app, matching "no side effects a human didn't ask for" in
; automation.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Carried between the two uninstall steps: the choice is made while
// {app}\JARVIS.exe still exists (usUninstall), and the data directory is
// swept after the files have gone (usPostUninstall).
var
  UninstallCompleteRemoval: Boolean;
  UninstallCleanupSucceeded: Boolean;
  UninstallCleanupReportPath: String;

// ---------------------------------------------------------------------------
// WebView2: install it if it is missing, rather than letting the app
// discover that at first launch.
//
// JARVIS's window is a WebView2 control. On a machine without the
// runtime the app used to start, fail to build a window, and fall back
// to a browser tab — which is what made the browser look like the
// product's real interface. Detecting it here and installing it once,
// during setup, is where that belongs.
//
// The URL is Microsoft's own permanent link to the Evergreen
// Bootstrapper, a ~2 MB downloader that fetches and installs the current
// runtime. It is not redistributed in this installer — bundling it would
// pin a version that goes stale, and Microsoft's guidance is to use this
// link.
//
// **A failure here never fails the install.** An offline machine, a
// blocked download or a declined elevation all leave JARVIS installed
// and working, minus the native window; Diagnostics then reports exactly
// what is missing with a link. Aborting a working installation over an
// optional component would be the worse trade.
// ---------------------------------------------------------------------------
const
  WebView2ClientKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Wow6432Key = 'Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2BootstrapperUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function VersionMeansInstalled(const Version: String): Boolean;
begin
  // Microsoft documents "0.0.0.0" as explicitly meaning "not installed"
  // rather than the key being absent. Matches
  // app/launcher/runtime_check.py::_read_webview2_version().
  Result := (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, WebView2Wow6432Key, 'pv', Version) then
    if VersionMeansInstalled(Version) then
    begin
      Result := True;
      Exit;
    end;
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, WebView2ClientKey, 'pv', Version) then
    if VersionMeansInstalled(Version) then
    begin
      Result := True;
      Exit;
    end;
  if RegQueryStringValue(HKEY_CURRENT_USER, WebView2ClientKey, 'pv', Version) then
    Result := VersionMeansInstalled(Version);
end;

function OnWebView2DownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;
end;

procedure EnsureWebView2();
var
  BootstrapperPath: String;
  ResultCode: Integer;
begin
  if WebView2Installed() then
    Exit;

  Log('WebView2 runtime not found; fetching the Microsoft bootstrapper.');
  try
    DownloadTemporaryFile(
      WebView2BootstrapperUrl, 'MicrosoftEdgeWebview2Setup.exe', '',
      @OnWebView2DownloadProgress);
  except
    // Offline, or the download was blocked. The app still installs.
    Log('WebView2 bootstrapper could not be downloaded: ' + GetExceptionMessage);
    Exit;
  end;

  BootstrapperPath := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
  // Per-user install, silent: matches this installer's own
  // PrivilegesRequired=lowest, so it never provokes an elevation prompt
  // in an install the user chose to run without one.
  if Exec(BootstrapperPath, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('WebView2 bootstrapper finished with exit code ' + IntToStr(ResultCode))
  else
    Log('WebView2 bootstrapper could not be started.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Runs in silent installs too, which NextButtonClick would not — the
  // CI clean-install test and any unattended deployment both take this
  // path. Always returns '' : a non-empty result aborts setup, and a
  // missing optional runtime is not grounds for that.
  Result := '';
  EnsureWebView2();
end;

function GetJarvisDataDir(): String;
begin
  // Matches app/core/app_paths.py::app_data_root() exactly for a
  // packaged (frozen) process: %LOCALAPPDATA%\JARVIS — deliberately
  // the sibling of, not inside, {localappdata}\Programs\JARVIS (the
  // {app} install directory above). Because these are two separate
  // trees, a normal file-overwrite upgrade of {app} can never touch
  // user data — there is nothing to migrate, by construction, not
  // because of any special-cased upgrade logic here.
  Result := ExpandConstant('{localappdata}\JARVIS');
end;

// Runs the application's own cleanup, before its files are removed.
//
// Inno removes what Inno installed. It has never heard of the sign-in
// shortcut the *application* writes when somebody switches that on in
// Settings, and it does not know how the API key was stored — only
// app/core/credentials.py knows that. An installer guessing at a
// Windows Credential Manager target name is how an uninstall leaves a
// secret behind while reporting success. So the application is asked
// to remove its own things. See app/launcher/uninstall.py and
// app/core/ownership.py, which is the manifest of what "everything
// JARVIS owns" actually means.
//
// Cleanup writes a durable report under {tmp} and returns non-zero when
// anything remains. The uninstaller then keeps the data directory and
// copies the report there, so a full-uninstall failure is visible and
// the only recovery evidence is not deleted by the post-uninstall sweep.
function RunApplicationCleanup(PurgeData: Boolean): Boolean;
var
  Exe: String;
  Params: String;
  ResultCode: Integer;
begin
  Result := False;
  UninstallCleanupReportPath := ExpandConstant('{tmp}\JARVIS-uninstall-cleanup.json');
  DeleteFile(UninstallCleanupReportPath);

  Exe := ExpandConstant('{app}\{#MyAppExeName}');
  if not FileExists(Exe) then
  begin
    Log('JARVIS cleanup executable is missing; cleanup cannot be verified.');
    Exit;
  end;

  Params := '--uninstall-cleanup --report-file="' + UninstallCleanupReportPath + '"';
  if PurgeData then
    Params := Params + ' --purge-data';

  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Log('JARVIS cleanup could not be started; recovery evidence will be preserved.');
    Exit;
  end;

  if ResultCode <> 0 then
  begin
    Log('JARVIS cleanup returned exit code ' + IntToStr(ResultCode) +
      '; recovery evidence will be preserved.');
    Exit;
  end;

  if not FileExists(UninstallCleanupReportPath) then
  begin
    Log('JARVIS cleanup returned success without its report; treating cleanup as incomplete.');
    Exit;
  end;

  Result := True;
end;

procedure PreserveCleanupEvidence(DataDir: String);
var
  EvidencePath: String;
begin
  ForceDirectories(DataDir);
  EvidencePath := DataDir + '\uninstall-cleanup-report.json';

  if FileExists(UninstallCleanupReportPath) then
  begin
    if FileCopy(UninstallCleanupReportPath, EvidencePath, False) then
      Log('JARVIS uninstall cleanup report preserved at ' + EvidencePath)
    else
      Log('JARVIS uninstall cleanup report could not be copied to ' + EvidencePath);
  end
  else
  begin
    EvidencePath := DataDir + '\uninstall-cleanup-error.txt';
    SaveStringToFile(
      EvidencePath,
      'JARVIS uninstall cleanup could not be verified. The data folder was kept for recovery.' + #13#10,
      False
    );
    Log('JARVIS uninstall cleanup failure notice preserved at ' + EvidencePath);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Answer: Integer;
  ShouldDelete: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    // Asked here, while {app}\JARVIS.exe still exists, and answered
    // before anything is deleted. The prompt is the one place a person
    // gets to choose between "uninstall" and "uninstall and forget me",
    // so it has to say exactly what each one means.
    ShouldDelete := False;
    DataDir := GetJarvisDataDir();

    if UninstallSilent() then
    begin
      // A silent uninstall (e.g. CI's automated clean-install test)
      // never shows a blocking dialog. Data is preserved unless
      // explicitly opted into removal via an explicit /DELETEDATA=yes
      // command-line flag — the same "unchecked/no by default" rule
      // as the interactive prompt below, just expressed as a flag a
      // human never has to type by accident.
      ShouldDelete := (CompareText(ExpandConstant('{param:DELETEDATA|no}'), 'yes') = 0);
    end
    else if DirExists(DataDir) then
    begin
      Answer := MsgBox(
        'Remove everything JARVIS owns?' + #13#10 + #13#10 +
        'Choosing No uninstalls the application and keeps your settings, chat history, ' +
        'saved API key and any voice or speech model you downloaded, at:' + #13#10 +
        DataDir + #13#10 + #13#10 +
        'Choosing Yes also deletes all of that, permanently, and removes your API key ' +
        'from Windows Credential Manager. This cannot be undone.' + #13#10 + #13#10 +
        'Either way, JARVIS never removes shared Windows components such as WebView2 ' +
        'or the Visual C++ Runtime, never removes Ollama or its models, and never ' +
        'touches your notes in Documents\JARVIS_Notes.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2
      );
      ShouldDelete := (Answer = IDYES);
    end;

    UninstallCleanupSucceeded := RunApplicationCleanup(ShouldDelete);
    UninstallCompleteRemoval := ShouldDelete and UninstallCleanupSucceeded;

    if not UninstallCleanupSucceeded then
    begin
      PreserveCleanupEvidence(DataDir);
      if not UninstallSilent() then
        MsgBox(
          'JARVIS could not finish removing its Startup shortcut and/or saved credentials.' +
          #13#10 + #13#10 +
          'Your data was kept for recovery at:' + #13#10 + DataDir + #13#10 + #13#10 +
          'Review uninstall-cleanup-report.json there. You may need to remove JARVIS entries ' +
          'from Windows Credential Manager manually.',
          mbError, MB_OK
        );
    end;
  end;

  if CurUninstallStep = usPostUninstall then
  begin
    // A belt-and-braces sweep of the data directory. The application's
    // own cleanup above is the one that knows about the credential
    // store; this catches the case where the executable was already
    // gone (a partially removed installation) and there was nothing to
    // run.
    DataDir := GetJarvisDataDir();
    if UninstallCompleteRemoval and DirExists(DataDir) then
      DelTree(DataDir, True, True, True);
  end;
end;
