# User Manual / 使用说明手册

This manual explains how to use your knowledge base system in daily engineering work.  
本手册用于说明你如何在日常工程开发中使用这套知识库系统。

## 1. What This System Does / 系统能做什么

- Store knowledge as durable records in the primary store selected by mode (direct-install: SQLite; Docker: PostgreSQL).  
  把知识作为可持久化记录存入模式对应主存（直装版 SQLite；Docker 版 PostgreSQL）。
- Build vector index in Qdrant for semantic retrieval.  
  在 Qdrant 中建立向量索引用于语义检索。
- Provide APIs and MCP tools for coding agents (Codex/Claude Code).  
  提供 API 与 MCP 工具给编码 Agent 使用。
- Keep data usable even when original local files change or disappear.  
  即使原始本地文件改动或丢失，知识内容仍可用。

## 2. Quick Start / 快速启动

### 2.1 Start Services / 启动服务

**直装版（主路线）**：装好 DMG / 安装包后，点击托盘 / 菜单栏 App 图标，选「启动知识库」即可。底层调用 `scripts/kb-start.sh`（macOS）或 `scripts/local-restart-direct.ps1`（Windows），用户无需手工敲命令。

如需脚本启动（CI / 远程登录场景）：

```bash
# macOS
/Applications/KnowledgeBase/scripts/kb-start.sh

# Windows（管理员 PowerShell）
& "$env:LocalAppData\KnowledgeBase\scripts\local-restart-direct.ps1"
```

**Docker 版**：仓库当前未提供 `docker-compose.yml` / `Dockerfile`。如需 Docker 化部署，由调用方自行编排（`KB_BACKEND=postgres` + 外部 Qdrant + uvicorn）。

业务模型配置一律走控制台 `/settings` 页面写入 SQLite `system_config` 表，用户**无需**维护 `.env` 或导出环境变量。引导参数（host / port / 数据路径）由 `config/config.toml` 提供。

端口修改：在 `/settings` 修改 `service_port` 后回写 `config/config.toml [server].port` 并返回 `restart_required=true`，重启后生效。

### 2.2 Health Check / 健康检查

直装版默认端口 18000：

```bash
curl http://127.0.0.1:18000/health
```

Expected output / 预期输出:

```json
{"status":"ok"}
```

Docker 版需替换为编排层指定的端口。

### 2.3 Stop Services / 停止服务

直装版：托盘 / 菜单栏 App 选「停止知识库」或直接退出 App（菜单栏 App 退出时会异步 `kb-stop.sh` 停后端，2s 超时）。

CLI 直停：

```bash
# macOS
/Applications/KnowledgeBase/scripts/kb-stop.sh

# Windows
Get-Process kb-api | Stop-Process
```

## 3. Daily Workflow / 每日使用流程

### 3.1 Import Markdown Documents / 导入 Markdown 文档

```bash
cd ~/Documents/knowledge-base-system
python scripts/import_markdown.py \
  --file ~/docs/api-spec.md \
  --project project-a \
  --domain work \
  --type fact
```

Optional fields / 可选参数:
- `--title` 指定标题
- `--summary` 指定摘要
- `--knowledge-item-id` 指定已有条目 ID（用于追加新版本）

### 3.1.1 Batch Import Markdown Directory / 批量导入 Markdown 目录

```bash
cd ~/Documents/knowledge-base-system
python scripts/import_directory.py \
  --dir ~/docs \
  --project project-a \
  --domain work \
  --type fact \
  --recursive \
  --continue-on-error
```

Common options / 常用参数:
- `--recursive` 递归扫描子目录
- `--max-files 100` 限制最多导入数量
- `--continue-on-error` 单个文件失败时继续

### 3.1.2 Import One Document (.md/.txt/.pdf/.docx) / 导入单个文档

```bash
cd ~/Documents/knowledge-base-system
python scripts/import_document.py \
  --file ~/docs/spec.pdf \
  --project project-a \
  --domain work \
  --type fact
```

Supported formats / 支持格式:
- Markdown: `.md`, `.markdown`
- Text: `.txt`
- Word: `.docx`
- PDF: `.pdf`

### 3.1.3 Optional LLM / Embedding / Rerank 配置

