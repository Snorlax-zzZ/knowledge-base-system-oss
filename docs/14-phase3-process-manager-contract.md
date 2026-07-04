# Phase 3 ProcessManager 共用契约

> **目的**：mac-app (`EmbeddingProcessManager.swift`) 与 windows-app (`embedding_process_manager.py`) 两个实现的唯一对照参考。两端必须遵守本契约，差异只允许出现在平台 API 层（Foundation `Process` vs Win32 Job Object）。
>
> **依据**：`openspec/changes/embedded-embedding-service/design.md` v1.2，§3.2 + §3.3 + §7 AC14/AC19/AC21/AC24/AC25/AC26 + §4.4。
>
> **不变量**：壳层是 infinity 子进程的**唯一 owner**。kb-api 只声明 desired-state，从不直接 spawn / kill / install。

---

## 1. 角色与边界

| 角色 | 进程 | 职责 |
|---|---|---|
| kb-api | 单 FastAPI 进程（已实现） | 持久化 desired-state；接收并校验 actual-state；SSE 转发 install 进度；reindex 编排；warming_up 期间返 202 |
| 壳层 ProcessManager | mac-app menu bar / windows-app tray | 安装计划执行；infinity 子进程生命周期；进度文件 flush；actual-state 回写 |
| infinity-emb | 壳层 spawn 的 Python 子进程 | 提供 `POST /v1/embeddings`；首次启动需 warming（加载模型权重） |

**职责绝不交叉**：kb-api 不写 `runtime/install_status.json`，壳层不写 `runtime/owner_token`。

---

## 2. 启动顺序与 owner_token 引导

```
T0  操作系统 / 用户启动 kb-api
T1  kb-api 完成 startup hook：
      - EmbeddingServiceState() 构造时随机生成 owner_token
      - write_owner_token_file 把 token 写到 {data_root}/runtime/owner_token（0o600）
T2  壳层 ProcessManager 启动（可早于 kb-api，也可晚于）
T3  壳层进入 owner_token 引导循环：
      while True:
        try:
          token = read_text({data_root}/runtime/owner_token)
          break
        except FileNotFoundError:
          sleep(1.0)
        如果连续 60 秒没读到 → 状态行显示"kb-api 未就绪"，继续 retry
T4  壳层进入 reconcile loop
```

**关键**：
- 壳层把 token 缓存在内存；kb-api 进程重启会改写文件 → 壳层下一次回写 actual-state 时拿到 401 → 重新读文件刷新 token（实现细节：401 时清缓存 + 立即 re-read，最多重试 3 次）。
- 壳层进程重启不影响 token：只是 re-read。

---

## 3. 控制平面 HTTP 契约

base URL：`http://127.0.0.1:{kb_api_port}`，端口从 `~/.knowledgebase/config.toml` 的 `[server].port` 读（与 mac-app/windows-app 现有逻辑一致）。

### 3.1 GET `/v1/system/embedding-service/desired-state`

请求头：
- `X-Embedding-Owner-Token: <token>`

响应 200：
```json
{
  "action": "none|install|start|stop|switch_model",
  "model_id": "bge-m3",
  "device": "cpu|cuda|mps",
  "enabled": true,
  "generation": 7,
  "updated_at": 1717999999.0
}
```

401 = token 不匹配 → re-read 文件 + 重试。

### 3.2 POST `/v1/system/embedding-service/actual-state`

请求头：
- `X-Embedding-Owner-Token: <token>`
- `Content-Type: application/json`

请求体（必须**所有字段**都填，缺字段视为 422）：
```json
{
  "acknowledged_generation": 7,
  "installed": true,
  "running": true,
  "warming_up": false,
  "model_id": "bge-m3",
  "port": 7687,
  "pid": 54321,
  "device": "cpu",
  "restart_count": 0,
  "last_error": ""
}
```

响应：
- 200 = 接受
- 401 = `X-Embedding-Owner-Token` 不匹配
- 409 = `acknowledged_generation` 落后于最后一次 ack（壳层基于旧 desired 在回写；丢弃即可）
- 422 = 字段缺失 / 类型错

**回写频率**：
- 状态变化时立即写
- 否则 ≤5s 心跳一次（kb-api 用 `last_health_check` 判存活）

### 3.3 install_status.json（壳层写，kb-api 读）

路径：`{data_root}/runtime/install_status.json`，覆盖式 flush，≤2s 一次。

