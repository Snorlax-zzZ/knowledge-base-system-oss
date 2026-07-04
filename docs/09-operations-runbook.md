# Operations Runbook / 运维与维护手册

## Mode Scope / 适用模式

本手册以**直装版（SQLite + Qdrant local 嵌入模式）**为主线，覆盖 macOS 与 Windows 两个平台。Docker 编排目前未提供 artefact，调用方自行编排时可参考本手册做映射。

## 1. Service Topology / 服务拓扑

直装版只跑一个常驻 HTTP 服务：

- API：`127.0.0.1:18000`（端口可在控制台 `/settings` 修改，回写 `config/config.toml`）
- 主存：嵌入式 SQLite（单文件 `data/knowledge.db`）
- 向量库：Qdrant local 模式（目录 `data/qdrant_local/`）
- 进程拓扑：
  - macOS：`KnowledgeBaseMenuBar.app` ↔ `/Applications/KnowledgeBase/bin/kb-api`
  - Windows：`kb-tray.exe` ↔ `%LocalAppData%\KnowledgeBase\bin\kb-api.exe`

观测：API 暴露 Prometheus 文本端点 `GET /metrics`，可被外部 Prometheus 抓取，不内置 Grafana / 监控面板。

## 2. Startup And Shutdown / 启停

托盘 / 菜单栏 App 操作（推荐）：

- 启动：菜单项「启动知识库」
- 停止：菜单项「停止知识库」
- 退出 App：自动异步触发 `kb-stop.sh` 收尾（2s 超时）

CLI 启停：

```bash
# macOS
/Applications/KnowledgeBase/scripts/kb-start.sh
/Applications/KnowledgeBase/scripts/kb-stop.sh
/Applications/KnowledgeBase/scripts/kb-status.sh

# Windows（管理员 PowerShell）
& "$env:LocalAppData\KnowledgeBase\scripts\local-restart-direct.ps1"
Stop-Process -Name kb-api -Force
```

`kb-start.sh` 启动前会做僵尸进程清理：
1. 读取 `data/.local_api.pid` 中的 PID，若进程存活则 `kill` → `kill -9` 兜底。
2. 用 `lsof -ti tcp:$PORT` 找端口占用进程，命令名匹配 `kb-api` / `uvicorn` / `python` 时 kill。
3. 清理后再 bind 端口，避免新进程启动失败。

## 3. Health Checks / 健康检查

```bash
curl http://127.0.0.1:18000/health
# {"status":"ok"}

curl http://127.0.0.1:18000/v1/system/version
# {"version":"<安装版本>"}

curl http://127.0.0.1:18000/metrics
# Prometheus 格式 metrics
```

健康端点也用于：
- 菜单栏 / 托盘 App 每 4s 刷新状态徽章
- `kb-start.sh` 启动后用 `wait_healthy` 等最多 120 × 0.5s = 60s

## 4. Routine Ops / 日常运维

### 4.1 备份导出

控制台「系统设置 → 数据管理 → 导出全量备份」，或 HTTP：

```bash
curl -X POST http://127.0.0.1:18000/v1/system/backup/export \
  -o /tmp/kb-backup-$(date +%Y%m%d).tar.gz
```

包内：`manifest.json` + `data/knowledge.db` + `data/qdrant_local/` + `meta/system_config_redacted.json`（脱敏后的 system_config）。

### 4.2 备份导入

```bash
curl -X POST http://127.0.0.1:18000/v1/system/backup/import \
  -F "mode=overwrite" \
  -F "confirm=I-CONFIRM-OVERWRITE" \
  -F "file=@/tmp/kb-backup.tar.gz"
```

- `mode=overwrite`：清库后整包还原（含 system_config 真凭证），confirm = `I-CONFIRM-OVERWRITE`。
- `mode=merge`：补充本地缺失条目，confirm = `I-CONFIRM-MERGE`。

restore 期间所有写类请求返回 503 + `Retry-After: 60`，检索 / 问答仍可读。

### 4.3 自动快照

