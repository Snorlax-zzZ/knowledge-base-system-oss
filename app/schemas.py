from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def _normalize_domain_input(value: object) -> object:
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "person":
            return "personal"
        return v
    return value


class SourceRefInput(BaseModel):
    type: str = Field(pattern="^(file|chat|commit|pr)$", description="来源类型：file/chat/commit/pr。")
    uri: str = Field(min_length=1, description="来源地址。")
    source_hash: str | None = Field(default=None, description="可选来源摘要（如 git sha / 文件 hash）。")
    captured_at: datetime | None = Field(default=None, description="可选采集时间，默认服务端当前时间。")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, description="检索关键词或问题。")
    domain: str = Field(pattern="^(work|personal)$", description="知识域：`work` 工作知识，`personal` 个人知识。")
    project: str | None = Field(default=None, description="项目名过滤（可选）。")
    module: str | None = Field(default=None, description="模块名过滤（可选）。")
    feature: str | None = Field(default=None, description="功能点过滤（可选）。")
    tags: list[str] = Field(default_factory=list, description="标签过滤（可选，传多个时为包含匹配）。")
    source_uri: str | None = Field(default=None, description="来源路径/链接过滤（可选，模糊匹配）。")
    as_of: datetime | None = Field(default=None, description="按生效时间过滤（可选，ISO 8601）。")
    top_k: int = Field(default=8, ge=1, le=50, description="返回结果数量上限，范围 1~50。")
    actor: str = Field(default="manual", description="调用方身份，用于 ACL 权限过滤。")

    @field_validator("domain", mode="before")
    @classmethod
    def normalize_domain(cls, value: object) -> object:
        return _normalize_domain_input(value)


class UpsertRequest(BaseModel):
    knowledge_item_id: str | None = Field(default=None, description="知识条目 ID。为空时新建，传值时更新并产生新版本。")
    title: str = Field(description="知识标题。")
    domain: str = Field(pattern="^(work|personal)$", description="知识域：`work` 工作知识，`personal` 个人知识。")
    project: str = Field(description="所属项目名。")
    module: str = Field(default="", description="所属模块名（可选）。")
    feature: str = Field(default="", description="所属功能点（可选）。")
    tags: list[str] = Field(default_factory=list, description="标签列表（可选）。")
    sources: list[SourceRefInput] = Field(default_factory=list, description="结构化来源引用列表（可选），写入 source_ref 表。")
    source_uri: str = Field(default="", description="原始来源路径或 URL（可选）。")
    effective_from: datetime | None = Field(default=None, description="知识生效开始时间（可选，ISO 8601）。")
    effective_to: datetime | None = Field(default=None, description="知识生效结束时间（可选，ISO 8601）。")
    type: str = Field(pattern="^(decision|runbook|lesson|fact)$", description="知识类型：decision/runbook/lesson/fact。")
    content_markdown: str = Field(min_length=1, description="知识正文（Markdown）。")
    summary: str = Field(default="", description="摘要（可选）。")
    author: str = Field(description="作者。")
    change_note: str = Field(default="", description="本次变更说明（可选）。")
    public_read: bool = Field(default=True, description="是否允许所有角色读取。")
    acl_actors: list[str] = Field(default_factory=list, description="允许读取的 actor 列表（当 public_read=false 时生效）。")

    @field_validator("domain", mode="before")
    @classmethod
    def normalize_domain(cls, value: object) -> object:
        return _normalize_domain_input(value)


class HealthResponse(BaseModel):
    status: str = Field(description="服务状态。正常固定为 `ok`。")


class SourceRef(BaseModel):
    type: str = Field(description="来源类型，例如 `source_uri`。")
    uri: str = Field(description="来源地址。")


class SearchResultItem(BaseModel):
    knowledge_item_id: str = Field(description="知识条目 ID。")
    version: int = Field(description="命中的知识版本号。")
    score: float = Field(description="相关性分数（0~1）。")
    snippet: str = Field(description="命中片段摘要。")
    title: str = Field(description="知识标题。")
    source: list[SourceRef] = Field(default_factory=list, description="来源引用列表。")


class SearchResponse(BaseModel):
    results: list[SearchResultItem] = Field(default_factory=list, description="检索结果列表。")
    trace_id: str | None = Field(default=None, description="检索追踪 ID；仅命中结果时返回。")
    knowledge_item_ids: list[str] | None = Field(default=None, description="命中的知识条目 ID 列表；仅命中结果时返回。")


