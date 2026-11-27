; netzsim (rtpowerflow) - Windows installer, Inno Setup 6.3+ / 7
;
; Do not compile this by hand: packaging\windows\build.ps1 stages the payload
; (embedded Python runtime with all packages, the netzsim sources, the built
; React UI, the committed dataset) and then calls ISCC with the /D defines
; below.
;
; Per-user install: no admin rights, no UAC prompt, nothing outside
; {localappdata}. That also keeps data\ writable - netzsim stores recordings,
; imported grids and saved scenarios there.
;
; Keep this file pure ASCII (Inno Setup 7 rejects bytes that are invalid in
; the file's code page).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\build\windows\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif

#define AppName "netzsim"
#define AppUrl "https://github.com/markisbell/rtpowerflow"
; what the start-menu entry runs: the bundled interpreter, no console script
#define Launcher "{app}\python\python.exe"
#define LauncherArgs "-m netzsim.desktop"

[Setup]
AppId={{6207A85D-28D7-4934-B20E-D6F9EA4EDD4D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Markus Bell
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
VersionInfoVersion={#AppVersionNumeric}
VersionInfoDescription=netzsim - realtime distribution-grid simulator (setup)
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile={#StageDir}\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename=netzsim-setup-{#AppVersion}
SetupIconFile=netzsim.ico
UninstallDisplayIcon={app}\netzsim.ico
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; netzsim.desktop holds this mutex while it runs: setup and uninstall then ask
; the user to close the simulator first instead of failing on locked files
AppMutex=netzsim-desktop-running

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; an update replaces the program folders wholesale (no stale modules from the
; previous version) - data\ is never touched
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\ui"
Type: filesandordirs; Name: "{app}\licenses"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{#Launcher}"; \
  Parameters: "{#LauncherArgs}"; WorkingDir: "{app}"; \
  IconFilename: "{app}\netzsim.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{#Launcher}"; \
  Parameters: "{#LauncherArgs}"; WorkingDir: "{app}"; \
  IconFilename: "{app}\netzsim.ico"; Tasks: desktopicon

[Run]
Filename: "{#Launcher}"; Parameters: "{#LauncherArgs}"; WorkingDir: "{app}"; \
  Description: "{cm:LaunchProgram,{#AppName}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; bytecode caches Python wrote next to the installed modules. Files the USER
; created under data\ (recordings, own grids, saved scenarios) are kept.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\src"
Type: filesandordirs; Name: "{app}\ui"
; folders the simulator created itself go away only when they are empty
Type: dirifempty; Name: "{app}\data\recordings"
Type: dirifempty; Name: "{app}\data\user_grids"
Type: dirifempty; Name: "{app}\data\scenarios"
Type: dirifempty; Name: "{app}\data"
Type: dirifempty; Name: "{app}"

[Code]
// [InstallDelete] wipes {app}\python, {app}\src and {app}\ui before copying.
// That is right for an update of netzsim - and destructive if somebody points
// the wizard at an existing folder of something else (or at a source
// checkout). So: only an empty/new folder or an existing netzsim install.

function DirIsEmpty(const Dir: String): Boolean;
var
  Rec: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Dir) + '*', Rec) then
  try
    repeat
      if (Rec.Name <> '.') and (Rec.Name <> '..') then
        Result := False;
    until (not Result) or (not FindNext(Rec));
  finally
    FindClose(Rec);
  end;
end;

function IsNetzsimInstall(const Dir: String): Boolean;
begin
  // the root files setup writes first; a source checkout has pyproject.toml
  Result := (FileExists(AddBackslash(Dir) + 'netzsim.bat')
             or FileExists(AddBackslash(Dir) + 'netzsim.ico'))
            and not FileExists(AddBackslash(Dir) + 'pyproject.toml');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir: String;
begin
  Result := '';
  Dir := ExpandConstant('{app}');
  if DirExists(Dir) and (not DirIsEmpty(Dir)) and (not IsNetzsimInstall(Dir)) then
  begin
    // umlauts as character codes: this file stays pure ASCII
    if ActiveLanguage = 'german' then
      Result := 'Der Ordner "' + Dir + '" ist nicht leer und enth' + #228 +
        'lt keine netzsim-Installation.' + #13#10 + #13#10 +
        'Bitte "Zur' + #252 + 'ck" w' + #228 + 'hlen und einen neuen oder leeren ' +
        'Ordner angeben - Setup ersetzt darin die Unterordner python, src und ui ' +
        'vollst' + #228 + 'ndig.'
    else
      Result := 'The folder "' + Dir + '" is not empty and does not contain a ' +
        'netzsim installation.' + #13#10 + #13#10 +
        'Please go back and choose a new or empty folder - setup replaces the ' +
        'subfolders python, src and ui in it completely.';
  end;
end;
