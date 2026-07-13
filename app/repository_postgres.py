from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

from app.repository_base import BaseKnowledgeRepo
from app.text_tokens import query_terms_for_like
from app.vector_index import VectorIndex


@dataclass
class PostgresKnowledgeRepo(BaseKnowledgeRepo):
    database_url: str
    vector_index: VectorIndex | None = None

    def __post_init__(self) -> None:
        self._ensure_schema_extensions()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _ensure_schema_extensions(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE knowledge_item ADD COLUMN IF NOT EXISTS module TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE knowledge_item ADD COLUMN IF NOT EXISTS feature TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE knowledge_item ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}'::text[]")
                cur.execute("ALTER TABLE knowledge_item ADD COLUMN IF NOT EXISTS source_uri TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE knowledge_item ADD COLUMN IF NOT EXISTS effective_from TIMESTAMPTZ NULL")
                cur.execute("ALTER TABLE knowledge_item ADD COLUMN IF NOT EXISTS effective_to TIMESTAMPTZ NULL")
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_knowledge_item_domain_project_module_feature_status
                    ON knowledge_item(domain, project, module, feature, status)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_knowledge_item_tags_gin
                    ON knowledge_item USING GIN(tags)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS system_config (
                      id INTEGER PRIMARY KEY CHECK (id = 1),
                      api_base_url TEXT NOT NULL,
                      service_port INTEGER NOT NULL DEFAULT 18000,
                      grafana_url TEXT NOT NULL,
                      ui_theme TEXT NOT NULL DEFAULT 'neo',
                      llm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      llm_api_key TEXT NOT NULL DEFAULT '',
                      llm_base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
                      llm_model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
                      llm_timeout_sec DOUBLE PRECISION NOT NULL DEFAULT 30.0,
                      llm_temperature DOUBLE PRECISION NOT NULL DEFAULT 0.2,
                      llm_max_tokens INTEGER NOT NULL DEFAULT 1024,
                      llm_max_tokens_auto BOOLEAN NOT NULL DEFAULT TRUE,
                      embedding_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      embedding_api_key TEXT NOT NULL DEFAULT '',
                      embedding_base_url TEXT NOT NULL DEFAULT '',
                      embedding_model TEXT NOT NULL DEFAULT '',
                      embedding_dim INTEGER NOT NULL DEFAULT 384,
                      embedding_timeout_sec DOUBLE PRECISION NOT NULL DEFAULT 20.0,
                      rerank_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      rerank_api_key TEXT NOT NULL DEFAULT '',
                      rerank_base_url TEXT NOT NULL DEFAULT '',
                      rerank_model TEXT NOT NULL DEFAULT '',
                      rerank_path TEXT NOT NULL DEFAULT '/rerank',
                      rerank_timeout_sec DOUBLE PRECISION NOT NULL DEFAULT 20.0,
                      enrichment_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS ui_theme TEXT NOT NULL DEFAULT 'neo'")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS service_port INTEGER NOT NULL DEFAULT 18000")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_enabled BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_api_key TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1'")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_model TEXT NOT NULL DEFAULT 'gpt-4o-mini'")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_timeout_sec DOUBLE PRECISION NOT NULL DEFAULT 30.0")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_temperature DOUBLE PRECISION NOT NULL DEFAULT 0.2")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_max_tokens INTEGER NOT NULL DEFAULT 1024")
                # 已有部署继续使用原显式 Token 限制；全新表由 CREATE 默认 TRUE。
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS llm_max_tokens_auto BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS embedding_enabled BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS embedding_api_key TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS embedding_base_url TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS embedding_dim INTEGER NOT NULL DEFAULT 384")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS embedding_timeout_sec DOUBLE PRECISION NOT NULL DEFAULT 20.0")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS rerank_enabled BOOLEAN NOT NULL DEFAULT FALSE")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS rerank_api_key TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS rerank_base_url TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS rerank_model TEXT NOT NULL DEFAULT ''")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS rerank_path TEXT NOT NULL DEFAULT '/rerank'")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS rerank_timeout_sec DOUBLE PRECISION NOT NULL DEFAULT 20.0")
                cur.execute("ALTER TABLE system_config ADD COLUMN IF NOT EXISTS enrichment_enabled BOOLEAN NOT NULL DEFAULT FALSE")

    def upsert_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("knowledge_item_id") or str(uuid4())
        now = datetime.now(timezone.utc)
        chunk_rows_for_index: list[dict[str, Any]] = []

        module = str(payload.get("module") or "").strip()
        feature = str(payload.get("feature") or "").strip()
        tags = self._normalize_tags(payload.get("tags"))
        source_uri = str(payload.get("source_uri") or "").strip()
        effective_from = self._coerce_datetime(payload.get("effective_from"))
        effective_to = self._coerce_datetime(payload.get("effective_to"))

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_version, status FROM knowledge_item WHERE id = %s",
                    (item_id,),
                )
                row = cur.fetchone()

                if row is not None and row["status"] == "deleted":
                    raise ValueError(
                        f"knowledge item '{item_id}' has been deleted; restore manually before upsert"
                    )

                if row is None:
                    version = 1
                    cur.execute(
                        """
                        INSERT INTO knowledge_item (
                            id, title, domain, project, module, feature, tags, source_uri,
                            effective_from, effective_to, type, status, current_version, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
                        """,
                        (
                            item_id,
                            payload["title"],
                            payload["domain"],
                            payload["project"],
                            module,
                            feature,
                            tags,
                            source_uri,
                            effective_from,
                            effective_to,
                            payload["type"],
                            version,
                            now,
                            now,
                        ),
                    )
                else:
                    version = int(row["current_version"]) + 1
                    cur.execute(
                        """
                        UPDATE knowledge_item
                        SET title = %s,
                            domain = %s,
                            project = %s,
                            module = %s,
                            feature = %s,
                            tags = %s,
                            source_uri = %s,
                            effective_from = %s,
                            effective_to = %s,
                            type = %s,
                            status = 'active',
                            current_version = %s,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            payload["title"],
                            payload["domain"],
                            payload["project"],
                            module,
                            feature,
                            tags,
                            source_uri,
                            effective_from,
                            effective_to,
                            payload["type"],
                            version,
                            now,
                            item_id,
                        ),
                    )

                version_id = str(uuid4())
                cur.execute(
                    """
                    INSERT INTO knowledge_version (
                        id, knowledge_item_id, version, content_markdown, summary, author, change_note, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        version_id,
                        item_id,
                        version,
                        payload["content_markdown"],
                        payload.get("summary", ""),
                        payload["author"],
                        payload.get("change_note", ""),
                        now,
                    ),
                )

                chunks = self._chunk_text(payload["content_markdown"])
                for idx, chunk in enumerate(chunks):
                    chunk_id = str(uuid4())
                    cur.execute(
                        """
                        INSERT INTO knowledge_chunk (
                            id, knowledge_version_id, chunk_index, chunk_text, token_count, vector_id
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (chunk_id, version_id, idx, chunk, self._token_count(chunk), chunk_id),
                    )
                    chunk_rows_for_index.append(
                        {
                            "chunk_id": chunk_id,
                            "text": chunk,
                            "knowledge_item_id": item_id,
                            "domain": payload["domain"],
                            "project": payload["project"],
                            "module": module,
                            "feature": feature,
                            "tags": tags,
                            "source_uri": source_uri,
                            "effective_from": effective_from.isoformat() if effective_from else "",
                            "effective_to": effective_to.isoformat() if effective_to else "",
                            "version": version,
                            "title": payload["title"],
                            "chunk_index": idx,
                        }
                    )

                # 写入 source_ref：优先使用结构化 sources 字段，兜底将 source_uri 转为 type=file 条目。
                raw_sources = payload.get("sources") or []
                source_rows: list[dict] = []
                for src in raw_sources:
                    if not isinstance(src, dict):
                        continue
                    src_type = str(src.get("type") or "").strip().lower()
                    src_uri = str(src.get("uri") or "").strip()
                    if src_type not in {"file", "chat", "commit", "pr"} or not src_uri:
                        continue
                    captured_at = self._coerce_datetime(src.get("captured_at")) or now
                    source_rows.append(
                        {
                            "type": src_type,
                            "uri": src_uri,
                            "source_hash": str(src.get("source_hash") or "").strip() or None,
                            "captured_at": captured_at,
                        }
                    )
                if not source_rows and source_uri:
                    source_rows.append({"type": "file", "uri": source_uri, "source_hash": None, "captured_at": now})
                for src in source_rows:
                    cur.execute(
                        """
                        INSERT INTO source_ref (id, knowledge_version_id, source_type, source_uri, source_hash, captured_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (str(uuid4()), version_id, src["type"], src["uri"], src["source_hash"], src["captured_at"]),
                    )

                # Rebuild ACL rows for this item.
                cur.execute("DELETE FROM acl_policy WHERE knowledge_item_id = %s", (item_id,))
                public_read = bool(payload.get("public_read", True))
                acl_actors = [str(a).strip() for a in payload.get("acl_actors", []) if str(a).strip()]

                if public_read:
                    cur.execute(
                        """
                        INSERT INTO acl_policy (id, knowledge_item_id, allow_actor, allow_scope, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (str(uuid4()), item_id, "*", "read", now),
                    )
                for actor in acl_actors:
                    cur.execute(
                        """
                        INSERT INTO acl_policy (id, knowledge_item_id, allow_actor, allow_scope, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (str(uuid4()), item_id, actor, "read", now),
                    )

        if self.vector_index is not None:
            try:
                self.vector_index.upsert_chunks(chunk_rows_for_index)
            except Exception:
                logger.exception(
                    "向量写入失败 item_id=%s version=%s，数据已落库但向量索引缺失",
                    item_id, version,
                )
            else:
                # 新向量写入成功后再清理旧版本，排除刚写入的 chunk 避免误删
                new_chunk_ids = [r["chunk_id"] for r in chunk_rows_for_index]
                self.vector_index.delete_item_vectors(item_id, exclude_chunk_ids=new_chunk_ids)

        return {"knowledge_item_id": item_id, "version": version}

    def get_item(self, item_id: str, actor: str | None = None) -> dict[str, Any] | None:
        actor = actor or "anonymous"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      i.id::text AS knowledge_item_id,
                      i.title,
                      i.domain,
                      i.project,
                      i.module,
                      i.feature,
                      i.tags,
                      i.source_uri,
                      i.effective_from,
                      i.effective_to,
                      i.type,
                      i.status,
                      i.current_version AS version,
                      v.content_markdown,
                      v.summary,
                      i.updated_at
                    FROM knowledge_item i
                    JOIN knowledge_version v
                      ON v.knowledge_item_id = i.id
                     AND v.version = i.current_version
                    WHERE i.id = %s
                      AND i.status = 'active'
                      AND (
                        NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                        OR EXISTS (
                            SELECT 1
                            FROM acl_policy ap
                            WHERE ap.knowledge_item_id = i.id
                              AND ap.allow_scope = 'read'
                              AND (ap.allow_actor = '*' OR ap.allow_actor = %s)
                        )
                      )
                    """,
                    (item_id, actor),
                )
                row = cur.fetchone()
                if row is None:
                    return None

                cur.execute(
                    """
                    SELECT source_type AS type, source_uri AS uri
                    FROM source_ref
                    WHERE knowledge_version_id = (
                      SELECT id
                      FROM knowledge_version
                      WHERE knowledge_item_id = %s AND version = %s
                      LIMIT 1
                    )
                    ORDER BY captured_at DESC
                    """,
                    (item_id, row["version"]),
                )
                row["sources"] = cur.fetchall()
                return row

    def delete_item(self, item_id: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE knowledge_item
                    SET status = 'deleted', updated_at = NOW()
                    WHERE id = %s AND status = 'active'
                    """,
                    (item_id,),
                )
                deleted = cur.rowcount > 0
        if deleted and self.vector_index:
            self.vector_index.delete_item_vectors(item_id)
        return deleted

    def search(
        self,
        query: str,
        domain: str,
        project: str | None,
        module: str | None,
        feature: str | None,
        tags: list[str] | None,
        source_uri: str | None,
        as_of: datetime | None,
        top_k: int,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        actor = actor or "anonymous"
        normalized_tags = self._normalize_tags(tags)

        keyword_rows = self._search_keywords(
            query=query,
            domain=domain,
            project=project,
            module=module,
            feature=feature,
            tags=normalized_tags,
            source_uri=source_uri,
            as_of=as_of,
            top_k=top_k,
            actor=actor,
        )
        vector_rows: list[dict[str, Any]] = []

        if self.vector_index is not None:
            try:
                vector_hits = self.vector_index.search(
                    query=query,
                    domain=domain,
                    project=project,
                    top_k=max(top_k * 3, top_k),
                )
                if vector_hits:
                    vector_score_map = {hit.chunk_id: hit.score for hit in vector_hits}
                    vector_rows = self._search_by_chunk_ids(
                        chunk_ids=list(vector_score_map.keys()),
                        vector_score_map=vector_score_map,
                        domain=domain,
                        project=project,
                        module=module,
                        feature=feature,
                        tags=normalized_tags,
                        source_uri=source_uri,
                        as_of=as_of,
                        actor=actor,
                    )
            except Exception:
                logger.warning("向量搜索失败，降级为纯关键词检索 query=%s domain=%s", query, domain, exc_info=True)
                vector_rows = []

        return self._merge_results(keyword_rows=keyword_rows, vector_rows=vector_rows, top_k=top_k)

    def _search_keywords(
        self,
        *,
        query: str,
        domain: str,
        project: str | None,
        module: str | None,
        feature: str | None,
        tags: list[str],
        source_uri: str | None,
        as_of: datetime | None,
        top_k: int,
        actor: str,
    ) -> list[dict[str, Any]]:
        as_of = self._coerce_datetime(as_of)
        source_like = f"%{source_uri.strip()}%" if source_uri and source_uri.strip() else None
        tags_filter = tags or None
        query_terms = query_terms_for_like(query)
        query_patterns = [f"%{term}%" for term in query_terms]
        if not query_patterns:
            query_patterns = [f"%{query}%"]

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      i.id::text AS knowledge_item_id,
                      i.current_version AS version,
                      i.title,
                      i.source_uri,
                      v.content_markdown AS full_content,
                      i.updated_at
                    FROM knowledge_item i
                    JOIN knowledge_version v
                      ON v.knowledge_item_id = i.id
                     AND v.version = i.current_version
                    WHERE i.domain = %s
                      AND i.status = 'active'
                      AND (%s::text IS NULL OR i.project = %s::text)
                      AND (%s::text IS NULL OR i.module = %s::text)
                      AND (%s::text IS NULL OR i.feature = %s::text)
                      AND (%s::text IS NULL OR i.source_uri ILIKE %s::text)
                      AND (%s::text[] IS NULL OR i.tags @> %s::text[])
                      AND (%s::timestamptz IS NULL OR i.effective_from IS NULL OR i.effective_from <= %s::timestamptz)
                      AND (%s::timestamptz IS NULL OR i.effective_to IS NULL OR i.effective_to >= %s::timestamptz)
                      AND (
                        NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                        OR EXISTS (
                            SELECT 1
                            FROM acl_policy ap
                            WHERE ap.knowledge_item_id = i.id
                              AND ap.allow_scope = 'read'
                              AND (ap.allow_actor = '*' OR ap.allow_actor = %s)
                        )
                      )
                      AND (
                        i.title ILIKE ANY(%s::text[])
                        OR v.content_markdown ILIKE ANY(%s::text[])
                      )
                    ORDER BY i.updated_at DESC
                    LIMIT %s
                    """,
                    (
                        domain,
                        project,
                        project,
                        module,
                        module,
                        feature,
                        feature,
                        source_like,
                        source_like,
                        tags_filter,
                        tags_filter,
                        as_of,
                        as_of,
                        as_of,
                        as_of,
                        actor,
                        query_patterns,
                        query_patterns,
                        top_k * 3,
                    ),
                )
                rows = cur.fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            snippet = self._build_snippet(row["full_content"], query_terms, width=180)
            lexical = self._lexical_match_ratio(query_terms, row["title"], snippet)
            keyword_score = round(0.40 + 0.60 * lexical, 6)
            source = [{"type": "source_uri", "uri": row["source_uri"]}] if row.get("source_uri") else []
            out.append(
                {
                    "knowledge_item_id": row["knowledge_item_id"],
                    "version": row["version"],
                    "score": keyword_score,
                    "snippet": snippet,
                    "title": row["title"],
                    "source": source,
                }
            )
        return out

    def _search_by_chunk_ids(
        self,
        *,
        chunk_ids: list[str],
        vector_score_map: dict[str, float],
        domain: str,
        project: str | None,
        module: str | None,
        feature: str | None,
        tags: list[str],
        source_uri: str | None,
        as_of: datetime | None,
        actor: str,
    ) -> list[dict[str, Any]]:
        if not chunk_ids:
            return []

        source_like = f"%{source_uri.strip()}%" if source_uri and source_uri.strip() else None
        tags_filter = tags or None

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      c.id::text AS chunk_id,
                      i.id::text AS knowledge_item_id,
                      i.current_version AS version,
                      i.title,
                      i.source_uri,
                      LEFT(c.chunk_text, 180) AS snippet
                    FROM knowledge_chunk c
                    JOIN knowledge_version v ON c.knowledge_version_id = v.id
                    JOIN knowledge_item i ON v.knowledge_item_id = i.id
                    WHERE c.id = ANY(%s::uuid[])
                      AND i.domain = %s
                      AND i.status = 'active'
                      AND v.version = i.current_version
                      AND (%s::text IS NULL OR i.project = %s::text)
                      AND (%s::text IS NULL OR i.module = %s::text)
                      AND (%s::text IS NULL OR i.feature = %s::text)
                      AND (%s::text IS NULL OR i.source_uri ILIKE %s::text)
                      AND (%s::text[] IS NULL OR i.tags @> %s::text[])
                      AND (%s::timestamptz IS NULL OR i.effective_from IS NULL OR i.effective_from <= %s::timestamptz)
                      AND (%s::timestamptz IS NULL OR i.effective_to IS NULL OR i.effective_to >= %s::timestamptz)
                      AND (
                        NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                        OR EXISTS (
                            SELECT 1
                            FROM acl_policy ap
                            WHERE ap.knowledge_item_id = i.id
                              AND ap.allow_scope = 'read'
                              AND (ap.allow_actor = '*' OR ap.allow_actor = %s)
                        )
                      )
                    """,
                    (
                        chunk_ids,
                        domain,
                        project,
                        project,
                        module,
                        module,
                        feature,
                        feature,
                        source_like,
                        source_like,
                        tags_filter,
                        tags_filter,
                        as_of,
                        as_of,
                        as_of,
                        as_of,
                        actor,
                    ),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            raw = vector_score_map.get(row["chunk_id"], 0.0)
            # Qdrant 余弦相似度对归一化向量通常在 [0, 1]，不再假设 [-1, 1]
            norm = max(0.0, min(1.0, raw))
            score = 0.40 + 0.60 * norm
            source = [{"type": "source_uri", "uri": row["source_uri"]}] if row.get("source_uri") else []
            results.append(
                {
                    "knowledge_item_id": row["knowledge_item_id"],
                    "version": row["version"],
                    "score": score,
                    "snippet": row["snippet"],
                    "title": row["title"],
                    "source": source,
                }
            )
        return results


    def has_system_config(self) -> bool:
        """是否已经保存过系统设置，用于区分 DB 配置和 env fallback。"""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM system_config WHERE id = 1")
                return cur.fetchone() is not None

    def get_system_config(self) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT api_base_url, service_port, grafana_url, ui_theme,
                           llm_enabled, llm_api_key, llm_base_url, llm_model, llm_timeout_sec, llm_temperature, llm_max_tokens, llm_max_tokens_auto,
                           embedding_enabled, embedding_api_key, embedding_base_url, embedding_model, embedding_dim, embedding_timeout_sec,
                           rerank_enabled, rerank_api_key, rerank_base_url, rerank_model, rerank_path, rerank_timeout_sec,
                           enrichment_enabled, updated_at
                    FROM system_config
                    WHERE id = 1
                    """
                )
                row = cur.fetchone()
            if row is not None:
                return row

        return {
            "api_base_url": "http://127.0.0.1:18000",
            "service_port": 18000,
            "grafana_url": "http://127.0.0.1:3000",
            "ui_theme": "neo",
            "llm_enabled": False,
            "llm_api_key": "",
            "llm_base_url": "https://api.openai.com/v1",
            "llm_model": "gpt-4o-mini",
            "llm_timeout_sec": 30.0,
            "llm_temperature": 0.2,
            "llm_max_tokens": 1024,
            "llm_max_tokens_auto": True,
            "embedding_enabled": False,
            "embedding_api_key": "",
            "embedding_base_url": "",
            "embedding_model": "",
            "embedding_dim": 384,
            "embedding_timeout_sec": 20.0,
            "rerank_enabled": False,
            "rerank_api_key": "",
            "rerank_base_url": "",
            "rerank_model": "",
            "rerank_path": "/rerank",
            "rerank_timeout_sec": 20.0,
            "enrichment_enabled": False,
            "updated_at": None,
        }

    def upsert_system_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_base_url = str(payload.get("api_base_url") or "").strip()
        service_port = int(payload.get("service_port") or 18000)
        grafana_url = str(payload.get("grafana_url") or "").strip()
        ui_theme = str(payload.get("ui_theme") or "neo").strip().lower()
        llm_enabled = bool(payload.get("llm_enabled", False))
        llm_api_key = str(payload.get("llm_api_key") or "").strip()
        llm_base_url = str(payload.get("llm_base_url") or "https://api.openai.com/v1").strip().rstrip("/")
        llm_model = str(payload.get("llm_model") or "gpt-4o-mini").strip()
        llm_timeout_sec = float(payload.get("llm_timeout_sec") or 30.0)
        llm_temperature = float(payload.get("llm_temperature") or 0.2)
        llm_max_tokens = int(payload.get("llm_max_tokens") or 1024)
        llm_max_tokens_auto = bool(payload.get("llm_max_tokens_auto", True))
        embedding_enabled = bool(payload.get("embedding_enabled", False))
        embedding_api_key = str(payload.get("embedding_api_key") or "").strip()
        embedding_base_url = str(payload.get("embedding_base_url") or "").strip().rstrip("/")
        embedding_model = str(payload.get("embedding_model") or "").strip()
        embedding_dim = max(1, int(payload.get("embedding_dim") or 384))
        embedding_timeout_sec = float(payload.get("embedding_timeout_sec") or 20.0)
        rerank_enabled = bool(payload.get("rerank_enabled", False))
        rerank_api_key = str(payload.get("rerank_api_key") or "").strip()
        rerank_base_url = str(payload.get("rerank_base_url") or "").strip().rstrip("/")
        rerank_model = str(payload.get("rerank_model") or "").strip()
        rerank_path = str(payload.get("rerank_path") or "/rerank").strip()
        if not rerank_path.startswith("/"):
            rerank_path = "/" + rerank_path
        rerank_timeout_sec = float(payload.get("rerank_timeout_sec") or 20.0)
        enrichment_enabled = bool(payload.get("enrichment_enabled", False))

        if ui_theme not in {"linear", "glass", "neo"}:
            ui_theme = "neo"
        if not api_base_url or not grafana_url:
            raise ValueError("api_base_url and grafana_url are required")

        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system_config (
                      id, api_base_url, service_port, grafana_url, ui_theme,
                      llm_enabled, llm_api_key, llm_base_url, llm_model,
                      llm_timeout_sec, llm_temperature, llm_max_tokens, llm_max_tokens_auto,
                      embedding_enabled, embedding_api_key, embedding_base_url, embedding_model, embedding_dim, embedding_timeout_sec,
                      rerank_enabled, rerank_api_key, rerank_base_url, rerank_model, rerank_path, rerank_timeout_sec,
                      enrichment_enabled, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT(id) DO UPDATE SET
                      api_base_url=excluded.api_base_url,
                      service_port=excluded.service_port,
                      grafana_url=excluded.grafana_url,
                      ui_theme=excluded.ui_theme,
                      llm_enabled=excluded.llm_enabled,
                      llm_api_key=excluded.llm_api_key,
                      llm_base_url=excluded.llm_base_url,
                      llm_model=excluded.llm_model,
                      llm_timeout_sec=excluded.llm_timeout_sec,
                      llm_temperature=excluded.llm_temperature,
                      llm_max_tokens=excluded.llm_max_tokens,
                      llm_max_tokens_auto=excluded.llm_max_tokens_auto,
                      embedding_enabled=excluded.embedding_enabled,
                      embedding_api_key=excluded.embedding_api_key,
                      embedding_base_url=excluded.embedding_base_url,
                      embedding_model=excluded.embedding_model,
                      embedding_dim=excluded.embedding_dim,
                      embedding_timeout_sec=excluded.embedding_timeout_sec,
                      rerank_enabled=excluded.rerank_enabled,
                      rerank_api_key=excluded.rerank_api_key,
                      rerank_base_url=excluded.rerank_base_url,
                      rerank_model=excluded.rerank_model,
                      rerank_path=excluded.rerank_path,
                      rerank_timeout_sec=excluded.rerank_timeout_sec,
                      enrichment_enabled=excluded.enrichment_enabled,
                      updated_at=excluded.updated_at
                    """,
                    (
                        1, api_base_url, service_port, grafana_url, ui_theme,
                        llm_enabled, llm_api_key, llm_base_url, llm_model,
                        llm_timeout_sec, llm_temperature, llm_max_tokens, llm_max_tokens_auto,
                        embedding_enabled, embedding_api_key, embedding_base_url, embedding_model, embedding_dim, embedding_timeout_sec,
                        rerank_enabled, rerank_api_key, rerank_base_url, rerank_model, rerank_path, rerank_timeout_sec,
                        enrichment_enabled, now,
                    ),
                )

        return self.get_system_config()


    # ── /ask 专用 chunk 级检索 ─────────────────────────────────────────────

    def search_chunks_for_ask(
        self,
        query: str,
        domain: str,
        project: str | None,
        top_k: int,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """为 /ask 端点检索最相关的 chunk，优先走向量，降级走关键词。"""
        actor = actor or "anonymous"
        chunk_ids: list[str] = []
        vector_score_map: dict[str, float] = {}

        if self.vector_index is not None:
            try:
                hits = self.vector_index.search(
                    query=query, domain=domain, project=project, top_k=top_k * 3
                )
                if hits:
                    chunk_ids = [h.chunk_id for h in hits]
                    vector_score_map = {h.chunk_id: h.score for h in hits}
            except Exception:
                logger.warning("ask 向量检索失败，降级关键词 query=%s", query, exc_info=True)

        if chunk_ids:
            return self._fetch_chunks_by_ids(chunk_ids, vector_score_map, domain, project, actor, top_k)
        return self._keyword_search_chunks(query=query, domain=domain, project=project, actor=actor, top_k=top_k)

    def _fetch_chunks_by_ids(
        self,
        chunk_ids: list[str],
        vector_score_map: dict[str, float],
        domain: str,
        project: str | None,
        actor: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id::text AS chunk_id,
                           c.chunk_text,
                           i.id::text AS knowledge_item_id,
                           i.title,
                           i.current_version AS version
                    FROM knowledge_chunk c
                    JOIN knowledge_version v ON c.knowledge_version_id = v.id
                    JOIN knowledge_item i ON v.knowledge_item_id = i.id
                    WHERE c.id = ANY(%s::uuid[])
                      AND i.domain = %s
                      AND i.status = 'active'
                      AND v.version = i.current_version
                      AND (%s::text IS NULL OR i.project = %s::text)
                      AND (i.effective_from IS NULL OR i.effective_from <= NOW())
                      AND (i.effective_to IS NULL OR i.effective_to >= NOW())
                      AND (
                        NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                        OR EXISTS (
                            SELECT 1 FROM acl_policy ap
                            WHERE ap.knowledge_item_id = i.id
                              AND ap.allow_scope = 'read'
                              AND (ap.allow_actor = '*' OR ap.allow_actor = %s)
                        )
                      )
                    """,
                    (chunk_ids, domain, project, project, actor),
                )
                rows = cur.fetchall()

        rows.sort(key=lambda r: vector_score_map.get(r["chunk_id"], 0.0), reverse=True)
        return list(rows[:top_k])

    def _keyword_search_chunks(
        self,
        query: str,
        domain: str,
        project: str | None,
        actor: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        query_terms = query_terms_for_like(query)
        patterns = [f"%{t}%" for t in query_terms] if query_terms else [f"%{query}%"]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id::text AS chunk_id,
                           c.chunk_text,
                           i.id::text AS knowledge_item_id,
                           i.title,
                           i.current_version AS version
                    FROM knowledge_chunk c
                    JOIN knowledge_version v ON c.knowledge_version_id = v.id
                    JOIN knowledge_item i ON v.knowledge_item_id = i.id
                    WHERE i.domain = %s
                      AND i.status = 'active'
                      AND v.version = i.current_version
                      AND (%s::text IS NULL OR i.project = %s::text)
                      AND (i.effective_from IS NULL OR i.effective_from <= NOW())
                      AND (i.effective_to IS NULL OR i.effective_to >= NOW())
                      AND c.chunk_text ILIKE ANY(%s::text[])
                      AND (
                        NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                        OR EXISTS (
                            SELECT 1 FROM acl_policy ap
                            WHERE ap.knowledge_item_id = i.id
                              AND ap.allow_scope = 'read'
                              AND (ap.allow_actor = '*' OR ap.allow_actor = %s)
                        )
                      )
                    ORDER BY i.updated_at DESC
                    LIMIT %s
                    """,
                    (domain, project, project, patterns, actor, top_k * 3),
                )
                rows = cur.fetchall()

        # 按 lexical score 重排，与向量路径排序风格对齐
        result = sorted(
            rows,
            key=lambda r: self._lexical_match_ratio(query_terms, r.get("title", ""), r.get("chunk_text", "")),
            reverse=True,
        )
        return result[:top_k]
