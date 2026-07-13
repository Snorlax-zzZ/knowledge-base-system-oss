from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Any, Protocol

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware

from app.metrics import metrics_middleware, metrics_response
from app.mcp_tools import KnowledgeMcpTools
from app.services.backup_service import (
    AutoBackupService,
    BackupImportError,
    BackupService,
)
from app.services.confirm_token import ConfirmTokenError, require_confirm_token
from app.services.disk_space import InsufficientDiskSpaceError
from app.services.embedding_install import (
    DEFAULT_MODEL_KEY,
    MODEL_REGISTRY,
    REINDEX_MAINTENANCE_THRESHOLD,
    build_install_plan,
    resolve_model,
)
from app.services.embedding_service_state import (
    GenerationConflict,
    OwnerTokenMismatch,
    get_embedding_service_state,
    write_owner_token_file,
)
from app.services.install_progress import (
    InstallSseStreamer,
    resolve_install_paths,
)
from app.services.rebuild_runner import (
    EmbeddingNotReady,
    RebuildAlreadyRunning,
    get_rebuild_runner,
)
from app.services.maintenance import (
    MaintenanceReason,
    get_maintenance_flag,
)
from app.services.manifest import ManifestParseError
from app.services.origin_guard import should_block_request
from app.services.pre_restore_recover import (
    detect_and_warn as _pre_restore_detect_and_warn,
    execute_recover as _pre_restore_execute_recover,
)
from app.schemas import (
    AskRequest,
    AskResponse,
    CleanupExpiredKnowledgeRequest,
    ClearKnowledgeBaseRequest,
    EmbeddingModelOption,
    EmbeddingModelsResponse,
    ReindexPreviewResponse,
    EmbeddingServiceActualStateRequest,
    EmbeddingServiceActualStateResponse,
    EmbeddingServiceDesiredStateResponse,
    EmbeddingServiceInstallPlanResponse,
    EmbeddingServiceInstallRequest,
    EmbeddingServiceStartStopRequest,
    EmbeddingServiceStatusResponse,
    EmbeddingServiceSwitchModelRequest,
    EmbeddingServiceSwitchModelResponse,
    RebuildVectorIndexRequest,
    RebuildVectorIndexResponse,
    RebuildVectorIndexStatusResponse,
    ExportKnowledgePackageRequest,
    HealthResponse,
    ImportIncrementalRequest,
    ImportKnowledgePackageRequest,
    KnowledgeItemResponse,
    SearchRequest,
    SearchResponse,
    SystemConfigResponse,
    SystemConfigUpsertRequest,
    UpsertRequest,
    UpsertResponse,
)
from app.service import KnowledgeService
from app.vector_index import VectorIndex


logger = logging.getLogger(__name__)

# 配置 mode 切换与 install/start/stop desired-state 写入必须串行化：否则旧的
# local 启动请求可能在用户已切 external/disabled 后反盖 stop。
_embedding_control_lock = threading.RLock()


def _load_app_version() -> str:
    # 优先读运行目录的 VERSION（直装版打包时写入 /Applications/KnowledgeBase/VERSION）
    candidates = [
        FilePath(os.environ.get("KB_APP_ROOT", "")) / "VERSION" if os.environ.get("KB_APP_ROOT") else None,
        FilePath(__file__).resolve().parent.parent / "VERSION",
        FilePath.cwd() / "VERSION",
    ]
    for p in candidates:
        if p is None:
            continue
        try:
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return v
        except OSError:
            continue
    return os.environ.get("KB_APP_VERSION", "dev")


APP_VERSION = _load_app_version()


class KnowledgeRepo(Protocol):
    def upsert_item(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def delete_item(self, item_id: str) -> bool: ...

    def get_item(self, item_id: str, actor: str | None = None) -> dict[str, Any] | None: ...

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
    ) -> list[dict[str, Any]]: ...

    def search_chunks_for_ask(
        self,
        query: str,
        domain: str,
        project: str | None,
        top_k: int,
        actor: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_system_config(self) -> dict[str, Any]: ...

    def has_system_config(self) -> bool: ...

    def upsert_system_config(self, payload: dict[str, Any]) -> dict[str, Any]: ...


app = FastAPI(
    title="百变怪芝士包 API",
    version=APP_VERSION,
    description="本地知识库服务 API。支持知识写入、检索、按 ID 查询，以及监控健康检查。",
)
# 仅放行本机环回（127.0.0.1 / localhost），从浏览器侧阻断 CSRF：
# 直装版只在本机访问，外部站点不应能通过浏览器把 cookie/请求带到本服务。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.middleware("http")(metrics_middleware)


# 只读路由路径前缀白名单（maintenance 期间放行）
_MAINTENANCE_READ_ONLY_POST_PATHS = frozenset({
    "/v1/knowledge/search",
    "/v1/knowledge/ask",
    # actual-state 是壳层 → kb-api 的心跳回写，maintenance 期间也必须放行，
    # 否则 reindex 时拿不到 infinity 实况（AC25 控制面与业务面解耦）。
    "/v1/system/embedding-service/actual-state",
})


def _is_read_only_request(path: str, method: str) -> bool:
    """判断请求是否属于 maintenance 期间放行的只读访问。"""
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if method == "POST" and path in _MAINTENANCE_READ_ONLY_POST_PATHS:
        return True
    # recover 端点是解除 maintenance 的入口，必须放行
    if path.startswith("/v1/system/recover/"):
        return True
    return False


class MaintenanceMiddleware(BaseHTTPMiddleware):
    """maintenance flag 置位时拦截写类请求。

    - 默认（backup_import / pre_restore_stale）→ 503 + Retry-After: 60
    - REINDEX（AC10 阈值放行场景）→ **202 + Retry-After: 30**：告诉客户端"请稍后
      重试"，而非"服务挂了"；前端可显示 "重建中（约 X 分钟），将自动恢复"
    """

    async def dispatch(self, request, call_next):
        flag = get_maintenance_flag()
        if flag.is_active() and not _is_read_only_request(request.url.path, request.method):
            reason = flag.reason()
            if reason == MaintenanceReason.REINDEX:
                return JSONResponse(
                    status_code=202,
                    headers={"Retry-After": "30"},
                    content={
                        "detail": "vector index rebuild in progress; please retry",
                        "reason": reason.value,
                        "info": flag.detail(),
                    },
                )
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": "60"},
                content={
                    "detail": "service in maintenance: backup/restore in progress",
                    "reason": reason.value if reason else None,
                    "info": flag.detail(),
                },
            )
        return await call_next(request)


class OriginGuardMiddleware(BaseHTTPMiddleware):
    """CSRF 深度防御（审计 #1）：写类请求若带非环回 Origin/Referer 直接 403。

    与 CORS 互补：CORS 只挡浏览器读响应，不阻止浏览器发送 multipart simple
    request；本中间件在请求进入路由前直接拦截，破坏请求无法到达业务逻辑。
    """

    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if should_block_request(request.method, origin, referer):
            logger.warning(
                "origin guard blocked request: method=%s path=%s origin=%r referer=%r",
                request.method,
                request.url.path,
                origin,
                referer,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "request blocked by origin guard: "
                    "writes must originate from 127.0.0.1 / localhost or be made "
                    "without a browser origin (e.g. curl / server-to-server)"
                },
            )
        return await call_next(request)


class WarmingUpMiddleware(BaseHTTPMiddleware):
    """infinity warming（模型加载中）时，语义检索 API 返 202 + Retry-After（AC19）。

    壳层拉起 infinity 后模型 warmup 5-30s。期间 ``actual.warming_up=True``，
    语义查询请求不能 500（infinity 还没就绪），返 202 让前端 retry 而不是报错。
    其他业务（关键词检索、文档管理、配置）不受影响——AC24 分级就绪原则。
    """

    _SEMANTIC_PATHS = frozenset({"/v1/knowledge/search", "/v1/knowledge/ask"})

    async def dispatch(self, request, call_next):
        if (
            request.method == "POST"
            and request.url.path in self._SEMANTIC_PATHS
        ):
            actual = get_embedding_service_state().actual()
            if actual.warming_up:
                return JSONResponse(
                    status_code=202,
                    headers={"Retry-After": "5"},
                    content={
                        "detail": "embedding service warming up; please retry",
                        "model_id": actual.model_id,
                    },
                )
        return await call_next(request)


app.add_middleware(MaintenanceMiddleware)
app.add_middleware(WarmingUpMiddleware)
app.add_middleware(OriginGuardMiddleware)


@app.on_event("startup")
def _detect_pre_restore_on_startup() -> None:
    """启动钩子（审计 #7）：探测 .pre-restore 残留。

    sqlite 之外的 backend 不参与本机文件路径检测。
    """
    backend_raw = os.getenv("KB_BACKEND", "").strip().lower()
    if backend_raw != "sqlite":
        return
    sqlite_path = os.getenv("SQLITE_PATH", "data/knowledge.db")
    qdrant_local_path = os.getenv("QDRANT_LOCAL_PATH", "data/qdrant_local")
    try:
        _pre_restore_detect_and_warn(sqlite_path, qdrant_local_path)
    except Exception:
        logger.warning("pre-restore detect failed", exc_info=True)


