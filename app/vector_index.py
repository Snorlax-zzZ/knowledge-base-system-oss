from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from app.model_settings import EmbeddingConfig, embedding_config_from_env
from app.text_tokens import tokenize_for_retrieval

logger = logging.getLogger(__name__)


# DB embedding 字段 → env var 映射。空字段会显式 pop env，
# 防止旧值残留（例如切换 embedding 模型留空 base_url 时拿到旧 url）。
_EMBEDDING_OPTIONAL_ENV_FIELDS: dict[str, str] = {
    "embedding_api_key": "KB_EMBEDDING_API_KEY",
    "embedding_base_url": "KB_EMBEDDING_BASE_URL",
    "embedding_timeout_sec": "KB_EMBEDDING_TIMEOUT_SEC",
}


def _apply_local_infinity_to_env(model_key: str, port: int) -> int | None:
    """mode=local 时把 KB embedding env 强制指向本机 infinity（127.0.0.1:port）。

    返回 model 注册表里的 dim；未知 model_key 返回 None，调用方应当跳过 ApiEmbedding 激活。

    设计意图：mode=local 时 PUT /v1/system/config 锁了 embedding_base_url/model 字段（§2.10），
    用户填的远程豆包等配置不会被改写但也不应再被读取。把"用本地"这条切换收敛到 env 注入这一处，
    vector_index/EmbeddingProvider 不感知 mode，自然走 ApiEmbedding → POST http://127.0.0.1:port/embeddings。
    """
    # 延迟 import：embedding_install 不在 vector_index 启动期热路径，避免顶层导入连环开销
    from app.services.embedding_install import DEFAULT_EMBEDDING_PORT, MODEL_REGISTRY

    spec = MODEL_REGISTRY.get(model_key)
    if spec is None:
        # 未知 key：清掉 enabled，让上层退到 HashEmbedding 兜底（不裸跑错配 ApiEmbedding 调远程）
        os.environ["KB_EMBEDDING_ENABLED"] = "0"
        for env_key in _EMBEDDING_OPTIONAL_ENV_FIELDS.values():
            os.environ.pop(env_key, None)
        os.environ.pop("KB_EMBEDDING_MODEL", None)
        os.environ.pop("VECTOR_DIM", None)
        return None

    effective_port = port if port and port > 0 else DEFAULT_EMBEDDING_PORT
    # ApiEmbedding payload 的 model 字段 = infinity GET /models 暴露的 id；
    # infinity 启动参数 --model-id 传的是绝对路径 /Applications/KnowledgeBase/models/{key}，
    # /models 返回的 id 取路径末两段，所以这里固定写 "models/{key}"。
    os.environ["KB_EMBEDDING_ENABLED"] = "1"
    os.environ["KB_EMBEDDING_API_KEY"] = "local-infinity"
    os.environ["KB_EMBEDDING_BASE_URL"] = f"http://127.0.0.1:{effective_port}"
    os.environ["KB_EMBEDDING_MODEL"] = f"models/{model_key}"
    os.environ.pop("KB_EMBEDDING_TIMEOUT_SEC", None)  # 用默认 20s
    os.environ["VECTOR_DIM"] = str(spec.dim)
    return spec.dim


def _apply_db_embedding_to_env(db_cfg: dict) -> int | None:
    """把 DB embedding 配置注入 os.environ，空字段显式 pop 旧值。

    路由：
    - ``embedding_service_mode == "local"``：忽略 embedding_* 远程字段，强制指向本机 infinity
    - 其他（external / disabled）：沿用 embedding_enabled + embedding_model + embedding_base_url 远程配置

    返回 DB / 注册表指定的 dim（如有），便于覆盖外层默认值；无则返回 None。
    """
    mode = str(db_cfg.get("embedding_service_mode") or "disabled").lower()
    if mode == "local":
        model_key = str(db_cfg.get("embedding_service_model_id") or "").strip()
        port = int(db_cfg.get("embedding_service_port") or 0)
        return _apply_local_infinity_to_env(model_key, port)

    os.environ["KB_EMBEDDING_ENABLED"] = "1"
    os.environ["KB_EMBEDDING_MODEL"] = str(db_cfg["embedding_model"])

    for db_key, env_key in _EMBEDDING_OPTIONAL_ENV_FIELDS.items():
        val = db_cfg.get(db_key)
        if val:
            os.environ[env_key] = str(val)
        else:
            os.environ.pop(env_key, None)

    raw_dim = db_cfg.get("embedding_dim")
    if raw_dim:
        dim_int = int(raw_dim)
        os.environ["VECTOR_DIM"] = str(dim_int)
        return dim_int
    os.environ.pop("VECTOR_DIM", None)
    return None