- 触发：每次 DMG / installer 升级前由 `Install.command` 触发；每次 import-package / restore 前由服务自动触发。
- 存放：
  - macOS：`~/Library/Application Support/KnowledgeBase/auto-backup/{YYYYMMDD_HHMMSS}/`
  - Windows：`%LocalAppData%\KnowledgeBase\auto-backup\{YYYYMMDD_HHMMSS}\`
- **v1.3 起备份范围**（macOS APFS 用 clonefile 瞬时完成）：
  - `data/`（必须，失败立即 abort）
  - `models/`（v1.3 新增，1-4 GB 模型权重；失败仅警告，升级后走 `/setup` 重下）
  - `embedding-service/`（v1.3 新增，venv 含 infinity-emb 等依赖；失败仅警告，升级后重 pip 装）
- 永久保留，建议每季度手清（特别是 models/ 占 GB 级空间）。

### 4.4 .pre-restore 残留处理

overwrite 失败若卡在中间状态，启动钩子会检测 `data/knowledge.db.pre-restore.bak` 与 `data/qdrant_local.pre-restore-qdrant/`：

```bash
# 回滚到 restore 前状态
curl -X POST http://127.0.0.1:18000/v1/system/recover/pre-restore \
  -H "Content-Type: application/json" \
  -d '{"action":"rollback","confirm":"I-CONFIRM-ROLLBACK"}'

# 丢弃残留（恢复成功后清理）
curl -X POST http://127.0.0.1:18000/v1/system/recover/pre-restore \
  -H "Content-Type: application/json" \
  -d '{"action":"discard","confirm":"I-CONFIRM-DISCARD"}'
```

## 5. Incident Response / 故障处理

### 5.1 API 5xx

1. 看日志（macOS）：
   ```bash
   tail -100 /Applications/KnowledgeBase/logs/api.err.log
   tail -100 /Applications/KnowledgeBase/logs/api.log
   ```
   Windows：`%LocalAppData%\KnowledgeBase\logs\api.err.log`
2. 检查端口：`curl /health` 是否通；`lsof -i:18000` 看是否被其他进程占用。
3. 进程是否还在：
   ```bash
   pgrep -af kb-api          # macOS / Linux
   Get-Process kb-api        # Windows
   ```

### 5.2 启动失败 / 进程秒退

1. 看 `api.err.log` 末尾 stacktrace。
2. 常见原因：
   - PyInstaller frozen binary 找不到 `VERSION` / `config.toml`：确认 `kb-start.sh` export 了 `KB_APP_ROOT`。
   - 端口被占用：用 `kb-stop.sh` 强清后再启。
   - SQLite 损坏：从 `auto-backup` 最近一份还原。

### 5.3 检索质量差 / 召回为空

1. 控制台 `/settings` 确认 `embedding_enabled` 与 `embedding_*` 字段是否填全；不填则使用本地 hash embedding fallback，召回质量受限。
2. 检查 `data/qdrant_local/` 是否被 restore 流程暂停（`VectorIndex.pause()`）后未恢复：重启服务即可触发 `resume()`。
3. 用 `POST /v1/knowledge/search` 直接打 API，对比 console UI 看是不是前端层过滤。

### 5.4 写入返回 503

服务处于 maintenance 模式（导入 / 备份恢复进行中）。等 `Retry-After: 60` 后重试，或查 `logs/api.log` 找触发 maintenance 的请求。

### 5.5 导入失败

1. 文件编码：确保 UTF-8。
2. `.docx` / `.pdf` 需要 `python-docx` / `pypdf`；PyInstaller 打包时已包含，开发模式需 `pip install`。
3. 路径越界：报错提示 `KB_DATA_ROOTS` 时，把所在父目录加入 env：
   ```bash
   export KB_DATA_ROOTS="$HOME/work:/tmp/ingest"
   ```

## 6. Capacity Notes / 容量建议

- 关注 `data/knowledge.db` 与 `data/qdrant_local/` 增长，定期清旧版本（`POST /v1/knowledge/cleanup-expired`）。
- `auto-backup/` 无上限，按季度手清。
- 备份成功后建议异机保存至少一份。

## 7. Runbook Checklist / 值班清单

- `curl /health` 返回 `{"status":"ok"}`
- `curl /v1/system/version` 与安装目录 `VERSION` 一致
- 最近一次自动 / 手动备份存在且非零字节
- `metrics` 端点可达
- `logs/api.err.log` 末尾无最近 5xx