class KnowledgeItemResponse(BaseModel):
    knowledge_item_id: str = Field(description="知识条目 ID。")
    title: str = Field(description="知识标题。")
    domain: str = Field(description="知识域。")
    project: str = Field(description="项目名。")
    module: str = Field(description="模块名。")
    feature: str = Field(description="功能点。")
    tags: list[str] = Field(default_factory=list, description="标签列表。")
    source_uri: str = Field(description="来源路径或 URL。")
    effective_from: datetime | None = Field(default=None, description="生效开始时间。")
    effective_to: datetime | None = Field(default=None, description="生效结束时间。")
    type: str = Field(description="知识类型。")
    status: str = Field(description="状态。")
    version: int = Field(description="当前版本号。")
    content_markdown: str = Field(description="正文 Markdown。")
    summary: str = Field(description="摘要。")
    updated_at: datetime = Field(description="最近更新时间。")
    sources: list[SourceRef] = Field(default_factory=list, description="来源引用列表。")


class UpsertResponse(BaseModel):
    knowledge_item_id: str = Field(description="知识条目 ID。")
    version: int = Field(description="写入后的版本号。")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, description="自然语言问题。")
    domain: str = Field(pattern="^(work|personal)$", description="知识域：work / personal。")
    project: str | None = Field(default=None, description="项目名过滤（可选）。")
    top_k_chunks: int = Field(default=5, ge=1, le=20, description="喂给 LLM 的 chunk 数上限，范围 1~20。")
    actor: str = Field(default="manual", description="调用方身份，用于 ACL 过滤。")

    @field_validator("domain", mode="before")
    @classmethod
    def normalize_domain(cls, value: object) -> object:
        return _normalize_domain_input(value)


class AskChunkRef(BaseModel):
    knowledge_item_id: str = Field(description="来源知识条目 ID。")
    title: str = Field(description="知识标题。")
    snippet: str = Field(description="引用片段（chunk 正文前 180 字）。")
    version: int = Field(description="知识版本号。")


class AskResponse(BaseModel):
    question: str = Field(description="原始问题。")
    answer: str | None = Field(default=None, description="LLM 生成的回答；LLM 未配置或调用失败时为 null。")
    llm_available: bool = Field(description="LLM 是否已配置并可用。")
    llm_error: str | None = Field(default=None, description="LLM 调用失败时的错误类型；成功时为 null。")
    finish_reason: str | None = Field(default=None, description="供应商返回的生成停止原因。")
    truncated: bool = Field(default=False, description="回答是否因达到输出长度上限而截断。")
    chunks_used: list[AskChunkRef] = Field(default_factory=list, description="实际喂给 LLM 的 chunk 来源列表。")


class ImportIncrementalRequest(BaseModel):
    directory: str = Field(min_length=1, description="增量导入目录。")
    project: str = Field(min_length=1, description="项目名。")
    domain: str = Field(default="work", pattern="^(work|personal)$", description="知识域：work / personal。")
    knowledge_type: str = Field(
        default="fact",
        pattern="^(decision|runbook|lesson|fact)$",
        description="知识类型：decision/runbook/lesson/fact。",
    )

    @field_validator("domain", mode="before")
    @classmethod
    def normalize_domain(cls, value: object) -> object:
        return _normalize_domain_input(value)


class ExportKnowledgePackageRequest(BaseModel):
    export_dir: str | None = Field(default=None, description="导出目录（可选）。")


class ImportKnowledgePackageRequest(BaseModel):
    package_path: str = Field(min_length=1, description="知识包路径。")
    confirm: bool = Field(default=False, description="危险操作确认。")


class ClearKnowledgeBaseRequest(BaseModel):
    confirm: bool = Field(default=False, description="危险操作确认。")
    backup_dir: str | None = Field(default=None, description="清空前备份目录（可选）。")


class CleanupExpiredKnowledgeRequest(BaseModel):
    mode: str = Field(default="archive", pattern="^(archive|delete)$", description="清理模式：archive / delete。")
    as_of: str | None = Field(default=None, description="统计时间（可选，YYYY-MM-DD）。")
    backup_dir: str | None = Field(default=None, description="备份目录（可选）。")
    confirm: bool = Field(default=False, description="delete 模式危险操作确认。")


