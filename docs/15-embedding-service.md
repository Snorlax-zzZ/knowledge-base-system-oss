# 内置 Embedding 服务

> **读者**：开发 / 排障人员。终端用户视角见 `使用说明.md`。
>
> **目的**：解释内置 embedding 服务的架构、子进程编排契约、故障诊断流程，让接手的人能从代码与日志快速定位问题。
>
> **相关文档**：
> - [docs/14-phase3-process-manager-contract.md](14-phase3-process-manager-contract.md) — 壳层 ProcessManager 契约（Mac/Windows 实现共用）
> - 内部设计规范 v1.2（27 条 AC）— 完整设计文档由维护者持有，如需查阅请联系维护者

---

## 1. 为什么需要"内置"

直装版用户的核心痛点：要用语义检索，得先配 7 个字段（base_url / api_key / model / dim / timeout / fallback / rerank），再去某个 cloud 上找 API key。门槛高到非工程用户根本走不完。

内置服务把这件事压缩成"两次点击 + 等下载完"：
- 点 `/setup` 进引导页 → 选「本地内置」
- 选模型（默认 BGE-M3 推荐）→ 点「开始安装」
- 后续全自动：建 venv → pip 装 `infinity-emb` → 下载模型 → 起子进程 → 探活 → 就绪

---

## 2. 进程模型

```
┌─────────────────────────────────────────────────────────────┐
│  kb-api (FastAPI 主服务,uvicorn 单 worker)                  │
│   - 暴露 /v1/system/embedding-service/* 控制平面            │
│   - 启动钩子写 runtime/owner_token (0o600)                  │
│   - 不直接 spawn infinity 子进程                            │
└──────────┬──────────────────────────────────────────────────┘
           │ HTTP (X-Embedding-Owner-Token)
           ↓
┌─────────────────────────────────────────────────────────────┐
│  壳层 ProcessManager (托盘 / 菜单栏 App 进程内,daemon 线程) │
│   - reconcile loop (≤3s 拉 desired-state)                   │
│   - 唯一 owner of infinity 子进程                           │
│   - Mac:  mac-app/MenuBarApp/EmbeddingProcessManager.swift  │
│   - Win:  windows-app/embedding_process_manager.py          │
└──────────┬──────────────────────────────────────────────────┘
           │ Process.spawn (subprocess.Popen)
           ↓
┌─────────────────────────────────────────────────────────────┐
│  infinity-emb (Python venv 内独立进程)                      │
│   - listen 127.0.0.1:7687 (默认)                            │
│   - POST /v1/embeddings 返回 dense vectors                  │
│   - 跟 kb-api 解耦,挂掉不影响主服务                         │
└─────────────────────────────────────────────────────────────┘
```

**关键不变量**：
- kb-api **从不**直接 spawn / kill infinity——所有进程动作集中在壳层
- 壳层周期回写 actual-state 让 kb-api 知道当前真实状态
- kb-api 重启会生成新 owner_token；壳层下次回写拿到 401 自动 re-read 文件

---

## 3. 文件布局

`{data_root}` 通常等于 `KB_APP_ROOT`（直装版 `%LocalAppData%\KnowledgeBase\` 或 `~/Library/Application Support/KnowledgeBase/`）。

| 路径 | 写者 | 读者 | 说明 |
|------|------|------|------|
| `runtime/owner_token` | kb-api startup | 壳层 | 进程间认证 token，0o600，kb-api 重启时刷新 |
| `runtime/install_status.json` | 壳层 | kb-api SSE | 安装进度快照，≤2s 覆盖式 flush |
| `runtime/pid` | 壳层 | 壳层（自检） | infinity 子进程 PID |
| `runtime/port` | 壳层 | 壳层（自检） | infinity 实际监听端口 |
| `runtime/restart_count` | 壳层 | 壳层 | 崩溃重启计数（>3 放弃） |
| `logs/pip.log` | 壳层 tee | kb-api SSE | pip install 输出 append-only |
| `logs/infinity.log` | 壳层 tee | — | infinity 子进程 stdout/stderr |
| `embedding-service/venv/` | 壳层 | infinity 子进程 | 独立 venv，跟 kb-api 解耦避免依赖冲突 |
| `models/{model_key}/` | 壳层 | infinity 子进程 | snapshot_download 落盘的模型权重 |

---

## 4. 控制平面 API

详见 `app/main.py`（对应内部设计规范 §3.2）。简表：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/system/embedding-service/status` | 公开汇总视图（前端 / 托盘） |
| GET | `/v1/system/embedding-service/desired-state` | 壳层 reconcile 拉，要 owner_token |
| POST | `/v1/system/embedding-service/actual-state` | 壳层回写，owner_token + generation 双校验 |
| POST | `/v1/system/embedding-service/install` | 触发安装，返 SSE 流（≤15s keepalive） |
| POST | `/v1/system/embedding-service/start` | 期望状态置 running |
| POST | `/v1/system/embedding-service/stop` | 期望状态置 stopped |
| POST | `/v1/system/embedding-service/switch-model` | 切模型，必须 `confirm` + reindex 联动 |
| POST | `/v1/system/rebuild-vector-index` | 重建向量索引，必须 `I-CONFIRM-OVERWRITE` |
| POST | `/v1/system/rebuild-vector-index/abort` | AC23 中止 + 回滚 |
| GET | `/v1/system/rebuild-vector-index/status` | 重建进度 |
| GET | `/v1/system/embedding-models` | 可选模型注册表（给 `/setup` 用） |
| GET | `/v1/system/reindex-preview` | reindex 触发前 chunk 数 + 预估耗时 |

