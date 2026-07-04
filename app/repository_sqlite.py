from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Generator
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.repository_base import BaseKnowledgeRepo
from app.text_tokens import query_terms_for_like


@dataclass
class SqliteKnowledgeRepo(BaseKnowledgeRepo):
    sqlite_path: str
    vector_index: Any = field(default=None)

    def __post_init__(self) -> None:
        import os

        db_dir = os.path.dirname(os.path.abspath(self.sqlite_path))
        if db_dir and not os.path.isdir(db_dir):
            raise RuntimeError(
                f"SQLite 数据库目录不存在: {db_dir}，请先创建该目录。"
            )
        if db_dir and not os.access(db_dir, os.W_OK):
            raise RuntimeError(
                f"SQLite 数据库目录无写权限: {db_dir}"
            )
        if os.path.exists(self.sqlite_path) and not os.access(self.sqlite_path, os.W_OK):
            raise RuntimeError(
                f"SQLite 数据库文件无写权限: {self.sqlite_path}"
            )
        self._ensure_tables()

    @contextlib.contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_schema(self) -> None:
        """公开重建 schema 入口。

        场景：X2.5 legacy lane 的 clear / import-package 走文件级 rm knowledge.db +
        cp 新文件后，本单例 repo 内部无 fd 缓存但 sqlite3.connect(path) 遇到不存在的
        文件会创建空 db（无表） → 后续所有请求撞 ``no such table: system_config`` 500。
        clear / import 完成后必须调 reset_schema() 让 CREATE TABLE IF NOT EXISTS 兜底
        建表。__post_init__ 已经在启动时跑过一次，reset_schema 是运行时"数据文件被外部
        动过"的自愈入口，跟 on_qdrant_reinit 对称。
        """
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_item (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    project TEXT NOT NULL DEFAULT '',
                    module TEXT NOT NULL DEFAULT '',
                    feature TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    source_uri TEXT NOT NULL DEFAULT '',
                    effective_from TEXT,
                    effective_to TEXT,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    current_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_version (
                    id TEXT PRIMARY KEY,
                    knowledge_item_id TEXT NOT NULL REFERENCES knowledge_item(id),
                    version INTEGER NOT NULL,
                    content_markdown TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    change_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_chunk (
                    id TEXT PRIMARY KEY,
                    knowledge_version_id TEXT NOT NULL REFERENCES knowledge_version(id),
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    vector_id TEXT
                );
                CREATE TABLE IF NOT EXISTS source_ref (
                    id TEXT PRIMARY KEY,
                    knowledge_version_id TEXT NOT NULL REFERENCES knowledge_version(id),
                    source_type TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_hash TEXT,
                    captured_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS acl_policy (
                    id TEXT PRIMARY KEY,
                    knowledge_item_id TEXT NOT NULL REFERENCES knowledge_item(id),
                    allow_actor TEXT NOT NULL,
                    allow_scope TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    api_base_url TEXT NOT NULL,
                    service_port INTEGER NOT NULL DEFAULT 18000,
                    grafana_url TEXT NOT NULL,
                    ui_theme TEXT NOT NULL DEFAULT 'neo',
                    llm_enabled INTEGER NOT NULL DEFAULT 0,
                    llm_api_key TEXT NOT NULL DEFAULT '',
                    llm_base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
                    llm_model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
                    llm_timeout_sec REAL NOT NULL DEFAULT 30.0,
                    llm_temperature REAL NOT NULL DEFAULT 0.2,
                    llm_max_tokens INTEGER NOT NULL DEFAULT 1024,
                    embedding_enabled INTEGER NOT NULL DEFAULT 0,
                    embedding_api_key TEXT NOT NULL DEFAULT '',
                    embedding_base_url TEXT NOT NULL DEFAULT '',
                    embedding_model TEXT NOT NULL DEFAULT '',
                    embedding_dim INTEGER NOT NULL DEFAULT 384,
                    embedding_timeout_sec REAL NOT NULL DEFAULT 20.0,
                    rerank_enabled INTEGER NOT NULL DEFAULT 0,
                    rerank_api_key TEXT NOT NULL DEFAULT '',
                    rerank_base_url TEXT NOT NULL DEFAULT '',
                    rerank_model TEXT NOT NULL DEFAULT '',
                    rerank_path TEXT NOT NULL DEFAULT '/rerank',
                    rerank_timeout_sec REAL NOT NULL DEFAULT 20.0,
                    enrichment_enabled INTEGER NOT NULL DEFAULT 0,
                    embedding_service_mode TEXT NOT NULL DEFAULT 'disabled',
                    embedding_service_managed INTEGER NOT NULL DEFAULT 0,
                    embedding_service_model_id TEXT NOT NULL DEFAULT '',
                    embedding_service_port INTEGER NOT NULL DEFAULT 0,
                    embedding_service_device TEXT NOT NULL DEFAULT 'cpu',
                    embedding_service_pytorch_mirror TEXT NOT NULL DEFAULT 'https://download.pytorch.org/whl/',
                    embedding_service_cuda_version TEXT NOT NULL DEFAULT 'cu124',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ki_domain_project_status
                    ON knowledge_item(domain, project, status);
                CREATE INDEX IF NOT EXISTS idx_kv_item_version
                    ON knowledge_version(knowledge_item_id, version);
                CREATE INDEX IF NOT EXISTS idx_kc_version
                    ON knowledge_chunk(knowledge_version_id);
                CREATE INDEX IF NOT EXISTS idx_acl_item
                    ON acl_policy(knowledge_item_id);
            """)
            # Backward-compatible migration for existing DBs.
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN ui_theme TEXT NOT NULL DEFAULT 'neo'")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN service_port INTEGER NOT NULL DEFAULT 18000")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_enabled INTEGER NOT NULL DEFAULT 0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_api_key TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1'")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_model TEXT NOT NULL DEFAULT 'gpt-4o-mini'")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_timeout_sec REAL NOT NULL DEFAULT 30.0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_temperature REAL NOT NULL DEFAULT 0.2")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN llm_max_tokens INTEGER NOT NULL DEFAULT 1024")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_enabled INTEGER NOT NULL DEFAULT 0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_api_key TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_base_url TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_dim INTEGER NOT NULL DEFAULT 384")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_timeout_sec REAL NOT NULL DEFAULT 20.0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN rerank_enabled INTEGER NOT NULL DEFAULT 0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN rerank_api_key TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN rerank_base_url TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN rerank_model TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN rerank_path TEXT NOT NULL DEFAULT '/rerank'")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN rerank_timeout_sec REAL NOT NULL DEFAULT 20.0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN enrichment_enabled INTEGER NOT NULL DEFAULT 0")
            # 内置 embedding 服务字段（openspec embedded-embedding-service v1.2 §配置变更）。
            # 老用户默认 mode=disabled，无感升级（不启用内置服务=走老配置）。
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_service_mode TEXT NOT NULL DEFAULT 'disabled'")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_service_managed INTEGER NOT NULL DEFAULT 0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_service_model_id TEXT NOT NULL DEFAULT ''")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_service_port INTEGER NOT NULL DEFAULT 0")
            with contextlib.suppress(Exception):
                conn.execute("ALTER TABLE system_config ADD COLUMN embedding_service_device TEXT NOT NULL DEFAULT 'cpu'")
            # v1.3.12 cuda wheel 安装配置（device=cuda 时生效）
            with contextlib.suppress(Exception):
                conn.execute(
                    "ALTER TABLE system_config ADD COLUMN embedding_service_pytorch_mirror"
                    " TEXT NOT NULL DEFAULT 'https://download.pytorch.org/whl/'"
                )
            with contextlib.suppress(Exception):
                conn.execute(
                    "ALTER TABLE system_config ADD COLUMN embedding_service_cuda_version"
                    " TEXT NOT NULL DEFAULT 'cu124'"
                )

    @staticmethod
    def _dt_str(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        # naive datetime 视为 UTC，统一序列化为带时区 ISO8601 保证字典序可比
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()

    def upsert_item(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("knowledge_item_id") or str(uuid4())
        now = datetime.now(timezone.utc)
        now_str = self._dt_str(now)
        chunk_rows_for_index: list[dict[str, Any]] = []

        module = str(payload.get("module") or "").strip()
        feature = str(payload.get("feature") or "").strip()
        tags = self._normalize_tags(payload.get("tags"))
        tags_json = json.dumps(tags)
        source_uri = str(payload.get("source_uri") or "").strip()
        effective_from = self._coerce_datetime(payload.get("effective_from"))
        effective_to = self._coerce_datetime(payload.get("effective_to"))
        ef_str = self._dt_str(effective_from)
        et_str = self._dt_str(effective_to)

        with self._connect() as conn:
            row = conn.execute(
                "SELECT current_version, status FROM knowledge_item WHERE id = ?", (item_id,)
            ).fetchone()

            if row is not None and row["status"] == "deleted":
                raise ValueError(
                    f"knowledge item '{item_id}' has been deleted; restore manually before upsert"
                )

            if row is None:
                version = 1
                conn.execute(
                    """
                    INSERT INTO knowledge_item (
                        id, title, domain, project, module, feature, tags, source_uri,
                        effective_from, effective_to, type, status, current_version,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        item_id, payload["title"], payload["domain"], payload["project"],
                        module, feature, tags_json, source_uri,
                        ef_str, et_str, payload["type"],
                        version, now_str, now_str,
                    ),
                )
            else:
                version = int(row["current_version"]) + 1
                conn.execute(
                    """
                    UPDATE knowledge_item
                    SET title=?, domain=?, project=?, module=?, feature=?, tags=?,
                        source_uri=?, effective_from=?, effective_to=?, type=?,
                        status='active', current_version=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        payload["title"], payload["domain"], payload["project"],
                        module, feature, tags_json, source_uri,
                        ef_str, et_str, payload["type"],
                        version, now_str, item_id,
                    ),
                )

            version_id = str(uuid4())
            conn.execute(
                """
                INSERT INTO knowledge_version (
                    id, knowledge_item_id, version, content_markdown, summary,
                    author, change_note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id, item_id, version, payload["content_markdown"],
                    payload.get("summary", ""), payload["author"],
                    payload.get("change_note", ""), now_str,
                ),
            )

            chunks = self._chunk_text(payload["content_markdown"])
            for idx, chunk in enumerate(chunks):
                chunk_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO knowledge_chunk (
                        id, knowledge_version_id, chunk_index, chunk_text, token_count, vector_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
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
                        "effective_from": ef_str or "",
                        "effective_to": et_str or "",
                        "version": version,
                        "title": payload["title"],
                        "chunk_index": idx,
                    }
                )

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
                        "captured_at": self._dt_str(captured_at),
                    }
                )
            if not source_rows and source_uri:
                source_rows.append({"type": "file", "uri": source_uri, "source_hash": None, "captured_at": now_str})

            for src in source_rows:
                conn.execute(
                    """
                    INSERT INTO source_ref (
                        id, knowledge_version_id, source_type, source_uri, source_hash, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid4()), version_id, src["type"], src["uri"], src["source_hash"], src["captured_at"]),
                )

            conn.execute("DELETE FROM acl_policy WHERE knowledge_item_id = ?", (item_id,))
            public_read = bool(payload.get("public_read", True))
            acl_actors = [str(a).strip() for a in payload.get("acl_actors", []) if str(a).strip()]

            if public_read:
                conn.execute(
                    "INSERT INTO acl_policy (id, knowledge_item_id, allow_actor, allow_scope, created_at) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid4()), item_id, "*", "read", now_str),
                )
            for actor in acl_actors:
                conn.execute(
                    "INSERT INTO acl_policy (id, knowledge_item_id, allow_actor, allow_scope, created_at) VALUES (?, ?, ?, ?, ?)",
                    (str(uuid4()), item_id, actor, "read", now_str),
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
            row = conn.execute(
                """
                SELECT
                    i.id AS knowledge_item_id,
                    i.title, i.domain, i.project, i.module, i.feature,
                    i.tags, i.source_uri, i.effective_from, i.effective_to,
                    i.type, i.status,
                    i.current_version AS version,
                    v.content_markdown, v.summary,
                    i.updated_at
                FROM knowledge_item i
                JOIN knowledge_version v
                    ON v.knowledge_item_id = i.id AND v.version = i.current_version
                WHERE i.id = ?
                  AND i.status = 'active'
                  AND (
                    NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                    OR EXISTS (
                        SELECT 1 FROM acl_policy ap
                        WHERE ap.knowledge_item_id = i.id
                          AND ap.allow_scope = 'read'
                          AND (ap.allow_actor = '*' OR ap.allow_actor = ?)
                    )
                  )
                """,
                (item_id, actor),
            ).fetchone()

            if row is None:
                return None

            result = dict(row)
            result["tags"] = json.loads(result.get("tags") or "[]")

            sources = conn.execute(
                """
                SELECT source_type AS type, source_uri AS uri
                FROM source_ref
                WHERE knowledge_version_id = (
                    SELECT id FROM knowledge_version
                    WHERE knowledge_item_id = ? AND version = ?
                    LIMIT 1
                )
                ORDER BY captured_at DESC
                """,
                (item_id, result["version"]),
            ).fetchall()
            result["sources"] = [dict(r) for r in sources]
            return result

    def delete_item(self, item_id: str) -> bool:
        now_str = self._dt_str(datetime.now(timezone.utc))
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE knowledge_item
                SET status = 'deleted', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now_str, item_id),
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
        tags_json = json.dumps(tags) if tags else None
        as_of_str = self._dt_str(as_of)
        query_terms = query_terms_for_like(query)
        query_patterns = [f"%{t}%" for t in query_terms] or [f"%{query}%"]

        # 动态构建 LIKE 条件（SQLite 无 ILIKE ANY 语法）
        like_parts = []
        like_params: list[str] = []
        for pat in query_patterns:
            like_parts.append("(LOWER(i.title) LIKE LOWER(?) OR LOWER(v.content_markdown) LIKE LOWER(?))")
            like_params.extend([pat, pat])
        like_sql = " OR ".join(like_parts)

        params: list[Any] = [
            domain,
            project, project,
            module, module,
            feature, feature,
            source_like, source_like,
            tags_json, tags_json,
            as_of_str, as_of_str,
            as_of_str, as_of_str,
            actor,
        ] + like_params + [top_k * 3]

        sql = f"""
            SELECT
                i.id AS knowledge_item_id,
                i.current_version AS version,
                i.title, i.source_uri,
                v.content_markdown AS full_content,
                i.updated_at
            FROM knowledge_item i
            JOIN knowledge_version v
                ON v.knowledge_item_id = i.id AND v.version = i.current_version
            WHERE i.domain = ?
              AND i.status = 'active'
              AND (? IS NULL OR i.project = ?)
              AND (? IS NULL OR i.module = ?)
              AND (? IS NULL OR i.feature = ?)
              AND (? IS NULL OR LOWER(i.source_uri) LIKE LOWER(?))
              AND (? IS NULL OR NOT EXISTS (
                  SELECT 1 FROM json_each(?) req
                  WHERE req.value NOT IN (SELECT value FROM json_each(i.tags))
              ))
              AND (? IS NULL OR i.effective_from IS NULL OR i.effective_from <= ?)
              AND (? IS NULL OR i.effective_to IS NULL OR i.effective_to >= ?)
              AND (
                NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                OR EXISTS (
                    SELECT 1 FROM acl_policy ap
                    WHERE ap.knowledge_item_id = i.id
                      AND ap.allow_scope = 'read'
                      AND (ap.allow_actor = '*' OR ap.allow_actor = ?)
                )
              )
              AND ({like_sql})
            ORDER BY i.updated_at DESC
            LIMIT ?
        """

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        out: list[dict[str, Any]] = []
        for row in rows:
            snippet = self._build_snippet(row["full_content"], query_terms, width=180)
            lexical = self._lexical_match_ratio(query_terms, row["title"], snippet)
            keyword_score = round(0.40 + 0.60 * lexical, 6)
            source = [{"type": "source_uri", "uri": row["source_uri"]}] if row["source_uri"] else []
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
        tags_json = json.dumps(tags) if tags else None
        as_of_str = self._dt_str(as_of)
        placeholders = ",".join("?" * len(chunk_ids))

        sql = f"""
            SELECT
                c.id AS chunk_id,
                i.id AS knowledge_item_id,
                i.current_version AS version,
                i.title, i.source_uri,
                SUBSTR(c.chunk_text, 1, 180) AS snippet
            FROM knowledge_chunk c
            JOIN knowledge_version v ON c.knowledge_version_id = v.id
            JOIN knowledge_item i ON v.knowledge_item_id = i.id
            WHERE c.id IN ({placeholders})
              AND i.domain = ?
              AND i.status = 'active'
              AND v.version = i.current_version
              AND (? IS NULL OR i.project = ?)
              AND (? IS NULL OR i.module = ?)
              AND (? IS NULL OR i.feature = ?)
              AND (? IS NULL OR LOWER(i.source_uri) LIKE LOWER(?))
              AND (? IS NULL OR NOT EXISTS (
                  SELECT 1 FROM json_each(?) req
                  WHERE req.value NOT IN (SELECT value FROM json_each(i.tags))
              ))
              AND (? IS NULL OR i.effective_from IS NULL OR i.effective_from <= ?)
              AND (? IS NULL OR i.effective_to IS NULL OR i.effective_to >= ?)
              AND (
                NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                OR EXISTS (
                    SELECT 1 FROM acl_policy ap
                    WHERE ap.knowledge_item_id = i.id
                      AND ap.allow_scope = 'read'
                      AND (ap.allow_actor = '*' OR ap.allow_actor = ?)
                )
              )
        """
        params: list[Any] = list(chunk_ids) + [
            domain,
            project, project,
            module, module,
            feature, feature,
            source_like, source_like,
            tags_json, tags_json,
            as_of_str, as_of_str,
            as_of_str, as_of_str,
            actor,
        ]

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            raw = vector_score_map.get(row["chunk_id"], 0.0)
            norm = max(0.0, min(1.0, raw))
            score = 0.40 + 0.60 * norm
            source = [{"type": "source_uri", "uri": row["source_uri"]}] if row["source_uri"] else []
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

    def search_chunks_for_ask(
        self,
        query: str,
        domain: str,
        project: str | None,
        top_k: int,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
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
        now_str = self._dt_str(datetime.now(timezone.utc))
        placeholders = ",".join("?" * len(chunk_ids))

        sql = f"""
            SELECT c.id AS chunk_id,
                   c.chunk_text,
                   i.id AS knowledge_item_id,
                   i.title,
                   i.current_version AS version
            FROM knowledge_chunk c
            JOIN knowledge_version v ON c.knowledge_version_id = v.id
            JOIN knowledge_item i ON v.knowledge_item_id = i.id
            WHERE c.id IN ({placeholders})
              AND i.domain = ?
              AND i.status = 'active'
              AND v.version = i.current_version
              AND (? IS NULL OR i.project = ?)
              AND (i.effective_from IS NULL OR i.effective_from <= ?)
              AND (i.effective_to IS NULL OR i.effective_to >= ?)
              AND (
                NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                OR EXISTS (
                    SELECT 1 FROM acl_policy ap
                    WHERE ap.knowledge_item_id = i.id
                      AND ap.allow_scope = 'read'
                      AND (ap.allow_actor = '*' OR ap.allow_actor = ?)
                )
              )
        """
        params: list[Any] = list(chunk_ids) + [domain, project, project, now_str, now_str, actor]

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        result = [dict(r) for r in rows]
        result.sort(key=lambda r: vector_score_map.get(r["chunk_id"], 0.0), reverse=True)
        return result[:top_k]

    def _keyword_search_chunks(
        self,
        query: str,
        domain: str,
        project: str | None,
        actor: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        now_str = self._dt_str(datetime.now(timezone.utc))
        query_terms = query_terms_for_like(query)
        patterns = [f"%{t}%" for t in query_terms] if query_terms else [f"%{query}%"]

        like_parts = [f"LOWER(c.chunk_text) LIKE LOWER(?)" for _ in patterns]
        like_sql = " OR ".join(like_parts)

        sql = f"""
            SELECT c.id AS chunk_id,
                   c.chunk_text,
                   i.id AS knowledge_item_id,
                   i.title,
                   i.current_version AS version
            FROM knowledge_chunk c
            JOIN knowledge_version v ON c.knowledge_version_id = v.id
            JOIN knowledge_item i ON v.knowledge_item_id = i.id
            WHERE i.domain = ?
              AND i.status = 'active'
              AND v.version = i.current_version
              AND (? IS NULL OR i.project = ?)
              AND (i.effective_from IS NULL OR i.effective_from <= ?)
              AND (i.effective_to IS NULL OR i.effective_to >= ?)
              AND ({like_sql})
              AND (
                NOT EXISTS (SELECT 1 FROM acl_policy ap0 WHERE ap0.knowledge_item_id = i.id)
                OR EXISTS (
                    SELECT 1 FROM acl_policy ap
                    WHERE ap.knowledge_item_id = i.id
                      AND ap.allow_scope = 'read'
                      AND (ap.allow_actor = '*' OR ap.allow_actor = ?)
                )
              )
            ORDER BY i.updated_at DESC
            LIMIT ?
        """
        params: list[Any] = [domain, project, project, now_str, now_str] + patterns + [actor, top_k * 3]

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        # 按 lexical score 重排，与向量路径排序风格对齐
        query_terms = query_terms_for_like(query)
        result = sorted(
            [dict(r) for r in rows],
            key=lambda r: self._lexical_match_ratio(query_terms, r.get("title", ""), r.get("chunk_text", "")),
            reverse=True,
        )
        return result[:top_k]

    # ------------------------------------------------------------------
    # backup / reindex 辅助
    # ------------------------------------------------------------------

    def iter_pending_chunks(self, batch_size: int = 100) -> Generator[dict[str, Any], None, None]:
        """按 batch 流式返回 vector_id 为空的 chunk，供 reindex 续传使用。

        仅返回当前版本（current_version）且 item 状态为 active 的 chunk。
        字段：id / chunk_text / chunk_index / knowledge_item_id / vector_id。
        """
        sql = """
            SELECT c.id, c.chunk_text, c.chunk_index,
                   v.knowledge_item_id, c.vector_id
            FROM knowledge_chunk c
            JOIN knowledge_version v ON c.knowledge_version_id = v.id
            JOIN knowledge_item i ON v.knowledge_item_id = i.id
            WHERE (c.vector_id IS NULL OR c.vector_id = '')
              AND i.status = 'active'
              AND v.version = i.current_version
            ORDER BY c.id
            LIMIT ? OFFSET ?
        """
        offset = 0
        while True:
            with self._connect() as conn:
                rows = conn.execute(sql, (batch_size, offset)).fetchall()
            if not rows:
                break
            for r in rows:
                yield dict(r)
            offset += len(rows)

    def iter_active_chunks_for_reindex(
        self, batch_size: int = 100
    ) -> Generator[dict[str, Any], None, None]:
        """流式返回所有 active 当前版本 chunk 的完整字段，供全量 rebuild 使用。

        与 iter_pending_chunks 的区别：① 返回**全部** active chunk（不只 vector_id
        为空的）② 字段对齐 VectorIndex 向量 payload 需求（domain/project/version/title）。
        字段：chunk_id / text / chunk_index / knowledge_item_id / version /
        domain / project / title。
        """
        sql = """
            SELECT c.id AS chunk_id, c.chunk_text AS text, c.chunk_index,
                   v.knowledge_item_id, v.version,
                   i.domain, i.project, i.title
            FROM knowledge_chunk c
            JOIN knowledge_version v ON c.knowledge_version_id = v.id
            JOIN knowledge_item i ON v.knowledge_item_id = i.id
            WHERE i.status = 'active'
              AND v.version = i.current_version
            ORDER BY c.id
            LIMIT ? OFFSET ?
        """
        offset = 0
        while True:
            with self._connect() as conn:
                rows = conn.execute(sql, (batch_size, offset)).fetchall()
            if not rows:
                break
            for r in rows:
                yield dict(r)
            offset += len(rows)

    def count_active_chunks(self) -> int:
        """统计 active 当前版本 chunk 总数（rebuild 进度分母 / reindex 阈值判定）。"""
        sql = """
            SELECT COUNT(*) AS n
            FROM knowledge_chunk c
            JOIN knowledge_version v ON c.knowledge_version_id = v.id
            JOIN knowledge_item i ON v.knowledge_item_id = i.id
            WHERE i.status = 'active' AND v.version = i.current_version
        """
        with self._connect() as conn:
            row = conn.execute(sql).fetchone()
        return int(row["n"]) if row else 0

    def reset_all_vector_ids(self) -> int:
        """把所有 chunk 的 vector_id 置空（rebuild 前调用），返回受影响行数。

        rebuild 流程：删 collection → reset_all_vector_ids → 流式重 embed 后逐条
        set_chunk_vector_id 回写，使 vector_id 状态与新索引一致。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE knowledge_chunk SET vector_id = NULL "
                "WHERE vector_id IS NOT NULL AND vector_id != ''"
            )
            return cur.rowcount

    def set_chunk_vector_ids(self, chunk_ids: list[str]) -> None:
        """批量回写 vector_id（rebuild 中 qdrant point id 即 chunk_id，故 vector_id=chunk_id）。"""
        if not chunk_ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE knowledge_chunk SET vector_id = ? WHERE id = ?",
                [(cid, cid) for cid in chunk_ids],
            )

    def clear_all_active_data(self) -> None:
        """清空所有业务表（保留 system_config）。

        供 backup import overwrite 使用。调用前必须确保 maintenance flag 已置位，
        且外层 auto-backup + 内层 .pre-restore 已经完成。
        """
        with self._connect() as conn:
            # acl_policy 和 source_ref 有外键依赖，按依赖顺序删除
            for table in (
                "acl_policy",
                "source_ref",
                "knowledge_chunk",
                "knowledge_version",
                "knowledge_item",
            ):
                conn.execute(f"DELETE FROM {table}")

    def get_system_config(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT api_base_url, service_port, grafana_url, ui_theme,"
                " llm_enabled, llm_api_key, llm_base_url, llm_model, llm_timeout_sec, llm_temperature, llm_max_tokens,"
                " embedding_enabled, embedding_api_key, embedding_base_url, embedding_model, embedding_dim, embedding_timeout_sec,"
                " rerank_enabled, rerank_api_key, rerank_base_url, rerank_model, rerank_path, rerank_timeout_sec,"
                " enrichment_enabled,"
                " embedding_service_mode, embedding_service_managed, embedding_service_model_id,"
                " embedding_service_port, embedding_service_device,"
                " embedding_service_pytorch_mirror, embedding_service_cuda_version, updated_at"
                " FROM system_config WHERE id = 1"
            ).fetchone()
            if row:
                out = dict(row)
                for k in ("llm_enabled", "embedding_enabled", "rerank_enabled", "enrichment_enabled",
                          "embedding_service_managed"):
                    out[k] = bool(out.get(k))
                return out
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
                "embedding_service_mode": "disabled",
                "embedding_service_managed": False,
                "embedding_service_model_id": "",
                "embedding_service_port": 0,
                "embedding_service_device": "cpu",
                "embedding_service_pytorch_mirror": "https://download.pytorch.org/whl/",
                "embedding_service_cuda_version": "cu124",
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
        # 内置 embedding 服务字段；非法 mode/device 回落默认，避免脏值流入启动命令。
        embedding_service_mode = str(payload.get("embedding_service_mode") or "disabled").strip().lower()
        if embedding_service_mode not in {"local", "external", "disabled"}:
            embedding_service_mode = "disabled"
        embedding_service_managed = bool(payload.get("embedding_service_managed", False))
        embedding_service_model_id = str(payload.get("embedding_service_model_id") or "").strip()
        embedding_service_port = int(payload.get("embedding_service_port") or 0)
        embedding_service_device = str(payload.get("embedding_service_device") or "cpu").strip().lower()
        if embedding_service_device not in {"cpu", "cuda", "mps"}:
            embedding_service_device = "cpu"
        # CUDA wheel 安装配置（v1.3.12）：仅 device=cuda 时影响 pip 命令。
        # 非法值回落默认，避免脏 URL / 版本号流入 inline pip script。
        # 只允许 https:// —— pip 装 wheel 时 http 易被 MITM 替换为恶意 wheel
        # （供应链入口，v1.3.12 审计 P2）；如确需内网 http mirror，用户在自己
        # 改源码 + 接受风险，不开公开默认 path。
        embedding_service_pytorch_mirror = str(
            payload.get("embedding_service_pytorch_mirror")
            or "https://download.pytorch.org/whl/"
        ).strip()
        if not (
            embedding_service_pytorch_mirror.startswith("https://")
            and embedding_service_pytorch_mirror.endswith("/")
        ):
            embedding_service_pytorch_mirror = "https://download.pytorch.org/whl/"
        embedding_service_cuda_version = str(
            payload.get("embedding_service_cuda_version") or "cu124"
        ).strip().lower()
        if not re.fullmatch(r"cu\d{2,4}", embedding_service_cuda_version):
            embedding_service_cuda_version = "cu124"
        if ui_theme not in {"linear", "glass", "neo"}:
            ui_theme = "neo"
        if not api_base_url or not grafana_url:
            raise ValueError("api_base_url and grafana_url are required")
        now_str = self._dt_str(datetime.now(timezone.utc)) or datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO system_config (
                  id, api_base_url, service_port, grafana_url, ui_theme,
                  llm_enabled, llm_api_key, llm_base_url, llm_model,
                  llm_timeout_sec, llm_temperature, llm_max_tokens,
                  embedding_enabled, embedding_api_key, embedding_base_url, embedding_model, embedding_dim, embedding_timeout_sec,
                  rerank_enabled, rerank_api_key, rerank_base_url, rerank_model, rerank_path, rerank_timeout_sec,
                  enrichment_enabled,
                  embedding_service_mode, embedding_service_managed, embedding_service_model_id,
                  embedding_service_port, embedding_service_device,
                  embedding_service_pytorch_mirror, embedding_service_cuda_version, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                  embedding_service_mode=excluded.embedding_service_mode,
                  embedding_service_managed=excluded.embedding_service_managed,
                  embedding_service_model_id=excluded.embedding_service_model_id,
                  embedding_service_port=excluded.embedding_service_port,
                  embedding_service_device=excluded.embedding_service_device,
                  embedding_service_pytorch_mirror=excluded.embedding_service_pytorch_mirror,
                  embedding_service_cuda_version=excluded.embedding_service_cuda_version,
                  updated_at=excluded.updated_at
                """,
                (
                    api_base_url, service_port, grafana_url, ui_theme,
                    1 if llm_enabled else 0, llm_api_key, llm_base_url, llm_model,
                    llm_timeout_sec, llm_temperature, llm_max_tokens,
                    1 if embedding_enabled else 0, embedding_api_key, embedding_base_url, embedding_model, embedding_dim, embedding_timeout_sec,
                    1 if rerank_enabled else 0, rerank_api_key, rerank_base_url, rerank_model, rerank_path, rerank_timeout_sec,
                    1 if enrichment_enabled else 0,
                    embedding_service_mode, 1 if embedding_service_managed else 0, embedding_service_model_id,
                    embedding_service_port, embedding_service_device,
                    embedding_service_pytorch_mirror, embedding_service_cuda_version,
                    now_str,
                ),
            )
        return self.get_system_config()
