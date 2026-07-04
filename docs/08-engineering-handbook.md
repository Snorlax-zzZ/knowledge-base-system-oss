# Engineering Handbook / 工程实施手册

## 1. Scope / 范围

This handbook is for developers who maintain and extend the knowledge-base-system codebase.  
本手册面向维护和扩展 knowledge-base-system 的开发人员。

## 2. Code Structure / 代码结构

- `app/main.py`: FastAPI 入口、路由注册、中间件栈装配（CORS → OriginGuard → Maintenance → metrics）、`/v1/system/version` `_load_app_version` 解析逻辑。
- `app/service.py`: 业务编排（search / get / upsert + 可选 rerank）。
- `app/repository_base.py`: 仓储基类，含 `_split_markdown_sections` heading-aware chunking 与 chunk overlap 实现。
- `app/repository_sqlite.py`: SQLite 仓储（直装版主路径），含全部 6 张表的 DDL 与 backward-compat ALTER 兜底。
- `app/repository_postgres.py`: PostgreSQL 仓储（代码保留，便于未来恢复 Docker 编排）。
- `app/vector_index.py`: Qdrant 集成；`pause()` / `resume()` 在备份恢复期间释放文件句柄；embedding provider 不可达自动降级 hash embedding。
- `app/services/`：横切服务模块。
  - `backup_service.py`：导出 / 导入流式 tarball、`.pre-restore` 双层防护、回滚执行。
  - `maintenance.py`：进程级 maintenance flag 单例 + `MaintenanceMiddleware` 把写类请求降级为 503。
  - `confirm_token.py`：`I-CONFIRM-OVERWRITE` 等语义化 token 校验。
  - `origin_guard.py`：CSRF 深度防御，写类请求 Origin/Referer 非环回直接 403。
  - `pre_restore_recover.py`：启动时检测 `.pre-restore` 残留、提供 rollback / discard 端点。
  - `disk_space.py`：备份导出前预估可用空间，避免 507 落盘失败。
  - `manifest.py`：备份包 `manifest.json` 的 schema_version / sha256 / stats 计算与校验。
- `app/mcp_server.py` + `app/mcp_tools.py`: 进程内 MCP（直连数据库，开发者调试模式）。
- `agent-integration/kb-mcp-proxy.py`: MCP HTTP 代理（标准接入方式，stdio → HTTP）。
- `agent-integration/SKILL.md`: Skill 行为定义；`skills/claude/knowledge-base-first/SKILL.md` 为同步副本，由 `tests/test_skill_sync.py` 守护一致性。
- `agent-integration/setup_*_mcp.py` / `setup_*_skill.py`: 用户侧安装入口（MCP 与 Skill 分离）。
- `scripts/`: 导入、备份 / 恢复、平台启停脚本。关键脚本：
  - `kb-start.sh` / `kb-stop.sh` / `kb-status.sh`：macOS 启停（PID file + 端口 + 进程名三重兜底僵尸清理）。
  - `kb-ports.sh`：端口解析（`$KB_PORT_API` env → `config.toml [server].port` → fallback 18000）。
  - `build_mac_kb_api.sh`：PyInstaller 打包 `bin/kb-api` onefile。
  - `build_menubar_app.sh`：编译 Swift 菜单栏 App，注入 `project_root.txt`。
  - `build_mac_direct_install_dmg.sh`：组装 macOS DMG（kb-api + MenuBar.app + scripts + config + Install.command）。
  - `build_direct_install.ps1` + `installer.iss`：Windows PyInstaller + Inno Setup 打包。
  - `local-restart-direct.ps1` / `local-start.ps1` / `local-stop.ps1`：Windows 直装版启停 / 重启。
