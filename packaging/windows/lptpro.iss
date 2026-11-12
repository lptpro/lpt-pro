; LPT Pro - Windows installer (Inno Setup 6, free: https://jrsoftware.org/isinfo.php)
; Build:  ISCC.exe /DAppVersion=0.16.0 /DDistDir=..\..\dist\LPTPro packaging\windows\lptpro.iss
; Installs for the current user only (no administrator rights needed).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\..\dist\LPTPro"
#endif

[Setup]
AppId={{FC41982F-3526-40FD-AAC6-FC83E914CC16}
AppName=LPT Pro
AppVersion={#AppVersion}
AppVerName=LPT Pro {#AppVersion}
AppPublisher=LPT Pro contributors
AppPublisherURL=https://github.com/lptpro/lpt-pro
AppSupportURL=https://github.com/lptpro/lpt-pro/issues
AppComments=Copy photos and videos from your iPhone to this PC
DefaultDirName={autopf}\LPT Pro
DefaultGroupName=LPT Pro
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
WizardStyle=modern
SetupIconFile=..\..\lptpro\icon.ico
UninstallDisplayIcon={app}\LPTPro.exe
UninstallDisplayName=LPT Pro
OutputDir=..\..\dist
OutputBaseFilename=LPT-Pro-Setup-{#AppVersion}
Compression=lzma2/normal
SolidCompression=yes
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put an LPT Pro icon on the desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\LPT Pro"; Filename: "{app}\LPTPro.exe"; Comment: "Copy photos and videos from your iPhone"
Name: "{autodesktop}\LPT Pro"; Filename: "{app}\LPTPro.exe"; Tasks: desktopicon; Comment: "Copy photos and videos from your iPhone"

[Run]
Filename: "{app}\LPTPro.exe"; Description: "Start LPT Pro now"; Flags: nowait postinstall skipifsilent

[Code]
const
  AppleStoreLink = 'ms-windows-store://pdp/?productid=9NP83LWLPZ9K';
  CheckMark = #$2714;
  CrossMark = #$2716;

var
  CheckPage: TWizardPage;
  PythonLabel, AppleLabel, AppleNote: TNewStaticText;
  StoreButton, RecheckButton: TNewButton;

function AppleServiceInstalled(): Boolean;
var
  rc: Integer;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service');
  if not Result then
    Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query "Apple Mobile Device Service"', '',
                   SW_HIDE, ewWaitUntilTerminated, rc) and (rc = 0);
end;

procedure UpdateAppleStatus();
begin
  if AppleServiceInstalled() then
  begin
    AppleLabel.Caption := CheckMark + '  Apple''s iPhone USB service: found. You''re all set.';
    AppleLabel.Font.Color := clGreen;
    AppleNote.Caption := 'iTunes is not required and not recommended.';
    StoreButton.Visible := False;
    RecheckButton.Visible := False;
  end
  else
  begin
    AppleLabel.Caption := CrossMark + '  Apple''s iPhone USB service: NOT found.';
    AppleLabel.Font.Color := $004A99F2;
    AppleNote.Caption :=
      'LPT Pro needs the free "Apple Devices" app from the Microsoft Store to talk to an ' +
      'iPhone over the USB cable.' + #13#10 + #13#10 +
      'iTunes is NOT required and not recommended.' + #13#10 + #13#10 +
      'You can finish installing LPT Pro now and install Apple Devices afterwards. ' +
      'LPT Pro connects by itself once it is there.';
    StoreButton.Visible := True;
    RecheckButton.Visible := True;
  end;
end;

procedure StoreButtonClick(Sender: TObject);
var
  ec: Integer;
begin
  ShellExec('open', AppleStoreLink, '', '', SW_SHOWNORMAL, ewNoWait, ec);
end;

procedure RecheckButtonClick(Sender: TObject);
begin
  UpdateAppleStatus();
end;

procedure InitializeWizard();
var
  top: Integer;
begin
  CheckPage := CreateCustomPage(wpWelcome, 'Checking this PC',
    'LPT Pro looks for what it needs to talk to an iPhone.');

  PythonLabel := TNewStaticText.Create(CheckPage);
  PythonLabel.Parent := CheckPage.Surface;
  PythonLabel.Top := 0;
  PythonLabel.Font.Style := [fsBold];
  PythonLabel.Font.Color := clGreen;
  PythonLabel.Caption := CheckMark + '  Python: not needed. LPT Pro has everything it needs built in.';

  top := PythonLabel.Top + ScaleY(28);
  AppleLabel := TNewStaticText.Create(CheckPage);
  AppleLabel.Parent := CheckPage.Surface;
  AppleLabel.Top := top;
  AppleLabel.Font.Style := [fsBold];

  AppleNote := TNewStaticText.Create(CheckPage);
  AppleNote.Parent := CheckPage.Surface;
  AppleNote.Top := top + ScaleY(26);
  AppleNote.Width := CheckPage.SurfaceWidth;
  AppleNote.AutoSize := False;
  AppleNote.WordWrap := True;
  AppleNote.Height := ScaleY(110);

  StoreButton := TNewButton.Create(CheckPage);
  StoreButton.Parent := CheckPage.Surface;
  StoreButton.Top := AppleNote.Top + AppleNote.Height + ScaleY(6);
  StoreButton.Width := ScaleX(300);
  StoreButton.Height := ScaleY(28);
  StoreButton.Caption := 'Get Apple Devices from the Microsoft Store';
  StoreButton.OnClick := @StoreButtonClick;

  RecheckButton := TNewButton.Create(CheckPage);
  RecheckButton.Parent := CheckPage.Surface;
  RecheckButton.Top := StoreButton.Top;
  RecheckButton.Left := StoreButton.Left + StoreButton.Width + ScaleX(10);
  RecheckButton.Width := ScaleX(110);
  RecheckButton.Height := ScaleY(28);
  RecheckButton.Caption := 'Check again';
  RecheckButton.OnClick := @RecheckButtonClick;

  UpdateAppleStatus();
end;
