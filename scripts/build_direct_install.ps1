param(
    [string]$Version = "1.3.12"
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

# 写入 VERSION 文件 — 与 mac dmg 流程一致，供 app/main.py 启动时读为 APP_VERSION
Set-Content -Path "$RootDir\VERSION" -Value $Version -NoNewline -Encoding utf8
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
    --noconsole `
    "$RootDir\app\server_entry.py"

if ($LASTEXITCODE -ne 0) {
    Write-Error "kb-api.exe 构建失败（exit $LASTEXITCODE）"
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
    "$RootDir\windows-app\tray_app_local.py"

if ($LASTEXITCODE -ne 0) {
    Write-Error "kb-tray.exe 构建失败（exit $LASTEXITCODE）"
    exit 1
}

# ── 安装包 ────────────────────────────────────────────────────────────────────
Write-Host "=== 构建安装包 ===" -ForegroundColor Cyan

$InnoSetup = "E:\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $InnoSetup)) {
    Write-Warning "找不到 Inno Setup：$InnoSetup，跳过安装包构建"
} else {
    New-Item -ItemType Directory -Force -Path "$RootDir\dist" | Out-Null
    & $InnoSetup "/DAppVersion=$Version" "$RootDir\scripts\installer.iss"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "安装包构建失败（exit $LASTEXITCODE）"
        exit 1
    }
    Write-Host "  dist\KnowledgeBase-Setup-*.exe"
}

# ── 完成 ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Build OK ===" -ForegroundColor Green
Write-Host "  bin\kb-api\kb-api.exe (onedir)"
Write-Host "  bin\kb-tray\kb-tray.exe (onedir)"
Write-Host "  dist\KnowledgeBase-Setup-*.exe"
