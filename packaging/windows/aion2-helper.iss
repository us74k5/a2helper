; Inno Setup script. Build: ISCC /DVersion=3.0.0 /DStage=<folder> /O<outdir> aion2-helper.iss
; <Stage> is PyInstaller's output folder (aion2-helper.exe and _internal\).
#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Stage
  #define Stage "..\..\dist\aion2-helper"
#endif
#ifndef Root
  #define Root "..\.."
#endif

[Setup]
AppId={{B7C4F1D2-3A5E-4F60-9C1B-2E8D7A6F5B41}
AppName=AION 2 Helper
AppVersion={#Version}
AppPublisher=AION 2 Helper
VersionInfoDescription=AION 2 Helper setup
VersionInfoProductName=AION 2 Helper
DefaultDirName={autopf}\AION2Helper
DefaultGroupName=AION 2 Helper
; Per-user install: no administrator prompt.
PrivilegesRequired=lowest
OutputBaseFilename=AION2Helper-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
UninstallDisplayIcon={app}\aion2-helper.exe
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#Root}\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "{#Root}\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Root}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
Name: "{group}\AION 2 Helper"; Filename: "{app}\aion2-helper.exe"
Name: "{group}\License and notices"; Filename: "{app}\THIRD-PARTY-NOTICES.md"
Name: "{group}\Uninstall AION 2 Helper"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AION 2 Helper"; Filename: "{app}\aion2-helper.exe"; Tasks: desktopicon

[Run]
; The cube overlay reads game traffic through Npcap, which may not be bundled (its license forbids it).
Filename: "https://npcap.com/#download"; Description: "Open the Npcap download page (the cube overlay needs Npcap)"; Flags: postinstall shellexec skipifsilent; Check: NeedsNpcap
Filename: "{app}\aion2-helper.exe"; Description: "Open AION 2 Helper"; Flags: postinstall nowait skipifsilent

[Code]
function NeedsNpcap(): Boolean;
begin
  Result := not FileExists(ExpandConstant('{sys}\Npcap\wpcap.dll'));
end;
