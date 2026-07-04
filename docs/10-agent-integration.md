# Agent Integration Guide / Agent 接入说明

## 1. 文档定位

- README 负责：项目介绍、快速启动、核心命令。
- 本文档（`10-agent-integration.md`）负责：Agent 接入的完整说明——MCP、Skill、权限策略、验收步骤。

## 2. 整体架构

Agent 接入知识库有两条路径，可单独安装或同时安装：

| 组件 | 职责 | 类比 |
|------|------|------|
| **MCP** | 提供访问知识库的能力（检索、写入、导入、清理等） | 手 |
| **Skill** | 告诉 Agent 什么时候该查、查完怎么输出、冲突以谁为准 | 脑 |

两者同时存在时**互补，不冲突**：MCP 管"能不能做"，Skill 管"该不该做、怎么做"。

推荐组合：
1. MCP 提供工具能力
2. Skill 约束行为流程
3. 客户端权限策略控制确认次数

单装说明：
1. 仅 MCP：可直接调用知识库工具，但缺少统一行为约束。
2. 仅 Skill：可约束行为，但不会自动获得知识库工具调用能力。

## 3. MCP 接入方式

### 3.1 统一架构：HTTP 代理

两种部署模式均通过 `agent-integration/kb-mcp-proxy.py` 接入，该代理以 stdio MCP 服务暴露给 Claude Code / Codex，内部通过 HTTP 调用运行中的知识库 API。

```
Agent (Claude/Codex)
    ↓ stdio MCP
kb-mcp-proxy.py
    ↓ HTTP
知识库 API (127.0.0.1:{port})
```

优势：
- Agent 侧不需要知道数据库类型、不需要环境变量、不需要 Python 虚拟环境
- 只要知识库 API 在运行（直装版托盘启动 / Docker 版 compose 启动），MCP 就能工作
- 代理自动读取 `config/config.toml` 获取端口，无需手工配置

### 3.2 两种模式的区别

| | 直装版（主路线） | Docker 版（代码保留，未交付编排 artefact） |
|---|---|---|
| API 地址 | `http://127.0.0.1:18000`（默认端口，可在 `/settings` 改） | `http://127.0.0.1:<API_PORT>`，端口由调用方在编排层指定 |
| API 启动方式 | 菜单栏 / 托盘 App 启动；底层走 `scripts/kb-start.sh` 或 `local-restart-direct.ps1` | 由调用方自行编排 `python -m uvicorn app.main:app` 等 |
| 代理配置 | 自动读 `config/config.toml` | 通过 `KB_PORT` 环境变量指定 API 端口 |
| 前提条件 | 托盘图标显示运行中 | `curl /health` 返回 ok |

> 仓库内当前未提供 `Dockerfile` / `docker-compose.yml`。Docker 部署需调用方自行编排，但 `repository_postgres.py` 与 `KB_BACKEND=postgres` 代码路径保留。

### 3.3 暴露工具

| 工具 | 作用 | 风险等级 |
|------|------|---------|
| `search_knowledge` | 检索知识库（关键词 + 语义） | 安全 |
| `get_knowledge_item` | 按 ID 获取条目详情 | 安全 |
| `upsert_knowledge` | 写入 / 更新知识条目 | 安全 |
| `import_incremental_knowledge` | 增量导入文档 | 安全 |
| `export_knowledge_package` | 导出知识包 | 安全 |
| `import_knowledge_package` | 导入知识包 | ⚠️ 危险 |
| `clear_knowledge_base` | 清空知识库 | ⚠️ 危险 |
| `cleanup_expired_knowledge` | 清理过期知识（`mode=delete`） | ⚠️ 危险 |

`kb-mcp-proxy.py` 已对齐全量 8 个工具，均通过 HTTP 调用后端 API 端点，不依赖源码直连路径。

### 3.4 服务端确认策略

- 默认：`KB_MCP_REQUIRE_DANGEROUS_CONFIRM=0`（不做第二次 confirm 拦截）
- 严格模式：`KB_MCP_REQUIRE_DANGEROUS_CONFIRM=1`（危险工具额外要求 `confirm=true`）

若追求"危险操作只确认一次"，建议保持默认，让客户端权限机制做唯一确认。

## 4. Skill 行为规则

### 4.1 Skill 是什么

Skill 是写入 Agent 配置目录的行为指令文件（`SKILL.md`），安装后注入到 Agent 上下文中。Agent 在匹配场景时会自动遵循 Skill 规则。
Skill 可以单独安装；若同时安装 MCP，可形成“行为规则 + 工具调用”的完整路径。

### 4.2 触发方式

Skill 支持两种触发方式：

1. **自动触发**（主要方式）：Skill 安装后，Agent 识别到匹配场景时自动遵循。触发场景包括：
   - 需求实现前需要确认历史方案
   - 调试时需要查已有故障结论
   - 改动接口/配置前需要确认约束
   - 用户明确提到"按知识库来""参考历史文档"

2. **手动触发**：在 Claude Code 会话中执行 `/knowledge-base-first`，强制激活知识库优先模式。

### 4.3 行为规则

