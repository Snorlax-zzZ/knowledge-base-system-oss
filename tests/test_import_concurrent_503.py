"""import 期间并发写返 503（maintenance middleware 行为校验）。"""
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

    from app.main import app
    return TestClient(app)


def test_concurrent_upsert_during_import_returns_503(client):
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag

    flag = get_maintenance_flag()
    flag.clear()
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="simulated long import")
    try:
        resp = client.post(
            "/v1/knowledge/items/upsert",
            json={
                "title": "blocked",
                "domain": "work",
                "type": "fact",
                "content_markdown": "x",
                "author": "wzt",
            },
        )
        assert resp.status_code == 503
        assert resp.headers.get("retry-after") == "60"
    finally:
        flag.clear()


def test_concurrent_delete_during_import_returns_503(client):
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag

    flag = get_maintenance_flag()
    flag.clear()
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="simulated long import")
    try:
        resp = client.delete("/v1/knowledge/items/some-id")
        assert resp.status_code == 503
    finally:
        flag.clear()


def test_flag_set_race_in_route_returns_503(client):
    """中间件检查后到 flag.set 之间的竞态：第二个 import 必须 503 而不是 500（审计 #5）。

    在 service env 内手工事先 set flag，模拟"中间件已放行（因为 set 在 dispatch
    后才发生）但路由层尝试 set 抢占失败"——必须 503 + Retry-After。
    """
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag

    flag = get_maintenance_flag()
    flag.clear()
    # 模拟另一个 import 已经 set 了 flag（实际场景里中间件正常会拦下，但
    # 在 dispatch 那一刻 flag 还没 set，等到达路由再 set 时就晚了）
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="another import already running")
    try:
        resp = client.post(
            "/v1/system/backup/import",
            data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
            files={"file": ("x.tar.gz", b"x", "application/gzip")},
        )
        # 这里 middleware 已经拦住 503 了（因为路由本身是写类），但即使被路由
        # 接到，路由内 flag.set 也会抛 RuntimeError → 路由 except 转 503
        assert resp.status_code == 503
        # 不论走哪条路径，Retry-After 都应该存在（middleware 写的）
        assert resp.headers.get("retry-after") == "60"
    finally:
        flag.clear()


def test_route_flag_set_race_when_middleware_bypassed(monkeypatch, client):
    """真测路由层 flag.set 失败分支（绕过 middleware；审计补漏 #16）。

    上一条测试因 middleware 先一步拦下 503，实际命中的是 middleware；本测试
    monkeypatch flag.is_active 让 middleware 误以为未置位，请求进入路由后再
    由 import_full_backup 内部调用 flag.set 时抛 RuntimeError，必须被
    except RuntimeError 捕获并转 503 + Retry-After: 60。
    """
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag

    flag = get_maintenance_flag()
    flag.clear()
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="held by another import")

    real_is_active = flag.is_active

    def fake_is_active():
        return False  # 强制 middleware 放行

    monkeypatch.setattr(flag, "is_active", fake_is_active)
    try:
        resp = client.post(
            "/v1/system/backup/import",
            data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
            files={"file": ("x.tar.gz", b"x", "application/gzip")},
        )
        # 路由内 flag.set 抢占失败 → except → 503 + Retry-After
        assert resp.status_code == 503, resp.text
        assert resp.headers.get("retry-after") == "60"
        body = resp.json()
        assert "maintenance" in body["detail"].lower()
    finally:
        monkeypatch.undo()
        flag.clear()


def test_concurrent_search_during_import_passes(client):
    """search 是只读，应该放行。"""
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag

    flag = get_maintenance_flag()
    flag.clear()
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="simulated long import")
    try:
        resp = client.post(
            "/v1/knowledge/search",
            json={"query": "anything", "domain": "work"},
        )
        assert resp.status_code == 200
    finally:
        flag.clear()
