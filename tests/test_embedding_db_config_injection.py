"""验证 DB 模型配置（embedding / rerank）→ os.environ 注入路径的正确性。

覆盖以下回归点：
1. _apply_db_embedding_to_env：DB 字段为空时显式 pop env，防旧值残留
2. VectorIndex.from_repo：从 repo.system_config 拿 db_cfg 走 from_env(db_cfg=)
3. PUT /v1/system/config 后 repo 单例被清空，下次 get_repo 拿新 VectorIndex
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient


class _StubRepoWithConfig:
    """最小 repo stub：只暴露 get_system_config，验证 from_repo 流程。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self.calls = 0

    def get_system_config(self) -> dict[str, Any]:
        self.calls += 1
        return dict(self._config)


def test_apply_db_embedding_to_env_pops_empty_optional_fields(monkeypatch):
    """空字段必须显式从 env 中 pop，防止历史旧值残留导致脏配。"""
    from app.vector_index import _apply_db_embedding_to_env

    # 预设旧 env 模拟历史残留
    monkeypatch.setenv("KB_EMBEDDING_BASE_URL", "https://old-host.example/v1")
    monkeypatch.setenv("KB_EMBEDDING_API_KEY", "old-key")
    monkeypatch.setenv("KB_EMBEDDING_TIMEOUT_SEC", "30")
    monkeypatch.setenv("VECTOR_DIM", "768")

    # 新 DB 配置只填了 model，其他字段全空
    db_cfg = {
        "embedding_enabled": True,
        "embedding_model": "new-model",
        "embedding_api_key": "",
        "embedding_base_url": "",
        "embedding_timeout_sec": "",
        "embedding_dim": None,
    }

    db_dim = _apply_db_embedding_to_env(db_cfg)

    assert db_dim is None
    assert os.environ["KB_EMBEDDING_MODEL"] == "new-model"
    assert "KB_EMBEDDING_BASE_URL" not in os.environ
    assert "KB_EMBEDDING_API_KEY" not in os.environ
    assert "KB_EMBEDDING_TIMEOUT_SEC" not in os.environ
    assert "VECTOR_DIM" not in os.environ


def test_apply_db_embedding_to_env_writes_provided_fields(monkeypatch):
    """有值的字段必须写入 env，并返回 dim 给调用方覆盖默认。"""
    from app.vector_index import _apply_db_embedding_to_env

    # 清掉可能的旧 env
    for k in ("KB_EMBEDDING_BASE_URL", "KB_EMBEDDING_API_KEY",
              "KB_EMBEDDING_TIMEOUT_SEC", "VECTOR_DIM"):
        monkeypatch.delenv(k, raising=False)

    db_cfg = {
        "embedding_enabled": True,
        "embedding_model": "text-embedding-3-small",
        "embedding_api_key": "sk-xxx",
        "embedding_base_url": "https://api.example.com/v1",
        "embedding_timeout_sec": 60,
        "embedding_dim": 1536,
    }

    db_dim = _apply_db_embedding_to_env(db_cfg)

    assert db_dim == 1536
    assert os.environ["KB_EMBEDDING_ENABLED"] == "1"
    assert os.environ["KB_EMBEDDING_MODEL"] == "text-embedding-3-small"
    assert os.environ["KB_EMBEDDING_API_KEY"] == "sk-xxx"
    assert os.environ["KB_EMBEDDING_BASE_URL"] == "https://api.example.com/v1"
    assert os.environ["KB_EMBEDDING_TIMEOUT_SEC"] == "60"
    assert os.environ["VECTOR_DIM"] == "1536"


