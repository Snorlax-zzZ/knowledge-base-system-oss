# Data Model / 数据模型

## Scope / 适用范围

本数据模型是逻辑模型，适用于两种模式：
- 直装版：落地到 SQLite。
- Docker 版：落地到 PostgreSQL。

字段语义、API 输入输出和检索行为保持一致，不因平台或模式分叉。

## Core Tables / 核心表

1. `knowledge_item`（知识主对象）
- `id` (uuid)
- `title` (text)
- `domain` (`work` | `personal`)
- `project` (text)
- `module` (text，可空)
- `feature` (text，可空)
- `tags` (text[]，可空)
- `source_uri` (text，可空)
- `effective_from`, `effective_to` (timestamp，可空，用于 `as_of` 时间切片检索)
- `type` (`decision` | `runbook` | `lesson` | `fact`)
- `status` (`active` | `superseded` | `archived` | `deleted`) — `deleted` 为软删除标记，由控制台删除按钮设置；所有检索/详情接口默认仅返回 `active`
- `current_version` (int)
- `created_at`, `updated_at`

2. `knowledge_version`（版本正文）
- `id` (uuid)
- `knowledge_item_id` (uuid)
- `version` (int)
- `content_markdown` (text)
- `summary` (text)
- `author` (text)
- `change_note` (text)
- `created_at` (timestamp)

3. `knowledge_chunk`（检索分块）
- `id` (uuid)
- `knowledge_version_id` (uuid)
- `chunk_index` (int)
- `chunk_text` (text) — heading-aware 切块：先按 Markdown 标题层级分段，再按段落细分；每块保留标题路径作为语境前缀；代码块 fence 内不被误识别为标题
- `token_count` (int) — CJK 友好计数
- `vector_id` (text) — 对应 Qdrant 集合中的向量 ID

4. `source_ref`（来源追踪）
- `id` (uuid)
- `knowledge_version_id` (uuid)
- `source_type` (`file` | `chat` | `commit` | `pr`)
- `source_uri` (text)
- `source_hash` (text)
- `captured_at` (timestamp)

5. `acl_policy`（访问控制）
- `id` (uuid)
- `knowledge_item_id` (uuid)
- `allow_actor` (text)
- `allow_scope` (text)
- `created_at` (timestamp)

6. `system_config`（系统配置，单行表）
- `id` (int，CHECK = 1) — 强制单行
- `api_base_url` (text) — 自身 API base，console 与重启脚本回写用
- `service_port` (int，默认 18000) — 修改后回写 `config/config.toml`
- `grafana_url` (text) — 观测面板入口
- `ui_theme` (text，默认 `neo`) — 控制台主题
- `llm_enabled / llm_api_key / llm_base_url / llm_model / llm_timeout_sec / llm_temperature / llm_max_tokens` — LLM 凭证与参数
- `embedding_enabled / embedding_api_key / embedding_base_url / embedding_model / embedding_dim (默认 384) / embedding_timeout_sec` — 向量化提供商
- `rerank_enabled / rerank_api_key / rerank_base_url / rerank_model / rerank_path (默认 `/rerank`) / rerank_timeout_sec` — 可选重排
- `enrichment_enabled` (bool) — 入库时是否调 LLM 做摘要/标签富化
- `updated_at` (timestamp)
- 注意：本表持久化 API key 明文，仅适用于本机隔离场景；导出备份包时会脱敏（不写入 tarball）。

## Indexing Notes / 索引建议

- B-Tree on `knowledge_item(domain, project, type, status)`.
- Full-text index on `knowledge_version(content_markdown)`.
- Vector index lives in Qdrant keyed by `vector_id`.
- 中文说明：主库负责结构化过滤与全文检索；向量库负责语义召回。