class SystemConfigResponse(BaseModel):
    api_base_url: str = Field(default="http://127.0.0.1:18000")
    service_port: int = Field(default=18000, ge=1, le=65535)
    grafana_url: str = Field(default="http://127.0.0.1:3000")
    ui_theme: str = Field(default="neo")
    llm_enabled: bool = Field(default=False)
    llm_api_key: str = Field(default="")
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_model: str = Field(default="gpt-4o-mini")
    llm_timeout_sec: float = Field(default=30.0)
    llm_temperature: float = Field(default=0.2)
    llm_max_tokens: int = Field(default=1024)
    llm_max_tokens_auto: bool = Field(default=True)
    embedding_enabled: bool = Field(default=False)
    embedding_api_key: str = Field(default="")
    embedding_base_url: str = Field(default="")
    embedding_model: str = Field(default="")
    embedding_dim: int = Field(default=384, ge=1)
    embedding_timeout_sec: float = Field(default=20.0)
    # 内置 embedding 服务 5 字段（v1.2）
    embedding_service_mode: str = Field(default="disabled")
    embedding_service_managed: bool = Field(default=False)
    embedding_service_model_id: str = Field(default="")
    embedding_service_port: int = Field(default=0, ge=0, le=65535)
    embedding_service_device: str = Field(default="cpu")
    # CUDA wheel 安装配置（v1.3.12，device=cuda 时生效；cpu/mps 忽略）
    embedding_service_pytorch_mirror: str = Field(default="https://download.pytorch.org/whl/")
    embedding_service_cuda_version: str = Field(default="cu124")
    rerank_enabled: bool = Field(default=False)
    rerank_api_key: str = Field(default="")
    rerank_base_url: str = Field(default="")
    rerank_model: str = Field(default="")
    rerank_path: str = Field(default="/rerank")
    rerank_timeout_sec: float = Field(default=20.0)
    enrichment_enabled: bool = Field(default=False)
    restart_required: bool = Field(default=False)
    runtime_port_managed_by: str | None = Field(default=None)
    updated_at: datetime | None = None


class SystemConfigUpsertRequest(BaseModel):
    # 多个内置客户端只提交自己编辑的字段；默认值只负责让请求进入 handler，
    # handler 会依据 model_fields_set 用数据库当前值补齐未显式提交的字段。
    api_base_url: str = Field(default="http://127.0.0.1:18000", min_length=1)
    service_port: int = Field(default=18000, ge=1, le=65535)
    grafana_url: str = Field(default="http://127.0.0.1:3000", min_length=1)
    ui_theme: str = Field(default="neo", pattern="^(linear|glass|neo)$")
    llm_enabled: bool = Field(default=False)
    llm_api_key: str = Field(default="")
    llm_base_url: str = Field(default="https://api.openai.com/v1")
    llm_model: str = Field(default="gpt-4o-mini")
    llm_timeout_sec: float = Field(default=30.0, ge=1, le=300)
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    llm_max_tokens: int = Field(default=1024, ge=1, le=8192)
    llm_max_tokens_auto: bool = Field(default=True)
    embedding_enabled: bool = Field(default=False)
    embedding_api_key: str = Field(default="")
    embedding_base_url: str = Field(default="")
    embedding_model: str = Field(default="")
    embedding_dim: int = Field(default=384, ge=1, le=65536)
    embedding_timeout_sec: float = Field(default=20.0, ge=1, le=300)
    # 内置 embedding 服务 5 字段（v1.2）；mode=local 时 base_url/model 写入会被拒（Batch E）
    embedding_service_mode: str = Field(default="disabled", pattern="^(disabled|local|external)$")
    embedding_service_managed: bool = Field(default=False)
    embedding_service_model_id: str = Field(default="")
    embedding_service_port: int = Field(default=0, ge=0, le=65535)
    embedding_service_device: str = Field(default="cpu", pattern="^(cpu|cuda|mps)$")
    # CUDA wheel 安装配置（v1.3.12）：device=cuda 时 pip 走 --index-url {mirror}{cuda_version}
    # 拉 cuda wheel。默认 PyTorch 官方 PEP 503 索引 + cu124（cp313+win_amd64 唯一覆盖）；
    # 国内用户慢请配代理，或在 setup 改 mirror 指向自建 PEP 503 兼容源。
    # 老驱动 (<530) 用户改 cu118；新驱动 (>=570) 用户改 cu128。
    embedding_service_pytorch_mirror: str = Field(
        default="https://download.pytorch.org/whl/",
        min_length=1,
        max_length=256,
        pattern=r"^https://[^\s]+/$",
    )
    embedding_service_cuda_version: str = Field(
        default="cu124",
        pattern=r"^cu\d{2,4}$",
    )
    # 改 mode / model_id 会触发 reindex，请求方需带 I-CONFIRM-REINDEX 才能放行
    confirm_reindex: str | None = Field(default=None, max_length=64)
    rerank_enabled: bool = Field(default=False)
    rerank_api_key: str = Field(default="")
    rerank_base_url: str = Field(default="")
    rerank_model: str = Field(default="")
    rerank_path: str = Field(default="/rerank")
    rerank_timeout_sec: float = Field(default=20.0, ge=1, le=300)
    enrichment_enabled: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Embedding service 控制面（v1.2 §3.2 / AC25）
