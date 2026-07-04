# Architecture / 系统架构

## Deployment Modes / 部署模式

- 直装版：`KB_BACKEND=sqlite`，主存为 SQLite，Qdrant 使用 local 模式，由托盘 / 菜单栏 App 管理 `kb-api` 进程生命周期。
- Docker 版：`KB_BACKEND=postgres`，主存为 PostgreSQL，Qdrant 使用 server 模式，由 `docker compose` 编排。
- 同一套 API / MCP / 检索逻辑在两种模式下保持一致，差异仅在存储后端与启动壳层。

## Process Model / 进程模型

直装版（macOS / Windows）由两个常驻进程组成：

1. **`kb-api`**（FastAPI HTTP 服务）
   - 监听 `127.0.0.1:18000`（端口从 `config/config.toml` 读取，可通过 `system_config.service_port` 回写并重启生效）。
   - 持有 SQLite 与 Qdrant 句柄，是唯一的业务执行体。
   - 启动方式：PyInstaller 单文件二进制（`bin/kb-api`）；若不存在则 fallback 到 `.venv/bin/python -m uvicorn app.main:app`。
   - 由 `scripts/kb-start.sh`（macOS）或 `local-restart-direct.ps1`（Windows）拉起。

2. **托盘 / 菜单栏 App**（Swift on macOS, Python tray on Windows）
   - 显示运行状态徽章，每 4s 健康检查刷新一次。
   - 提供启动 / 停止 / 重启、打开控制台、导入导出、清空、清理过期等菜单项。
   - 退出钩子异步调 `kb-stop.sh`（2s 超时），不阻塞应用退出窗口。
   - 通过 Bundle 内的 `project_root.txt` 定位安装目录，fallback `/Applications/KnowledgeBase` 或 `%LocalAppData%\KnowledgeBase`。

启动目录定位由 `KB_APP_ROOT` 环境变量传入（`kb-start.sh` 显式 export），让 PyInstaller frozen binary 能正确读到 `VERSION`、`config/config.toml` 与 `scripts/`。

## High-Level Components / 核心组件

1. Ingestion Workers / 数据接入
   - 解析输入资料（`.md` / `.markdown` / `.txt` / `.docx` / `.pdf`）。
   - 解析依赖（`pypdf` / `python-docx`）已嵌入 PyInstaller binary，直装版用户无需在系统装 Python 包。
   - OCR（扫描件 PDF / 图片识别）暂不带，后续版本规划中。
   - 文本切块：heading-aware —— 先按 Markdown 标题层级分段，再按段落细分；每块保留标题路径作为语境前缀，代码块 fence 内不被误识别为标题；每块最大 800 字符，100 字符重叠；CJK 友好的 token 计数。
   - 自动打元数据：`domain` / `project` / `module` / `feature` / `tags` / `source_uri` / `effective_from-to`。

2. Knowledge Storage (Primary) / 主存层
   - 直装版：SQLite（`data/knowledge.db`），单文件，安装时 auto-backup。
   - Docker 版：PostgreSQL（`docker compose` 起的 db 容器）。
   - 表结构详见 `docs/03-data-model.md`。

3. Vector Index (Secondary) / 向量索引层
   - 直装版：Qdrant local 模式，落地到 `data/qdrant_local/`。
   - Docker 版：Qdrant server，独立容器。
   - 备份 / 恢复期间 `VectorIndex.pause()` 暂停写入并释放文件句柄，避免 `cp -R qdrant_local` 与运行中实例并发。
   - Embedding provider 不可达时，自动降级到 hash embedding，保证检索链路不中断。

4. HTTP Middleware Stack / 中间件栈
   按顺序：`CORSMiddleware` → `OriginGuardMiddleware` → `MaintenanceMiddleware` → `metrics_middleware` → 路由。
   - CORS 严格限本机环回。
   - OriginGuard 在写类请求到达路由前挡 CSRF。
   - Maintenance 在导入 / 恢复进行中把写类请求降级为 503 + `Retry-After: 60`，仅放行 search / ask / recover。
   - 详细策略见 `docs/05-security-and-acl.md`。

5. Retrieval Layer / 检索编排层
   - ACL 过滤先于召回打分。
   - 关键词召回（SQLite FTS / PostgreSQL `to_tsvector`）+ 向量召回（Qdrant）混合排序。
   - 可选 Rerank：开启 `rerank_enabled` 后调用配置的 rerank provider。
   - 输出 `trace_id` + `knowledge_item_ids` 供调用方回溯。

6. Agent Integration / Agent 接入层
   - MCP HTTP 代理（`agent-integration/kb-mcp-proxy.py`，基于 FastMCP stdio）转译 Claude / Codex 的 MCP 调用为对运行中 `kb-api` 的 HTTP 请求。
   - Skill（`knowledge-base-first`）约束 Agent 行为：何时查、怎么输出、冲突以谁为准。
   - 安装脚本将 MCP server 与 Skill 注册到 `~/.claude/` 或 Codex 配置目录。
   - 分工：MCP 管"能不能做"，Skill 管"该不该做、怎么做"。

## Data Flow / 数据流

1. 接入 → 规范化 → 按模式写入主存（SQLite 或 PostgreSQL）。
2. 生成嵌入 → 写入 Qdrant 对应 collection。
3. Agent 查询 → kb-api → ACL 过滤 → 混合召回 → 可选 rerank → 返回片段 + 来源 + 版本 + `trace_id`。
4. Agent 作答并可通过 `upsert_knowledge` 回写新版本。

## Restart Dispatch / 重启分发

`POST /v1/system/restart` 根据平台分发：

- macOS：调 `mac-app/restart.sh`（重启 `kb-api` 但保留菜单栏 App 进程）。
- Windows：优先 `scripts/local-restart-direct.ps1`（taskkill + 重新拉起 `bin\kb-api.exe`），fallback 到 `local-restart.ps1`。
- Docker：不支持，返回 409 + 提示 `docker compose restart`。

## Why This Split / 为什么这样分层

- 主存（直装版 SQLite，Docker 版 PostgreSQL）作为稳定事实源。
- Qdrant 只做语义召回加速，可由主存重建。
- API 层统一权限、审计、维护模式策略，业务变更不影响数据契约。
- 托盘 / 菜单栏 App 独立于 `kb-api`，关闭 UI 不一定关闭服务，方便后台保留运行。