- `mac-app/restart.sh`: macOS 直装版重启脚本（优先 `bin/kb-api`，fallback `.venv` uvicorn）。
- `mac-app/MenuBarApp/main.swift`: macOS 菜单栏 App 源码（健康检查 4s 刷新、SF Symbol 图标、退出钩子异步停 kb-api 2s 超时）。
- `windows-app/tray_app_local.py`: Windows 托盘 App 源码。
- `config/config.toml`: 启动引导配置（host / port 18000 / sqlite_path / qdrant_local_path）。
- `tests/`: unit and behavior tests（见 §7）。

## 3. Core Flows / 核心流程

### 3.1 Upsert Flow

1. Validate payload.
2. Chunk `content_markdown` with heading-aware splitter (`BaseKnowledgeRepo._chunk_text`)：先按 Markdown 标题层级切 section，每块保留所属标题路径作为前缀；代码块 fence 内 `#` 不被误识别为标题；段落仍按 `\n\n` 细分；超长段落滑窗保留前缀。详见 `tests/test_chunk_markdown.py`。
3. Write canonical record into primary store (`knowledge_item`, `knowledge_version`, `knowledge_chunk`):
   - direct-install: SQLite
   - Docker: PostgreSQL
4. Rebuild ACL rows for target knowledge item.
5. 删除旧版本向量后 upsert 到 Qdrant（有限重试,最终失败记录 `VECTOR_SYNC_FAILED` 日志）。

### 3.2 Search Flow

1. Apply ACL filter in SQL query.
2. Retrieve keyword matches from primary store (SQLite/PostgreSQL by mode).
3. Retrieve vector matches from Qdrant and map back to chunks/items.
4. Merge keyword+vector candidates.
5. Apply lightweight reranker in service layer.

### 3.3 Get Item Flow

1. Query by item id with actor-based ACL filter.
2. Join latest version and source refs.

## 4. Engineering Rules / 工程约束

- Source of truth is primary-store content (SQLite in direct-install, PostgreSQL in Docker), not vector index.
- Qdrant unavailability must not break write path.
- 向量写入失败有有限重试（3 次），最终失败打日志 `VECTOR_SYNC_FAILED`，不静默吞异常。
- 版本更新前清理旧版本向量，防止旧内容挤占 topK。
- ACL filter must run before result exposure.
- All behavior changes should have tests.

## 5. Environment Variables / 关键环境变量

仅启动引导用环境变量：

- `KB_BACKEND` (`sqlite` | `postgres`) — 选择仓储实现。
- `SQLITE_PATH` — 直装版数据库文件路径（由 `kb-start.sh` 设为 `data/knowledge.db`）。
- `DATABASE_URL` — Postgres backend 时的连接字符串。
- `QDRANT_MODE` (`local` | `server`)、`QDRANT_LOCAL_PATH` / `QDRANT_URL`、`VECTOR_ENABLED`、`VECTOR_DIM`。
- `KB_APP_ROOT` — `kb-start.sh` export 给 PyInstaller binary，让 `_load_app_version` 与 config 解析能定位安装目录。
- `KB_PORT_API` — 启动端口覆盖（优先级高于 `config.toml`）。
- `KB_DATA_ROOTS` — 冒号分隔的额外允许写文件根目录（备份导出 / 增量导入路径白名单扩展用）。
- `UVICORN_WORKERS` — uvicorn 路径下的进程数（直装版恒为 1）。
- `KB_MCP_REQUIRE_DANGEROUS_CONFIRM` (`0` | `1`) — MCP HTTP 代理服务端是否额外做危险工具二次确认。
- `KB_APP_VERSION` — `_load_app_version` 三层 fallback 的最后一层。

业务运行时配置（LLM / Embedding / Rerank / 主题 / 服务端口）一律存 `system_config` 表，通过 `PUT /v1/system/config` 修改并 reflect 到 `config/config.toml` 的 `service_port`；**不通过环境变量配置**。

## 6. Development Workflow / 开发工作流

1. Add/modify tests first.
2. Implement minimal change.
3. Run focused tests.
4. Run end-to-end smoke where applicable.
5. Update docs (`07/08/09`) if behavior changes.

## 7. Verification Checklist / 交付前检查

