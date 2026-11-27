; Agentsii One-Click API Installer - Windows setup (Inno Setup 6)
#define AppName "Agentsii One-Click API Installer"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E7C1B9A-3F42-4C8E-9A1D-0A6E7511C0DE}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Agentsii
DefaultDirName={localappdata}\Programs\Agentsii
DefaultGroupName=Agentsii
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=AgentsiiSetup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\AgentsiiInstaller.exe
WizardStyle=modern
WizardImageFile=..\assets\wizard.bmp
WizardSmallImageFile=..\assets\wizard-small.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
SetupMutex=AgentsiiSetupMutex

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#ifexist "compiler:Languages\Arabic.isl"
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\AgentsiiInstaller\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\docs\UserGuide-AR.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\UserGuide-EN.html"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Agentsii One-Click API Installer"; Filename: "{app}\AgentsiiInstaller.exe"
Name: "{group}\دليل الاستخدام (Arabic Guide)"; Filename: "{app}\UserGuide-AR.html"
Name: "{group}\User Guide"; Filename: "{app}\UserGuide-EN.html"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Agentsii API Installer"; Filename: "{app}\AgentsiiInstaller.exe"; Tasks: desktopicon

[UninstallRun]
Filename: "{app}\AgentsiiInstaller.exe"; Parameters: "--uninstall-cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "AgentsiiCleanup"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\Agentsii\bin"

[Run]
Filename: "{app}\AgentsiiInstaller.exe"; Description: "{cm:LaunchProgram,Agentsii}"; Flags: nowait postinstall skipifsilent
