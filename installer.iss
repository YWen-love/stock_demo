#define AppName "智能库存分析助手"
#define AppVersion "1.0.0"
#define AppPublisher "Stock Demo"
#define AppExeName "StockInventoryApp.exe"

[Setup]
AppId={{C0A7D85C-8C3D-4F90-9B6A-8C4DBF9B4E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\StockInventoryApp
DefaultGroupName={#AppName}
OutputDir=installer_output
OutputBaseFilename=StockInventorySetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\StockInventoryApp\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: ".env.example"; DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "启动{#AppName}"; Flags: nowait postinstall skipifsilent