#
# 三组模型对应 status / desired-state / actual-state 三端点；owner_token 走
# header `X-Embedding-Owner-Token`，不入 body 以免日志 / 转储泄漏。
# ---------------------------------------------------------------------------

class EmbeddingServiceStatusResponse(BaseModel):
    """GET /v1/system/embedding-service/status —— 汇总视图（前端 / 托盘菜单读）。"""
    mode: str = Field(default="disabled")            # disabled / local / external
    installed: bool = Field(default=False)
    running: bool = Field(default=False)
    warming_up: bool = Field(default=False)
    model_id: str = Field(default="")
    port: int = Field(default=0, ge=0, le=65535)
    pid: int | None = Field(default=None)
    device: str = Field(default="cpu")
    restart_count: int = Field(default=0, ge=0)
    last_health_check: float | None = Field(default=None)
    last_error: str = Field(default="")


class EmbeddingServiceDesiredStateResponse(BaseModel):
    """GET /v1/system/embedding-service/desired-state（内部，仅壳层）。"""
    action: str = Field(default="none")
    model_id: str = Field(default="")
    device: str = Field(default="cpu")
    enabled: bool = Field(default=False)
    # v1.3.12：cuda wheel 安装配置（device=cuda 时壳层据此拉 cuda wheel）
    pytorch_mirror: str = Field(default="https://download.pytorch.org/whl/")
    cuda_version: str = Field(default="cu124")
    generation: int = Field(default=0, ge=0)
    updated_at: float = Field(default=0.0)


class EmbeddingServiceActualStateRequest(BaseModel):
    """POST /v1/system/embedding-service/actual-state 请求体。"""
    acknowledged_generation: int = Field(ge=0)
    installed: bool
    running: bool
    warming_up: bool
    model_id: str = Field(default="")
    port: int = Field(default=0, ge=0, le=65535)
    pid: int | None = Field(default=None, ge=1)
    device: str = Field(default="cpu")
    restart_count: int = Field(default=0, ge=0)
    last_error: str = Field(default="")


class EmbeddingServiceActualStateResponse(BaseModel):
    accepted: bool = Field(default=True)
    acknowledged_generation: int = Field(ge=0)
    updated_at: float = Field(default=0.0)


class EmbeddingModelOption(BaseModel):
    """GET /v1/system/embedding-models 列表项(给 /setup 模型选择网格用)。"""
    key: str = Field(min_length=1)
    model_id: str
    display_name: str
    dim: int = Field(ge=1)
    size_bytes: int = Field(ge=0)
    ram_bytes: int = Field(ge=0)
    multilingual: bool
    recommended: bool = Field(default=False)


class EmbeddingModelsResponse(BaseModel):
    models: list[EmbeddingModelOption]
    default_key: str = Field(default="bge-m3")


class ReindexPreviewResponse(BaseModel):
    """GET /v1/system/reindex-preview 响应（给 /settings reindex 确认对话框用）。

    AC22 强制确认前必须告诉用户 chunk 数 + 预计耗时,本端点提供数据。
    """
    active_chunks: int = Field(ge=0)
    threshold_blocked_writes: bool   # 是否会触发 maintenance flag (≥ REINDEX_MAINTENANCE_THRESHOLD)
    estimated_seconds: int = Field(ge=0)   # 粗估,按 ~100 chunk/s 算
    threshold: int = Field(ge=0)


class EmbeddingServiceInstallRequest(BaseModel):
    """POST /v1/system/embedding-service/install 请求体。

    model_id 必填（如 ``bge-m3``）；device / mirror 可选，默认 cpu / hf-mirror。
    device=cuda 时 pytorch_mirror + cuda_version 决定 torch wheel 来源；cpu/mps 忽略。
    """
    model_id: str = Field(min_length=1, max_length=128)
    device: str = Field(default="cpu", pattern="^(cpu|cuda|mps)$")
    mirror: str | None = Field(default=None, max_length=256)
    pytorch_mirror: str | None = Field(
        default=None, max_length=256, pattern=r"^https://[^\s]+/$"
    )
    cuda_version: str | None = Field(default=None, pattern=r"^cu\d{2,4}$")