核心测试套件（`pytest tests/`）：

- API 与路由：`test_api.py` / `test_routes.py` / `test_schema_domain_alias.py`
- 仓储与数据：`test_repository_sqlite.py` / `test_repository_postgres.py` / `test_chunk_markdown.py`
- 安全与边界：`test_origin_guard.py` / `test_maintenance_mode.py` / `test_confirm_token.py` / `test_data_path_boundary.py` / `test_zip_slip.py`
- 备份恢复：`test_backup_export.py` / `test_backup_import_overwrite.py` / `test_backup_import_merge.py` / `test_pre_restore_recover.py` / `test_auto_backup_snapshot.py`
- 向量索引：`test_vector_index_pause.py`（备份期间 pause/resume 行为）
- MCP / Skill：`test_kb_mcp_proxy.py` / `test_mcp_tools.py` / `test_skill_sync.py`

手工冒烟：

- `curl http://127.0.0.1:18000/health` → `{"status":"ok"}`
- `curl http://127.0.0.1:18000/v1/system/version` → `{"version":"<安装版本>"}`
- 控制台导入 / 检索 / 问答各跑一遍
- 退出菜单栏 App 观察 `logs/api.log` 是否记录 graceful shutdown，且 2s 内进程消失

## 8. Extension Guide / 扩展指引

- Add new ingestion type: extend `scripts/import_document.py` and tests.
- Add new retrieval strategy: extend repository + service rerank path.
- Add new MCP tools: update `mcp_tools.py` and `mcp_server.py` — 同时需要在 `agent-integration/kb-mcp-proxy.py` 补全对应 HTTP 代理工具.
- Modify Skill behavior: edit `agent-integration/SKILL.md` — 按接入方式复制到 Agent 的 Skill 目录（如 `~/.claude/skills/` 或 `~/.codex/skills/`）。
- Skill 单一源：`agent-integration/SKILL.md` 为唯一源文件；`skills/claude/knowledge-base-first/SKILL.md` 为同步副本，需保持一致（见 `tests/test_skill_sync.py`）。

## 9. MCP Architecture / MCP 接入架构

两种 MCP 接入方式并存：

| | 进程内 MCP（开发者模式） | HTTP 代理 MCP（标准接入） |
|---|---|---|
| 入口 | `python -m app.mcp_server` | `python agent-integration/kb-mcp-proxy.py` |
| 直连 | 数据库（需要 `DATABASE_URL` 或 `KB_BACKEND`） | API HTTP（不需要数据库环境变量） |
| 工具数 | 8（全量） | 8（全量） |
| 适用 | 开发者本地调试 | 直装版 / Docker 版标准接入 |

标准接入路径：Agent → stdio → `kb-mcp-proxy.py` → HTTP → 运行中 API → 数据库。

接入关系说明：
- MCP 与 Skill 是两条独立路径，可单独安装，也可组合使用。
- 组合使用时，MCP 提供工具能力，Skill 约束调用时机与输出规范。

## 10. Restart Dispatch / 重启链路分发

`/v1/system/restart` 按"后端模式 + 平台"分发：

- `KB_BACKEND=sqlite` + Windows：优先调用 `scripts/local-restart-direct.ps1`（直装版：taskkill `kb-api.exe` + 重启 `bin\kb-api.exe`）；若不存在则 fallback 到 `scripts/local-restart.ps1`（开发模式，依赖 `.venv`）。
- `KB_BACKEND=sqlite` + macOS：调用 `mac-app/restart.sh`（直装版优先 `bin/kb-api` 二进制，fallback 到 `.venv` uvicorn）。
- `KB_BACKEND=postgres`（Docker 模式）：返回 HTTP 409 + "请通过 docker compose restart 操作"。
- 其他平台：返回 HTTP 501。
- 脚本缺失：返回 HTTP 404。

约束：
- 核心模块不得硬编码单平台命令路径。
- 不支持的平台返回显式错误，不返回伪成功。