@dataclass(frozen=True)
class _ResolvedRuntime:
    """派生自 DB config + env 的 embedding runtime 快照。

    只覆盖 VectorIndex 生命周期中允许原地热刷新的字段（enabled / embedding /
    fallback / dim）；qdrant_url / qdrant_local_path / collection_name 属于
    进程级绑定，不在热刷新范围内——这些改了必须重建 client（现阶段不支持）。
    """

    enabled: bool
    embedding: "EmbeddingProvider"
    fallback: "EmbeddingProvider"
    dim: int


def _resolve_runtime_from_db_cfg(
    db_cfg: dict | None, *, default_dim: int
) -> _ResolvedRuntime:
    """从 DB config + env 派生 embedding runtime；供 __init__ / from_env / reload 复用。

    副作用：写 os.environ（复用 _apply_db_embedding_to_env 的 env 桥接机制）。
    幂等：相同 db_cfg 多次调用结果一致；db_cfg 为空 / 未激活时回退纯 env 读取。
    """
    dim = default_dim
    if db_cfg:
        mode = str(db_cfg.get("embedding_service_mode") or "disabled").lower()
        local_ready = mode == "local" and bool(db_cfg.get("embedding_service_model_id"))
        remote_ready = bool(
            db_cfg.get("embedding_enabled") and db_cfg.get("embedding_model")
        )
        if local_ready or remote_ready:
            db_dim = _apply_db_embedding_to_env(db_cfg)
            if db_dim is not None:
                dim = db_dim

    enabled = os.getenv("VECTOR_ENABLED", "1") not in ("0", "false", "False")
    emb_cfg = embedding_config_from_env(default_dim=dim)
    if emb_cfg.active:
        embedding: EmbeddingProvider = ApiEmbedding(emb_cfg)
        fallback: EmbeddingProvider = HashEmbedding(dim=emb_cfg.dim)
        dim = emb_cfg.dim
    else:
        embedding = HashEmbedding(dim=dim)
        fallback = HashEmbedding(dim=dim)
    return _ResolvedRuntime(enabled=enabled, embedding=embedding, fallback=fallback, dim=dim)


class HashEmbedding:
    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize_for_retrieval(text)
        if not tokens:
            return vec

        for tok in tokens:
            idx = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.dim
            vec[idx] += 1.0

        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0:
            return vec
        return [x / norm for x in vec]


@dataclass
class VectorSearchHit:
    chunk_id: str
    score: float


