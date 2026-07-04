from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _allow_tmpdir_in_kb_data_roots(monkeypatch):
    """生产环境 _allowed_data_roots 不再默认包含 $TMPDIR（审计 #12 二次收紧），
    但测试在 pytest tmp_path / 其他 tmp 下创建 SQLite/qdrant，需要白名单注入。
    每个测试都通过 monkeypatch 临时把 tempfile.gettempdir() 加入 KB_DATA_ROOTS。
    """
    existing = monkeypatch.getenv("KB_DATA_ROOTS") if hasattr(monkeypatch, "getenv") else None
    # 简单：直接覆盖（测试不会依赖外部 KB_DATA_ROOTS）
    monkeypatch.setenv("KB_DATA_ROOTS", tempfile.gettempdir())


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    """清掉进程级单例残留：maintenance flag + rebuild_runner + embedding_service_state。

    背景：FastAPI 这些控制面用进程级单例存状态（maintenance flag / embedding state /
    rebuild runner），跨 test 不会自动复位。test_backup_import / test_origin_guard /
    test_pre_restore_recover 之类的 test 显式 clear，但 test_embedding_service_api
    的 client fixture 漏了，导致跑全套时前面 test set 的 flag 污染后面 rebuild 端点
    （writes 期间返 503）。集中在 conftest 清一次，任何 test 文件都不必各自管。
    """
    from app.services.maintenance import get_maintenance_flag
    get_maintenance_flag().clear()
    yield
    get_maintenance_flag().clear()


@pytest.fixture(autouse=True)
def _reset_embedding_env_leaks():
    """清掉 ``_apply_local_infinity_to_env`` / ``_apply_db_embedding_to_env`` 写进
    ``os.environ`` 的 embedding runtime 变量，避免跨 test 泄漏污染 ``VectorIndex.from_env``。

    背景：mode=local 的 PUT config / actual-state sync 会走这两个 helper，把
    ``KB_EMBEDDING_ENABLED=1`` / ``KB_EMBEDDING_BASE_URL=http://127.0.0.1:xxx``
    / ``KB_EMBEDDING_MODEL=models/bge-m3`` 等直接写 ``os.environ``。monkeypatch 只覆盖
    显式 setenv 的 key，不动 helper 写的 key——跨 test 泄漏后下一个 test 建 vi 时会拿到
    ``ApiEmbedding``，触发 ``_default_embedding_probe`` 走真 httpx（撞 cc 全局 SOCKS 代理
    env）报 socksio ImportError。集中在 conftest 前后各清一次，防止 test 顺序敏感。
    """
    import os

    _EMBED_ENV_KEYS = (
        "KB_EMBEDDING_ENABLED",
        "KB_EMBEDDING_API_KEY",
        "KB_EMBEDDING_BASE_URL",
        "KB_EMBEDDING_MODEL",
        "KB_EMBEDDING_TIMEOUT_SEC",
        "VECTOR_DIM",
    )

    for k in _EMBED_ENV_KEYS:
        os.environ.pop(k, None)
    yield
    for k in _EMBED_ENV_KEYS:
        os.environ.pop(k, None)