def test_apply_db_embedding_to_env_local_mode_redirects_to_infinity(monkeypatch):
    """mode=local 时强制指向本机 infinity，忽略远程 embedding_* 字段（bug3 回归）。

    场景：用户先配过远程豆包（embedding_base_url=ark.../v3），后从 /setup 切到本地 bge-m3。
    PUT /v1/system/config 因 mode=local 锁不改远程字段，但 vector_index 必须无视脏值，
    直接走 http://127.0.0.1:7687 + models/bge-m3 + dim=1024。
    """
    from app.vector_index import _apply_db_embedding_to_env

    # 预设脏 env 模拟用户切换前的远程配置残留
    monkeypatch.setenv("KB_EMBEDDING_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3")
    monkeypatch.setenv("KB_EMBEDDING_API_KEY", "ark-xxxxx")
    monkeypatch.setenv("VECTOR_DIM", "384")

    db_cfg = {
        # 老远程字段还在 DB 里（PUT /v1/system/config 在 mode=local 时锁字段，不让清）
        "embedding_enabled": True,
        "embedding_model": "doubao-embedding-vision",
        "embedding_base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "embedding_api_key": "ark-xxxxx",
        "embedding_dim": 384,
        # 新的本地配置字段
        "embedding_service_mode": "local",
        "embedding_service_model_id": "bge-m3",
        "embedding_service_port": 7687,
    }

    db_dim = _apply_db_embedding_to_env(db_cfg)

    assert db_dim == 1024
    assert os.environ["KB_EMBEDDING_ENABLED"] == "1"
    assert os.environ["KB_EMBEDDING_BASE_URL"] == "http://127.0.0.1:7687"
    assert os.environ["KB_EMBEDDING_MODEL"] == "models/bge-m3"
    assert os.environ["KB_EMBEDDING_API_KEY"] == "local-infinity"
    assert os.environ["VECTOR_DIM"] == "1024"


def test_apply_db_embedding_to_env_local_mode_default_port_when_zero(monkeypatch):
    """mode=local 时 port=0（DB 漂移） → 落到 DEFAULT_EMBEDDING_PORT 7687。"""
    from app.vector_index import _apply_db_embedding_to_env

    db_cfg = {
        "embedding_service_mode": "local",
        "embedding_service_model_id": "bge-m3",
        "embedding_service_port": 0,
    }
    _apply_db_embedding_to_env(db_cfg)
    assert os.environ["KB_EMBEDDING_BASE_URL"] == "http://127.0.0.1:7687"


def test_apply_db_embedding_to_env_local_mode_unknown_model_disables(monkeypatch):
    """mode=local 但 model_id 不在注册表 → KB_EMBEDDING_ENABLED=0，让上层退到 HashEmbedding 而非裸跑错配。"""
    from app.vector_index import _apply_db_embedding_to_env

    monkeypatch.setenv("KB_EMBEDDING_API_KEY", "sk-old")
    monkeypatch.setenv("VECTOR_DIM", "1024")

    db_cfg = {
        "embedding_service_mode": "local",
        "embedding_service_model_id": "totally-unknown-model",
        "embedding_service_port": 7687,
    }
    db_dim = _apply_db_embedding_to_env(db_cfg)
    assert db_dim is None
    assert os.environ["KB_EMBEDDING_ENABLED"] == "0"
    assert "KB_EMBEDDING_API_KEY" not in os.environ
    assert "VECTOR_DIM" not in os.environ


def test_apply_db_embedding_to_env_external_mode_keeps_remote_fields(monkeypatch):
    """mode=external 时沿用原远程 embedding_* 字段，不受 bug3 修复影响（回归保护）。"""
    from app.vector_index import _apply_db_embedding_to_env

    db_cfg = {
        "embedding_enabled": True,
        "embedding_model": "doubao-embedding-vision",
        "embedding_base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
        "embedding_api_key": "ark-xxx",
        "embedding_dim": 384,
        "embedding_service_mode": "external",
        "embedding_service_model_id": "",
        "embedding_service_port": 0,
    }
    db_dim = _apply_db_embedding_to_env(db_cfg)
    assert db_dim == 384
    assert os.environ["KB_EMBEDDING_BASE_URL"] == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert os.environ["KB_EMBEDDING_MODEL"] == "doubao-embedding-vision"


