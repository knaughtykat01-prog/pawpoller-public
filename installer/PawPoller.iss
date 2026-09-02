; PawPoller Windows Installer
; ===========================
;
; Compiled via Inno Setup 6 (iscc). Build is driven from CI
; (.github/workflows/build.yml) — version comes in via /DMyAppVersion="x.y.z"
; passed to iscc. For local builds:
;   iscc /DMyAppVersion="2.23.3" installer\PawPoller.iss
;
; Source files come from PyInstaller's dist/PawPoller/ tree, which must
; exist before this script runs (CI builds it via `pyinstaller pawpoller.spec`).
;
; Install model:
;   - Per-user OR system-wide (user chooses on the privileges page).
;     Default per-user → no UAC prompt for the common case.
;   - User data lives in %APPDATA%\PawPoller — installer does NOT touch
;     it on install or uninstall so a reinstall preserves the SQLite DB,
;     settings.json, vault, logs. Uninstaller offers to wipe it via a
;     final prompt for users who actually want a clean slate.
;   - Autostart (HKCU\...\Run) is opt-in via task on the components page.
;     The in-app Settings → General toggle still works independently
;     after install — the installer task just pre-seeds it.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

#define MyAppName       "PawPoller"
#define MyAppPublisher  "KnaughtyKat"
#define MyAppURL        "https://github.com/knaughtykat01-prog/PawPoller"
#define MyAppExeName    "PawPoller.exe"
#define MyAppId         "{{A8E2F7B4-3D9C-4F1E-8B5A-2C7D9E1F0A6B}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; Default to per-user install (no UAC). User can flip to system-wide
; on the privileges page if they want it in Program Files.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

; Single-file installer artifact in installer\Output\
OutputDir=Output
OutputBaseFilename=PawPoller-Setup-{#MyAppVersion}

; Reasonable defaults
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
DisableWelcomePage=no
ShowLanguageDialog=no
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=..\LICENSE
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon";  Description: "Create a &desktop shortcut";                 GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startupicon";  Description: "Launch {#MyAppName} when Windows starts";    GroupDescription: "Run on login:";         Flags: unchecked

[Files]
; PyInstaller produces dist/PawPoller/ as a folder with PawPoller.exe
; plus all DLLs and bundled assets. Mirror that whole tree under {app}.
Source: "..\dist\{#MyAppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; Optional desktop shortcut (driven by Tasks)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; Uninstall entry under Start Menu — convenience
Name: "{autoprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Registry]
; HKCU\...\Run entry for autostart. Only written when the user ticks
; the startupicon task. Uses HKCU (current user) so no admin needed
; even on system-wide installs.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Tasks: startupicon; \
    Flags: uninsdeletevalue

[Run]
; Tick-on-finish to launch the app right after install. nowait so the
; installer can exit; postinstall + skipifsilent so /SILENT installs
; (auto-update path) don't pop the GUI unexpectedly.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Best-effort kill of any running PawPoller.exe before uninstall so the
; uninstaller can delete files without "in use" errors. Ignores result.
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#MyAppExeName}"; \
    Flags: runhidden; RunOnceId: "KillPawPoller"

[Code]
// On uninstall, offer to wipe user data (%APPDATA%\PawPoller). Default
// No — most uninstalls are upgrades or troubleshooting, not "delete
// everything". Users who really want a clean slate can tick Yes.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\{#MyAppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox(
        'Also delete your PawPoller data folder?' + #13#10 + #13#10 +
        DataDir + #13#10 + #13#10 +
        'This contains your SQLite database, settings, logs, and ' +
        'credential vault. Choose No to keep your data for a future ' +
        'reinstall.',
        mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
