#ifndef AppVersion
  #define AppVersion "3.0.1"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\OfficeFlow"
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist\installer"
#endif
#ifndef SetupIcon
  #define SetupIcon "..\..\build\release\officeflow.ico"
#endif

[Setup]
AppId={{824ED7F8-1C70-4B62-A862-130A68B37C35}
AppName=OfficeFlow
AppVersion={#AppVersion}
AppVerName=OfficeFlow {#AppVersion}
AppPublisher=OfficeFlow
VersionInfoVersion={#AppVersion}.0
VersionInfoProductVersion={#AppVersion}.0
DefaultDirName={localappdata}\Programs\OfficeFlow
DefaultGroupName=OfficeFlow
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=OfficeFlow-{#AppVersion}-Setup
SetupIconFile={#SetupIcon}
UninstallDisplayIcon={app}\OfficeFlow.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes
DisableProgramGroupPage=auto
MinVersion=10.0

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면 바로가기 만들기"; GroupDescription: "추가 바로가기:"; Flags: unchecked
Name: "startup"; Description: "Windows에 로그인할 때 OfficeFlow 시작"; GroupDescription: "실행 옵션:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\OfficeFlow"; Filename: "{app}\OfficeFlow.exe"
Name: "{group}\OfficeFlow 사용자 안내"; Filename: "{app}\OfficeFlow-사용자안내.html"
Name: "{autodesktop}\OfficeFlow"; Filename: "{app}\OfficeFlow.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OfficeFlow v3"; ValueData: """{app}\OfficeFlow.exe"" --background"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\OfficeFlow.exe"; Description: "OfficeFlow 실행"; Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
Filename: "{app}\OfficeFlow.exe"; Parameters: "--remove-startup"; Flags: runhidden waituntilterminated; RunOnceId: "OfficeFlowRemoveStartup"