三类模型默认全部关闭：
- LLM：用于问答生成 + 可选 enrichment（导入时自动摘要 / 标签）。
- Embedding：未启用时用本地 hash embedding fallback；启用后两选一：**本地内置 infinity 子进程**（v1.2 起，推荐，零配置一键安装；详见 `使用说明.md` §4.5）或**外部 provider**（豆包 / OpenAI 兼容 / 自建 infinity 集群等）。
- Rerank：未启用时用本地 lexical fallback；启用后调用外部 rerank 服务。

> **v1.3 行为**：切换 `embedding_service_mode`（local / external / disabled）会自动联动壳层启停 infinity 子进程；切到 local 自动 install + start，离开 local 自动 stop 释放 ~1.5GB 内存。切换 mode 或 model 后必须重建向量索引（macOS 托盘"知识库管理 → 重建向量索引"或 `/settings` 重建按钮）。

**配置入口**：控制台 `/settings` → 各模型卡片填入 `api_key / base_url / model` → 勾选 `enabled` → 保存。配置写入 `system_config` 表，重启不丢失。

控制台不可达时（如 CI 脚本预置），可用 `PUT /v1/system/config` 接口写入：

```bash
curl -X PUT http://127.0.0.1:18000/v1/system/config \
  -H "Content-Type: application/json" \
  -d '{
    "api_base_url":"http://127.0.0.1:18000",
    "service_port":18000,
    "grafana_url":"http://127.0.0.1:3000",
    "ui_theme":"neo",
    "llm_enabled":true,
    "llm_api_key":"<your-key>",
    "llm_base_url":"https://api.openai.com/v1",
    "llm_model":"gpt-4o-mini",
    "enrichment_enabled":true
  }'
```

字段完整清单见 `docs/04-retrieval-api.md` §7。

### 3.2 Search Knowledge / 检索知识

```bash
curl -X POST http://127.0.0.1:18000/v1/knowledge/search \
  -H "Content-Type: application/json" \
  -d '{
    "query":"refresh token",
    "domain":"work",
    "project":"project-a",
    "top_k":5,
    "actor":"manual"
  }'
```

### 3.3 Read a Knowledge Item / 读取知识条目

```bash
curl "http://127.0.0.1:18000/v1/knowledge/items/<knowledge_item_id>?actor=codex-local"
```

### 3.4 Write/Update Knowledge by API / 通过 API 新增或更新知识

```bash
curl -X POST http://127.0.0.1:18000/v1/knowledge/items/upsert \
  -H "Content-Type: application/json" \
  -d '{
    "title":"Auth token strategy",
    "domain":"work",
    "project":"project-a",
    "type":"decision",
    "content_markdown":"Use short-lived access token with refresh token.",
    "summary":"JWT baseline",
    "author":"you",
    "change_note":"initial",
    "public_read": true,
    "acl_actors": []
  }'
```

Private item example / 私有条目示例:
```bash
curl -X POST http://127.0.0.1:18000/v1/knowledge/items/upsert \
  -H "Content-Type: application/json" \
  -d '{
    "title":"Private note",
    "domain":"work",
    "project":"project-a",
    "type":"decision",
    "content_markdown":"Only alice can read",
    "summary":"private",
    "author":"you",
    "change_note":"acl",
    "public_read": false,
    "acl_actors": ["alice"]
  }'
```

### 3.5 Console Parameter Guide / 控制台参数说明

控制台主要分为“检索工作台 / 大模型问答 / 知识编辑 / 系统设置”四个区域。  
下面按区域说明每个参数的含义和使用建议。

#### 3.5.1 Shared Concepts / 共有概念

| 参数 | 含义 | 建议 |
|------|------|------|
| `domain` | 领域分区（如 `work`、`personal`），用于逻辑隔离 | 团队知识统一用 `work`；个人笔记用 `personal` |
| `project` | 项目标识（同一项目知识聚合键） | 使用稳定名字，如 `core-api`，避免同义多写 |
| `type` | 知识类型（`decision/runbook/lesson/fact`） | `decision` 记录决策，`runbook` 记录操作步骤，`lesson` 记录复盘，`fact` 记录事实 |
| `actor` | 调用身份，用于 ACL 过滤 | 默认 `manual`；多角色时建议按人/系统命名 |

说明：若输入 `person`，系统会自动按 `personal` 处理。