完整 schema：
```json
{
  "phase": "preparing|downloading|pip_installing|warming|completed|failed",
  "progress": 0.42,
  "message": "下载 model.safetensors (5/12)",
  "bytes_downloaded": 1234567890,
  "total_bytes": 2400000000,
  "started_at": 1717999000.0,
  "updated_at": 1717999050.0,
  "error": ""
}
```

- `phase=completed` 或 `failed` 是终止态；kb-api 的 SSE streamer 看到立即结束流
- 写时**先写临时文件再 rename 覆盖**（避免 kb-api 读到半截 JSON）
- `error` 字段在 `phase=failed` 时必填

### 3.4 pip.log（壳层 tee，kb-api append-only 读）

路径：`{data_root}/logs/pip.log`，append-only。
- 壳层 spawn pip 时 `stdout=stderr=tee_to(pip_log)`
- 不限大小（最终 `phase=completed/failed` 时结束）
- kb-api SSE streamer 增量读取

---

## 4. Reconcile loop 状态机

```
loop_period = 3s
last_done_generation = -1
backoff = 0   # 失败时 1s/2s/4s/8s（capped 30s）

while True:
    sleep(loop_period if backoff == 0 else min(backoff, 30))
    try:
        desired = GET /desired-state
    except (网络错 / 401 / 5xx):
        backoff = next_backoff(backoff)
        continue
    backoff = 0

    actual = self.current_actual()  # 壳层进程内维护

    if desired.generation == last_done_generation and desired.action == "none":
        write_actual_heartbeat(actual)  # 5s 心跳
        continue

    try:
        if desired.action == "install":
            execute_install(desired)
        elif desired.action == "start":
            execute_start(desired)
        elif desired.action == "stop":
            execute_stop()
        elif desired.action == "switch_model":
            execute_stop()
            execute_install(desired)  # 若 model 已装则跳过下载
            execute_start(desired)
        last_done_generation = desired.generation
    except Exception as e:
        actual.last_error = str(e)[:512]
        write_actual(actual)
        # 不 raise，下个循环继续重试
```

**关键不变量**：
- 同一 generation 只执行一次；幂等通过 `last_done_generation` 保证
- generation 倒退（kb-api 重启后状态归零）= 重新执行
- reconcile 内任何步骤崩 → catch + 写 actual.last_error，不杀 loop

---

## 5. 安装计划执行

`build_install_plan()` 返回的 `InstallPlan` 字段映射到壳层动作：

| InstallPlan 字段 | 壳层动作 |
|---|---|
| `create_venv_cmd` | `python -m venv {venv_dir}`；失败 → phase=failed |
| `pip_install_cmd` | spawn pip，stdout/stderr tee pip.log，环境变量 `PIP_INDEX_URL` 可选 |
| `download_args` | 调 `huggingface_hub.snapshot_download(**args)`（壳层在 venv 内 Python 子进程跑） |
| `start_cmd` | spawn infinity 子进程（见 §6） |
| `device_detect_cmd` | venv 内 `python -c "import torch; print(...)"`，结果回传给 build_install_plan 二次裁决 |

### 5.1 hf-mirror 兜底链

```
mirror_chain = [
    download_args.endpoint,  # 默认 hf-mirror.com
    "https://huggingface.co",
]
for endpoint in mirror_chain:
    try:
        snapshot_download(repo_id=..., local_dir=..., endpoint=endpoint, resume_download=True)
        break
    except Exception:
        continue
else:
    phase = failed
```

`resume_download=True` 实现断点续传（hf-hub 自带）。失败保留已下载文件，下次直接续传。

### 5.2 磁盘预检

调用 `require_model_disk_space(model_key, model_dir)`，free space < model_size × 1.5 → 立即 phase=failed，不进入下载。

### 5.3 进度上报

每个阶段切换 / 每 1MB 下载 / 每秒至少一次 → 重写 `install_status.json`。
- `phase`：单调推进 `preparing → downloading → pip_installing → warming → completed`
- `progress`：单调递增 0.0 ~ 1.0

---

## 6. 子进程拉起 + 保活

### 6.1 启动

1. 调 `find_free_port(start=7687)` 或读 desired 配置端口
2. spawn `start_cmd + ["--port", str(port)]`
3. 把 PID 写 `{data_root}/runtime/pid`，端口写 `{data_root}/runtime/port`
4. warming_up = True；进入健康探活循环

### 6.2 健康探活

```
GET http://127.0.0.1:{port}/health  (或 POST /v1/embeddings 带空 input)
- timeout=2s
- 每 1s 一次，最长 120s
- 拿到 200 → warming_up = False，状态回写
- 120s 仍失败 → 杀进程，restart_count += 1，重启
```

### 6.3 崩溃保活

