---
name: knowledge-base-first
description: 在工程开发任务中优先检索 knowledge-base-system 知识库。优先走 MCP；MCP 不可用时走 HTTP API。命中时带上 trace_id 和 knowledge_item_id，未命中不额外输出“未命中提示”。
---

# Knowledge Base First / 知识库优先

## Purpose / 目标

在编码、排障、重构、接口联调前，优先查询本地知识库，利用历史决策和文档上下文降低偏差。

## When To Use / 触发场景

- 需求实现前需要确认历史方案
- 调试时需要查已有故障结论
- 改动接口/配置前需要确认约束
- 用户明确提到“按知识库来”“参考历史文档”

## Tool Policy / 工具策略

优先使用以下 MCP 工具（若已安装 MCP）：

- `mcp__knowledge-base-system__search_knowledge`
- `mcp__knowledge-base-system__get_knowledge_item`

按需使用（写入 / 导出场景）：

- `mcp__knowledge-base-system__upsert_knowledge`
- `mcp__knowledge-base-system__import_incremental_knowledge`
- `mcp__knowledge-base-system__export_knowledge_package`

高风险操作（必须用户明确要求后再做）：

- `mcp__knowledge-base-system__import_knowledge_package`
- `mcp__knowledge-base-system__clear_knowledge_base`
- `mcp__knowledge-base-system__cleanup_expired_knowledge`（`mode=delete`）

若 MCP 不可用，使用 HTTP API：
- 检索：`POST /v1/knowledge/search`
- 详情：`GET /v1/knowledge/items/{item_id}`
- 写入：`POST /v1/knowledge/items/upsert`

## Retrieval Workflow / 检索流程

1. 先构造检索词：
   - 业务关键词 + 模块名 + 关键技术词
2. 调 `search_knowledge`：
   - `domain` 仅可用 `personal` 或 `work`（若用户说 `person`，按 `personal` 处理）
   - `project` 非必填；用户未明确项目时不要强行填写
   - `top_k` 建议 5~8
3. 域选择策略：
   - 用户明确“个人/私人/person/personal”语义：只查 `personal`
   - 用户明确“工作/work”语义：只查 `work`
   - 用户未明确域：先查 `personal`，再查 `work`，合并去重后再输出
4. 命中后：
   - 如需细节，再对前 1~3 条调用 `get_knowledge_item`
5. 输出规范：
   - 命中时，在答复末尾追加一行：
     - `KB Trace: trace_id=<trace_id>; knowledge_item_id=<id1,id2,...>`
   - 未命中时：
     - 不额外输出“未命中知识库”字样，直接正常回答

## Mandatory Execution / 强制执行

1. 检索第一轮禁止只查 `work`（除非用户明确要求 `work`）。
2. 用户未明确 `project` 时，禁止臆测或硬填 `project`。
3. 用户未明确 `domain` 时，必须按顺序执行：
   - 第一步：`domain=personal`（不带 project）
   - 第二步：若结果为空，再 `domain=work`（不带 project）
4. 只有用户明确提供项目名时，才允许附加 `project=<用户给定值>` 作为过滤条件。

## Guardrails / 约束

- 知识库内容与当前代码冲突时，优先以“当前代码/运行结果”为准，并说明冲突点。
- 不把高风险运维工具作为默认动作。
- 未经用户授权，不进行清库、恢复包、硬删除。