**控制平面错误码约定**：
- `401` — `X-Embedding-Owner-Token` 不匹配，壳层 invalidate cache + retry
- `409` — `acknowledged_generation` 落后于最后一次 ack（旧 desired 在回写），壳层丢弃即可
- `202` — `actual.warming_up=True` 时检索 API 返此（AC19）；reindex 进行中且 chunk ≥ 5000 时写 API 返此（AC10）

---

## 5. Reconcile Loop 状态机

壳层每 3 秒一轮：

```
1. GET /desired-state           # 失败 → 1s/2s/4s/8s 退避 (cap 30s)
2. 对比 last_done_generation     # 已 done → 仅 ≤5s 心跳一次
3. dispatch action:
     install      → InstallExecutor.execute()
     start        → StaleResidueCleaner → StartHandler.spawnAndWaitReady()
     stop         → StopHandler.terminateAndWait()
     switch_model → stop → install → start
4. 回写 actual-state             # 携带 generation 单调校验
5. 子进程崩溃监听 (supervise)    # restart_count ≤ 3
```

异常吃法：任何步骤崩 → catch + 写 `actual.last_error`，绝不杀 loop。

---

## 6. 退出契约（AC14a）

```
1. handle.terminate()       # SIGTERM / Windows: subprocess.terminate
2. wait grace_sec (默认 3s)
3. handle.kill()            # SIGKILL / Windows: subprocess.kill
4. cleanup runtime/{pid,port}
```

`fake_infinity.py --sigterm-mode ignore` 模拟"恶意"子进程忽略 SIGTERM，用于验证强杀路径；本仓 tests 已含端到端用例（`tests/test_embedding_process_manager.py::TestEndToEndWithFakeInfinity`）。

---

## 7. 残留清理（AC14b）

壳层启动 / reconcile 进入 start 前都跑一遍：

```
读 runtime/pid → 死的 → 删 pid + port
              → 活的 → ps -p {pid} -o command= 取 cmdline
                       → 含 "infinity --port {p} --model-id {m}" → adopt
                       → 其他 (PID 复用 / 切模型残留) → 不动外人,换端口
```

判定函数：`app/services/embedding_install.is_owned_infinity` — 纯逻辑，跨平台。

---

## 8. 故障诊断速查

### 8.1 安装阶段卡住

1. 看 `runtime/install_status.json` 的 `phase` 字段
2. `phase=pip_installing` 卡住 → 看 `logs/pip.log` 末尾
3. `phase=downloading` 卡住 → 多半网络问题，壳层会按 `https://hf-mirror.com → https://huggingface.co` 顺序兜底
4. `phase=failed` → `error` 字段是 stdout 末 512 字节

### 8.2 安装完成但语义检索不可用

1. `/v1/system/embedding-service/status` 看 `running` + `warming_up`
2. `running=False, installed=True` → 壳层没拉起；看 `actual.last_error`
3. `warming_up=True` 持续超 2 分钟 → 模型加载失败；看 `logs/infinity.log` 末尾
4. `restart_count` 累计 → infinity 频繁崩，多半是内存不够或 device=cuda 但没装驱动

### 8.3 切模型后没自动 reindex

切模型只更新 `desired-state`，不直接触发 reindex（避免新 infinity 还在装时被 reindex 拒）。前端拿到 `next_action="POST /v1/system/rebuild-vector-index"` 提示后手动调，符合设计 §4.5。

### 8.4 reindex 一直 maintenance flag 状态

AC10 阈值放行：≥ 5000 chunk 才置 flag。看 `GET /v1/system/reindex-preview` 的 `threshold_blocked_writes` 字段。可在 `/settings` 重新触发 reindex 或点「中止并回滚」。