| 规则 | 说明 |
|------|------|
| 知识库优先 | 编码/排障/重构前，优先查知识库 |
| 命中输出 | 命中时在回复末尾附上 `KB Trace: trace_id=...; knowledge_item_id=...` |
| 未命中静默 | 未命中时不额外输出"未命中知识库"，直接正常回答 |
| 域回退策略 | `domain` 仅 `personal/work`；用户未明确时先查 `personal` 再查 `work` |
| 冲突处理 | 知识库内容与当前代码冲突时，以当前代码/运行结果为准，并说明冲突点 |
| 高风险管控 | 未经用户授权，不执行清库、恢复包、硬删除操作 |
| 安全工具免确认 | search/get/upsert/incremental_import/export 默认免确认 |
| 危险工具需确认 | clear/import_package/cleanup(delete) 必须用户明确授权 |

### 4.4 Skill 文件位置

| 位置 | 用途 |
|------|------|
| `agent-integration/SKILL.md` | 安装脚本复制源 |
| `skills/claude/knowledge-base-first/SKILL.md` | 开发模式直接加载 |
| `~/.claude/skills/knowledge-base-first/SKILL.md` | Claude 安装后的生效位置 |
| `~/.codex/skills/knowledge-base-first/SKILL.md` | Codex 安装后的生效位置 |

## 5. 安装步骤

前置条件：
1. 知识库服务已运行（直装版菜单栏 / 托盘 App 启动；Docker 版自行编排）
2. Python 3.10+ 已安装并加入 PATH（运行 `kb-mcp-proxy.py` 需要）
3. 系统 Python 已安装：`python3 -m pip install --user mcp httpx`

### 5.1 让 AI 自助接入（推荐用户路径）

在已经装好 Claude Code / Codex 的机器上开一个会话，把安装目录下的指南丢给它：

```
请按 /Applications/KnowledgeBase/agent-integration/安装说明.md 帮我接入知识库。
```

Windows 上把路径换成 `C:\Users\<用户名>\AppData\Local\KnowledgeBase\agent-integration\安装说明.md`。

AI 会自动完成：

1. 检测前置条件（curl `/health` / Python / mcp & httpx 包）
2. 解析本机绝对路径
3. 写入 MCP 配置：`~/.claude/.mcp.json` 或 `~/.codex/config.toml`
4. 复制 Skill 文件：`~/.claude/skills/knowledge-base-first/SKILL.md` 或 `~/.codex/skills/...`
5. 合并 Claude Code 权限白名单：`~/.claude/settings.json` 的 `permissions.allow`
6. 校验：`claude mcp list` / `codex mcp list` 看到 `knowledge-base-system`

每一步异常会即时反馈，AI 会停下来让用户处理后再继续。

### 5.2 手动接入（不想让 AI 接管时的兜底）

如需手工写配置，可直接参考 `agent-integration/安装说明.md` 自行执行：编辑 `~/.claude/.mcp.json` 追加 `mcpServers.knowledge-base-system` 段；复制 `agent-integration/SKILL.md` 到 `~/.claude/skills/knowledge-base-first/SKILL.md`；同上为 Codex 改 `~/.codex/config.toml` 与 `~/.codex/skills/`。

### 5.3 开发者内部调试（源码直连，非用户路径）

仅在内部调试场景下可使用 `scripts/` 目录下的安装脚本，通过进程内 MCP 直连数据库（非默认支持路径）：

```bash
# Claude Code
python scripts/install_claude_integration.py --mode docker

# Codex
python scripts/install_codex_integration.py --mode docker --python-bin python3 --set-project-trust
```

直装调试可改为：

```bash
python scripts/install_claude_integration.py --mode direct
python scripts/install_codex_integration.py --mode direct --python-bin python3 --set-project-trust
```

该方式暴露全量 8 个 MCP 工具。脚本会按 `--mode` 自动写入显式 `KB_BACKEND` 与对应数据库参数；不建议普通用户使用。

### 5.4 端口变更

修改端口后的同步规则：

1. 直装版：通过 `/settings` 修改 `service_port` 后会同步回写 `config/config.toml` 的 `[server].port`，API 会返回 `restart_required=true`。重启后 MCP 代理自动读取新端口，无需重装。
2. Docker 版：`/settings` 会返回 `runtime_port_managed_by=docker` 提示，仍需同步更新 MCP 启动环境中的 `KB_PORT`（及容器端口映射）。
3. 若直装版写回 `config.toml` 失败，API 会返回错误，不会伪装保存成功。

### 5.5 卸载

Claude Code：
- 删除 `~/.claude/.mcp.json` 中 `knowledge-base-system` 条目
- 删除 `~/.claude/skills/knowledge-base-first/` 目录
- 移除 `~/.claude/settings.json` 中相关权限条目

Codex：
- 删除 `~/.codex/config.toml` 中 `[mcp_servers.knowledge-base-system]` 段落
- 删除 `~/.codex/skills/knowledge-base-first/` 目录（若已安装 Skill）

## 6. 验收清单

1. 知识库服务正常：
```bash
# 直装版默认端口 18000；Docker 版替换为自定义端口
curl -sS http://127.0.0.1:18000/health
```

2. Agent MCP 已接入：
```bash
claude mcp list      # Claude Code
codex mcp list       # Codex
```

3. Skill 文件在位（按所选客户端）：
```bash
ls -la ~/.claude/skills/knowledge-base-first/SKILL.md
ls -la ~/.codex/skills/knowledge-base-first/SKILL.md
```

4. 真实任务抽测：
- 让 Claude 做一个工程任务并先查知识库
- 命中时是否输出 `KB Trace: trace_id=...; knowledge_item_id=...`
- 未命中时是否正常回答（不额外提示）
- 危险操作是否仍要求确认
