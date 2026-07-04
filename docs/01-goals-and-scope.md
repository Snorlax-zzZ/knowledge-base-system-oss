# Goals And Scope / 目标与范围

## Goals / 目标

1. 为软件工程任务提供可长期保存的知识记忆。
2. 让 Codex / Claude Code 在编码时可直接检索到相关历史决策、排障记录、工程规范。
3. 原始本地文件被修改或删除后，知识内容仍可保留。
4. 支持 Mac 与 Windows 跨设备使用，并保证按角色的访问控制。

## Non-Goals / 非目标

1. 企业级 IAM/SSO 全量接入。
2. 多租户计费与配额管理。
3. 强依赖复杂图谱推理。

## Success Criteria / 成功标准

1. Agent 能基于检索结果回答项目问题，并附带出处（`KB Trace: trace_id=...; knowledge_item_id=...`）。
2. 新决策、经验可回写为版本化记录。
3. ACL 阻止 `work` 与 `personal` 跨域泄露。
4. 原文件丢失后系统仍可用。

## In-Scope Capabilities / 当前能力范围

1. 接入文档：`.md` / `.markdown` / `.txt` / `.docx` / `.pdf`（PDF 可选 OCR 兜底）；以及通过 `upsert_knowledge` 写入的任意 Markdown 片段。
2. 双部署模式存储：
   - 直装版：SQLite 主存 + Qdrant local（嵌入模式）
   - Docker 版：PostgreSQL 主存 + Qdrant server
3. 同一套 API / MCP / 检索逻辑跨两种模式保持一致。
4. 统一 HTTP API 与 MCP 工具，先做 ACL 过滤再做混合（关键词 + 向量）召回与重排。
5. 可写回新知识条目，自动版本化。