class EmbeddingProvider:
    dim: int

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class ApiEmbedding(EmbeddingProvider):
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self.dim = config.dim

    def embed(self, text: str) -> list[float]:
        payload = {"model": self.config.model, "input": [text]}
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.config.timeout_sec) as client:
            resp = client.post(f"{self.config.base_url}/embeddings", headers=headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise ValueError("invalid embedding response: missing data")
        embedding = data[0].get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("invalid embedding response: missing vector")
        vec = [float(v) for v in embedding]
        self.dim = len(vec)
        return vec


def _collection_vector_size(info: Any) -> int | None:
    params = getattr(getattr(info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    if vectors is None:
        return None
    size = getattr(vectors, "size", None)
    if isinstance(size, int):
        return size
    if isinstance(vectors, dict):
        # Named vectors layout.
        for cfg in vectors.values():
            if isinstance(cfg, dict) and isinstance(cfg.get("size"), int):
                return int(cfg["size"])
            val = getattr(cfg, "size", None)
            if isinstance(val, int):
                return val
    return None


class VectorIndex:
    def __init__(
        self,
        *,
        enabled: bool,
        qdrant_url: str,
        collection_name: str,
        dim: int = 384,
        qdrant_local_path: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.qdrant_url = qdrant_url
        self.qdrant_local_path = qdrant_local_path
        self.collection_name = collection_name
        self.embedding: EmbeddingProvider = HashEmbedding(dim=dim)
        self._fallback_embedding = HashEmbedding(dim=dim)
        self._client = None
        # backup import 期间会 pause（审计 #6）：close client 后挡住 search/ask
        # 的懒重连，防止拿到 stale handle 或与 cp qdrant_local 并发；resume 后
        # 下一次 _ensure 重建。pause/resume 在 maintenance 流程串行调用。
        self._paused = False
        self._lock = threading.RLock()
        # reload_from_repo 检测到 dim 变化时 True；_ensure_client_and_collection 会重
        # 检 collection size 决定是否 recreate。默认 False 让 upsert/search 热路径直接
        # 走 client is not None 的 fast return。
        self._schema_refresh_needed = False
        # 记录 _ensure_client_and_collection 最近一次失败（如 QdrantClient(path=)
        # 撞 stale lock、recreate_collection 抛 io 错误等），供诊断端点回读。仅调试
        # 用，非稳态属性——成功建 client 后清空。
        self._last_init_error: str | None = None

        emb_cfg = embedding_config_from_env(default_dim=dim)
        if emb_cfg.active:
            self.embedding = ApiEmbedding(emb_cfg)
            self._fallback_embedding = HashEmbedding(dim=emb_cfg.dim)
        else:
            logger.warning(
                "KB_EMBEDDING_ENABLED=0 或 embedding 配置未激活：使用 HashEmbedding 兜底。"
                "该模式基于词 hash 词袋，不具备语义同义召回能力，向量检索质量有限。"
                "生产环境建议设置 KB_EMBEDDING_ENABLED=1 并配置外部 embedding 模型。"
            )

        if self.enabled:
            self._ensure_client_and_collection()

    @classmethod
    def from_env(cls, db_cfg: dict | None = None) -> "VectorIndex":
        """从环境变量构建 VectorIndex，db_cfg 中的 embedding 配置优先于环境变量。"""
        enabled = os.getenv("VECTOR_ENABLED", "1") not in ("0", "false", "False")
        qdrant_mode = os.getenv("QDRANT_MODE", "server").lower()
        qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
        qdrant_local_path = os.getenv("QDRANT_LOCAL_PATH", "./data/qdrant_local") if qdrant_mode == "local" else None
        collection_name = os.getenv("QDRANT_COLLECTION", "knowledge_chunks")
        dim = int(os.getenv("VECTOR_DIM", "384"))

        # DB 配置优先于 env：embedding 字段写入 os.environ 供 embedding_config_from_env 读取。
        # 空字段显式 os.environ.pop()，防止历史旧值残留（例如切换 embedding 模型时
        # 留空 base_url，旧 base_url 残留 env 会导致请求发到错误服务器）。
        #
        # 路由分支（与 _apply_db_embedding_to_env 内部分支保持一致）：
        # - mode=local：忽略 embedding_enabled/model 远程字段，按 embedding_service_model_id 走本地 infinity
        # - 其他：沿用 embedding_enabled + embedding_model（external 或用户手配远程 API）
        mode = str((db_cfg or {}).get("embedding_service_mode") or "disabled").lower()
        local_ready = mode == "local" and bool((db_cfg or {}).get("embedding_service_model_id"))
        remote_ready = bool(
            db_cfg
            and db_cfg.get("embedding_enabled")
            and db_cfg.get("embedding_model")
        )
        if db_cfg and (local_ready or remote_ready):
            db_dim = _apply_db_embedding_to_env(db_cfg)
            if db_dim is not None:
                dim = db_dim

        return cls(
            enabled=enabled,
            qdrant_url=qdrant_url,
            collection_name=collection_name,
            dim=dim,
            qdrant_local_path=qdrant_local_path,
        )

    @classmethod
    def from_repo(cls, repo: Any) -> "VectorIndex":
        """从 repo 的 system_config 构建 VectorIndex。

        封装"读 DB 配置 + from_env(db_cfg=)"流程，供 FastAPI / MCP 入口复用，
        避免调用方各自直接调 from_env() 漏传 db_cfg 导致 /settings 配置不生效。
        """
        try:
            db_cfg = repo.get_system_config() or {}
        except Exception:
            logger.warning("读 DB system_config 失败，回退 env 默认值", exc_info=True)
            db_cfg = {}
        return cls.from_env(db_cfg=db_cfg)

    def pause(self) -> None:
        """暂停向量索引：close 当前 client，禁止懒重连（审计 #6）。

        **只给 maintenance 用**（backup import / recover / 独占重建索引这类需要"关文件
        句柄再重开"的窗口），**禁止** 塞进 config hot reload 路径——原地热刷新走
        ``reload_from_repo``。以前 ``_invalidate_repo_singletons`` 里 clear + pause
        导致的 portalocker AlreadyLocked 竞态就是这个约束被违反造成的，别再犯（方案 E）。
        """
        with self._lock:
            self._paused = True
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning("vector_index.pause: qdrant client close failed", exc_info=True)

    def resume(self) -> None:
        """恢复向量索引：clear paused + 主动 eager reinit client。

        **只给 maintenance 用**——同 ``pause`` 的约束。config hot reload 不要走这里。

        2026-07-03 P0 修：resume 后主动 ``_ensure_client_and_collection()``，
        保证 client 立即就位。之前只 clear paused + _client=None，依赖下一次
        search/write 触发 lazy _ensure，撞非 search 路径（如 rebuild-vector-index
        `scripts/rebuild_vector_index.py:84` 直接读 ``vector_index._client`` 属性
        不 trigger lazy init）读到 None → "Qdrant 客户端不可用，无法重建"。
        实测 X2.5 clear+import roundtrip 后立刻 rebuild 100% 复现。
        eager _ensure 失败不 raise（保持向后兼容 lazy 语义）：只 warn，下一次
        search 仍会重试触发。
        """
        with self._lock:
            self._paused = False
            self._client = None  # 强制 _ensure 重建
        try:
            self._ensure_client_and_collection()
        except Exception:
            logger.warning(
                "vector_index.resume: eager _ensure_client_and_collection failed, "
                "will retry on next search/write call", exc_info=True,
            )

    def reload_from_repo(self, repo: Any, *, allow_schema_refresh: bool) -> None:
        """原地热刷新 embedding runtime，**不 close 也不重建** QdrantClient。

        - config hot reload 路径唯一入口（``_refresh_live_repo_vector_indexes`` 里调）。
        - 保持进程内 QdrantClient(path=...) 单实例持有者语义，从根上杜绝 portalocker
          AlreadyLocked 竞态。
        - ``allow_schema_refresh=True``：dim 变了就 mark schema refresh，随后
          ``_ensure_client_and_collection`` 重检 collection size 并 recreate。
        - ``allow_schema_refresh=False``：dim 变了也不动 collection——留给用户显式 rebuild
          路径决定（如 mode / model_id 切换必须走 confirm_reindex）。
        """
        try:
            db_cfg = repo.get_system_config() or {}
        except Exception:
            logger.warning("reload_from_repo 读 DB config 失败，跳过热刷新", exc_info=True)
            return
        resolved = _resolve_runtime_from_db_cfg(db_cfg, default_dim=int(self.embedding.dim))
        with self._lock:
            old_dim = int(self.embedding.dim)
            self.enabled = resolved.enabled
            self.embedding = resolved.embedding
            self._fallback_embedding = resolved.fallback
            if allow_schema_refresh and old_dim != int(resolved.embedding.dim):
                self._schema_refresh_needed = True
        if allow_schema_refresh and self.enabled:
            # client 已在时 _ensure 走"复用 client + 重检 collection"分支
            self._ensure_client_and_collection()

    def _ensure_client_and_collection(self) -> None:
        # 快照决策：paused 直接不动；client 已在且无 schema refresh 请求 fast return。
        # client 已在但 _schema_refresh_needed=True 时（reload_from_repo dim 变），进入
        # 下面的"复用 client + 重检 collection size"分支，不 close 也不重建 client。
        with self._lock:
            if self._paused:
                return
            need_schema_check = self._schema_refresh_needed
            if self._client is not None and not need_schema_check:
                return

        try:
            from qdrant_client import QdrantClient, models
        except Exception as _imp_exc:
            import traceback as _tb
            logger.warning("qdrant_client 导入失败，向量检索禁用", exc_info=True)
            self.enabled = False
            with self._lock:
                self._last_init_error = (
                    f"qdrant_client import failed: {type(_imp_exc).__name__}: {_imp_exc}\n"
                    + _tb.format_exc()
                )
            return

        try:
            if self._client is None:
                if self.qdrant_local_path is not None:
                    import os as _os
                    _os.makedirs(self.qdrant_local_path, exist_ok=True)
                    self._client = QdrantClient(path=self.qdrant_local_path)
                else:
                    self._client = QdrantClient(url=self.qdrant_url)
            collections = self._client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            if not exists:
                self._client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(size=self.embedding.dim, distance=models.Distance.COSINE),
                )
            else:
                # Ensure local index matches configured vector size. Reset collection only when dimension mismatches.
                info = self._client.get_collection(collection_name=self.collection_name)
                actual_size = _collection_vector_size(info)
                if actual_size is not None and actual_size != int(self.embedding.dim):
                    self._client.recreate_collection(
                        collection_name=self.collection_name,
                        vectors_config=models.VectorParams(
                            size=self.embedding.dim,
                            distance=models.Distance.COSINE,
                        ),
                    )
            # schema 校验完成，清标志（下次 upsert/search 走 fast return）
            with self._lock:
                self._schema_refresh_needed = False
                self._last_init_error = None
        except Exception as _init_exc:
            import traceback as _tb
            logger.warning("Qdrant 连接/初始化失败 url=%s，降级为关键词检索", self.qdrant_url, exc_info=True)
            self._client = None
            self.enabled = False
            with self._lock:
                self._last_init_error = (
                    f"{type(_init_exc).__name__}: {_init_exc}\n"
                    + _tb.format_exc()
                )

    def _embed_with_fallback(self, text: str) -> list[float]:
        try:
            return self.embedding.embed(text)
        except Exception:
            logger.warning("嵌入 API 失败，降级为 HashEmbedding", exc_info=True)
            if not isinstance(self.embedding, HashEmbedding):
                self.embedding = self._fallback_embedding
                try:
                    self._ensure_client_and_collection()
                except Exception:
                    logger.debug("降级后重新初始化 Qdrant 客户端失败", exc_info=True)
            return self.embedding.embed(text)

    def upsert_chunks(self, chunks: list[dict[str, Any]], *, max_retries: int = 3) -> None:
        # 半刷新防御：锁内抓 enabled 快照，避免读到 reload 中途的半状态
        with self._lock:
            enabled = self.enabled
        if not enabled or not chunks:
            return
        self._ensure_client_and_collection()
        with self._lock:
            client = self._client
        if client is None:
            logger.warning("Qdrant 客户端不可用，跳过向量写入 chunk_count=%d", len(chunks))
            return

        from qdrant_client import models

        points: list[models.PointStruct] = []
        for row in chunks:
            vector = self._embed_with_fallback(row["text"])
            payload = {
                "knowledge_item_id": row["knowledge_item_id"],
                "domain": row["domain"],
                "project": row["project"],
                "version": row["version"],
                "title": row["title"],
                "chunk_index": row["chunk_index"],
            }
            points.append(models.PointStruct(id=row["chunk_id"], vector=vector, payload=payload))

        # 有限重试：短时网络抖动可自愈
        for attempt in range(1, max_retries + 1):
            try:
                client.upsert(collection_name=self.collection_name, points=points)
                if attempt > 1:
                    logger.info("向量写入重试成功 attempt=%d/%d", attempt, max_retries)
                return
            except Exception:
                if attempt < max_retries:
                    logger.warning("向量写入失败，重试中 attempt=%d/%d", attempt, max_retries, exc_info=True)
                else:
                    logger.error(
                        "向量写入最终失败 VECTOR_SYNC_FAILED chunk_count=%d item_id=%s，数据已落库但向量索引缺失",
                        len(chunks),
                        chunks[0].get("knowledge_item_id", "?"),
                    )
                    raise

    def delete_item_vectors(
        self,
        knowledge_item_id: str,
        exclude_chunk_ids: list[str] | None = None,
    ) -> None:
        """清理 knowledge_item 的旧版本向量。

        exclude_chunk_ids: 新写入的 chunk id 列表，排除在外以免误删当前版本向量。
        """
        # 半刷新防御：锁内抓 enabled + client 快照
        with self._lock:
            enabled = self.enabled
            client = self._client
        if not enabled or client is None:
            return
        try:
            from qdrant_client import models
            must = [
                models.FieldCondition(
                    key="knowledge_item_id",
                    match=models.MatchValue(value=knowledge_item_id),
                )
            ]
            if exclude_chunk_ids:
                filter_ = models.Filter(
                    must=must,
                    must_not=[models.HasIdCondition(has_id=exclude_chunk_ids)],
                )
            else:
                filter_ = models.Filter(must=must)
            client.delete(
                collection_name=self.collection_name,
                points_selector=models.FilterSelector(filter=filter_),
            )
            logger.info("已清理旧版本向量 item_id=%s", knowledge_item_id)
        except Exception:
            logger.warning("清理旧版本向量失败 item_id=%s", knowledge_item_id, exc_info=True)

    def search(self, *, query: str, domain: str, project: str | None, top_k: int) -> list[VectorSearchHit]:
        # 半刷新防御：锁内抓 enabled 快照
        with self._lock:
            enabled = self.enabled
        if not enabled:
            return []
        self._ensure_client_and_collection()
        with self._lock:
            client = self._client
        if client is None:
            return []

        from qdrant_client import models

        must = [models.FieldCondition(key="domain", match=models.MatchValue(value=domain))]
        if project:
            must.append(models.FieldCondition(key="project", match=models.MatchValue(value=project)))

        query_vector = self._embed_with_fallback(query)
        # qdrant-client 新版本移除了 search()，统一使用 query_points()
        response = client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=models.Filter(must=must),
            limit=top_k,
            with_payload=False,
        )
        points = response.points
        return [VectorSearchHit(chunk_id=str(p.id), score=float(p.score)) for p in points]
