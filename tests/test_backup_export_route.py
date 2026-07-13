"""POST /v1/system/backup/export 路由测试。"""
from __future__ import annotations

from collections import namedtuple

import pytest
from fastapi.testclient import TestClient


_FakeUsage = namedtuple("DiskUsage", "total used free")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("QDRANT_LOCAL_PATH", str(tmp_path / "qdrant_local"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")

    (tmp_path / "qdrant_local").mkdir()
    (tmp_path / "qdrant_local" / "stub.bin").write_bytes(b"x")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[server]\nport = 18000\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_TOML_PATH", str(cfg_path))

    from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
    _repo_singleton_sqlite.cache_clear()
    _repo_singleton_postgres.cache_clear()

    from app.main import app
    return TestClient(app)


def test_export_endpoint_returns_gzip_stream(client):
    # 先 upsert 一条数据，触发 db 创建
    r = client.post("/v1/knowledge/items/upsert", json={
        "title": "t",
        "domain": "work",
        "project": "p",
        "type": "fact",
        "content_markdown": "c",
        "author": "wzt",
        "change_note": "init",
    })
    assert r.status_code == 200

    resp = client.post("/v1/system/backup/export")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/gzip")
    cd = resp.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "kb-backup-" in cd
    assert len(resp.content) > 0
    # tar.gz magic
    assert resp.content[:2] == b"\x1f\x8b"


def test_export_blocked_when_maintenance_active(client):
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag
    flag = get_maintenance_flag()
    flag.clear()
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="test")
    try:
        resp = client.post("/v1/system/backup/export")
        assert resp.status_code == 503
    finally:
        flag.clear()


def test_export_returns_507_on_insufficient_space(client, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _FakeUsage(1_000_000, 999_000, 1000))

    resp = client.post("/v1/system/backup/export")
    assert resp.status_code == 507, resp.text
    body = resp.json()
    # FastAPI 把 detail 包成对象时直接挂在 detail 下
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert "required_bytes" in detail
    assert "available_bytes" in detail


def test_export_rejects_postgres_backend(client, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "postgres")
    resp = client.post("/v1/system/backup/export")
    assert resp.status_code == 501, resp.text
    assert "sqlite" in resp.json()["detail"].lower()
