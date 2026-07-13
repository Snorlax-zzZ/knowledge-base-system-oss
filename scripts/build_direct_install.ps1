param(
    [string]$Version = "1.3.13"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $RootDir

$VenvPython = "$RootDir\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Error "找不到虚拟环境 Python: $VenvPython"
    exit 1
}

function Test-OpenSslRuntimeBundle {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundleDir
    )

    foreach ($DllName in @("libcrypto-3-x64.dll", "libssl-3-x64.dll")) {
        $BundledDll = Join-Path $BundleDir "_internal\$DllName"
        if (-not (Test-Path -LiteralPath $BundledDll)) {
            Write-Error "打包产物缺少 Python SSL 运行时：$BundledDll"
            return $false
        }
    }
    return $true
}

# VERSION 文件已改为 git 追踪 (v1.3.13 决策), ps1 不再覆盖源码根 VERSION。
# 校验四处版本号一致 (VERSION + ps1 -Version default + installer.iss AppVersion + dmg.sh VERSION)。
# 不一致 → 拒 build 防版本漂移。tests/test_release_contract 会兜底,build 前拦一次。
$SourceVersion = (Get-Content -Raw -LiteralPath "$RootDir\VERSION").Trim()
if ($SourceVersion -ne $Version) {
    Write-Error "VERSION 源码文件 ($SourceVersion) 与 -Version 参数 ($Version) 不一致; 请 bump 四处版本再打包"
    exit 1
}
Write-Host "=== 版本: $Version ===" -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path "$RootDir\bin" | Out-Null
New-Item -ItemType Directory -Force -Path "$RootDir\build" | Out-Null

# ── kb-api.exe ────────────────────────────────────────────────────────────────
Write-Host "=== 构建 kb-api.exe ===" -ForegroundColor Cyan

$AnacondaDll = "E:\anaconda\Library\bin"

& $VenvPython -m PyInstaller `
    --onedir `
    --noconfirm `
    --name kb-api `
    --distpath "$RootDir\bin" `
    --workpath "$RootDir\build\api" `
    --specpath "$RootDir\build" `
    --collect-all app `
    --collect-all qdrant_client `
    --collect-all grpc `
    --collect-all numpy `
    --hidden-import uvicorn.lifespan.on `
    --hidden-import uvicorn.protocols.http.h11_impl `
    --hidden-import uvicorn.protocols.http.httptools_impl `
    --hidden-import uvicorn.protocols.websockets.websockets_impl `
    --hidden-import uvicorn.protocols.websockets.wsproto_impl `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.loops.asyncio `
    --hidden-import h11 `
    --hidden-import anyio `
    --hidden-import starlette `
    --add-data "$RootDir\app\static;app/static" `
    --add-data "$RootDir\scripts;scripts" `
    --add-binary "$AnacondaDll\ffi.dll;." `
    --add-binary "$AnacondaDll\ffi-8.dll;." `
    --add-binary "$AnacondaDll\libexpat.dll;." `
    --add-binary "$AnacondaDll\sqlite3.dll;." `
    --add-binary "$AnacondaDll\libbz2.dll;." `
    --add-binary "$AnacondaDll\liblzma.dll;." `
    --add-binary "$AnacondaDll\libmpdec-4.dll;." `
    --add-binary "$AnacondaDll\libcrypto-3-x64.dll;." `
    --add-binary "$AnacondaDll\libssl-3-x64.dll;." `
    --noconsole `
    "$RootDir\app\server_entry.py"

if ($LASTEXITCODE -ne 0) {
    Write-Error "kb-api.exe 构建失败（exit $LASTEXITCODE）"
    exit 1
}
if (-not (Test-OpenSslRuntimeBundle -BundleDir "$RootDir\bin\kb-api")) {
    exit 1
}

# ── kb-api pre-ship 自测（2026-07-01 方案 4）───────────────────────────────
# 用刚打包的 kb-api.exe 起一次（KB_PROBE_ONLY=1 让它 probe 完立即退出），读
# probe log 验证 qdrant_client 及关键传递依赖 import 全部通过。任一 fail 直接
# build 失败——把 hidden import / DLL 链问题拦在构建机，不让老大再装第 4 次。
Write-Host "=== kb-api pre-ship 自测（probe qdrant_client 打包依赖）===" -ForegroundColor Cyan

$ProbeRoot = Join-Path $env:TEMP "kb-api-probe-$Version-$(Get-Random)"
$ProbeLog = Join-Path $ProbeRoot "logs\qdrant-import-probe.log"
New-Item -ItemType Directory -Force -Path $ProbeRoot | Out-Null

