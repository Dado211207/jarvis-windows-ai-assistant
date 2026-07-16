; JARVIS Windows Installer — Inno Setup script.
;
; Produces JARVIS-Setup-<version>.exe: a per-user installer that requires no
; Administrator privileges and installs under %LOCALAPPDATA%\Programs\JARVIS
; (never Program Files). See docs/WINDOWS_INSTALLER.md for the full
; architecture, build process, and signing-readiness notes.
;
; Prerequisite: a PyInstaller --onedir build already produced at
; dist\JARVIS\ (see .github/workflows/windows-build.yml), containing
; JARVIS.exe and its bundled dependencies.
;
; Version is NOT hardcoded here — app/__init__.py's __version__ is the single
; source of truth. Pass it at compile time:
;   iscc /DMyAppVersion=0.1.7-alpha installer\JARVIS.iss
; (the CI workflow extracts it from app/__init__.py automatically).

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

#define MyAppName "JARVIS"
#define MyAppPublisher "JARVIS"
#define MyAppExeName "JARVIS.exe"
#define MyAppSourceDir "..\dist\JARVIS"

[Setup]
AppId={{6F1B6E6E-6C0B-4B93-9C3E-2B7B2D6C9E11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=https://github.com/dado211207/jarvis-windows-ai-assistant
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install, no elevation required. PrivilegesRequiredOverridesAllowed
; still lets a user choose an all-users install manually if they want to, but
; that is never the default and is never required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=JARVIS-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
; This build is unsigned. See docs/WINDOWS_INSTALLER.md "Code signing" for
; the documented SmartScreen expectation and the signing pipeline this is
; architected for (not enabled). Do not claim or fake a signature here.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// --- Uninstall: user data is preserved by default. -------------------------
// Deleting %LOCALAPPDATA%\JARVIS (settings, memory, conversation history,
// logs, the stored API key) is opt-in only, and the default answer is "No"
// (MB_DEFBUTTON2) so a silent/scripted uninstall or an Enter keypress never
// deletes user data.

var
  ShouldDeleteUserData: Boolean;

function InitializeUninstall(): Boolean;
begin
  ShouldDeleteUserData := False;
  if MsgBox(
    'Also delete your JARVIS data (settings, personality memory, conversation ' + #13#10 +
    'history, logs, and your stored API key)?' + #13#10 + #13#10 +
    'This cannot be undone. Choose "No" to keep your data — for example if ' + #13#10 +
    'you plan to reinstall JARVIS later.',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    ShouldDeleteUserData := True;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and ShouldDeleteUserData then
  begin
    DelTree(ExpandConstant('{localappdata}\JARVIS'), True, True, True);
  end;
end;