---

## 9. 测试金字塔

| 层 | 位置 | 数量 | 说明 |
|----|------|------|------|
| 单元 | `tests/test_embedding_service_api.py` 等 | 60+ | API 端点 / 状态机 / install plan |
| 单元 | `tests/test_embedding_process_manager.py` | 76 | Windows 壳层各 handler 全套 |
| 集成 | 同上 `TestEndToEndWithFakeInfinity` | 2 | 真子进程 spawn + 信号 |
| 端到端 | （手动） | — | install→start→reindex 全链路 |

跑：`.venv/bin/pytest tests/test_embedding_service_api.py tests/test_embedding_process_manager.py -v`

---

## 10. 扩展点

- 加新模型：编辑 `app/services/embedding_install.py` 的 `MODEL_REGISTRY`，UI 自动出现在 `/setup` 模型网格
- 加镜像：`InstallSpec.mirror_chain` 默认 `[hf-mirror.com, huggingface.co]`，可在 spec_factory 里追加
- 换 embedding backend（vllm / tei）：替换 `start_cmd` 模板 + 实现 `/health` 即可，壳层契约不变
- 多模型并行：当前架构只支持单 infinity 实例。若要多实例，需扩展 desired-state 数组 + 端口段分配

---

## 11. 已知边界

- **Windows 上 reconcile 线程在主进程内**：托盘进程退出 → reconcile thread 自动 daemon 死掉，infinity 子进程靠 `taskkill /T /F` 清扫
- **Mac 上 reconcile 在 DispatchQueue**：menu bar app 退出 → `applicationWillTerminate` 显式 `manager.stop()` 跑完 SIGTERM/SIGKILL 流程
- **owner_token 单一**：暂不支持多个壳层并发（Mac + Web UI 同时管控）；如要扩展需要 token 列表 + 标识哪个壳层
- **安装期间 kill kb-api**：壳层 reconcile 拉 desired-state 会一直 transport error 退避；下次 kb-api 起来后 token 已变 → 401 → invalidate → re-read → 继续。安装过程的中间状态在 `runtime/install_status.json`，壳层重启会接着原来 phase 跑

---

## 12. mode 路由细则（v1.3）

`embedding_service_mode` 三个值控制 KB 主服务 vector_index 的 EmbeddingProvider 选择。这是 v1.2 设计的关键链路，但 v1.3 实装才补完整。

### 12.1 mode=local — 走本机 infinity（默认推荐）

**链路**：

```
db_cfg["embedding_service_mode"] == "local"
  → _apply_db_embedding_to_env() 走 local 分支
  → _apply_local_infinity_to_env(model_key, port)
  → os.environ["KB_EMBEDDING_BASE_URL"] = "http://127.0.0.1:{port}"
  → os.environ["KB_EMBEDDING_MODEL"]    = "models/{model_key}"
  → os.environ["KB_EMBEDDING_API_KEY"]  = "local-infinity"
  → os.environ["VECTOR_DIM"]            = MODEL_REGISTRY[model_key].dim
  → embedding_config_from_env() 拿到上述 env
  → ApiEmbedding(emb_cfg) → POST http://127.0.0.1:{port}/embeddings
```

**关键**：DB 表里的 `embedding_base_url` / `embedding_model` / `embedding_dim` 字段在 mode=local 时**被忽略**（PUT config 也锁住不让改，§2.10）。换句话说，老用户从远程豆包切到本地 bge-m3 时，DB 里残留的远程字段不会影响 vector_index 实际走向。

**port 兜底**：`embedding_service_port == 0` 时退到 `DEFAULT_EMBEDDING_PORT = 7687`，防 DB 漂移导致连不上。

**model_key 校验**：未在 `MODEL_REGISTRY` 注册的 key（用户手动改 DB）→ 强制 `KB_EMBEDDING_ENABLED=0`，让上层退到 HashEmbedding 兜底而不是裸跑错配 `ApiEmbedding` 调远程。

### 12.2 mode=external — 走用户配的远程 API

走原 `embedding_enabled + embedding_model + embedding_base_url + embedding_api_key` 字段，不变。用户配豆包 / OpenAI / 自建 infinity 集群等都用这个。

### 12.3 mode=disabled — 关闭 embedding

`KB_EMBEDDING_ENABLED=0` → `embedding_config_from_env().active = false` → vector_index 用 `HashEmbedding`（无语义召回，仅关键词词袋 fallback）。

### 12.4 mode 切换的副作用（v1.3 bug 4 修复）

`PUT /v1/system/config` 在 `mode_changed || model_changed` 时**额外调** `bump_desired`，触发壳层 reconcile loop 真停 / 真启 infinity：