$env:KB_PROBE_ONLY = "1"
$env:KB_APP_ROOT = $ProbeRoot
try {
    $ProbeExe = "$RootDir\bin\kb-api\kb-api.exe"
    # kb-api.exe 会走 _write_dependency_probe → 遇到 KB_PROBE_ONLY 立即 exit(0)
    # -Wait 阻塞到 exit；-PassThru 拿到 process 对象看 exit code
    $p = Start-Process -FilePath $ProbeExe -Wait -PassThru -WindowStyle Hidden
    if ($p.ExitCode -ne 0) {
        Write-Error "kb-api pre-ship 自测启动失败（exit $($p.ExitCode)），probe log 可能未落"
        exit 1
    }

    if (-not (Test-Path $ProbeLog)) {
        Write-Error "kb-api pre-ship 自测跑了但没落 probe log: $ProbeLog"
        exit 1
    }

    $ProbeJson = Get-Content -Raw -Encoding UTF8 $ProbeLog | ConvertFrom-Json
    if (-not $ProbeJson.qdrant_import_ok) {
        Write-Host ""
        Write-Host "!!! qdrant_client import 在打包产物里失败 !!!" -ForegroundColor Red
        Write-Host ""
        Write-Host "--- probe log ---" -ForegroundColor Yellow
        Get-Content $ProbeLog | Write-Host
        Write-Host ""
        Write-Error "kb-api 打包依赖不全，build 失败（拦住不出包）"
        exit 1
    }
    Write-Host "  qdrant_client import OK" -ForegroundColor Green
    Write-Host "  probe log: $ProbeLog"
} finally {
    Remove-Item Env:KB_PROBE_ONLY -ErrorAction SilentlyContinue
    Remove-Item Env:KB_APP_ROOT -ErrorAction SilentlyContinue
    # 保留 $ProbeRoot 让后续排查能看 probe log；如需清理由手动 rm
}

# ── kb-tray.exe ───────────────────────────────────────────────────────────────
Write-Host "=== 构建 kb-tray.exe ===" -ForegroundColor Cyan

$AnacondaDll = "E:\anaconda\Library\bin"

$AnacondaLib = "E:\anaconda\Library\lib"

& $VenvPython -m PyInstaller `
    --onedir `
    --noconfirm `
    --name kb-tray `
    --distpath "$RootDir\bin" `
    --workpath "$RootDir\build\tray" `
    --specpath "$RootDir\build" `
    --noconsole `
    --icon "$RootDir\windows-app\assets\app.ico" `
    --add-data "$RootDir\windows-app\assets;assets" `
    --hidden-import tkinter `
    --hidden-import tkinter.ttk `
    --hidden-import tkinter.filedialog `
    --hidden-import tkinter.messagebox `
    --hidden-import _tkinter `
    --add-binary "$AnacondaDll\tcl86t.dll;." `
    --add-binary "$AnacondaDll\tk86t.dll;." `
    --add-binary "$AnacondaDll\zlib.dll;." `
    --add-data "$AnacondaLib\tcl8.6;_tcl" `
    --add-data "$AnacondaLib\tk8.6;_tk" `
    --add-binary "$AnacondaDll\ffi.dll;." `
    --add-binary "$AnacondaDll\ffi-8.dll;." `
    --add-binary "$AnacondaDll\libexpat.dll;." `
    --add-binary "$AnacondaDll\sqlite3.dll;." `
    --add-binary "$AnacondaDll\libbz2.dll;." `
    --add-binary "$AnacondaDll\liblzma.dll;." `
    --add-binary "$AnacondaDll\libmpdec-4.dll;." `
    --add-binary "$AnacondaDll\libcrypto-3-x64.dll;." `
    --add-binary "$AnacondaDll\libssl-3-x64.dll;." `
    "$RootDir\windows-app\tray_app_local.py"

if ($LASTEXITCODE -ne 0) {
    Write-Error "kb-tray.exe 构建失败（exit $LASTEXITCODE）"
    exit 1
}
if (-not (Test-OpenSslRuntimeBundle -BundleDir "$RootDir\bin\kb-tray")) {
    exit 1
}

# ── 安装包 ────────────────────────────────────────────────────────────────────
Write-Host "=== 构建安装包 ===" -ForegroundColor Cyan

$InnoSetup = "E:\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $InnoSetup)) {
    Write-Error "找不到 Inno Setup：$InnoSetup，无法生成发布安装包"
    exit 1
} else {
    $InstallerBuildStarted = Get-Date
    New-Item -ItemType Directory -Force -Path "$RootDir\dist" | Out-Null
    & $InnoSetup "/DAppVersion=$Version" "$RootDir\scripts\installer.iss"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "安装包构建失败（exit $LASTEXITCODE）"
        exit 1
    }
    $InstallerPath = "$RootDir\dist\KnowledgeBase-Setup-$Version.exe"
    if (-not (Test-Path -LiteralPath $InstallerPath)) {
        Write-Error "安装包构建命令成功，但产物不存在：$InstallerPath"
        exit 1
    }
    if ((Get-Item -LiteralPath $InstallerPath).LastWriteTime -lt $InstallerBuildStarted) {
        Write-Error "安装包不是本轮构建生成的最新产物：$InstallerPath"
        exit 1
    }
    Write-Host "  $InstallerPath"
}

# ── 完成 ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Build OK ===" -ForegroundColor Green
Write-Host "  bin\kb-api\kb-api.exe (onedir)"
Write-Host "  bin\kb-tray\kb-tray.exe (onedir)"
Write-Host "  dist\KnowledgeBase-Setup-*.exe"