#### 3.5.2 Search Workbench / 检索工作台

| 参数 | 含义 | 默认值 | 何时需要改 |
|------|------|--------|-----------|
| `query` | 检索问题（关键词/自然语言） | 无 | 必填 |
| `domain` | 领域过滤 | `work` | 查个人知识时改为 `personal` |
| `top_k` | 返回条数 | `8` | 结果太少时调大，太噪声时调小 |
| `actor` | 身份过滤 | `manual` | 需要按 ACL 看“某角色能看到什么”时改 |
| `project` | 项目过滤 | 空 | 只看某项目时填写 |
| `module` | 模块过滤 | 空 | 知识量大时按模块缩小范围 |
| `feature` | 功能过滤 | 空 | 同模块下按功能进一步筛选 |
| `tags` | 标签过滤（逗号分隔） | 空 | 已形成标签体系后使用 |
| `source_uri` | 来源地址过滤 | 空 | 只查某文档/仓库来源时使用 |

#### 3.5.3 Ask / 大模型问答

| 参数 | 含义 | 默认值 | 何时需要改 |
|------|------|--------|-----------|
| `question` | 问题正文 | 无 | 必填 |
| `domain` | 问答检索的领域 | `work` | 查个人资料时改 `personal` |
| `project` | 项目范围 | 空 | 想让回答聚焦单项目时填写 |
| `top_k_chunks` | 参与回答的检索片段数 | `5` | 回答上下文不够可调大，幻觉偏多可调小 |
| `actor` | 身份过滤 | `manual` | 需要按权限验证回答可见性时改 |

#### 3.5.4 Knowledge Editor / 知识编辑

| 参数 | 含义 | 默认值 | 何时需要改 |
|------|------|--------|-----------|
| `title` | 条目标题 | 无 | 必填，建议一句话表达主题 |
| `knowledge_item_id` | 已有条目 ID（用于追加版本） | 空 | 更新已有知识时填写；新建留空 |
| `domain` | 领域分区 | `work` | 与检索一致 |
| `type` | 知识类型 | `decision` | 按知识性质选择 |
| `project` | 项目归属 | 无 | 必填，建议全团队统一命名 |
| `author` | 作者标识 | `manual` | 建议写真实维护人/机器人名 |
| `module` | 模块名 | 空 | 可选，推荐在大项目中填写 |
| `feature` | 功能名 | 空 | 可选，推荐与 issue/需求一致 |
| `tags` | 标签（逗号分隔） | 空 | 可选，建议控制在 3~6 个 |
| `source_uri` | 来源链接/路径 | 空 | 可选，强烈建议填写以便追溯 |
| `change_note` | 变更说明 | 空 | 更新版本时建议填写“改了什么” |
| `summary` | 摘要 | 空 | 可留空；开启 enrichment 时可自动生成 |
| `content_markdown` | 正文内容（Markdown） | 无 | 必填，核心知识本体 |
| `public_read` | 是否公开可读 | `true` | 涉及敏感内容时关闭 |
| `acl_actors` | 允许读取的 actor 列表（逗号分隔） | 空 | `public_read=false` 时填写白名单 |

ACL 规则：
- `public_read=true`：所有 actor 可读。
- `public_read=false`：仅 `acl_actors` 中身份可读。

#### 3.5.5 System Settings / 系统设置

