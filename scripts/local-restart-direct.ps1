Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

# 直装版重启脚本：用于 /v1/system/restart 在 Windows 平台调用。
# 安装目录布局：
#   <RootDir>\scripts\local-restart-direct.ps1   (本脚本)
#   <RootDir>\bin\kb-api\kb-api.exe              (1.3.12+ onedir;FastAPI 服务可执行)
#   <RootDir>\bin\kb-api.exe                     (1.3.11 及以前 onefile,兼容兜底)
#   <RootDir>\config\config.toml                 (端口/数据路径)

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $RootDir

# 优先 onedir 路径,onefile 兜底(过渡期老安装)
$ApiExeOnedir = Join-Path $RootDir "bin\kb-api\kb-api.exe"
$ApiExeOnefile = Join-Path $RootDir "bin\kb-api.exe"
if (Test-Path -LiteralPath $ApiExeOnedir) {
    $ApiExe = $ApiExeOnedir
} elseif (Test-Path -LiteralPath $ApiExeOnefile) {
    $ApiExe = $ApiExeOnefile
} else {
    Write-Error "kb-api.exe not found at either: $ApiExeOnedir or $ApiExeOnefile"
    exit 1
}

Get-Process -Name "kb-api" -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 2

Start-Process -FilePath $ApiExe `
    -WorkingDirectory $RootDir `
    -WindowStyle Hidden

Write-Output "Local knowledge base restarted via $ApiExe"