class EmbeddingServiceStartStopRequest(BaseModel):
    """POST /v1/system/embedding-service/start | stop 请求体（可空）。

    device 传空/None 表示沿用 prev desired（或 desired 空态 fallback DB config）；
    显式传 'cpu'/'mps'/'cuda' 覆盖。auto-bootstrap 场景（Mac Swift、Win Python
    壳层重启后 desired 归零）必须显式传 device，避免走 "cpu" 兜底吞掉 DB 里的
    mps/cuda 配置。
    """
    model_id: str = Field(default="", max_length=128)
    # 2026-07-02 P1 fix: 加 pattern 校验防脏值流入 desired.device;空串表示"沿用
    # prev / DB config",kb-api 端 3 级兜底。跨端一致(Win 传 cpu/cuda,Mac 传
    # cpu/mps/cuda 都命中此正则)。
    device: str = Field(default="", pattern="^(|cpu|cuda|mps)$")
    # manager 的 repair install 完成后续接 start 时携带；服务端只在 desired
    # 仍是该 generation 时接受，防用户较新的 stop 被旧 repair 回调覆盖。
    expected_generation: int | None = Field(default=None, ge=0)


class EmbeddingServiceInstallPlanResponse(BaseModel):
    """GET /v1/system/embedding-service/install-plan 响应。

    Mac Swift / Windows Python 壳层 ProcessManager 拉这个 plan 后实际执行
    （建 venv → pip 装 infinity-emb → snapshot_download 下模型 → 起 infinity）。
    设计动机：把 build_install_plan 作为单一真源，避免 Swift 端复刻一份导致漂移。
    """
    model_id: str               # HuggingFace repo id（如 BAAI/bge-m3）
    model_key: str              # MODEL_REGISTRY key（如 bge-m3）
    display_name: str
    dim: int
    venv_dir: str               # embedding-service/venv 绝对路径
    model_dir: str              # models/{key} 绝对路径
    device: str
    port: int                   # infinity 绑定端口（壳层探活 /health 用同值）
    create_venv_cmd: list[str]
    pip_install_cmd: list[str]
    download_args: dict[str, str]
    start_cmd: list[str]
    env: dict[str, str] = Field(default_factory=dict)  # 启动 infinity 时必须注入的 env（如 INFINITY_BETTERTRANSFORMER=false）
    device_detect_cmd: list[str] = Field(default_factory=list)


class EmbeddingServiceSwitchModelRequest(BaseModel):
    """POST /v1/system/embedding-service/switch-model 请求体（AC22）。

    切模型必然 dim/模型变 → 必须 reindex（design §4.5），故 confirm 强制。
    """
    model_id: str = Field(min_length=1, max_length=128)
    device: str = Field(default="cpu", pattern="^(cpu|cuda|mps)$")
    confirm: str = Field(min_length=1)
    pytorch_mirror: str | None = Field(
        default=None, max_length=256, pattern=r"^https://[^\s]+/$"
    )
    cuda_version: str | None = Field(default=None, pattern=r"^cu\d{2,4}$")


class EmbeddingServiceSwitchModelResponse(BaseModel):
    """切模型响应（202 Accepted）。

    ``next_action`` 告诉前端切完模型后需立即调用的端点（通常是 rebuild），
    避免前端逻辑分散。
    """
    action: str = Field(default="switch_model")
    model_id: str
    device: str
    generation: int = Field(ge=0)
    next_action: str = Field(default="POST /v1/system/rebuild-vector-index")


class RebuildVectorIndexRequest(BaseModel):
    """POST /v1/system/rebuild-vector-index 请求体（design v1.2 §4.5 / AC22）。

    ``confirm`` 必须严格等于 ``I-CONFIRM-OVERWRITE``（require_confirm_token 校验），
    防止前端误触发——重建期间语义检索降级到关键词。
    """
    confirm: str = Field(min_length=1)
    batch_size: int = Field(default=100, ge=1, le=10000)


class RebuildVectorIndexResponse(BaseModel):
    """POST /v1/system/rebuild-vector-index 响应（202 Accepted）。

    ``threshold_blocked_writes=True`` 表示本次 rebuild 触发 maintenance flag，
    写类 API 期间会返 202 + Retry-After（AC10）。前端可据此提示用户。
    """
    task_id: str
    status: str
    total: int
    threshold_blocked_writes: bool
    started_at: float


class RebuildVectorIndexStatusResponse(BaseModel):
    """GET /v1/system/rebuild-vector-index/status 响应。"""
    status: str           # idle / running / completed / failed / aborted
    task_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    total: int = 0
    processed: int = 0
    error: str = ""
    backup_path: str = ""
    threshold_blocked_writes: bool = False