| 参数 | 含义 | 默认值 | 何时需要改 |
|------|------|--------|-----------|
| `service_port` | API 服务端口 | `18000` | 端口冲突时改；改后需重启 |
| `api_base_url` | 控制台展示用 API 地址（由端口自动生成） | `http://127.0.0.1:<port>` | 只读，无需手改 |
| `ui_theme` | 控制台主题 | `neo` | 仅影响显示风格 |
| `llm_enabled` | 是否启用问答生成 | `false` | 需要“检索+生成”时开启 |
| `llm_api_key/base_url/model` | 大模型鉴权与模型配置 | 空/默认地址 | 接入模型供应商时填写 |
| `llm_timeout_sec` | LLM 超时秒数 | `30` | 网络慢或模型慢时调大 |
| `llm_temperature` | 生成发散度 | `0.2` | 要更稳可降到 `0~0.2` |
| `llm_max_tokens` | 回答最大 token | `1024` | 回答经常截断时调大 |
| `embedding_enabled` | 是否启用 Embedding | `false` | 追求更好语义召回时开启；mode=local 时由壳层自动管理 |
| `embedding_*` | 外部 Embedding 模型参数 | 空 | mode=external 时填写；mode=local 时被锁字段（vector_index 自动指向本机 infinity） |
| `embedding_dim` | 向量维度 | `384` | mode=external 时与外部模型对齐；mode=local 时按 model_id 自动查表（bge-m3=1024 等） |
| `embedding_service_mode` | Embedding 服务模式（v1.2 新增）| `disabled` | `local` 本机 infinity 子进程 / `external` 用户填外部 API / `disabled` 关闭 |
| `embedding_service_model_id` | 本地模型 key（v1.2 新增）| 空 | mode=local 时必填，可选 `bge-m3` / `bge-large-zh-v1.5` / `qwen3-embedding-0.6b` |
| `embedding_service_port` | infinity 监听端口（v1.2 新增）| `0` | 默认 0 走 `DEFAULT_EMBEDDING_PORT=7687`；mode=local 时只读字段，壳层启动后回写 |
| `embedding_service_device` | 推理设备（v1.2 新增）| `cpu` | `cpu` / `cuda` / `mps`，未装 driver 时务必显式 cpu |
| `rerank_enabled` | 是否启用外部重排 | `false` | 追求排序质量时开启 |
| `rerank_*` | 重排模型参数 | 空 | 开启 rerank 后填写 |
| `rerank_path` | 重排接口路径 | `/rerank` | 供应商路径不同时改 |
| `enrichment_enabled` | 写入时自动摘要/标签提取 | `false` | 希望减少手写摘要时开启 |

端口生效提醒：
- 直装版：保存 `service_port` 后会回写 `config/config.toml`，返回 `restart_required=true`，重启后生效。
- Docker 版：会返回 `runtime_port_managed_by=docker`，需要改 compose 端口映射后重启容器。

## 4. MCP + Skill Usage / MCP 与 Skill 使用

### 4.1 MCP 与 Skill 的关系

Agent 接入知识库支持两个组件（可单独使用）：

| 组件 | 作用 |
|------|------|
| **MCP** | 提供访问知识库的工具能力（检索、写入、导入等） |
| **Skill** | 告诉 Agent 什么时候该查、查完怎么输出、冲突以谁为准 |

MCP 管"能不能做"，Skill 管"该不该做、怎么做"。两者互补，但不是强绑定。

推荐理解：
- 仅 Skill：有行为规范，无工具调用能力。
- 仅 MCP：有工具能力，无行为规范。
- Skill + MCP：行为规范 + 工具能力完整闭环（推荐）。

安装入口：把 `agent-integration/安装说明.md` 丢给 Claude Code / Codex，让 AI 自助完成接入。
详细步骤见 [10-agent-integration.md](./10-agent-integration.md)。

### 4.2 Skill 触发方式

Skill 安装后有两种触发方式：

1. **自动触发**（主要方式）：Agent 识别到编码/排障/重构等场景时，自动遵循 Skill 规则优先查知识库。
2. **手动触发**：在 Claude Code 中执行 `/knowledge-base-first`，强制激活知识库优先模式。

Skill 的核心行为：
- 命中时在回复末尾附上 `KB Trace: trace_id=...; knowledge_item_id=...`
- 未命中时正常回答，不额外提示
- 知识库与当前代码冲突时，以代码为准并说明

### 4.3 Start MCP Server / 启动 MCP 服务

标准接入方式（两种模式通用）：

MCP 通过 HTTP 代理（`agent-integration/kb-mcp-proxy.py`）调运行中的 API，无需手工启动 MCP 服务。安装脚本（见 [10-agent-integration.md](./10-agent-integration.md)）会自动配置。

前提：知识库 API 正在运行（直装版托盘启动 / Docker 版 `docker compose up -d`）。

### 4.4 Available MCP Tools / 可用 MCP 工具

标准接入（HTTP 代理）已支持全量 8 个工具：

