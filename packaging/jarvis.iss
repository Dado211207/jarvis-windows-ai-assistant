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
; No enclosing braces here — AppId= below supplies them via the {{
; escape (a literal "{") followed by this constant's raw text, which is
; the standard Inno Setup idiom. Defining the braces in *both* places
; would double them up into a malformed GUID.
#define MyAppId "B4E6D9A1-7C2E-4F3A-9B1D-6A8E3F0C5D42"

[Setup]
AppId={{#MyAppId}
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
; oversight. PrivilegesRequiredOverridesAllowed=dialog lets a user who
; specifically wants a per-machine install opt into elevation; nothing
; here requires it.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
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

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
  Answer: Integer;
  ShouldDelete: Boolean;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := GetJarvisDataDir();
    if DirExists(DataDir) then
    begin
      ShouldDelete := False;
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
      else
      begin
        Answer := MsgBox(
          'JARVIS has been uninstalled.' + #13#10 + #13#10 +
          'Your JARVIS data (local database, logs, and any downloaded speech model) is still on this computer, at:' + #13#10 +
          DataDir + #13#10 + #13#10 +
          'Do you also want to permanently delete this data? This cannot be undone.',
          mbConfirmation, MB_YESNO or MB_DEFBUTTON2
        );
        ShouldDelete := (Answer = IDYES);
      end;

      if ShouldDelete then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
