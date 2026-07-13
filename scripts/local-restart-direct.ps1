Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 直装版重启脚本：用于 /v1/system/restart 在 Windows 平台调用。
# 必须保持 UTF-8 with BOM，确保 Windows PowerShell 5.1 正确解析中文注释。

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $RootDir

# 优先 onedir 路径，onefile 兜底（过渡期老安装）。
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

$StatusFile = Join-Path $RootDir "runtime\restart-status.json"
function Write-RestartStatus($status, $oldPid, $newPid, $err) {
    try {
        $obj = @{
            status = $status
            old_pid = $oldPid
            new_pid = $newPid
            err = $err
            updated_at = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
        } | ConvertTo-Json -Compress
        $dir = Split-Path -Parent $StatusFile
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null
        }
        Set-Content -LiteralPath $StatusFile -Value $obj -Encoding UTF8 -ErrorAction Stop
    } catch {
        Write-Warning "cannot write restart status: $($_.Exception.Message)"
    }
}

# Named Mutex 防同一安装根并发 restart。
$rootHash = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA1]::Create().ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($RootDir))
).Replace("-", "").Substring(0, 12)
$mutexName = "Global\KB_API_LIFECYCLE_$rootHash"

$createdNew = $false
$mutexAcquired = $false
$oldPids = @()
$mutex = New-Object System.Threading.Mutex($false, $mutexName, [ref]$createdNew)
try {
    if (-not $mutex.WaitOne(500)) {
        Write-Output "Another restart is in progress; abort. (mutex=$mutexName)"
        Write-RestartStatus "failed" $null $null "another restart in progress"
        exit 2
    }
    $mutexAcquired = $true

    # 只匹配当前安装根下的 kb-api 可执行文件。
    $apiExeNormalized = (Resolve-Path -LiteralPath $ApiExe -ErrorAction Stop).Path
    $targets = @(Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object {
            $_.ExecutablePath -and
            ($_.ExecutablePath -ieq $apiExeNormalized)
        })

    foreach ($p in $targets) {
        $oldPids += $p.ProcessId
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
    }
    Write-RestartStatus "running" ($oldPids -join ",") $null ""

    # 等旧进程真实退出，最多 10 秒。
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        $stillAlive = @(Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $apiExeNormalized) })
        if ($stillAlive.Count -eq 0) { break }
        Start-Sleep -Milliseconds 200
    }

    $stillAlive = @(Get-CimInstance Win32_Process -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $apiExeNormalized) })
    if ($stillAlive.Count -gt 0) {
        Write-RestartStatus "failed" ($oldPids -join ",") $null "old kb-api still alive after 10s wait-until-dead"
        exit 3
    }

    $newProc = Start-Process -FilePath $ApiExe -WorkingDirectory $RootDir -WindowStyle Hidden -PassThru -ErrorAction Stop
    if ($null -eq $newProc) {
        throw "Start-Process returned no process handle"
    }
    Start-Sleep -Milliseconds 500
    $newProc.Refresh()
    if ($newProc.HasExited) {
        throw "new kb-api exited immediately with code $($newProc.ExitCode)"
    }

    Write-RestartStatus "completed" ($oldPids -join ",") $newProc.Id ""
    Write-Output "Local knowledge base restarted via $ApiExe (new pid=$($newProc.Id))"
} catch {
    $message = $_.Exception.Message
    Write-RestartStatus "failed" ($oldPids -join ",") $null $message
    Write-Error -Message $message -ErrorAction Continue
    exit 4
} finally {
    if ($mutexAcquired) {
        try { $mutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $mutex) {
        $mutex.Dispose()
    }
}