监听子进程 exit；非 0 退出码：
- restart_count 持久化在 `{data_root}/runtime/restart_count`
- restart_count < 3：立即重启
- restart_count >= 3：放弃，actual.running=False，actual.last_error="restart_limit_exceeded"，desired 不变 → 等用户手动 stop+install 重置

---

## 7. 启动时残留清理（AC14b）

每次壳层启动 / reconcile 进入 install/start 前都跑一遍：

```
stale_pid = read({data_root}/runtime/pid)
if stale_pid is not None and stale_pid != my_managed_pid:
    if pid_alive(stale_pid):
        cmdline = read_cmdline(stale_pid)
        if is_owned_infinity(cmdline, port, model_id):
            # 真·上次自己留下的：捡起来管，不杀
            adopt(stale_pid)
        else:
            # 外人占了 PID（PID 复用） → 不动它，端口换一个
            pass
    else:
        # PID 已死 → 清 runtime/pid + runtime/port
        cleanup_runtime_files()

stale_port = read({data_root}/runtime/port)
if stale_port and port_in_use(stale_port):
    if 不是上面 adopt 的 PID:
        # 别人占了，find_free_port 重新挑
        port = find_free_port(stale_port + 1)
```

`is_owned_infinity` 已在 `app/services/embedding_install.py` 提供（pure function），壳层调用判断即可。**10s 内必须完成**（AC14b）。

---

## 8. 退出契约（AC14a）

外部触发 stop（用户点菜单 / 应用退出 / kb-api desired=stop）：

```
1. send SIGTERM to infinity_pid (Windows: taskkill /T /PID)
2. wait up to 3.0 seconds
3. if still alive: SIGKILL (Windows: taskkill /T /F /PID)
4. cleanup runtime/pid, runtime/port
5. actual.running=False, restart_count=0, write
```

**强 3 秒上限**，不允许 fake-infinity 忽略 SIGTERM 把流程拖死。

### 8.1 Mac 实现要点

- `Foundation.Process`：用 `terminate()` 发 SIGTERM，3s 后 `kill(pid, SIGKILL)`
- 不调 `setsid()`，否则信号传不到子孙
- pidfile 兜底：`kb-stop.sh` 读 `runtime/pid` 直接 kill

### 8.2 Windows 实现要点

- 优先 Job Object + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，托盘进程死自动连带 infinity 死
- Fallback：`subprocess.Popen(..., creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)`，stop 时 `taskkill /T /F /PID`（`/T` 含子孙）
- 端口找 PID 兜底：`netstat -ano | findstr :7687` → 提取 PID → taskkill
- tcl/tk 隔离：infinity venv 跟 tray app onefile 自带的 tcl/tk **不能共用**，否则 PyInstaller 解包冲突

---

## 9. 分级就绪（AC24）

kb-api 主服务必须**先**就绪，不等 infinity。具体：

- kb-api 启动后立即对外开放 `POST /v1/knowledge/import` / `/v1/knowledge/list` / 关键词检索
- 仅 `POST /v1/knowledge/search` / `/v1/knowledge/ask` 在 `actual.warming_up=True` 时返 202 + Retry-After:5（已实现，`WarmingUpMiddleware`）
- 壳层负责把 warming_up 在合适时机置 False（infinity 健康探活通过）

---

## 10. 文件清单

| 文件 | 写者 | 读者 | 说明 |
|---|---|---|---|
| `{data_root}/runtime/owner_token` | kb-api startup | 壳层 | 0o600，chmod 失败仍写 |
| `{data_root}/runtime/install_status.json` | 壳层 | kb-api SSE | 覆盖式 flush ≤2s |
| `{data_root}/runtime/pid` | 壳层 | 壳层（自检） | infinity 子进程 PID |
| `{data_root}/runtime/port` | 壳层 | 壳层（自检） | infinity 实际监听端口 |
| `{data_root}/runtime/restart_count` | 壳层 | 壳层 | crash 计数持久化 |
| `{data_root}/logs/pip.log` | 壳层 tee | kb-api SSE | append-only |

`runtime/` 与 `logs/` 已加入 `.gitignore`。

---

## 11. 测试钩子

### 11.1 fake-infinity

`tools/fake_infinity.py`（本契约配套提供）模拟 infinity-emb：
- 可控启动延迟（warming 模拟）
- 可控对 SIGTERM 的响应（normal / ignore_sigterm / crash_after_ready）
- 暴露 `POST /v1/embeddings` 返回 hash 向量

用于 AC14a/AC14b/AC19 端到端验证。

