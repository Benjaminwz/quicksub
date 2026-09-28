; 快字幕 QuickSub Windows 安裝程式（Inno Setup 6）
; 不要直接編譯：執行 build_release.ps1，它會先用 PyInstaller 做好 dist\QuickSub\，再呼叫 ISCC。
; 裝在使用者自己的資料夾（不用系統管理員權限）。AI 模型、顯示卡元件放在 %LOCALAPPDATA%\QuickSub，解除安裝時會問要不要一起刪。
; 測試用：ISCC /DTESTBUILD 會做一個不同 AppId、不建捷徑的版本，可以裝到任意資料夾試。

#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
#ifdef TESTBUILD
AppId={{5B7C2E91-TEST-4A3D-9F0E-000000000000}
#else
AppId={{5B7C2E91-6D48-4A3D-9F0E-7C1B2A9D4E63}
#endif
AppName=QuickSub
AppVersion={#AppVersion}
AppVerName=QuickSub {#AppVersion}
AppPublisher=Benjaminwz
AppPublisherURL=https://github.com/Benjaminwz/quicksub
AppSupportURL=https://github.com/Benjaminwz/quicksub/issues
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\QuickSub
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist
OutputBaseFilename=QuickSub-Setup-{#AppVersion}
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\QuickSub.exe
WizardStyle=modern
WizardImageFile=wizard.bmp,wizard_2x.bmp
WizardSmallImageFile=wizard_small.bmp,wizard_small_2x.bmp
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
ShowLanguageDialog=no
LanguageDetectionMethod=uilanguage

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "zh"; MessagesFile: "ChineseTraditional.isl"

[CustomMessages]
en.AppTitle=QuickSub
zh.AppTitle=快字幕
en.SendTo=QuickSub (make subtitles)
zh.SendTo=快字幕（產生字幕）
en.TaskSendTo=Add QuickSub to the right-click "Send to" menu
zh.TaskSendTo=在檔案右鍵的「傳送到」加入快字幕
en.LaunchApp=Open QuickSub now
zh.LaunchApp=現在打開快字幕
en.DeleteData=Also delete the downloaded AI models and GPU files (%1)?%n%nChoose No if you plan to install QuickSub again.
zh.DeleteData=要一起刪掉下載過的 AI 模型和顯示卡元件嗎（%1）？%n%n之後還會再裝快字幕的話，選「否」就不用重新下載。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "sendto"; Description: "{cm:TaskSendTo}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 舊版留下的程式檔（新版可能少了某些檔案）
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\QuickSub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
#ifndef TESTBUILD
Name: "{autoprograms}\{cm:AppTitle}"; Filename: "{app}\QuickSub.exe"; AppUserModelID: "Benjaminwz.QuickSub"
Name: "{usersendto}\{cm:SendTo}"; Filename: "{app}\QuickSub.exe"; Tasks: sendto
#endif
Name: "{autodesktop}\{cm:AppTitle}"; Filename: "{app}\QuickSub.exe"; Tasks: desktopicon; AppUserModelID: "Benjaminwz.QuickSub"

[Run]
Filename: "{app}\QuickSub.exe"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent
; 從程式裡按「更新」時是靜默安裝（/SILENT），裝完直接重新打開
Filename: "{app}\QuickSub.exe"; Flags: nowait skipifnotsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im QuickSub.exe /f"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
function DirSize(const Dir: String): Int64;
var
  R: TFindRec;
begin
  Result := 0;
  if FindFirst(Dir + '\*', R) then
  try
    repeat
      if (R.Name <> '.') and (R.Name <> '..') then
      begin
        if R.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
          Result := Result + DirSize(Dir + '\' + R.Name)
        else
          Result := Result + (Int64(R.SizeHigh) shl 32) + R.SizeLow;
      end;
    until not FindNext(R);
  finally
    FindClose(R);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
  Size: Int64;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  Data := ExpandConstant('{localappdata}\QuickSub');
  if not DirExists(Data) then
    exit;
  Size := DirSize(Data);
  if UninstallSilent then
    exit;
  if MsgBox(FmtMessage(CustomMessage('DeleteData'), [Format('%.1f GB', [Size / 1073741824.0])]), mbConfirmation, MB_YESNO) = IDYES then
    DelTree(Data, True, True, True);
end;