@app.on_event("startup")
def _persist_owner_token_on_startup() -> None:
    """启动钩子（design v1.2 §3.2 + AC25）：owner_token 落盘供壳层读取。

    本进程内 ``EmbeddingServiceState`` 已在构造时随机生成 ``owner_token``。
    壳层（mac-app / windows-app ProcessManager）启动后 reconcile loop 第一
    步要调 ``POST /v1/system/embedding-service/actual-state`` 回写，必须在
    ``X-Embedding-Owner-Token`` header 携带；token 不符 → 401。

    落盘失败不阻塞 kb-api 启动（只 warn）：开发者本机 / 单测场景没壳层，
    没文件也跑得起来；壳层启动时找不到文件会自己 retry。
    """
    # uvicorn 的 lifespan startup 可能早于真实 socket bind。若另一个 kb-api
    # 已经在目标端口监听，当前短命 loser 随后会 bind 失败；此时绝不能先把
    # winner 的磁盘 token 覆盖。这里只做短 TCP connect，不做 bind-and-close，
    # 避免探针自己短暂占用端口。
    port_raw = os.getenv("KB_PORT_API", "18000").strip()
    try:
        port = int(port_raw)
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            logger.warning(
                "kb-api port %d already has a listener; skip owner_token write "
                "to avoid clobbering the running instance",
                port,
            )
            return
    except (OSError, ValueError, OverflowError):
        # Connection refused/timeout means no confirmed listener. Invalid port
        # will be rejected by the actual server startup; keep legacy best-effort
        # token persistence semantics here.
        pass

    state = get_embedding_service_state()
    data_root = _resolve_data_root()
    try:
        target = write_owner_token_file(data_root, state.owner_token)
        logger.info("embedding owner_token persisted to %s", target)
    except Exception:
        logger.warning(
            "failed to persist embedding owner_token to %s/runtime/owner_token; "
            "shell ProcessManager actual-state writes will be rejected until fixed",
            data_root,
            exc_info=True,
        )