| 旧 mode | 新 mode | desired.action |
|---|---|---|
| disabled / external | local | `install`（壳层接到 install 会 venv 检测 + pip + 模型下载 + start，已装好则跳过对应 phase） |
| local | local（改 model_id） | `switch_model`（壳层串联 stop → install → start） |
| local | external / disabled | `stop`（壳层 SIGTERM/SIGKILL infinity，释放 ~1.5GB 内存） |
| external ↔ disabled | （都不动 infinity，infinity 本来就没跑） | 不 bump |

`_invalidate_repo_singletons()` 在 PUT config 末尾调，让 vector_index 重读 config 立即用新 mode；下次 query 走新链路。

---

## 13. 故障自愈（v1.3 bug 2）

### 13.1 warmup state 卡死

**症状**：`actual.warming_up=true, last_error="warmup timeout after 120.0s"`，但 `lsof :7687` 显示 infinity 真在监听、`curl /health` 返 200。前端 banner 一直显示"加载中"，写类 API 一直返 202。

**根因**：StartHandler.spawnAndWaitReady 在 120s 内拿不到 /health 200 时返回 `(handle, false, err)`，但 infinity process 仍在跑（model load 慢于 120s）。后续 reconcile tick 因 desired.generation 没涨被 `shouldSkip` 跳过 → 永远不会重 probe 重写 actual。

**自愈**：reconcile loop 每 tick 起手调 `selfHealWarmupIfNeeded`，三条件全满足时强制清状态（详见 `docs/14 §13.2`）。

### 13.2 install 跳过完整 model 重下（v1.3 bug 1）

InstallExecutor.execute 跑 snapshot_download 前先做 `isModelDirComplete` 预检，命中跳过下载 phase。详见 `docs/14 §13.1`。

触发场景：
- 升级 dmg：Install.command 把旧 models/ 注入新 staging，setup 再触发 install 不重下 4GB
- 用户手动清 `embedding-service/` 但保留 `models/`：setup install 时跳过下载只重建 venv

---

## 14. 升级时的数据保留（v1.3 bug 5）

`Install.command` 升级路径会自动 backup + inject 以下三个目录到新 staging，**用户无需任何手动操作**：

| 目录 | 失败行为 | 大小 |
|---|---|---|
| `data/` | backup 失败立即 abort（核心数据不容丢失） | 通常 ~1-10 MB |
| `models/` | backup 失败仅警告（可重下） | 单模型 ~1-4 GB |
| `embedding-service/`（venv） | backup 失败仅警告（可 pip 重装） | ~几百 MB |

实现细节：`scripts/build_mac_direct_install_dmg.sh` 的 Install.command heredoc + `clone_or_copy` helper（APFS clonefile 优先，跨卷 fallback 真复制；500MB 文件 cp -R 170ms vs cp -cR 2ms）。

**1.3.7 → 1.3.8 过渡兼容**：1.3.7 的 Install.command 不备份 models/venv。1.3.8 的 Install.command 在 inject 阶段加 fallback：backup 没命中就直接从原 `DST_DIR` cp（此时还在原位，step 5 才 mv 到 .old）。

---

## 15. 重建向量索引入口（v1.3 bug 7+8）

| 入口 | 触发 | UX |
|---|---|---|
| **Mac 托盘** → 知识库管理 → 重建向量索引 | NSAlert 二次确认 → POST `/v1/system/rebuild-vector-index` + `confirm=I-CONFIRM-OVERWRITE` | 启动通知 + 完成通知（poller 3s 轮询 /status，跑到 completed/failed 弹）|
| **前端** /settings | 按钮 → 同 POST API | 进度条 + 完成提示 |
| **CLI** | `curl -X POST .../rebuild-vector-index -d '{"confirm":"I-CONFIRM-OVERWRITE","batch_size":100}'` | 返 202 task_id，自己 GET /status 看 |

**HTTP status 处理**：rebuild 端点返 **202 Accepted**（异步任务接收成功），不是 200。Swift / 任何 HTTP 客户端处理时必须 `case 200, 202` 都视为成功（v1.3 bug 8 教训）。

**poller 实现**（mac 托盘）：`mac-app/MenuBarApp/main.swift` `pollRebuildStatusUntilDone(port:)`：
- background queue 每 3s GET `/v1/system/rebuild-vector-index/status`
- `status == "completed"` → 弹"重建完成 · 处理 X/Y · 用时 N 秒"
- `status == "failed"` → 弹"重建失败 · {error 前 220 字符}"
- 30 分钟兜底超时
- `rebuildPollerActive` 单实例锁防连点开多个 poller