- `search_knowledge` — 检索知识库（关键词 + 语义）
- `get_knowledge_item` — 按 ID 获取条目详情
- `upsert_knowledge` — 写入 / 更新知识条目
- `import_incremental_knowledge` — 增量导入
- `export_knowledge_package` — 导出知识包
- `import_knowledge_package` — ⚠️ 危险：导入知识包
- `clear_knowledge_base` — ⚠️ 危险：清空知识库
- `cleanup_expired_knowledge`（`mode=delete` 为危险操作）

Safety note / 安全说明:
- 默认建议由 Claude/Codex 客户端做工具权限控制（安全操作免确认，危险操作一次确认）。
- 如需双保险，可设置 `KB_MCP_REQUIRE_DANGEROUS_CONFIRM=1`，服务端会额外要求 `confirm=true`。

### 4.5 Smoke Test / 冒烟测试

```bash
cd ~/Documents/knowledge-base-system
python scripts/mcp_smoke.py
```

If output contains `UPSERT / SEARCH_COUNT / GET_TITLE`, MCP path is working.  
若输出包含 `UPSERT / SEARCH_COUNT / GET_TITLE`，说明 MCP 链路正常。

## 5. Data Model You Should Know / 你需要知道的数据模型

- `knowledge_item`: logical record (title/project/type/domain)
- `knowledge_version`: versioned content
- `knowledge_chunk`: chunked text + vector_id
- `source_ref`: traceable source links
- `acl_policy`: access rules (reserved for stricter policy)

简化理解：
- 你查询的是 `knowledge_item`
- 命中的内容来自 `knowledge_version` / `knowledge_chunk`
- 向量检索加速由 Qdrant 完成

## 6. Backup And Recovery / 备份与恢复

### 6.1 What To Backup / 需要备份什么

- 直装版：`data/knowledge.db`、`data/qdrant_local`
- Docker 版：`data/postgres`、`data/qdrant`
- `docs/`, `scripts/`, `app/` (code + docs)

### 6.2 Recommended Frequency / 建议频率

- Primary store daily backup / 主存每日备份
- Qdrant weekly snapshot / Qdrant 每周快照

### 6.3 Backup Script / 备份脚本

```bash
cd ~/Documents/knowledge-base-system
./scripts/backup_create.sh
```

Output path / 输出路径:
- Default: `backups/YYYYMMDD_HHMMSS`
- You can pass custom path as first argument.

### 6.4 Restore Script / 恢复脚本

```bash
cd ~/Documents/knowledge-base-system
./scripts/backup_restore.sh ~/Documents/knowledge-base-system/backups/<timestamp>
```

Note / 注意:
- Restore will reset primary store data and replace Qdrant storage（直装版为 SQLite；Docker 版为 PostgreSQL）.
- Do not run restore while writing new data.

### 6.5 Incremental Import / 增量导入（导入前备份）

```bash
cd ~/Documents/knowledge-base-system
python scripts/import_incremental.py \
  --dir ~/docs \
  --project project-a \
  --domain work \
  --type fact \
  --recursive
```

Behavior / 行为:
- Only changed files are imported.
- Unchanged files are skipped.
- Backup is created before import by default.

### 6.6 Full Export & Cross-Machine Import / 完整导出与跨机导入

在 macOS（直装版 / 开发环境）或 Linux：
```bash
cd ~/Documents/knowledge-base-system
./scripts/kb-export-package.sh
./scripts/kb-import-package.sh ~/Downloads/kb-export-<timestamp>.tar.gz
```

在 Windows 直装版：通过 HTTP API 或 MCP 工具触发：
```powershell
# 导出
curl.exe -X POST http://127.0.0.1:18000/v1/knowledge/export-package -H "Content-Type: application/json" -d "{}"

# 导入（危险，需 confirm=true）
curl.exe -X POST http://127.0.0.1:18000/v1/knowledge/import-package -H "Content-Type: application/json" `
  -d '{"package_path":"C:\\path\\to\\kb-export.tar.gz","confirm":true}'
