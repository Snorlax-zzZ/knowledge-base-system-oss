; 知识库直装版 Windows 安装脚本
; 使用方式：由 build_direct_install.ps1 自动调用，或手动用 ISCC.exe 编译

#define AppName "百变怪芝士包"
#ifndef AppVersion
  #define AppVersion "1.3.13"
#endif
; AppExeName 含 onedir 子目录:PyInstaller onedir 输出
; bin\kb-tray\kb-tray.exe + bin\kb-tray\_internal\... (PyInstaller 6.x 结构)。
; onefile→onedir 是 1.3.12 的根治方案,避免 onefile 解压到 %TEMP%\_MEIxxxxx\
; 后被系统/用户清理(macOS launchd 3 天清,Windows 清理工具/手动清 TEMP 同款)
; 触发 /console 404(static/ HTML 被清,进程持有 .dll 不被清,典型 mismatch)。
; onedir 跟 exe 一起装到 install root,系统不会动 → 永不撞这个坑。
#define AppExeName "kb-tray\kb-tray.exe"
#define RootDir ".."

[Setup]
AppId={{D2B0E5C4-7F1A-4E3B-9A8C-1B2C3D4E5F60}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=knowledge-base-system
; 默认安装路径走 [Code] 段 GetDefaultInstallDir 动态决定：
; ASCII 循环 D-Z 找第一个存在的非 C 盘 → {drive}\KnowledgeBase；都没找到
; 退到 {userdocs}\KnowledgeBase。不在脚本里写死任何具体盘符——
; 默认值取决于用户机器实际配置，用户仍可在向导 dir page 手选其他位置。
DefaultDirName={code:GetDefaultInstallDir}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; 强制显示「选择安装位置 / 开始菜单文件夹」向导页，
; 不让 Inno 的 auto 模式因任何残留状态偷偷跳过
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=no
DisableReadyPage=no
OutputDir={#RootDir}\dist
OutputBaseFilename=KnowledgeBase-Setup-{#AppVersion}
SetupIconFile={#RootDir}\windows-app\assets\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; lowest = 默认低权限,装到用户能写的目录;若用户在向导改成 D:\KnowledgeBase
; 这种需 admin 写权限的位置,PrivilegesRequiredOverridesAllowed=dialog 会让
; Inno 自动弹 UAC elevate 提示
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 最低 Windows 10
MinVersion=10.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; 核心程序(onedir 模式,递归打包整个目录含 _internal/ 子目录)
; bin\kb-api\kb-api.exe + bin\kb-api\_internal\<.dlls/.pyds/app/static>
; bin\kb-tray\kb-tray.exe + bin\kb-tray\_internal\<.dlls/.pyds/assets>
Source: "{#RootDir}\bin\kb-api\*";   DestDir: "{app}\bin\kb-api";  Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RootDir}\bin\kb-tray\*";  DestDir: "{app}\bin\kb-tray"; Flags: ignoreversion recursesubdirs createallsubdirs
; 图标（供快捷方式使用）
Source: "{#RootDir}\windows-app\assets\app.ico"; DestDir: "{app}"; Flags: ignoreversion
; 引导配置 — 首次安装写入，升级时保留用户已修改的版本
Source: "{#RootDir}\config\config.toml"; DestDir: "{app}\config"; Flags: ignoreversion onlyifdoesntexist
; 使用说明
Source: "{#RootDir}\使用说明.md"; DestDir: "{app}"; Flags: ignoreversion
; 版本标识 — app/main.py 启动时读 {KB_APP_ROOT}\VERSION 作为 APP_VERSION
Source: "{#RootDir}\VERSION"; DestDir: "{app}"; Flags: ignoreversion
; 直装版重启脚本（/v1/system/restart 调用）
Source: "{#RootDir}\scripts\local-restart-direct.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; 安装根限定的停止脚本（卸载与开发 fallback 共用，禁止按进程名全局强杀）
Source: "{#RootDir}\scripts\local-stop.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
; Agent 接入工具包
Source: "{#RootDir}\agent-integration\kb-mcp-proxy.py"; DestDir: "{app}\agent-integration"; Flags: ignoreversion
Source: "{#RootDir}\agent-integration\SKILL.md"; DestDir: "{app}\agent-integration"; Flags: ignoreversion
Source: "{#RootDir}\agent-integration\安装说明.md"; DestDir: "{app}\agent-integration"; Flags: ignoreversion

[InstallDelete]
; 1.3.12 onefile→onedir 迁移:删 1.3.11 及以前的 bin\kb-api.exe + bin\kb-tray.exe
; 旧 onefile 单 exe 文件,新版变成 bin\kb-api\ + bin\kb-tray\ 目录。Inno 默认
; 不删 [Files] 段没列的文件,旧 .exe 不删会跟新目录共存(用户搞不清哪个是当前
; 版本,任务管理器看 exe 路径也乱)。升级路径必须显式清旧 onefile 残留。
Type: files; Name: "{app}\bin\kb-api.exe"
Type: files; Name: "{app}\bin\kb-tray.exe"
; 旧版 agent-integration 通配打包可能遗留 __pycache__/*.pyc；新版改成精确白名单后
; Inno 不会自动删除不再出现在 [Files] 的旧文件，升级时显式清扫。
Type: filesandordirs; Name: "{app}\agent-integration\__pycache__"

[Dirs]
; 运行时目录，预先建好避免权限问题
Name: "{app}\data"
Name: "{app}\logs"

[Icons]
; 安装根目录放一个启动快捷方式，双击即可启动
Name: "{app}\{#AppName}";             Filename: "{app}\bin\{#AppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\{#AppName}";           Filename: "{app}\bin\{#AppExeName}"; IconFilename: "{app}\app.ico"
Name: "{group}\卸载 {#AppName}";      Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}";   Filename: "{app}\bin\{#AppExeName}"; IconFilename: "{app}\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\使用说明.md"; Description: "查看使用说明"; Flags: nowait postinstall skipifsilent shellexec unchecked
Filename: "{app}\bin\{#AppExeName}"; Description: "启动百变怪芝士包"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前只终止当前安装根拥有的进程，不按 kb-api.exe / kb-tray.exe 名称全局强杀。
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\scripts\local-stop.ps1"" -IncludeTray"; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; RunOnceId: "StopOwnedProcesses"

[UninstallDelete]
; 卸载时始终清理"程序文件"，与 mac Uninstall.command 行为对齐：
;   - logs：运行时日志，下次重装会重建
;   - runtime：owner_token 等运行时状态，重装时重新生成
; 用户数据（data / models / embedding-service / auto-backup）由 [Code] 段
; 在 InitializeUninstall 里弹 4 个 MsgBox 询问，在 usPostUninstall 阶段按需删
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\runtime"

[Code]
// 与 macOS Install.command 行为对齐：
// 1. InitializeSetup —— 检测旧版 kb-api.exe / kb-tray.exe 是否在跑，
//    在跑则提示用户先退出再装（防 SQLite / Qdrant 拿到不一致 snapshot）
// 2. PrepareToInstall —— 升级场景下，先 cp {app}\data 到
//    {localappdata}\KnowledgeBase\auto-backup\{时间戳}\data，失败则 abort，
//    不动任何旧文件（与 mac 端 #3 审计修复一致）
//
// 关于 Mac 端 bug 5（升级丢 4.3GB models + venv）的 Windows 等价处理：
// Mac dmg 用 "原子切换" 模式（mv 整个 .app 包）→ 会把 data/models/venv 一起换掉
// → 必须显式 backup + inject 才能保住。
//
// Windows Inno Setup 是 "声明式覆盖" 模式 —— **只动 [Files] 段列的文件**：
//   - [Files] 段只列了 bin/ + config/ + 使用说明 + VERSION + scripts/ + agent-integration/
//   - models/ 和 embedding-service/ 不在 [Files] 段 → 升级时 Inno 完全不碰
//   - [InstallDelete] / [UninstallDelete] 也只清 logs/ 和 runtime/
// 所以升级时 models/ + embedding-service/ 天然保留，**Windows 不需要 Mac bug 5 同款的
// backup-inject 两阶段逻辑**。若未来往 [Files] 段加任何写到 {app}\models 或
// {app}\embedding-service 的条目，需同步扩展 PrepareToInstall 备份范围。

function GetDefaultInstallDir(Param: String): String;
var
  i: Integer;
  drv: String;
begin
  // 默认安装路径动态决定:ASCII 循环 D(68) 到 Z(90)找第一个存在的盘根,
  // 拼 "{drive}\KnowledgeBase" 作为默认值。代码里不写死任何具体盘符——
  // 默认值由用户机器实际可用盘决定;用户在向导 dir page 仍可改任何位置。
  // 实在没找到非 C 盘 → 退到 {userdocs}\KnowledgeBase(仍在 C 盘但避开
  // LocalAppData,提示意味更明显)。
  for i := 68 to 90 do
  begin
    drv := Chr(i) + ':\';
    if DirExists(drv) then
    begin
      Result := drv + 'KnowledgeBase';
      Exit;
    end;
  end;
  Result := ExpandConstant('{userdocs}\KnowledgeBase');
end;

function IsProcessRunning(const ExeName: String): Boolean;
var
  ResultCode: Integer;
  TmpFile: String;
  Lines: TArrayOfString;
  i: Integer;
begin
  Result := False;
  TmpFile := ExpandConstant('{tmp}\kb-tasklist.txt');
  if Exec(ExpandConstant('{cmd}'),
          '/C tasklist /FI "IMAGENAME eq ' + ExeName + '" /NH > "' + TmpFile + '"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if LoadStringsFromFile(TmpFile, Lines) then
    begin
      for i := 0 to GetArrayLength(Lines) - 1 do
      begin
        if Pos(LowerCase(ExeName), LowerCase(Lines[i])) > 0 then
        begin
          Result := True;
          Break;
        end;
      end;
    end;
    DeleteFile(TmpFile);
  end;
end;

// 检查运行时真正会调用的 PATH `python` 是否恰好为 3.13。
// 注册表或 py launcher 命中但 PATH 无 python 时，当前 create_venv_cmd 仍会失败，
// 因此不能把它们当作可用；Anaconda / pyenv-win / portable 只要 PATH 正确即可通过。
function IsPython313Available(): Boolean;
var
  ResultCode: Integer;
begin
  Result := False;
  if Exec(ExpandConstant('{cmd}'),
          '/D /C python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>&1',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    if ResultCode = 0 then
      Result := True;
end;

function InitializeSetup(): Boolean;
var
  PyChoice: Integer;
begin
  Result := True;
  if IsProcessRunning('kb-api.exe') or IsProcessRunning('kb-tray.exe') then
  begin
    MsgBox('检测到知识库服务正在运行。' + #13#10 + #13#10 +
           '请先在托盘图标上右键「退出」后再继续安装。' + #13#10 +
           '（防止 SQLite / Qdrant 拿到不一致 snapshot）',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  // D1 直装承诺前置检查: local embedding 需外部 Python 3.13
  // 未装不阻断安装 (embedding_service_mode 可以是 external 或 disabled, 那种情况不需要 Python)
  // 只提示用户: 若打算用 local embedding, 请先装 Python 3.13
  if not IsPython313Available() then
  begin
    PyChoice := MsgBox(
      '未检测到 PATH 中可直接执行的 Python 3.13。' + #13#10 + #13#10 +
      '直装版核心组件(kb-api / kb-tray / SQLite / Qdrant)不需要 Python，可以正常安装使用。' + #13#10 + #13#10 +
      '但若你打算启用「本地 Embedding 服务」(embedding_service_mode=local, 默认关闭)，' +
      '需要系统预装 Python 3.13:' + #13#10 +
      '  下载地址: https://www.python.org/downloads/release/python-3130/' + #13#10 +
      '  或用 winget: winget install Python.Python.3.13' + #13#10 + #13#10 +
      '是否继续安装?' + #13#10 +
      '  [是] 继续 (稍后再装 Python, 或只用 external/disabled 模式)' + #13#10 +
      '  [否] 取消 (先装 Python 再回来装知识库)',
      mbConfirmation, MB_YESNO);
    if PyChoice = IDNO then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function GetTimestamp(): String;
begin
  // Inno 内置：DateTimeFormat 用 yyyymmdd_hhnnss，分隔符传 #0 表示忽略
  Result := GetDateTimeString('yyyymmdd_hhnnss', #0, #0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  SrcDataDir, BackupRoot, BackupDir, ManifestPath: String;
  ResultCode: Integer;
  ManifestContent: AnsiString;
begin
  Result := '';
  NeedsRestart := False;

  SrcDataDir := ExpandConstant('{app}\data');
  // 首次安装：data 目录不存在或为空文件夹 → 跳过备份
  if not DirExists(SrcDataDir) then
    Exit;
  if not (FileExists(SrcDataDir + '\knowledge.db') or DirExists(SrcDataDir + '\qdrant_local')) then
    Exit;

  BackupRoot := ExpandConstant('{localappdata}\KnowledgeBase\auto-backup');
  BackupDir := BackupRoot + '\' + GetTimestamp();

  if not ForceDirectories(BackupDir) then
  begin
    Result := '无法创建自动备份目录：' + BackupDir + #13#10 +
              '安装已中止，旧版数据未被改动。';
    Exit;
  end;

  // xcopy /E /I /Q /Y data → backup\data（/Y 防覆盖确认；/H 含隐藏）
  if not Exec(ExpandConstant('{cmd}'),
              '/C xcopy "' + SrcDataDir + '" "' + BackupDir + '\data\" /E /I /Q /Y /H',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '调用 xcopy 失败（无法启动子进程）。' + #13#10 +
              '安装已中止，旧版数据未被改动。';
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := 'xcopy 返回错误码 ' + IntToStr(ResultCode) + #13#10 +
              '可能磁盘空间不足或路径权限受限。' + #13#10 +
              '安装已中止，旧版数据未被改动。';
    Exit;
  end;

  // 最小 manifest — 与 mac 端 auto-backup manifest 同形
  ManifestPath := BackupDir + '\manifest.json';
  ManifestContent :=
    '{"schema_version":1,' +
    '"created_at":"' + GetTimestamp() + '",' +
    '"source":"windows-installer",' +
    '"app_version":"{#AppVersion}"}';
  SaveStringToFile(ManifestPath, ManifestContent, False);
end;

// ============================================================================
// 卸载阶段：4 个用户数据目录的去留交互（与 mac Uninstall.command 对齐）
// ============================================================================
// 设计：
//   - InitializeUninstall —— 进程检测 + 4 个 MsgBox 询问；用户选项记到全局 var
//   - CurUninstallStepChanged(usPostUninstall) —— Inno 卸载完声明式 [Files] /
//     [UninstallDelete] 后，按全局 var 删 data / models / embedding-service /
//     auto-backup；没被选中的目录保留原处，方便重装时找回
//   - 4 个询问用 TaskDialogMsgBox + 自定义 button label：[保留 XXX][确认删除][取消卸载]
//     语义化 button 避免「是/否」直觉误删；任一弹窗点取消立即 abort 整个卸载流程
// ----------------------------------------------------------------------------

var
  UninstCleanData: Boolean;
  UninstCleanModels: Boolean;
  UninstCleanEmbedding: Boolean;
  UninstCleanBackup: Boolean;

function GetBackupRoot(): String;
begin
  Result := ExpandConstant('{localappdata}\KnowledgeBase\auto-backup');
end;

// 破坏性确认对话框：返 IDYES=保留 / IDNO=确认删除 / IDCANCEL=取消整个卸载。
// 用 TaskDialogMsgBox + 自定义 button label（Win Vista+ task dialog API），
// 三 button 语义化避免「是/否」直觉误删（用户扫文字 + 习惯点最左 = 数据丢）。
//
// Inno Setup 6 的 TaskDialogMsgBox 只接受纯 button combination 常量
// （MB_OK / MB_YESNO / MB_YESNOCANCEL 等），不支持 MB_DEFBUTTON* 控制默认聚焦
// （加上会撞 Runtime error: Invalid Buttons）。所以默认聚焦永远是 Labels[0]
// = KeepLabel——刚好是 safe default：按 Enter / 直觉点最左 = 保留数据。
function ConfirmDestructive(const Instruction, Body, KeepLabel, DeleteLabel: String): Integer;
var
  Labels: TArrayOfString;
begin
  SetArrayLength(Labels, 3);
  Labels[0] := KeepLabel;
  Labels[1] := DeleteLabel;
  Labels[2] := '取消卸载';
  // ShieldButton=0 表示不给任何 button 加 UAC 盾牌图标
  Result := TaskDialogMsgBox(Instruction, Body, mbConfirmation, MB_YESNOCANCEL, Labels, 0);
end;

function InitializeUninstall(): Boolean;
var
  AppDir, DataDir, ModelsDir, EmbedDir, BackupDir: String;
  Choice: Integer;
begin
  Result := True;

  // 1. 进程检测：跟 Install 端同步
  if IsProcessRunning('kb-api.exe') or IsProcessRunning('kb-tray.exe') then
  begin
    MsgBox('检测到知识库服务正在运行。' + #13#10 + #13#10 +
           '请先在托盘图标上右键「退出」后再卸载。' + #13#10 +
           '（防止 SQLite / Qdrant 拿到不一致 snapshot）',
           mbError, MB_OK);
    Result := False;
    Exit;
  end;

  AppDir := ExpandConstant('{app}');
  DataDir := AppDir + '\data';
  ModelsDir := AppDir + '\models';
  EmbedDir := AppDir + '\embedding-service';
  BackupDir := GetBackupRoot();

  UninstCleanData := False;
  UninstCleanModels := False;
  UninstCleanEmbedding := False;
  UninstCleanBackup := False;

  // 2. 四问 —— 不存在的目录直接跳过，不打扰用户
  // 任一弹窗点「取消卸载」立即终止整个卸载流程（Result := False）
  if DirExists(DataDir) then
  begin
    Choice := ConfirmDestructive(
      '删除知识库数据吗？',
      '路径：' + DataDir + #13#10 +
      '内容：SQLite 主库 + Qdrant 向量索引' + #13#10 + #13#10 +
      '⚠ 删除后无法恢复，重装也找不回。建议保留。',
      '保留数据', '确认删除');
    if Choice = IDCANCEL then begin Result := False; Exit; end;
    if Choice = IDNO then UninstCleanData := True;
  end;

  if DirExists(ModelsDir) then
  begin
    Choice := ConfirmDestructive(
      '删除本地模型吗？',
      '路径：' + ModelsDir + #13#10 +
      '内容：已下载的 embedding 模型权重（通常 2~5 GB）' + #13#10 + #13#10 +
      '删除后重装需重新下载，重装可重建。',
      '保留模型', '确认删除');
    if Choice = IDCANCEL then begin Result := False; Exit; end;
    if Choice = IDNO then UninstCleanModels := True;
  end;

  if DirExists(EmbedDir) then
  begin
    Choice := ConfirmDestructive(
      '删除 Embedding 服务运行环境吗？',
      '路径：' + EmbedDir + #13#10 +
      '内容：独立 Python venv（infinity-emb 等依赖）' + #13#10 + #13#10 +
      '重装时可自动重建。保留也不影响（pip 检测已装依赖会跳过）。',
      '保留 venv', '确认删除');
    if Choice = IDCANCEL then begin Result := False; Exit; end;
    if Choice = IDNO then UninstCleanEmbedding := True;
  end;

  if DirExists(BackupDir) then
  begin
    Choice := ConfirmDestructive(
      '删除历史自动备份吗？',
      '路径：' + BackupDir + #13#10 +
      '内容：每次安装 / 升级前自动备份的 data/' + #13#10 + #13#10 +
      '⚠ 这是最后的救命稻草，删除后无法恢复。强烈建议保留。',
      '保留备份', '确认删除');
    if Choice = IDCANCEL then begin Result := False; Exit; end;
    if Choice = IDNO then UninstCleanBackup := True;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir, DataDir, ModelsDir, EmbedDir, BackupDir: String;
  RetainedMsg: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;

  AppDir := ExpandConstant('{app}');
  DataDir := AppDir + '\data';
  ModelsDir := AppDir + '\models';
  EmbedDir := AppDir + '\embedding-service';
  BackupDir := GetBackupRoot();

  // 按 InitializeUninstall 阶段记录的选项删
  if UninstCleanData and DirExists(DataDir) then
    DelTree(DataDir, True, True, True);
  if UninstCleanModels and DirExists(ModelsDir) then
    DelTree(ModelsDir, True, True, True);
  if UninstCleanEmbedding and DirExists(EmbedDir) then
    DelTree(EmbedDir, True, True, True);
  if UninstCleanBackup and DirExists(BackupDir) then
    DelTree(BackupDir, True, True, True);

  // 残留汇总：告诉用户哪些目录被保留了，方便手动清 / 重装识别
  RetainedMsg := '';
  if (not UninstCleanData) and DirExists(DataDir) then
    RetainedMsg := RetainedMsg + #13#10 + '  - ' + DataDir;
  if (not UninstCleanModels) and DirExists(ModelsDir) then
    RetainedMsg := RetainedMsg + #13#10 + '  - ' + ModelsDir;
  if (not UninstCleanEmbedding) and DirExists(EmbedDir) then
    RetainedMsg := RetainedMsg + #13#10 + '  - ' + EmbedDir;
  if (not UninstCleanBackup) and DirExists(BackupDir) then
    RetainedMsg := RetainedMsg + #13#10 + '  - ' + BackupDir;

  if RetainedMsg <> '' then
    MsgBox('卸载完成。' + #13#10 + #13#10 +
           '以下目录按你的选择保留在原处（重装时会被自动识别复用）：' +
           RetainedMsg,
           mbInformation, MB_OK);

  // 安装根目录如果空了，Inno 会自动删；非空则保留
end;