### 11.2 dry-run 模式

壳层应支持 env `KB_PROCESS_MANAGER_DRY_RUN=1`：
- 跳过实际 spawn，把要执行的命令打印到 logs/dry_run.log
- 仍正常 reconcile / 回写 actual-state
- 用于 CI 与开发机测试

---

## 12. AC 对应表

| AC | 实现位置 |
|---|---|
| AC14a 3 秒强杀 | §8 |
| AC14b 10 秒清残留 | §7 |
| AC19 warming_up 202 | §9（kb-api 已实现，壳层只需控制 warming_up 字段） |
| AC21 ≤15s keepalive | kb-api InstallSseStreamer 已实现，壳层只需保证 install_status.json ≤2s flush |
| AC24 分级就绪 | §9 |
| AC25 owner_token + generation | §2 + §3.2 |
| AC26 actual-state 完整字段 | §3.2 |
| AC27 安装归属在壳层 | 全文体现 |

---

## 13. v1.3 实装校正（2026-06-22）

针对 v1.2 设计在真实装机时暴露的两个壳层契约缺口（详见 `openspec/changes/embedded-embedding-service/design.md` §13）：

### 13.1 InstallExecutor 跳过完整 model 检测（bug 1）

**契约新增**：InstallExecutor 在跑 `snapshot_download` 命令前**必须**先做一次 local_dir 完整性预检。命中则**跳过整个 download phase**，直接 flush `phase=completed, progress=1.0, message="安装完成（模型复用）"`。

判定规则（保守，宁可重下也不让坏模型混进来）：
1. `<local_dir>/config.json` 存在
2. 以下任一权重文件存在且 size ≥ 50 MB：
   - `pytorch_model.bin`
   - `model.safetensors`
   - `onnx/model.onnx_data`
   - `onnx/model.onnx`

满足以上两条 → 跳过下载；任一不满足 → 走原 download 镜像链流程。

**Mac 实现**：`mac-app/MenuBarApp/EmbeddingProcessManager.swift` `InstallExecutor.isModelDirComplete()`。
**Windows 实现**：`windows-app/embedding_process_manager.py` 待同步（P1）。

**触发场景**：
- 升级 dmg 把旧版本 models/ 注入新 staging 后，再次走 install 不重下 4GB
- 用户手动 rm embedding-service/ 但保留 models/ 后重装

### 13.2 reconcile loop 自愈语义（bug 2）

**契约新增**：reconcile loop 每个 tick **必须先做一次** "warmup 卡死自愈" 检查，再做正常的 desired/actual reconcile。

自愈触发条件（全满足才动）：
1. `currentHandle != nil && currentHandle.isRunning`（壳层认为还在管这个 process）
2. `actual.warmingUp == true` 或 `actual.lastError` 含 `"warmup timeout"`
3. `starter.probe(actual.port)` 返回 true（HTTP GET /health 2xx）

满足 → 锁内重置：`actual.warmingUp = false; actual.lastError = ""; actual.running = true`，记 NSLog。

**Mac 实现**：`mac-app/MenuBarApp/EmbeddingProcessManager.swift` `EmbeddingProcessManager.selfHealWarmupIfNeeded()`，在 `tick()` 起手调。`StartHandler.probe(port)` 改 `fileprivate` 暴露给同文件的 `EmbeddingProcessManager` 复用同款探针。
**Windows 实现**：待同步（P1）。

**为什么需要自愈**：StartHandler.spawnAndWaitReady 在 120s 内拿不到 /health 200 时返回 `(handle, ready=false, "warmup timeout")`，但 process 仍然在跑（infinity 模型 load 慢于 120s）。此时 actual.warmingUp 卡 true，而 `shouldSkip` 因 desired.generation 没涨直接跳过 dispatch，永远不会重新走 doStart 重写 actual。自愈机制让"延迟 ready"的 infinity 不需要用户手动 stop+start 才能让 banner 变绿。

### 13.3 跨平台同步要求

Windows 端 `embedding_process_manager.py` 需要同款契约实现 + 测试覆盖。Python subprocess 行为跟 Swift Process 不同（subprocess 支持 PATH 搜索 cmd[0] / Python multiprocessing 生命周期），需单独验证。

| 契约项 | Mac 实装 | Windows 实装 |
|---|---|---|
| §13.1 isModelDirComplete | ✅ Swift FileManager | ⏳ 待同步（pathlib.Path + os.stat） |
| §13.2 selfHealWarmupIfNeeded | ✅ Swift URLSession + NSLock | ⏳ 待同步（requests + threading.RLock） |
