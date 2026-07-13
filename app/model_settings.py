from __future__ import annotations

import os
from dataclasses import dataclass


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clamp_float(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _clamp_int(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LlmConfig:
    enabled: bool
    api_key: str
    base_url: str
    model: str
    timeout_sec: float
    temperature: float
    max_tokens: int
    max_tokens_auto: bool

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.model)


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool
    api_key: str
    base_url: str
    model: str
    timeout_sec: float
    dim: int

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.model)


@dataclass(frozen=True)
class RerankConfig:
    enabled: bool
    api_key: str
    base_url: str
    model: str
    path: str
    timeout_sec: float

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key) and bool(self.model)


def llm_config_from_env() -> LlmConfig:
    # 兼容升级：旧部署若显式配置过 KB_LLM_MAX_TOKENS，默认继续视为手动限制；
    # 全新环境没有旧变量时默认 auto，由供应商决定输出长度。
    auto_default = "KB_LLM_MAX_TOKENS" not in os.environ
    return LlmConfig(
        enabled=env_bool("KB_LLM_ENABLED", False),
        api_key=os.getenv("KB_LLM_API_KEY", "").strip(),
        base_url=os.getenv("KB_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        model=os.getenv("KB_LLM_MODEL", "gpt-4o-mini").strip(),
        timeout_sec=env_float("KB_LLM_TIMEOUT_SEC", 30.0),
        temperature=_clamp_float(env_float("KB_LLM_TEMPERATURE", 0.2), 0.0, 2.0),
        max_tokens=_clamp_int(env_int("KB_LLM_MAX_TOKENS", 1024), 1, 4096),
        max_tokens_auto=env_bool("KB_LLM_MAX_TOKENS_AUTO", auto_default),
    )


def embedding_config_from_env(default_dim: int) -> EmbeddingConfig:
    return EmbeddingConfig(
        enabled=env_bool("KB_EMBEDDING_ENABLED", False),
        api_key=(os.getenv("KB_EMBEDDING_API_KEY") or os.getenv("KB_LLM_API_KEY", "")).strip(),
        base_url=(
            os.getenv("KB_EMBEDDING_BASE_URL")
            or os.getenv("KB_LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/"),
        model=os.getenv("KB_EMBEDDING_MODEL", "").strip(),
        timeout_sec=env_float("KB_EMBEDDING_TIMEOUT_SEC", 20.0),
        dim=max(1, env_int("KB_EMBEDDING_DIM", default_dim)),
    )


def rerank_config_from_env() -> RerankConfig:
    path = os.getenv("KB_RERANK_PATH", "/rerank").strip() or "/rerank"
    if not path.startswith("/"):
        path = "/" + path
    return RerankConfig(
        enabled=env_bool("KB_RERANK_ENABLED", False),
        api_key=(os.getenv("KB_RERANK_API_KEY") or os.getenv("KB_LLM_API_KEY", "")).strip(),
        base_url=(
            os.getenv("KB_RERANK_BASE_URL")
            or os.getenv("KB_LLM_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/"),
        model=os.getenv("KB_RERANK_MODEL", "").strip(),
        path=path,
        timeout_sec=env_float("KB_RERANK_TIMEOUT_SEC", 20.0),
    )