def test_apply_db_rerank_to_env_pops_empty_optional_fields(monkeypatch):
    """rerank 对称回归：空字段必须从 env pop。"""
    from app.reranker import _apply_db_rerank_to_env

    monkeypatch.setenv("KB_RERANK_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("KB_RERANK_PATH", "/v1/old/rerank")

    db_cfg = {
        "rerank_enabled": True,
        "rerank_model": "rerank-multilingual-v3",
        "rerank_api_key": "",
        "rerank_base_url": "",
        "rerank_path": "",
        "rerank_timeout_sec": None,
    }

    _apply_db_rerank_to_env(db_cfg)

    assert os.environ["KB_RERANK_MODEL"] == "rerank-multilingual-v3"
    assert "KB_RERANK_BASE_URL" not in os.environ
    assert "KB_RERANK_PATH" not in os.environ
    assert "KB_RERANK_API_KEY" not in os.environ


def test_vector_index_from_repo_reads_db_config_once(monkeypatch):
    """VectorIndex.from_repo 必须只调一次 repo.get_system_config，并把 db_cfg 注入 env。"""
    from app.vector_index import VectorIndex

    monkeypatch.setenv("VECTOR_ENABLED", "0")  # 关掉真实 qdrant 连接
    monkeypatch.delenv("KB_EMBEDDING_BASE_URL", raising=False)

    stub = _StubRepoWithConfig({
        "embedding_enabled": True,
        "embedding_model": "stub-model",
        "embedding_api_key": "stub-key",
        "embedding_base_url": "https://stub.example/v1",
    })

    vi = VectorIndex.from_repo(stub)

    assert stub.calls == 1
    assert os.environ["KB_EMBEDDING_MODEL"] == "stub-model"
    assert os.environ["KB_EMBEDDING_BASE_URL"] == "https://stub.example/v1"
    assert vi is not None


def test_vector_index_from_repo_tolerates_config_error(monkeypatch):
    """repo.get_system_config 抛错时必须回退 env 默认值，不阻塞 VectorIndex 创建。"""
    from app.vector_index import VectorIndex

    monkeypatch.setenv("VECTOR_ENABLED", "0")

    class _BrokenRepo:
        def get_system_config(self):
            raise RuntimeError("DB locked")

    vi = VectorIndex.from_repo(_BrokenRepo())
    assert vi is not None  # 不抛异常即通过


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[server]\nport = 18000\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_TOML_PATH", str(cfg_path))

    from app.main import _repo_singleton_postgres, _repo_singleton_sqlite
    _repo_singleton_sqlite.cache_clear()
    _repo_singleton_postgres.cache_clear()

    from app.main import app
    return TestClient(app)


def test_put_system_config_live_reloads_vector_index(client):
    """改完 /settings 必须让 vector_index 立即读到新配置。

    2026-07-01 方案 E：契约从"clear + pause 单例池"改成"live reload
    原地热刷新"——保持 QdrantClient(path=...) 单实例语义、绕开 portalocker
    AlreadyLocked 竞态。断言：
    - 单例池 **不清空**（老契约要求清空，本 PR 反转）
    - vi 对象身份不变（同一个 id）
    - vi.embedding 已按新 config 更新
    """
    from app.main import _repo_singletons
    from app.vector_index import HashEmbedding

    # 先触发一次 GET 让单例池命中
    resp = client.get("/v1/system/config")
    assert resp.status_code == 200
    assert len(_repo_singletons) == 1

    repo_before = next(iter(_repo_singletons.values()))
    vi_before = repo_before.vector_index
    vi_id_before = id(vi_before)

    # PUT 改配置：必填 api_base_url + grafana_url（min_length=1），其余字段用默认。
    put_resp = client.put("/v1/system/config", json={
        "api_base_url": "http://127.0.0.1:18000",
        "grafana_url": "http://127.0.0.1:3000",
        "embedding_enabled": False,
        "rerank_enabled": False,
        "llm_enabled": False,
    })
    assert put_resp.status_code == 200, put_resp.text

    # 新契约：单例池保留，vi 对象身份不变
    assert len(_repo_singletons) == 1, "live reload 契约禁止清空单例池"
    repo_after = next(iter(_repo_singletons.values()))
    assert repo_after is repo_before, "repo 对象跨 PUT 稳定"
    assert id(repo_after.vector_index) == vi_id_before, "vector_index 对象跨 PUT 稳定"
    # embedding_enabled=False → 退回 HashEmbedding
    assert isinstance(repo_after.vector_index.embedding, HashEmbedding)
