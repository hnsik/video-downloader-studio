; Inno Setup 6. Параметры: /DAppVersion=6.0.0
#ifndef AppVersion
  #define AppVersion "6.0.0"
#endif

[Setup]
AppId={{6D1B6C0E-3C77-4B0B-9E55-5C6B1E0A7A11}
AppName=Video Downloader Studio
AppVersion={#AppVersion}
AppPublisher=hnsik
DefaultDirName={autopf}\VideoDownloaderStudio
DefaultGroupName=Video Downloader Studio
UsePreviousAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=VideoDownloaderStudio-Update-Windows-x64-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName=Video Downloader Studio

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[Files]
; Установка поверх старой версии: одноимённые файлы заменяются, пользовательские данные (%APPDATA%) не затрагиваются.
Source: "..\dist\VideoDownloaderStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\bundled\*.exe"; DestDir: "{app}\bundled"; Flags: ignoreversion

[Icons]
Name: "{group}\Video Downloader Studio"; Filename: "{app}\VideoDownloaderStudio.exe"
Name: "{group}\Удалить Video Downloader Studio"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Video Downloader Studio"; Filename: "{app}\VideoDownloaderStudio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\VideoDownloaderStudio.exe"; Description: "Запустить Video Downloader Studio"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Удаляются только файлы программы. Скачанные видео, настройки, история и загруженные компоненты
; (%APPDATA%\VideoDownloaderStudio, %LOCALAPPDATA%\VideoDownloaderStudio) сохраняются.
Type: filesandordirs; Name: "{app}\bundled"