```

### 6.7 Clear Knowledge Base / 清空知识库（先备份）

macOS/Linux：
```bash
cd ~/Documents/knowledge-base-system
./scripts/kb-clear.sh --yes
```

Windows 直装版（HTTP API）：
```powershell
curl.exe -X POST http://127.0.0.1:18000/v1/knowledge/clear -H "Content-Type: application/json" -d "{\"confirm\":true}"
```

### 6.8 Cleanup Expired Knowledge / 清理过期知识

macOS/Linux：
```bash
cd ~/Documents/knowledge-base-system
./scripts/kb-clean-expired.sh --mode archive   # 归档（推荐）
./scripts/kb-clean-expired.sh --mode delete    # 硬删
```

Windows 直装版（HTTP API）：
```powershell
curl.exe -X POST http://127.0.0.1:18000/v1/knowledge/cleanup-expired -H "Content-Type: application/json" `
  -d "{\"mode\":\"archive\"}"
```

### 6.9 全量备份恢复（直装版）

直装版原生支持"打包整个知识库 + 跨机一键还原"，**升级 / 重装也会自动备份**，避免覆盖装丢数据。

#### 自动备份

每次升级（双击新版 DMG 内 `Install.command`）或控制台导入操作前，系统自动备份当前 `data/` 到：

```
~/Library/Application Support/KnowledgeBase/auto-backup/{YYYYMMDD_HHMMSS}/
├── data/
│   ├── knowledge.db
│   └── qdrant_local/
└── meta/
    └── manifest.json   # trigger=install | import_before
```

> ⚠️ 该目录会无限增长，建议每季度手动清理一次。
> Finder → ⌘⇧G → 输入路径 → 删不需要的时间戳目录即可。

#### 手动备份（HTTP / 控制台导出）

```bash
curl -X POST http://127.0.0.1:18000/v1/system/backup/export -o /tmp/kb-backup.tar.gz
```

或：控制台 → 系统设置 → 数据管理 → **导出全量备份**。

下载得到的 `.tar.gz` 建议放到云盘 / 移动硬盘 / 异机备份。

#### 手动恢复

```bash
# overwrite：清空当前库 + 整包还原（含 LLM/Embedding key）
curl -X POST http://127.0.0.1:18000/v1/system/backup/import \
  -F "mode=overwrite" \
  -F "confirm=I-CONFIRM-OVERWRITE" \
  -F "file=@/tmp/kb-backup.tar.gz"
```

可选模式：
- **`overwrite`**：清空当前库 + 整包还原 system_config。用于"回到 A 机器的完整状态"。confirm 必须为 `I-CONFIRM-OVERWRITE`。
- **`merge`**：保留当前库，按 `knowledge_item_id` 补充备份里本地没有的条目；本地已存在则跳过，本地软删除的视为可恢复。confirm 必须为 `I-CONFIRM-MERGE`。

双层防护：
- 操作前自动 `auto-backup` 一份永久保留
- overwrite 模式同时建内层 `.pre-restore.bak`，restore 阶段任一步骤失败自动回滚

#### ⚠️ 安全提示

**备份包包含完整 SQLite 数据库，含 LLM / Embedding API key 等敏感凭证**。请勿：

- 上传到公开仓库（GitHub Public 等）
- 通过未加密渠道（IM 文件传输 / 邮件正文）发送
- 在共享 / 公用电脑上长期保留

如需分享，建议先用 GPG 或文件系统加密包装。

## 7. Troubleshooting / 常见问题排查

### 7.1 API 500 with Qdrant connection

Check `QDRANT_URL` in API container should be `http://qdrant:6333`.  
确认 API 容器内 `QDRANT_URL` 为 `http://qdrant:6333`。

### 7.2 Docker build timeout from Docker Hub

Docker 版 Python base image 使用 `m.daocloud.io` 镜像源，避免 Docker Hub 超时。

### 7.3 Import failed / 导入失败

- Ensure API is healthy: `curl /health`
- Check file path and encoding (`utf-8`)
- Check API logs:
  - 直装版：查看 `logs/api.log` 和 `logs/api.err.log`
  - Docker 版：`docker compose logs --tail=120 api`

### 7.4 Observability Endpoints / 观测入口

- API metrics（直装版）：`http://127.0.0.1:18000/metrics`（Prometheus 文本格式，可被外部 Prometheus 抓取）
- Docker 版的 Prometheus / Grafana 路径由调用方编排自行决定。

### 7.5 Retrieval Evaluation / 检索评估

评估集位于 `eval/retrieval_eval_set.jsonl`。系统未内置评估脚本，可基于该数据集自行编写比对脚本，或通过 `POST /v1/knowledge/search` 批量调用对照。