APP_DIR = FilePath(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
CONSOLE_INDEX = STATIC_DIR / "console" / "index.html"
SETTINGS_INDEX = STATIC_DIR / "settings" / "index.html"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# repo 单例池：从 lru_cache 改成手写 dict + Lock，让 invalidate 能拿到旧实例
# 显式 pause 旧 VectorIndex 释放 qdrant_local portalocker 锁。lru_cache 只能 cache_clear
# 不暴露旧值，导致 mode 切换后旧 client 不 close、新 client 在同进程内 AlreadyLocked，
# 新 VectorIndex 静默退化到 enabled=False。
_repo_singletons: dict[tuple[str, str], Any] = {}
_repo_singletons_lock = threading.Lock()


def _repo_singleton_postgres(database_url: str) -> Any:
    key = ("postgres", database_url)
    with _repo_singletons_lock:
        existing = _repo_singletons.get(key)
        if existing is not None:
            return existing
        from app.repository_postgres import PostgresKnowledgeRepo
        # 一段式 init：先 repo(vector_index=None) → 读 DB 配置 → 一次 from_env(db_cfg=)，
        # 避免两段式重复创建 VectorIndex / Qdrant client 导致资源浪费与潜在锁冲突。
        repo = PostgresKnowledgeRepo(database_url=database_url, vector_index=None)
        repo.vector_index = VectorIndex.from_repo(repo)
        _repo_singletons[key] = repo
        return repo


def _repo_singleton_sqlite(sqlite_path: str) -> Any:
    key = ("sqlite", sqlite_path)
    with _repo_singletons_lock:
        existing = _repo_singletons.get(key)
        if existing is not None:
            return existing
        from app.repository_sqlite import SqliteKnowledgeRepo
        repo = SqliteKnowledgeRepo(sqlite_path=sqlite_path, vector_index=None)
        repo.vector_index = VectorIndex.from_repo(repo)
        _repo_singletons[key] = repo
        return repo


def _refresh_live_repo_vector_indexes(*, allow_schema_refresh: bool) -> None:
    """config hot reload 路径：原地热刷新 live VectorIndex，**不 clear repo dict、不 pause、
    不 close client**——保持进程内 QdrantClient(path=...) 单实例语义。

    方案 E：以前 `_invalidate_repo_singletons` 里 clear + pause 会释放旧 vector_index
    的 portalocker 锁，然后 get_repo 立刻新建 QdrantClient(path=...) 撞 AlreadyLocked。
    改成原地热刷新 embedding runtime 就绕开这个竞态。

    - ``allow_schema_refresh=True``：dim 变了会 recreate_collection（config sync 用）
    - ``allow_schema_refresh=False``：dim 变了也不动 collection（等用户显式 rebuild）
    """
    with _repo_singletons_lock:
        repos = list(_repo_singletons.values())
    for repo in repos:
        vi = getattr(repo, "vector_index", None)
        if vi is None or not hasattr(vi, "reload_from_repo"):
            continue
        try:
            vi.reload_from_repo(repo, allow_schema_refresh=allow_schema_refresh)
        except Exception:
            logger.warning("live vector_index reload 失败", exc_info=True)


def _drop_repo_singletons_for_tests() -> None:
    """硬失效：clear repo dict + pause 旧 vector_index。**仅测试 / 极少数硬失效场景用**。

    生产环境 config hot reload 走 `_refresh_live_repo_vector_indexes`——它保证
    QdrantClient(path=...) 单实例语义，绕开 portalocker 竞态。测试 fixture 用
    lru_cache 时代留下来的 `cache_clear` 别名走这里，语义与老 `_invalidate_repo_singletons`
    一致（clear + pause），继续兼容既有测试。
    """
    with _repo_singletons_lock:
        old = list(_repo_singletons.values())
        _repo_singletons.clear()
    for repo in old:
        vi = getattr(repo, "vector_index", None)
        if vi is None:
            continue
        try:
            vi.pause()  # close client + 阻止懒重连；新 repo 会拿到新的 VectorIndex
        except Exception:
            logger.warning("drop_repo_singletons: vector_index.pause failed", exc_info=True)


# 测试 fixture 历史上用 ``_repo_singleton_sqlite.cache_clear()`` 复位单例（lru_cache 时代）。
# 切到 dict + Lock 之后保留同名属性；语义等同 `_drop_repo_singletons_for_tests`——
# 清池 + pause 旧 vector_index 释放 qdrant 锁。
_repo_singleton_sqlite.cache_clear = _drop_repo_singletons_for_tests  # type: ignore[attr-defined]
_repo_singleton_postgres.cache_clear = _drop_repo_singletons_for_tests  # type: ignore[attr-defined]


def _resolve_backend() -> str:
    backend_raw = os.getenv("KB_BACKEND", "").strip().lower()
    if not backend_raw:
        raise HTTPException(
            status_code=500,
            detail="KB_BACKEND is not configured; set KB_BACKEND=sqlite or KB_BACKEND=postgres explicitly",
        )
    return backend_raw


def get_repo() -> KnowledgeRepo:
    backend = _resolve_backend()

    if backend == "postgres":
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")
        return _repo_singleton_postgres(db_url)

    if backend == "sqlite":
        sqlite_path = os.getenv("SQLITE_PATH", "./data/knowledge.db")
        return _repo_singleton_sqlite(sqlite_path)

    raise HTTPException(
        status_code=500,
        detail=f"不支持的 KB_BACKEND: {backend}，可选值: sqlite, postgres",
    )


@app.get("/health", response_model=HealthResponse, summary="健康检查", description="用于判断 API 服务是否可用。")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/system/version", summary="读取产品版本号")
def system_version() -> dict[str, str]:
    return {"version": APP_VERSION}


@app.get("/metrics")
def metrics():
    return metrics_response()


@app.get("/console", include_in_schema=False)
def console_page():
    if not CONSOLE_INDEX.exists():
        raise HTTPException(status_code=404, detail="console frontend not found")
    return FileResponse(
        CONSOLE_INDEX,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/settings", include_in_schema=False)
def settings_page():
    if not SETTINGS_INDEX.exists():
        raise HTTPException(status_code=404, detail="settings frontend not found")
    return FileResponse(
        SETTINGS_INDEX,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


SETUP_INDEX = STATIC_DIR / "setup" / "index.html"


@app.get("/setup", include_in_schema=False)
def setup_page():
    """首装引导页:三选一(本地 embedding / 外部服务 / 跳过) + 模型选择 + 安装进度。"""
    if not SETUP_INDEX.exists():
        raise HTTPException(status_code=404, detail="setup frontend not found")
    return FileResponse(
        SETUP_INDEX,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get(
    "/v1/system/reindex-preview",
    response_model=ReindexPreviewResponse,
    summary="reindex 触发前的 chunk 数 + 耗时预估(给 /settings 确认对话框)",
)
def get_reindex_preview(repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    """供 /settings reindex 按钮的确认对话框展示 AC22 必要信息。

    粗估算法:~100 chunk/s(实际取决于 embedding 模型 + 硬件,够给用户量级感受)。
    阈值放行:>= REINDEX_MAINTENANCE_THRESHOLD 才置 maintenance flag。
    """
    count = repo.count_active_chunks() if hasattr(repo, "count_active_chunks") else 0
    estimated = max(1, count // 100)
    return {
        "active_chunks": count,
        "threshold_blocked_writes": count >= REINDEX_MAINTENANCE_THRESHOLD,
        "estimated_seconds": estimated,
        "threshold": REINDEX_MAINTENANCE_THRESHOLD,
    }


@app.get(
    "/v1/system/embedding-models",
    response_model=EmbeddingModelsResponse,
    summary="列出可选内置 embedding 模型(给 /setup 模型选择网格用)",
)
def list_embedding_models() -> dict[str, Any]:
    """返回 MODEL_REGISTRY 全表 + 默认推荐 key。"""
    items: list[dict[str, Any]] = []
    for key, spec in MODEL_REGISTRY.items():
        items.append({
            "key": key,
            "model_id": spec.model_id,
            "display_name": spec.display_name,
            "dim": spec.dim,
            "size_bytes": spec.size_bytes,
            "ram_bytes": spec.ram_bytes,
            "multilingual": spec.multilingual,
            "recommended": (key == DEFAULT_MODEL_KEY),
        })
    return {"models": items, "default_key": DEFAULT_MODEL_KEY}


@app.post(
    "/v1/knowledge/search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    summary="检索知识",
    description="按关键词和过滤条件检索知识条目，返回命中列表、trace_id、knowledge_item_ids。",
)
def search_knowledge(req: SearchRequest, repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    return KnowledgeService(repo).search(req)


@app.get(
    "/v1/knowledge/items/{item_id}",
    response_model=KnowledgeItemResponse,
    summary="按 ID 获取知识",
    description="根据 knowledge_item_id 获取当前版本知识内容，按 actor 做 ACL 过滤。",
)
def get_knowledge_item(
    item_id: str = Path(..., description="知识条目 ID。"),
    actor: str = Query(default="manual", description="调用方身份，用于 ACL 权限过滤。"),
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    row = KnowledgeService(repo).get_item(item_id, actor=actor)
    if row is None:
        raise HTTPException(status_code=404, detail="knowledge item not found")
    return row


@app.post(
    "/v1/knowledge/items/upsert",
    response_model=UpsertResponse,
    summary="写入/更新知识",
    description="新建或更新知识条目。更新时会自动生成新版本。",
)
def upsert_knowledge(req: UpsertRequest, repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    try:
        return KnowledgeService(repo).upsert(req)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/v1/knowledge/import-file",
    response_model=UpsertResponse,
    summary="单文件导入知识",
    description=(
        "上传单个 .md / .markdown / .txt / .docx / .pdf 文件，"
        "服务端 in-process 解析并 upsert 入库。"
        "图片格式当前不支持（无 OCR）。文件类型不在白名单返回 415。"
    ),
)
async def import_file_knowledge(
    file: UploadFile = File(...),
    project: str = Form(...),
    domain: str = Form(...),
    knowledge_type: str = Form("fact"),
    actor: str = Form("manual"),
    title: str | None = Form(None),
    summary: str | None = Form(None),
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    import tempfile

    from pydantic import ValidationError

    from app.services.import_document import (
        EmptyDocumentError,
        ParseDependencyError,
        UnsupportedFileTypeError,
        parse_document,
    )

    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")

    suffix = FilePath(file.filename).suffix.lower()
    # 用原文件名后缀，让解析器按后缀识别格式
    tmp = tempfile.NamedTemporaryFile(
        prefix="kb-import-file-", suffix=suffix, delete=False
    )
    try:
        try:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        finally:
            tmp.close()

        try:
            payload = parse_document(
                FilePath(tmp.name),
                project=project,
                domain=domain,
                knowledge_type=knowledge_type,
                actor=actor,
                title=title,
                summary=summary,
                source_uri=f"file:///{file.filename}",
            )
        except UnsupportedFileTypeError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except EmptyDocumentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ParseDependencyError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        try:
            req = UpsertRequest(**payload)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.errors()) from exc

        try:
            return KnowledgeService(repo).upsert(req)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@app.delete(
    "/v1/console/knowledge/items/{item_id}",
    include_in_schema=False,
)
def console_delete_knowledge_item(
    item_id: str = Path(..., description="知识条目 ID。"),
    actor: str = Query(..., min_length=1, description="操作者标识，写入审计日志。"),
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    deleted = KnowledgeService(repo).delete_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="knowledge item not found")
    logger.info("knowledge_item deleted item_id=%s actor=%s", item_id, actor)
    return {"ok": True, "knowledge_item_id": item_id, "deleted": True}


@app.post(
    "/v1/knowledge/ask",
    response_model=AskResponse,
    summary="智能问答",
    description="基于 chunk 级混合检索 + LLM 生成回答。LLM 未配置时仍返回检索到的 chunks，answer 为 null。",
)
def ask_knowledge(req: AskRequest, repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    return KnowledgeService(repo).ask(req)


@app.get("/v1/system/vector-index-diagnostic", include_in_schema=False)
def get_vector_index_diagnostic(repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    """临时诊断：暴露当前 vector_index 实况，方便定位 rebuild 前的 client None 谜题。

    2026-07-01 方案 E 装机后仍撞 "Qdrant 客户端不可用"，需要看真机 vi 状态。
    """
    import os as _os
    import sys as _sys
    import importlib.util as _iu

    def _spec_origin(mod_name: str) -> dict[str, Any]:
        try:
            spec = _iu.find_spec(mod_name)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        if spec is None:
            return {"found": False}
        return {"found": True, "origin": spec.origin, "submodule_search_locations": list(spec.submodule_search_locations or [])[:4]}

    vi = getattr(repo, "vector_index", None)
    deps_diag = {
        "qdrant_client": _spec_origin("qdrant_client"),
        "grpc": _spec_origin("grpc"),
        "pydantic": _spec_origin("pydantic"),
        "google.protobuf": _spec_origin("google.protobuf"),
    }
    if vi is None:
        return {"vector_index_present": False, "deps": deps_diag, "sys_path_head": _sys.path[:8]}
    return {
        "vector_index_present": True,
        "enabled": bool(getattr(vi, "enabled", None)),
        "paused": bool(getattr(vi, "_paused", None)),
        "client_present": getattr(vi, "_client", None) is not None,
        "schema_refresh_needed": bool(getattr(vi, "_schema_refresh_needed", False)),
        "last_init_error": getattr(vi, "_last_init_error", None),
        "qdrant_local_path": getattr(vi, "qdrant_local_path", None),
        "qdrant_url": getattr(vi, "qdrant_url", None),
        "collection_name": getattr(vi, "collection_name", None),
        "embedding_class": type(getattr(vi, "embedding", None)).__name__,
        "embedding_dim": int(getattr(getattr(vi, "embedding", None), "dim", 0) or 0),
        "deps": deps_diag,
        "sys_path_head": _sys.path[:8],
        "env": {
            "VECTOR_ENABLED": _os.environ.get("VECTOR_ENABLED"),
            "QDRANT_MODE": _os.environ.get("QDRANT_MODE"),
            "QDRANT_LOCAL_PATH": _os.environ.get("QDRANT_LOCAL_PATH"),
            "QDRANT_URL": _os.environ.get("QDRANT_URL"),
            "KB_EMBEDDING_ENABLED": _os.environ.get("KB_EMBEDDING_ENABLED"),
            "KB_EMBEDDING_BASE_URL": _os.environ.get("KB_EMBEDDING_BASE_URL"),
            "KB_EMBEDDING_MODEL": _os.environ.get("KB_EMBEDDING_MODEL"),
            "VECTOR_DIM": _os.environ.get("VECTOR_DIM"),
        },
    }


@app.get("/v1/system/config", response_model=SystemConfigResponse, summary="系统配置读取")
def get_system_config(repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    return KnowledgeService(repo).get_system_config()


@app.put("/v1/system/config", response_model=SystemConfigResponse, summary="系统配置更新")
def put_system_config(req: SystemConfigUpsertRequest, repo: KnowledgeRepo = Depends(get_repo)) -> dict[str, Any]:
    with _embedding_control_lock:
        return _put_system_config_locked(req, repo)


def _put_system_config_locked(
    req: SystemConfigUpsertRequest,
    repo: KnowledgeRepo,
) -> dict[str, Any]:
    """系统配置写入。

    Embedding service 相关约束（tasks §2.9 + §2.10）：

    - **§2.10 mode=local 时锁 base_url / model**：mode=local 表示用户走内置
      infinity，外部地址 / 模型字段不应再被写——避免"以为切到 external 了但
      mode 还是 local"造成误以为生效的配置漂移。新值与旧值一致允许，仅当试图
      改成非空且不同的值时返 409 提示先切 external。
    - **§2.9 mode / model_id 变更 → require confirm_reindex**：dim / 向量空间
      变就必须 reindex（dim 变索引彻底失效；同 dim 不同模型语义空间漂移）；
      强制 ``I-CONFIRM-REINDEX`` 防误触。
    """
    current = repo.get_system_config()

    # 仓储层是全量覆盖，但多个客户端（/console 简版设置页、/settings、
    # /setup、托盘菜单等）只 PUT 自己编辑过的部分字段。Pydantic 会用 schema 默认值
    # 填充缺失字段，导致「保存 LLM 配置」意外把 rerank / enrichment /
    # embedding_service_* 等未编辑字段清空，以及把 embedding_service_mode
    # 误判成「用户想改成 disabled」→ 触发 I-CONFIRM-REINDEX 强制确认。
    #
    # 统一兜底：凡是客户端没显式传入的可持久化字段，一律用当前库值补齐，
    # 让「没编辑」等价于「保持不变」。confirm_reindex 是一次性请求参数、
    # 不可持久化，不参与兜底。
    # 拷贝一份：Pydantic setattr 会把字段加入原 model_fields_set，不能让循环中的
    # 补值反过来污染「客户端到底显式传了什么」这一事实。
    _sent = set(req.model_fields_set)
    for _field in type(req).model_fields:
        if _field == "confirm_reindex":
            continue
        if _field not in _sent and _field in current:
            setattr(req, _field, current[_field])

    # external/disabled 下，mode 是远程 embedding 的总开关。旧版/精简客户端
    # 可能只提交 mode，若继续沿用库里的 embedding_enabled，会产生
    # disabled+enabled=true 或 external+enabled=false 的矛盾配置。
    # local 是否 ready 由壳层 actual-state 决定，保留其既有 enabled 值，待真正
    # running 后再由 actual-state 同步为 true；普通局部保存也完整保留旧配置。
    if "embedding_service_mode" in _sent:
        if req.embedding_service_mode == "disabled":
            req.embedding_enabled = False
        elif req.embedding_service_mode == "external":
            req.embedding_enabled = True

    # managed 是 mode 的派生持久化状态，不接受客户端单独决定。这里必须在
    # partial PUT 与当前配置合并后无条件归一，不能只处理显式提交 mode 的请求；
    # 否则只提交 managed 的旧客户端仍能写出 local+false / external+true。
    req.embedding_service_managed = req.embedding_service_mode == "local"

    # Pydantic 默认不做 assignment validation；上面的 setattr 可能把历史 DB
    # 脏值带进已校验对象。合并/归一化后做一次完整 round-trip，防脏值穿透到
    # 全量 upsert。仍保留原始 _sent，后续 mode 语义只按客户端真实提交判断。
    try:
        req = type(req).model_validate(req.model_dump())
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False),
        ) from exc

    # §2.10 mode=local 锁外部 embedding 字段
    if req.embedding_service_mode == "local":
        cur_base = str(current.get("embedding_base_url") or "").rstrip("/")
        cur_model = str(current.get("embedding_model") or "")
        new_base = str(req.embedding_base_url or "").rstrip("/")
        new_model = str(req.embedding_model or "")
        # 新值非空 且 与当前不一致 → 视为试图改写，拒
        if (new_base and new_base != cur_base) or (new_model and new_model != cur_model):
            raise HTTPException(
                status_code=409,
                detail=(
                    "mode=local 已锁外部 embedding 配置；如需改 embedding_base_url "
                    "/ embedding_model，请先把 embedding_service_mode 切到 external"
                ),
            )

    # §2.9 mode / model_id 变更触发 reindex 确认
    old_mode = str(current.get("embedding_service_mode") or "disabled")
    mode_changed = req.embedding_service_mode != old_mode
    model_changed = req.embedding_service_model_id != str(
        current.get("embedding_service_model_id") or ""
    )
    if mode_changed or model_changed:
        try:
            require_confirm_token(mode="reindex", confirm=req.confirm_reindex)
        except ConfirmTokenError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    resp = KnowledgeService(repo).upsert_system_config(req)
    # 模型配置（embedding / rerank / llm）改动后走 live reload，让新配置立即生效但
    # **不** clear + pause 老 vector_index（否则 QdrantClient(path=...) portalocker
    # AlreadyLocked → 方案 E）。
    # 分流：mode / model_id 变了 → dim / 向量空间会变，但用户还没点 rebuild，此时禁止
    # 悄悄 recreate_collection（否则会打穿"点 rebuild 前 collection 不被替换"契约）。
    if mode_changed or model_changed:
        _refresh_live_repo_vector_indexes(allow_schema_refresh=False)
    else:
        _refresh_live_repo_vector_indexes(allow_schema_refresh=True)

    # bug 4：mode / model_id 变更后必须联动 desired-state，否则壳层不会停/启 infinity 子进程。
    # 表现：用户从 local 切到 external 后本地 infinity 仍在后台跑、占 1.5GB 内存；
    # 反过来从 external 切到 local 后 infinity 不会被自动拉起，前端 banner 永远 disabled。
    if mode_changed or model_changed:
        state = get_embedding_service_state()
        new_mode = req.embedding_service_mode
        if new_mode == "local":
            # 进入或保持 local：用 switch_model（local→local 改 model）/ install（其他→local）。
            # install 在壳层是幂等的（已有 venv 跳过建 venv 与 pip，本地有完整 model 跳过下载），
            # switch_model 包含 stop→install→start 串联，能覆盖换模型场景的清理需求。
            action = "switch_model" if old_mode == "local" else "install"
            state.bump_desired(
                action=action,
                model_id=req.embedding_service_model_id,
                device=req.embedding_service_device or "cpu",
                enabled=True,
                pytorch_mirror=req.embedding_service_pytorch_mirror,
                cuda_version=req.embedding_service_cuda_version,
            )
        elif old_mode == "local":
            # 离开 local（→ external / disabled）：让壳层 stop 现役 infinity 释放内存。
            # model_id 用切换前的，方便壳层定位要 stop 的 process。
            state.bump_desired(
                action="stop",
                model_id=str(current.get("embedding_service_model_id") or ""),
                device=str(current.get("embedding_service_device") or "cpu"),
                enabled=False,
                pytorch_mirror=str(
                    current.get("embedding_service_pytorch_mirror")
                    or "https://download.pytorch.org/whl/"
                ),
                cuda_version=str(current.get("embedding_service_cuda_version") or "cu124"),
            )
        # external ↔ disabled：infinity 本来就没跑，不需 bump
    return resp


# ---------------------------------------------------------------------------
# Embedding service 控制面（v1.2 §3.2 / AC25 / AC26）
#
# - status：汇总视图，前端 / 托盘菜单读，无需 token
# - desired-state / actual-state：内部端点，壳层（mac-app / windows-app）独占；
#   header 必须携带 X-Embedding-Owner-Token = kb-api 启动时生成的 owner_token；
#   actual-state 额外校验 acknowledged_generation 单调，拒绝旧覆盖新
# ---------------------------------------------------------------------------

_OWNER_TOKEN_HEADER = "x-embedding-owner-token"


def _require_owner_token(request: Request) -> None:
    token = request.headers.get(_OWNER_TOKEN_HEADER, "")
    if not token or token != get_embedding_service_state().owner_token:
        raise HTTPException(status_code=403, detail="owner token mismatch")


@app.get(
    "/v1/system/embedding-service/status",
    response_model=EmbeddingServiceStatusResponse,
    summary="Embedding 服务状态（汇总视图）",
)
def get_embedding_service_status(
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    """合并 DB 配置（mode / 默认值）与壳层回写的 actual-state。"""
    cfg = repo.get_system_config()
    actual = get_embedding_service_state().actual()
    # 2026-07-03 embedding auto-bootstrap 冷启事故 —— installed bit read-side
    # 兜底：能 running / warming_up 必然 filesystem 装好（infinity 都起来了模型
    # 和 venv 不可能没在盘），壳层若因 install SSE 秒关流 + reconcile 单 action
    # 分发时序死结未显式回写 installed，就在 server read 侧 derive 出正确视图，
    # 避免前端看到 installed=false + running=true 的矛盾态。
    # 只改 read 侧不动 apply_actual 写侧：壳层原始 installed 信号保留在
    # ActualState 里，未来 debug 能追溯壳层是否漏写。
    # 双端 Python 共享，Mac / Win 一次改完；跟 Win 壳层同款兜底并存构成双层
    # 防御，Mac Swift 壳层无需 mirror。
    installed_view = actual.installed or actual.running or actual.warming_up
    return {
        "mode": str(cfg.get("embedding_service_mode") or "disabled"),
        "installed": installed_view,
        "running": actual.running,
        "warming_up": actual.warming_up,
        # actual 有就用 actual，没回写时退到 DB 配置（前端不会显示空白）
        "model_id": actual.model_id or str(cfg.get("embedding_service_model_id") or ""),
        "port": actual.port or int(cfg.get("embedding_service_port") or 0),
        "pid": actual.pid,
        "device": actual.device or str(cfg.get("embedding_service_device") or "cpu"),
        "restart_count": actual.restart_count,
        "last_health_check": actual.last_health_check,
        "last_error": actual.last_error,
    }


@app.get(
    "/v1/system/embedding-service/desired-state",
    response_model=EmbeddingServiceDesiredStateResponse,
    summary="Embedding 服务期望状态（壳层 reconcile 用）",
)
def get_embedding_service_desired_state(request: Request) -> dict[str, Any]:
    _require_owner_token(request)
    d = get_embedding_service_state().desired()
    return {
        "action": d.action,
        "model_id": d.model_id,
        "device": d.device,
        "enabled": d.enabled,
        "pytorch_mirror": d.pytorch_mirror,
        "cuda_version": d.cuda_version,
        "generation": d.generation,
        "updated_at": d.updated_at,
    }


@app.get(
    "/v1/system/embedding-service/install-plan",
    response_model=EmbeddingServiceInstallPlanResponse,
    summary="生成壳层安装计划（壳层 ProcessManager 据此执行 pip / 下模型 / 起进程）",
)
def get_embedding_service_install_plan(
    request: Request,
    model_id: str,
    device: str = "cpu",
    detected_cuda: bool = False,
    pytorch_mirror: str | None = None,
    cuda_version: str | None = None,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    """返回 install_plan JSON，让 Mac Swift / Windows Python 壳层直接据此执行。

    设计动机：把 build_install_plan 定为单一真源，Swift 端用 HTTP 拉避免复刻
    （否则 MODEL_REGISTRY 加一项要改两处，必然漂移）。

    壳层调用 → owner_token 校验保护（127.0.0.1 + token，防 web 同源攻击）。

    pytorch_mirror + cuda_version：v1.3.12 加，device=cuda 时影响 pip 命令；
    缺参时从 system_config 读用户已保存的配置（保持跟 install / switch-model 端点
    一致，v1.3.12 收口点）。
    """
    _require_owner_token(request)
    cfg = repo.get_system_config()
    pytorch_mirror = pytorch_mirror or str(
        cfg.get("embedding_service_pytorch_mirror")
        or "https://download.pytorch.org/whl/"
    )
    cuda_version = cuda_version or str(
        cfg.get("embedding_service_cuda_version") or "cu124"
    )
    try:
        plan = build_install_plan(
            model_key=model_id,
            data_root=_resolve_data_root(),
            device=device,
            detected_cuda=detected_cuda,
            pytorch_mirror=pytorch_mirror,
            cuda_version=cuda_version,
        )
    except Exception as exc:
        # resolve_model / resolve_device 的业务异常 → 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "model_id": plan.model_spec.model_id,
        "model_key": model_id,
        "display_name": plan.model_spec.display_name,
        "dim": plan.model_spec.dim,
        "venv_dir": plan.venv_dir,
        "model_dir": plan.model_dir,
        "device": plan.device,
        "port": plan.port,
        "create_venv_cmd": plan.create_venv_cmd,
        "pip_install_cmd": plan.pip_install_cmd,
        "download_args": plan.download_args,
        "start_cmd": plan.start_cmd,
        "env": plan.env,
        "device_detect_cmd": plan.device_detect_cmd,
    }


@app.post(
    "/v1/system/embedding-service/actual-state",
    response_model=EmbeddingServiceActualStateResponse,
    summary="壳层回写 Embedding 服务实况",
)
def post_embedding_service_actual_state(
    request: Request,
    payload: EmbeddingServiceActualStateRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    token = request.headers.get(_OWNER_TOKEN_HEADER, "")
    state = get_embedding_service_state()
    try:
        actual = state.apply_actual(
            owner_token=token,
            acknowledged_generation=payload.acknowledged_generation,
            installed=payload.installed,
            running=payload.running,
            warming_up=payload.warming_up,
            model_id=payload.model_id,
            port=payload.port,
            pid=payload.pid,
            device=payload.device,
            restart_count=payload.restart_count,
            last_error=payload.last_error,
        )
    except OwnerTokenMismatch:
        raise HTTPException(status_code=403, detail="owner token mismatch")
    except GenerationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # 2026-07-01 bug fix：infinity ready 时把 port + dim 同步进 system_config，
    # 让 kb-api VectorIndex 走真 embedding（不再靠 hash fallback），且下次 kb-api
    # 冷启也能从 DB 读到正确 port 而不是初值 0。改动幂等：只有当值真变时才 PUT +
    # 触发 _invalidate_repo_singletons。仅在 running=True 且 port>0 时 sync；
    # 服务未 ready 时保留用户已保存的 config（避免 flapping 反复覆盖）。
    if payload.running and payload.port > 0:
        try:
            # 与 mode 切换共用控制锁：旧 local heartbeat 要么先同步、随后被
            # external/disabled 覆盖，要么后执行时看到新 mode 并跳过，不能用
            # “读旧 cfg→全量 upsert”回滚用户的新选择。
            with _embedding_control_lock:
                _sync_running_embedding_to_config(repo, payload)
        except Exception:  # noqa: BLE001
            logger.exception(
                "actual-state → DB config sync 失败 (port=%s model_id=%s)",
                payload.port, payload.model_id,
            )

    return {
        "accepted": True,
        "acknowledged_generation": actual.acknowledged_generation,
        "updated_at": actual.updated_at,
    }


def _sync_running_embedding_to_config(
    repo: "KnowledgeRepo", payload: "EmbeddingServiceActualStateRequest",
) -> None:
    """actual-state ready 时把真实 port + dim 回写 system_config；幂等。

    - port：直接用 payload.port (壳层探到的 /health 端口)
    - dim：从 DB config 读 model_key → MODEL_REGISTRY 查 dim。actual payload 里
           model_id 是绝对路径不好用，DB config 里的 model_key 是权威 UI 输入
    - enabled：仅 mode=local 时设 True；external/disabled 的 heartbeat 必须忽略，
      防旧本地进程在模式切换后把用户配置重新打开
    """
    cfg = repo.get_system_config() or {}
    if str(cfg.get("embedding_service_mode") or "disabled") != "local":
        return
    model_key = str(cfg.get("embedding_service_model_id") or "").strip()
    if not model_key:
        return  # 用户没配 model_key，不代替他决定
    spec = MODEL_REGISTRY.get(model_key)
    if spec is None:
        return  # 未知模型，不动 dim

    cur_port = int(cfg.get("embedding_service_port") or 0)
    cur_dim = int(cfg.get("embedding_dim") or 0)
    cur_enabled = bool(cfg.get("embedding_enabled") or False)

    changes: dict[str, Any] = {}
    if cur_port != payload.port:
        changes["embedding_service_port"] = payload.port
    if cur_dim != spec.dim:
        changes["embedding_dim"] = spec.dim
    if not cur_enabled:
        changes["embedding_enabled"] = True

    if not changes:
        return

    merged = {**cfg, **changes}
    # 剥掉 normalize 派生字段（PUT schema 不接受）
    for k in ("restart_required", "runtime_port_managed_by", "updated_at", "created_at"):
        merged.pop(k, None)
    merged.setdefault("api_base_url", "http://127.0.0.1:18000")
    merged.setdefault("grafana_url", "")

    try:
        req = SystemConfigUpsertRequest.model_validate(merged)
    except Exception:  # noqa: BLE001
        logger.warning("SystemConfigUpsertRequest 校验失败,跳过 sync", exc_info=True)
        return
    KnowledgeService(repo).upsert_system_config(req)
    # 方案 E：走 live reload 保持 QdrantClient(path=...) 单实例，不再 clear +
    # pause 老 vector_index 触发 portalocker AlreadyLocked。actual-state 心跳同步
    # 只涉及 port / dim / enabled，dim 变了让 collection schema 跟上（True）。
    _refresh_live_repo_vector_indexes(allow_schema_refresh=True)
    logger.info(
        "embedding actual-state → config sync: %s (model_key=%s)",
        {k: v for k, v in changes.items()}, model_key,
    )


# ---------------------------------------------------------------------------
# Embedding service 编排端点：install (SSE) / start / stop
#
# install 写期望状态 + 走 SSE 把壳层安装进度（install_status.json + pip.log）
# 转发给前端（含 ≤15s keepalive，AC21）。start/stop 仅改 desired-state，由壳层
# reconcile 实际进程。
# ---------------------------------------------------------------------------


def _require_local_embedding_mode(cfg: dict[str, Any]) -> None:
    mode = str(cfg.get("embedding_service_mode") or "disabled")
    if mode != "local":
        raise HTTPException(
            status_code=409,
            detail=(
                f"当前 embedding_service_mode={mode}；install/start 仅允许在 "
                "mode=local 时执行"
            ),
        )


def _resolve_data_root() -> str:
    """获取当前进程的数据根目录（runtime/ + logs/ 都挂在其下）。

    优先级：``KB_APP_ROOT``（直装版启动器注入）> 仓库根（dev mode）。
    """
    app_root = os.getenv("KB_APP_ROOT", "").strip()
    if app_root:
        return app_root
    return str(FilePath(__file__).resolve().parent.parent)


@app.post(
    "/v1/system/embedding-service/install",
    summary="触发 Embedding 服务安装（SSE 转发壳层进度）",
)
def post_embedding_service_install(
    req: EmbeddingServiceInstallRequest,
    repo: KnowledgeRepo = Depends(get_repo),
):
    """写期望状态 action=install + 返回 SSE 流 tail 壳层安装进度。

    壳层（mac-app / windows-app ProcessManager）轮询到新 desired 后执行安装
    计划，覆盖式 flush ``runtime/install_status.json``、tee pip 输出到
    ``logs/pip.log``。本端点的 SSE 把这两个文件的变更转发给前端，含 ≤15s
    keepalive（AC21 pip 安装不允许黑盒静默）。

    pytorch_mirror + cuda_version：None 时从 system_config 读，避免脚本直调
    install 端点时漏传两个参数导致壳层 build_install_plan 用模块默认值
    （跟用户已在 setup/settings 里配的值不一致）。
    """
    # 校验 model_id 合法（命中 MODEL_REGISTRY）；不合法直接 400
    try:
        resolve_model(req.model_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    status_path, pip_log_path = resolve_install_paths(_resolve_data_root())

    with _embedding_control_lock:
        cfg = repo.get_system_config()
        _require_local_embedding_mode(cfg)
        pytorch_mirror = req.pytorch_mirror or str(
            cfg.get("embedding_service_pytorch_mirror")
            or "https://download.pytorch.org/whl/"
        )
        cuda_version = req.cuda_version or str(
            cfg.get("embedding_service_cuda_version") or "cu124"
        )

        # 2026-07-03 P0 fix：新一轮 install 前必须 reset install_status.json，
        # 否则 SSE streamer initial snapshot 见上一轮遗留的 phase=completed 立即
        # close stream。reset 与 desired bump 同锁，避免壳层刚写的新状态又被删。
        try:
            status_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "failed to reset %s before install; SSE may close early if stale terminal phase",
                status_path, exc_info=True,
            )

        state = get_embedding_service_state()
        state.bump_desired(
            action="install",
            model_id=req.model_id,
            device=req.device,
            enabled=True,
            pytorch_mirror=pytorch_mirror,
            cuda_version=cuda_version,
        )

    streamer = InstallSseStreamer(
        status_path=status_path,
        pip_log_path=pip_log_path,
    )
    return StreamingResponse(
        streamer.events(),
        media_type="text/event-stream",
        headers={
            # 关掉浏览器/代理缓存与 buffer，避免 SSE 被攒帧
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/v1/system/embedding-service/start",
    response_model=EmbeddingServiceDesiredStateResponse,
    summary="启动 Embedding 服务（仅改期望状态，壳层 reconcile）",
)
def post_embedding_service_start(
    req: EmbeddingServiceStartStopRequest = Body(default_factory=EmbeddingServiceStartStopRequest),
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    with _embedding_control_lock:
        state = get_embedding_service_state()
        # model_id 兜底链：req > prev desired > DB config > "bge-m3"（防 desired + req 空态）
        # device 兜底链：req > prev desired > DB config > "cpu"（2026-07-02 P1-2 fix：
        # auto-bootstrap 场景 prev.device 是空的走 "cpu" 会吞掉 DB 里的 mps/cuda 配置）
        prev = state.desired()
        cfg = repo.get_system_config()
        _require_local_embedding_mode(cfg)
        if req.expected_generation is not None and (
            prev.generation != req.expected_generation or prev.action != "install"
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "start 前置 generation 已过期；当前 desired="
                    f"{prev.action}@{prev.generation}，请求基于 install@"
                    f"{req.expected_generation}"
                ),
            )
        model_id = req.model_id or prev.model_id or str(
            cfg.get("embedding_service_model_id") or "bge-m3"
        )
        device = req.device or prev.device or str(
            cfg.get("embedding_service_device") or "cpu"
        )
        d = state.bump_desired(
            action="start",
            model_id=model_id,
            device=device,
            enabled=True,
            pytorch_mirror=prev.pytorch_mirror,
            cuda_version=prev.cuda_version,
        )
    return {
        "action": d.action, "model_id": d.model_id, "device": d.device,
        "enabled": d.enabled,
        "pytorch_mirror": d.pytorch_mirror, "cuda_version": d.cuda_version,
        "generation": d.generation, "updated_at": d.updated_at,
    }


@app.post(
    "/v1/system/embedding-service/stop",
    response_model=EmbeddingServiceDesiredStateResponse,
    summary="停止 Embedding 服务（仅改期望状态，壳层 reconcile）",
)
def post_embedding_service_stop() -> dict[str, Any]:
    with _embedding_control_lock:
        state = get_embedding_service_state()
        prev = state.desired()
        d = state.bump_desired(
            action="stop",
            model_id=prev.model_id,
            device=prev.device or "cpu",
            enabled=False,
            pytorch_mirror=prev.pytorch_mirror,
            cuda_version=prev.cuda_version,
        )
    return {
        "action": d.action, "model_id": d.model_id, "device": d.device,
        "enabled": d.enabled,
        "pytorch_mirror": d.pytorch_mirror, "cuda_version": d.cuda_version,
        "generation": d.generation, "updated_at": d.updated_at,
    }


@app.post(
    "/v1/system/embedding-service/switch-model",
    response_model=EmbeddingServiceSwitchModelResponse,
    status_code=202,
    summary="切换 Embedding 模型（编排 + 触发 reindex 链路）",
)
def post_embedding_service_switch_model(
    req: EmbeddingServiceSwitchModelRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    """切换内置 Embedding 模型（design v1.2 §4.5 / AC22）。

    切模型必然让 dim / 向量空间改变 → 必须重建索引（rebuild_vector_index）。
    本端点只做 **编排**：

    1. confirm_token 强制（AC22 前置确认）
    2. 与正在跑的 rebuild 互斥（rebuild_runner.is_running → 409）
    3. bump_desired(action=switch_model)，壳层 reconcile：停现有 → 装新 model
       → 起新 infinity → 回写 actual-state
    4. **新模型起来后**，前端需 POST /v1/system/rebuild-vector-index 触发实际
       reindex（与单独的 rebuild 端点共用 runner，单一编排入口避免分裂）。
    """
    try:
        require_confirm_token(mode="overwrite", confirm=req.confirm)
    except ConfirmTokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        resolve_model(req.model_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 与 rebuild 互斥：reindex 期间不允许换模型，避免半成品索引混进新 dim 向量
    if get_rebuild_runner().is_running():
        raise HTTPException(
            status_code=409,
            detail="向量索引重建进行中，请等 rebuild 完成或先 abort 再切模型",
        )

    with _embedding_control_lock:
        cfg = repo.get_system_config()
        _require_local_embedding_mode(cfg)
        pytorch_mirror = req.pytorch_mirror or str(
            cfg.get("embedding_service_pytorch_mirror")
            or "https://download.pytorch.org/whl/"
        )
        cuda_version = req.cuda_version or str(
            cfg.get("embedding_service_cuda_version") or "cu124"
        )

        state = get_embedding_service_state()
        d = state.bump_desired(
            action="switch_model",
            model_id=req.model_id,
            device=req.device,
            enabled=True,
            pytorch_mirror=pytorch_mirror,
            cuda_version=cuda_version,
        )
    return {
        "action": d.action,
        "model_id": d.model_id,
        "device": d.device,
        "generation": d.generation,
        "next_action": "POST /v1/system/rebuild-vector-index",
    }


# ---------------------------------------------------------------------------
# 向量索引重建（design v1.2 §4.5 + AC10 + AC23）
#
# - POST rebuild-vector-index：require confirm_token + 阈值放行 + 起后台线程
# - POST rebuild-vector-index/abort：触发 abort + 回滚 qdrant_local 备份
# - GET  rebuild-vector-index/status：返回 runner 单例状态快照
# ---------------------------------------------------------------------------


# 测试钩子：注入 fake rebuild_fn / backup_fn / restore_fn 绕开 strict embedding
# 依赖 + 真实 qdrant 目录拷贝；生产代码保持 None，runner 走默认实现。
_REBUILD_FN_OVERRIDE: Any = None
_BACKUP_FN_OVERRIDE: Any = None
_RESTORE_FN_OVERRIDE: Any = None


def _resolve_qdrant_paths() -> tuple[str, str]:
    """返回 (qdrant_local_path, backup_root)。

    优先级与 ``_detect_pre_restore_on_startup`` / 备份逻辑一致：
    - QDRANT_LOCAL_PATH 环境变量 > 默认 ``data/qdrant_local``
    - KB_AUTO_BACKUP_ROOT > ``{data_root}/backups``
    """
    data_root = _resolve_data_root()
    qdrant_local = os.getenv("QDRANT_LOCAL_PATH", str(FilePath(data_root) / "data" / "qdrant_local"))
    backup_root = os.getenv("KB_AUTO_BACKUP_ROOT", str(FilePath(data_root) / "data" / "backups"))
    return qdrant_local, backup_root


@app.post(
    "/v1/system/rebuild-vector-index",
    response_model=RebuildVectorIndexResponse,
    status_code=202,
    summary="触发向量索引重建（后台 + strict）",
)
def post_rebuild_vector_index(
    req: RebuildVectorIndexRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    """重建当前 collection 全部 active chunk。

    - ``confirm`` 必须等于 ``I-CONFIRM-OVERWRITE``（AC22 前置确认对话框）
    - chunk ≥ ``REINDEX_MAINTENANCE_THRESHOLD``(5000) → 置 maintenance flag，
      期间写类 API 返 202 + Retry-After: 30（middleware）；小库后台跑不锁
    - 已有 rebuild 跑 → 409
    """
    try:
        require_confirm_token(mode="overwrite", confirm=req.confirm)
    except ConfirmTokenError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    qdrant_local, backup_root = _resolve_qdrant_paths()
    vector_index = getattr(repo, "vector_index", None)
    if vector_index is None:
        raise HTTPException(status_code=500, detail="vector_index 未挂载，无法 rebuild")

    runner = get_rebuild_runner()
    start_kwargs: dict[str, Any] = {
        "repo": repo,
        "vector_index": vector_index,
        "qdrant_local_path": qdrant_local,
        "backup_root": backup_root,
        "batch_size": req.batch_size,
        "threshold_chunks": REINDEX_MAINTENANCE_THRESHOLD,
        "rebuild_fn": _REBUILD_FN_OVERRIDE,
    }
    if _BACKUP_FN_OVERRIDE is not None:
        start_kwargs["backup_fn"] = _BACKUP_FN_OVERRIDE
    if _RESTORE_FN_OVERRIDE is not None:
        start_kwargs["restore_fn"] = _RESTORE_FN_OVERRIDE
    try:
        snap = runner.start(**start_kwargs)
    except RebuildAlreadyRunning as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except EmbeddingNotReady as exc:
        # 装机 + warmup 期间用户抢点 → 503 + Retry-After 让壳层稍后重试或提示等待。
        # 关键：rebuild_runner.start 在探针前未进 RUNNING / 未触发 backup，旧索引完整。
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": "5"},
        ) from exc

    return {
        "task_id": snap.task_id,
        "status": snap.status,
        "total": snap.total,
        "threshold_blocked_writes": snap.threshold_blocked_writes,
        "started_at": snap.started_at,
    }


@app.post(
    "/v1/system/rebuild-vector-index/abort",
    response_model=RebuildVectorIndexStatusResponse,
    summary="中止 rebuild + 回滚旧索引（AC23 逃生通道）",
)
def post_rebuild_vector_index_abort() -> dict[str, Any]:
    runner = get_rebuild_runner()
    snap = runner.abort()
    return {
        "status": snap.status, "task_id": snap.task_id,
        "started_at": snap.started_at, "ended_at": snap.ended_at,
        "total": snap.total, "processed": snap.processed,
        "error": snap.error, "backup_path": snap.backup_path,
        "threshold_blocked_writes": snap.threshold_blocked_writes,
    }


@app.get(
    "/v1/system/rebuild-vector-index/status",
    response_model=RebuildVectorIndexStatusResponse,
    summary="rebuild 进度查询",
)
def get_rebuild_vector_index_status() -> dict[str, Any]:
    snap = get_rebuild_runner().state()
    return {
        "status": snap.status, "task_id": snap.task_id,
        "started_at": snap.started_at, "ended_at": snap.ended_at,
        "total": snap.total, "processed": snap.processed,
        "error": snap.error, "backup_path": snap.backup_path,
        "threshold_blocked_writes": snap.threshold_blocked_writes,
    }


@app.post("/v1/knowledge/import-incremental", summary="增量导入知识")
def import_incremental_knowledge(
    req: ImportIncrementalRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    tools = KnowledgeMcpTools(repo)
    return tools.import_incremental_knowledge(
        directory=req.directory,
        project=req.project,
        domain=req.domain,
        knowledge_type=req.knowledge_type,
    )


@app.post("/v1/knowledge/export-package", summary="导出知识包")
def export_knowledge_package(
    req: ExportKnowledgePackageRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    tools = KnowledgeMcpTools(repo)
    return tools.export_knowledge_package(export_dir=req.export_dir)


@app.post("/v1/knowledge/import-package", summary="导入知识包")
def import_knowledge_package(
    req: ImportKnowledgePackageRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    tools = KnowledgeMcpTools(repo)
    return tools.import_knowledge_package(package_path=req.package_path, confirm=req.confirm)


@app.post("/v1/knowledge/clear", summary="清空知识库")
def clear_knowledge_base(
    req: ClearKnowledgeBaseRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    tools = KnowledgeMcpTools(repo)
    return tools.clear_knowledge_base(confirm=req.confirm, backup_dir=req.backup_dir)


@app.post("/v1/knowledge/cleanup-expired", summary="清理过期知识")
def cleanup_expired_knowledge(
    req: CleanupExpiredKnowledgeRequest,
    repo: KnowledgeRepo = Depends(get_repo),
) -> dict[str, Any]:
    tools = KnowledgeMcpTools(repo)
    return tools.cleanup_expired_knowledge(
        mode=req.mode,
        as_of=req.as_of,
        backup_dir=req.backup_dir,
        confirm=req.confirm,
    )


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------


def _allowed_data_roots() -> tuple[str, ...]:
    """允许的数据目录前缀（审计 #12 路径边界，二次收紧）。

    缺省允许：
    - 仓库根（开发模式跑）
    - KB_APP_ROOT 环境变量指向的安装根（直装版运行时由启动器注入，跨平台）
    - macOS：/Applications/KnowledgeBase、~/Library/Application Support/KnowledgeBase
    - Windows：%LocalAppData%\\KnowledgeBase（默认 auto-backup 位置）

    **不再默认允许整个 $HOME 或 $TMPDIR**——这两个目录下放任意文件都会被信任，
    误配 SQLITE_PATH=~/photos 会触发破坏性 cp。如需扩大范围（如 CI / 测试 / 自
    定义安装路径），通过 KB_DATA_ROOTS 环境变量显式追加，分隔符跟随 os.pathsep
    （POSIX 用 `:`，Windows 用 `;`——不能用 `:` 否则会把盘符冒号错切）。
    """
    defaults: list[str] = [
        str(FilePath(__file__).resolve().parent.parent),  # 仓库根 / dev mode
        "/Applications/KnowledgeBase",
        os.path.expanduser("~/Library/Application Support/KnowledgeBase"),
    ]

    # 直装版运行时安装根：mac kb-start.sh / windows tray 启动 kb-api 时 export
    app_root = os.getenv("KB_APP_ROOT", "").strip()
    if app_root:
        defaults.append(app_root)

    # Windows 默认 auto-backup 根（与 installer.iss 的 PrepareToInstall 对齐）
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        defaults.append(os.path.join(local_app_data, "KnowledgeBase"))

    extra = os.getenv("KB_DATA_ROOTS", "")
    if extra:
        defaults.extend(p for p in extra.split(os.pathsep) if p)
    return tuple(str(FilePath(p).resolve()) for p in defaults if p)


def _validate_data_path(path: str) -> str:
    """把传入路径 realpath 后校验是否落在 _allowed_data_roots() 内（审计 #12）。

    阻止误配的 SQLITE_PATH / QDRANT_LOCAL_PATH / KB_AUTO_BACKUP_ROOT 指向
    任意目录触发破坏性 cp / 删除。
    """
    if not path:
        raise HTTPException(status_code=500, detail="data path is empty")
    # realpath 不要求路径存在
    abs_path = str(FilePath(path).expanduser().resolve())
    roots = _allowed_data_roots()
    for root in roots:
        if abs_path == root or abs_path.startswith(root + os.sep):
            return abs_path
    raise HTTPException(
        status_code=500,
        detail=(
            f"data path '{path}' resolves to '{abs_path}' which is outside "
            f"allowed roots {roots}; set KB_DATA_ROOTS to extend"
        ),
    )


def _build_backup_service(repo: Any) -> BackupService:
    """根据当前 VectorIndex 单例构造 BackupService，注入 close/reinit 回调。

    审计 #6：用 VectorIndex.pause/resume 取代裸操作 _client，避免懒重连与
    cp qdrant_local 并发。pause 期间 search/ask 会优雅降级到关键词检索。
    """
    sqlite_path = _validate_data_path(os.getenv("SQLITE_PATH", "data/knowledge.db"))
    qdrant_local_path = _validate_data_path(
        os.getenv("QDRANT_LOCAL_PATH", "data/qdrant_local")
    )

    def _close() -> None:
        idx = getattr(repo, "vector_index", None)
        if idx is None:
            return
        if hasattr(idx, "pause"):
            idx.pause()
        else:
            # 旧路径兼容
            client = getattr(idx, "_client", None)
            if client is not None and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:
                    logger.warning("qdrant client close failed", exc_info=True)
            try:
                idx._client = None
            except Exception:
                pass

    def _reinit() -> None:
        idx = getattr(repo, "vector_index", None)
        if idx is None:
            return
        if hasattr(idx, "resume"):
            idx.resume()
        else:
            try:
                idx._client = None
            except Exception:
                pass

    return BackupService(
        repo=repo,
        sqlite_path=sqlite_path,
        qdrant_local_path=qdrant_local_path,
        on_qdrant_close=_close,
        on_qdrant_reinit=_reinit,
    )


@app.post(
    "/v1/system/backup/export",
    summary="导出全量备份（流式 tar.gz）",
    description=(
        "导出全量备份为 tar.gz：knowledge.db + qdrant_local + 脱敏配置 sidecar。"
        " 注意：完整 knowledge.db 仍包含 system_config 真凭证，整个备份包不是脱敏产物。"
        " 包内含 manifest.json（schema_version=1 + db sha256 + stats + embedding 配置）。"
        " maintenance flag 置位时返回 503；postgres backend 返 501；磁盘不足返 507。"
    ),
)
def export_full_backup() -> StreamingResponse:
    # 注意：必须在解析 repo 依赖前判断 backend——postgres 路径下 get_repo()
    # 会因缺 DATABASE_URL 报 500，先一步返回 501 给出明确语义
    backend = _resolve_backend()
    if backend != "sqlite":
        raise HTTPException(
            status_code=501,
            detail=(
                "备份导出仅支持 sqlite backend，"
                "postgres 见独立提案 backup-restore-docker-mode"
            ),
        )
    repo = get_repo()

    import tempfile

    tmp_out = tempfile.NamedTemporaryFile(
        prefix="kb-backup-", suffix=".tar.gz", delete=False
    )
    tmp_out.close()
    try:
        svc = _build_backup_service(repo)
        svc.export_to(tmp_out.name)
    except InsufficientDiskSpaceError as e:
        try:
            os.unlink(tmp_out.name)
        except OSError:
            pass
        raise HTTPException(
            status_code=507,
            detail={
                "message": "insufficient disk space",
                "required_bytes": e.required_bytes,
                "available_bytes": e.available_bytes,
                "target": e.target,
            },
        )
    except Exception:
        try:
            os.unlink(tmp_out.name)
        except OSError:
            pass
        raise

    filename = f"kb-backup-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.tar.gz"

    def _iter_and_cleanup():
        try:
            with open(tmp_out.name, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(tmp_out.name)
            except OSError:
                pass

    return StreamingResponse(
        _iter_and_cleanup(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _resolve_auto_backup_root() -> str:
    """auto-backup 根目录：优先环境变量（测试 / 容器），默认 macOS 用户目录。"""
    override = os.getenv("KB_AUTO_BACKUP_ROOT")
    if override:
        return override
    home = os.path.expanduser("~")
    return os.path.join(home, "Library", "Application Support", "KnowledgeBase", "auto-backup")


def _build_auto_backup_service() -> AutoBackupService:
    sqlite_path = _validate_data_path(os.getenv("SQLITE_PATH", "data/knowledge.db"))
    qdrant_local_path = _validate_data_path(
        os.getenv("QDRANT_LOCAL_PATH", "data/qdrant_local")
    )
    auto_backup_root = _validate_data_path(_resolve_auto_backup_root())
    return AutoBackupService(
        sqlite_path=sqlite_path,
        qdrant_local_path=qdrant_local_path,
        auto_backup_root=auto_backup_root,
    )


@app.post(
    "/v1/system/backup/import",
    summary="导入全量备份（overwrite / merge）",
    description=(
        "上传 tar.gz 备份包并覆盖 / 合并当前数据。"
        " 必须提供严格 confirm token（I-CONFIRM-OVERWRITE / I-CONFIRM-MERGE）。"
        " merge 模式在 P0 范围内尚未实现，返回 501。"
    ),
)
async def import_full_backup(
    mode: str = Form(...),
    confirm: str | None = Form(None),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    backend = _resolve_backend()
    if backend != "sqlite":
        raise HTTPException(status_code=501, detail="仅支持 sqlite backend")

    # 二次确认：缺失 / 弱 token / mode 错配 → 400
    try:
        require_confirm_token(mode=mode, confirm=confirm)
    except ConfirmTokenError as e:
        raise HTTPException(status_code=400, detail=str(e))

    repo = get_repo()
    flag = get_maintenance_flag()

    import tempfile

    tmp_pkg = tempfile.NamedTemporaryFile(
        prefix="kb-import-", suffix=".tar.gz", delete=False
    )
    flag_set_here = False
    try:
        try:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp_pkg.write(chunk)
        finally:
            tmp_pkg.close()

        # 中间件检查与 flag.set 之间存在窗口（审计 #5）：如果另一个 import 抢
        # 先 set，这里会抛 RuntimeError。捕获后映射成 503，与中间件行为一致。
        try:
            flag.set(MaintenanceReason.BACKUP_IMPORT, detail=f"mode={mode}")
        except RuntimeError as e:
            raise HTTPException(
                status_code=503,
                headers={"Retry-After": "60"},
                detail=f"service in maintenance: {e}",
            )
        flag_set_here = True

        svc = _build_backup_service(repo)
        auto_svc = _build_auto_backup_service()

        if mode == "overwrite":
            try:
                result = svc.import_overwrite(tmp_pkg.name, auto_svc)
            except BackupImportError as e:
                # 用结构化 kind 字段映射状态码（审计 #11），不再依赖错误消息关键词
                if e.kind == "client":
                    raise HTTPException(status_code=400, detail=str(e))
                # rolled_back / rollback_partial / server → 500
                raise HTTPException(status_code=500, detail=str(e))
        elif mode == "merge":
            raise HTTPException(
                status_code=501,
                detail="merge mode 在当前版本尚未实现，将在 P1 提供",
            )
        else:
            # require_confirm_token 已经过滤过 mode；此分支兜底
            raise HTTPException(status_code=400, detail=f"unknown mode: {mode}")
    finally:
        if flag_set_here:
            flag.clear()
        try:
            os.unlink(tmp_pkg.name)
        except OSError:
            pass

    return result


@app.post(
    "/v1/system/recover/pre-restore",
    summary="处理 .pre-restore 残留（rollback / discard）",
    description=(
        "上次 import_overwrite 被 kill -9 / 断电中断 → 启动检测发现 "
        "`.pre-restore.*` 副本，服务进入 maintenance。本端点接受用户决策。"
    ),
)
def recover_pre_restore(
    action: str = Form(...),
    confirm: str | None = Form(None),
) -> dict[str, Any]:
    if action not in ("rollback", "discard"):
        raise HTTPException(
            status_code=400,
            detail=f"action must be 'rollback' or 'discard', got {action!r}",
        )
    try:
        require_confirm_token(mode=action, confirm=confirm)
    except ConfirmTokenError as e:
        raise HTTPException(status_code=400, detail=str(e))

    backend = _resolve_backend()
    if backend != "sqlite":
        raise HTTPException(
            status_code=501,
            detail="recover only supports sqlite backend",
        )
    sqlite_path = _validate_data_path(os.getenv("SQLITE_PATH", "data/knowledge.db"))
    qdrant_local_path = _validate_data_path(
        os.getenv("QDRANT_LOCAL_PATH", "data/qdrant_local")
    )
    repo = get_repo()
    idx = getattr(repo, "vector_index", None)

    def _pause():
        if idx is not None and hasattr(idx, "pause"):
            idx.pause()

    def _resume():
        if idx is not None and hasattr(idx, "resume"):
            idx.resume()

    try:
        return _pre_restore_execute_recover(
            action=action,
            sqlite_path=sqlite_path,
            qdrant_local_path=qdrant_local_path,
            on_vector_pause=_pause,
            on_vector_resume=_resume,
        )
    except Exception as e:
        logger.error("recover pre-restore failed", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/system/restart", summary="重启本地服务")
def restart_local_service() -> dict[str, Any]:
    backend = _resolve_backend()
    if backend == "postgres":
        raise HTTPException(status_code=409, detail="请通过 docker compose restart 操作")
    if backend != "sqlite":
        raise HTTPException(status_code=400, detail=f"不支持的 KB_BACKEND: {backend}")

    # PyInstaller --onefile 坑:__file__.parent 在 frozen mode 下是 _MEIPASS 解压
    # 临时目录,_MEIPASS\scripts 里没有 local-restart-direct.ps1(build 没 add-data
    # 进去),会拿到 404 "restart script not found"。实际脚本在 installer 装到的
    # install root 下 ({install_root}\scripts\local-restart-direct.ps1),走
    # KB_APP_ROOT 环境变量(tray_app_local 启动 kb-api 时已注入)。dev 模式没注入
    # 时退到 APP_DIR.parent(此时 __file__ 在源码 app/ 下,APP_DIR.parent = 仓库根)。
    root_dir = FilePath(_resolve_data_root())

    try:
        if sys.platform.startswith("win"):
            # 直装版优先（scripts/local-restart-direct.ps1：taskkill kb-api + 启动 bin\kb-api\kb-api.exe（onedir）或 bin\kb-api.exe（onefile 兼容））
            # 开发模式 fallback（scripts/local-restart.ps1：local-stop.ps1 + local-start.ps1，依赖 .venv）
            scripts_dir = root_dir / "scripts"
            restart_script = scripts_dir / "local-restart-direct.ps1"
            if not restart_script.exists():
                restart_script = scripts_dir / "local-restart.ps1"
            if not restart_script.exists():
                raise HTTPException(status_code=404, detail="restart script not found")
            restart_env = os.environ.copy()
            restart_env["KB_RESTART_SCRIPT"] = str(restart_script)
            # v2 P0-5: 预检脚本 PowerShell 5.1 ParseFile 通过, 防假绿
            # (无 BOM UTF-8 脚本被 PS 5.1 按 ANSI 读会 parse fail, 但 fire-and-forget
            # Popen 拿不到 exit code, API 会返 ok=true 但脚本根本没跑)
            precheck = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                    "$errs = $null; $tokens = $null; "
                    "$null = [System.Management.Automation.Language.Parser]::ParseFile("
                    "$env:KB_RESTART_SCRIPT, [ref]$tokens, [ref]$errs); "
                    f"if ($errs.Count -gt 0) {{ Write-Error ($errs | ForEach-Object {{ $_.Message }}) -ErrorAction Stop; exit 1 }}; "
                    f"exit 0",
                ],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=restart_env,
            )
            if precheck.returncode != 0:
                # 脚本 parse 失败, 返 500 + 明确 err 让 tray/client 知道 restart 未启动
                raise HTTPException(
                    status_code=500,
                    detail=f"restart script parse failed (PS 5.1 syntax error): {precheck.stderr[:500]}",
                )
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "Start-Sleep -Seconds 1; & $env:KB_RESTART_SCRIPT",
                ],
                cwd=str(root_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=restart_env,
            )
        elif sys.platform == "darwin":
            # 直装版优先（scripts/restart.sh：由 build_mac_direct_install_dmg.sh 从 mac-app/restart.sh 拷过来）
            # 开发模式 fallback（mac-app/restart.sh：仓库根直接运行）
            restart_script = root_dir / "scripts" / "restart.sh"
            if not restart_script.exists():
                restart_script = root_dir / "mac-app" / "restart.sh"
            if not restart_script.exists():
                raise HTTPException(status_code=404, detail="restart script not found")
            subprocess.Popen(
                ["/bin/bash", str(restart_script)],
                cwd=str(root_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            raise HTTPException(status_code=501, detail=f"platform not supported for restart: {sys.platform}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"restart failed: {exc}") from exc

    return {"ok": True}
