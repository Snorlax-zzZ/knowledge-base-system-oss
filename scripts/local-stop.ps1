param([switch]$IncludeTray)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 仅停止当前安装根拥有的 kb-api / kb-tray；绝不按进程名或泛化 uvicorn 命令全局强杀。
$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$marker = Join-Path $RootDir "data\.local_api.pid"

function Resolve-ExistingPath([string]$path) {
    if (Test-Path -LiteralPath $path) {
        return (Resolve-Path -LiteralPath $path -ErrorAction Stop).Path
    }
    return $null
}

$apiExePaths = @(
    (Resolve-ExistingPath (Join-Path $RootDir "bin\kb-api\kb-api.exe")),
    (Resolve-ExistingPath (Join-Path $RootDir "bin\kb-api.exe"))
) | Where-Object { $_ }
$trayExePaths = @(
    (Resolve-ExistingPath (Join-Path $RootDir "bin\kb-tray\kb-tray.exe")),
    (Resolve-ExistingPath (Join-Path $RootDir "bin\kb-tray.exe"))
) | Where-Object { $_ }
$devPythonPath = Resolve-ExistingPath (Join-Path $RootDir ".venv\Scripts\python.exe")
$infinityExePath = Resolve-ExistingPath (Join-Path $RootDir "embedding-service\venv\Scripts\infinity_emb.exe")

function Test-PathInSet([string]$candidate, $paths) {
    if (-not $candidate) { return $false }
    foreach ($path in $paths) {
        if ($candidate -ieq $path) { return $true }
    }
    return $false
}

function Test-OwnedKbApiProcess($proc) {
    if ($null -eq $proc -or -not $proc.ExecutablePath) { return $false }
    if (Test-PathInSet $proc.ExecutablePath $apiExePaths) { return $true }
    if ($devPythonPath -and ($proc.ExecutablePath -ieq $devPythonPath)) {
        return [bool]($proc.CommandLine -match '(?:^|\s)-m\s+uvicorn\s+app\.main:app(?:\s|$)')
    }
    return $false
}

function Test-OwnedTrayProcess($proc) {
    if ($null -eq $proc -or -not $proc.ExecutablePath) { return $false }
    return (Test-PathInSet $proc.ExecutablePath $trayExePaths)
}

function Test-OwnedInfinityProcess($proc) {
    if ($null -eq $proc -or -not $proc.ExecutablePath -or -not $infinityExePath) {
        return $false
    }
    return ($proc.ExecutablePath -ieq $infinityExePath)
}

# PID 文件只能作为定位 hint；每次发信号前仍校验真实 ExecutablePath / 命令行归属。
if (Test-Path -LiteralPath $marker) {
    $procIdText = (Get-Content -LiteralPath $marker -Raw -ErrorAction Stop).Trim()
    $procId = 0
    if ([int]::TryParse($procIdText, [ref]$procId)) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction Stop
        if (Test-OwnedKbApiProcess $proc) {
            Stop-Process -Id $procId -Force -ErrorAction Stop
        }
    }
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
}

# 卸载时先停 tray，阻止 reconcile 在清理窗口重新拉起 infinity。
$processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
if ($IncludeTray) {
    $trayTargets = @($processes | Where-Object { Test-OwnedTrayProcess $_ })
    foreach ($proc in $trayTargets) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
    }
    Start-Sleep -Milliseconds 100
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
}

# 精确停止当前安装根的 API；卸载时一并停止同一安装根 venv 的 infinity。
$targets = @($processes | Where-Object {
    (Test-OwnedKbApiProcess $_) -or ($IncludeTray -and (Test-OwnedInfinityProcess $_))
})
foreach ($proc in $targets) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
}

# 退出前复核；不能把权限/CIM/Stop-Process 失败伪装成 stopped。
Start-Sleep -Milliseconds 200
$remaining = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
    (Test-OwnedKbApiProcess $_) -or
    ($IncludeTray -and ((Test-OwnedTrayProcess $_) -or (Test-OwnedInfinityProcess $_)))
})
if ($remaining.Count -gt 0) {
    $remainingPids = ($remaining | ForEach-Object { $_.ProcessId }) -join ","
    Write-Error "kb-api process is still running after stop (owned pid=$remainingPids)"
    exit 1
}

Write-Output "Local knowledge base stopped"
