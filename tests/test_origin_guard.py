"""OriginGuardMiddleware CSRF 深度防御测试。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[server]\nport = 18000\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_TOML_PATH", str(cfg_path))

    from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
    _repo_singleton_sqlite.cache_clear()
    _repo_singleton_postgres.cache_clear()
    from app.services.maintenance import get_maintenance_flag
    get_maintenance_flag().clear()

    from app.main import app
    return TestClient(app)


def _upsert(client, headers=None):
    return client.post(
        "/v1/knowledge/items/upsert",
        json={
            "title": "t",
            "domain": "work",
            "project": "p",
            "type": "fact",
            "content_markdown": "c",
            "author": "wzt",
        },
        headers=headers or {},
    )


# ---------------------------------------------------------------------------
# 单元测试：should_block_request 纯函数
# ---------------------------------------------------------------------------


def test_loopback_origin_allowed():
    from app.services.origin_guard import should_block_request
    for origin in (
        "http://127.0.0.1:18000",
        "http://localhost",
        "http://localhost:5173",
        "https://127.0.0.1:8443",
        "http://[::1]:18000",          # IPv6 环回（审计补漏）
        "https://[::1]:8443",
    ):
        assert not should_block_request("POST", origin, None), origin


def test_external_origin_blocked():
    from app.services.origin_guard import should_block_request
    for origin in (
        "https://evil.com",
        "http://attacker.example",
        "http://127.0.0.1.evil.com",
        "null",
        "file://",
    ):
        assert should_block_request("POST", origin, None), origin


def test_missing_headers_allowed():
    """无 Origin 无 Referer（curl / server-to-server）放行。"""
    from app.services.origin_guard import should_block_request
    assert not should_block_request("POST", None, None)


def test_referer_used_when_no_origin():
    from app.services.origin_guard import should_block_request
    assert not should_block_request("POST", None, "http://127.0.0.1:18000/console")
    assert should_block_request("POST", None, "https://evil.com/attack.html")


def test_read_methods_never_blocked():
    """GET/HEAD 即便 Origin 是恶意来源也不拦截（无破坏副作用）。"""
    from app.services.origin_guard import should_block_request
    for method in ("GET", "HEAD", "OPTIONS"):
        assert not should_block_request(method, "https://evil.com", None)


# ---------------------------------------------------------------------------
# 集成测试：middleware 在 FastAPI 中行为
# ---------------------------------------------------------------------------


def test_post_with_external_origin_returns_403(client):
    resp = _upsert(client, headers={"Origin": "https://evil.com"})
    assert resp.status_code == 403
    assert "origin guard" in resp.text.lower()


def test_post_with_loopback_origin_passes(client):
    resp = _upsert(client, headers={"Origin": "http://127.0.0.1:18000"})
    assert resp.status_code != 403


def test_post_without_origin_passes(client):
    """模拟 curl / agent 调用，无 Origin 头。"""
    resp = _upsert(client)
    assert resp.status_code != 403


def test_get_with_external_origin_passes(client):
    """GET /health 即便有恶意 Origin 也不拦（无副作用）。"""
    resp = client.get("/health", headers={"Origin": "https://evil.com"})
    assert resp.status_code == 200


def test_backup_import_with_external_origin_blocked(client):
    """对核心受保护路由再做一次端到端验证。"""
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
        headers={"Origin": "https://attacker.example"},
    )
    assert resp.status_code == 403
