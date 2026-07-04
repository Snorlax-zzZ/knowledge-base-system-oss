"""HTTP API 端点集成测试（使用 FastAPI TestClient + SQLite 临时库）。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[server]\nport = 18000\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_TOML_PATH", str(cfg_path))

    # 清除 lru_cache，避免跨测试共享 repo 实例
    from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
    _repo_singleton_sqlite.cache_clear()
    _repo_singleton_postgres.cache_clear()

    from app.main import app
    return TestClient(app)


def _upsert(client, **kw):
    payload = {
        "title": "Test",
        "domain": "work",
        "project": "proj-a",
        "type": "decision",
        "content_markdown": "content here",
        "summary": "summary",
        "author": "tester",
        "change_note": "init",
    }
    payload.update(kw)
    r = client.post("/v1/knowledge/items/upsert", json=payload)
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------

class TestUpsertEndpoint:
    def test_create_returns_id_and_version_1(self, client):
        data = _upsert(client)
        assert "knowledge_item_id" in data
        assert data["version"] == 1

    def test_update_increments_version(self, client):
        first = _upsert(client)
        iid = first["knowledge_item_id"]
        second = _upsert(client, knowledge_item_id=iid, title="Updated")
        assert second["knowledge_item_id"] == iid
        assert second["version"] == 2


# ---------------------------------------------------------------------------
# get_item
# ---------------------------------------------------------------------------

class TestGetItemEndpoint:
    def test_get_existing_item(self, client):
        created = _upsert(client, title="My Title", content_markdown="some text")
        iid = created["knowledge_item_id"]
        r = client.get(f"/v1/knowledge/items/{iid}")
        assert r.status_code == 200
        data = r.json()
        assert data["knowledge_item_id"] == iid
        assert data["title"] == "My Title"
        assert data["content_markdown"] == "some text"

    def test_get_nonexistent_returns_404(self, client):
        r = client.get("/v1/knowledge/items/nonexistent-id")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# console delete
# ---------------------------------------------------------------------------

class TestConsoleDeleteEndpoint:
    def test_delete_hides_item_from_console_and_search(self, client):
        created = _upsert(client, content_markdown="console delete target")
        iid = created["knowledge_item_id"]

        r = client.delete(f"/v1/console/knowledge/items/{iid}?actor=tester")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "knowledge_item_id": iid, "deleted": True}

        detail = client.get(f"/v1/knowledge/items/{iid}")
        assert detail.status_code == 404

        search = client.post("/v1/knowledge/search", json={
            "query": "console delete",
            "domain": "work",
            "top_k": 5,
        })
        assert search.status_code == 200
        assert search.json()["results"] == []

    def test_delete_unknown_item_returns_404(self, client):
        r = client.delete("/v1/console/knowledge/items/does-not-exist?actor=tester")
        assert r.status_code == 404

    def test_delete_without_actor_returns_422(self, client):
        created = _upsert(client)
        iid = created["knowledge_item_id"]
        r = client.delete(f"/v1/console/knowledge/items/{iid}")
        assert r.status_code == 422

    def test_upsert_with_deleted_id_returns_409(self, client):
        created = _upsert(client, content_markdown="will be deleted")
        iid = created["knowledge_item_id"]

        r = client.delete(f"/v1/console/knowledge/items/{iid}?actor=tester")
        assert r.status_code == 200

        # 试图用同 id 复活：应被拒绝
        resp = client.post("/v1/knowledge/items/upsert", json={
            "knowledge_item_id": iid,
            "title": "Resurrect attempt",
            "domain": "work",
            "project": "proj-a",
            "type": "decision",
            "content_markdown": "should not resurrect",
            "summary": "",
            "author": "tester",
            "change_note": "",
        })
        assert resp.status_code == 409
        assert "deleted" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearchEndpoint:
    def test_search_finds_upserted_item(self, client):
        _upsert(client, content_markdown="JWT refresh token strategy")
        r = client.post("/v1/knowledge/search", json={
            "query": "JWT token",
            "domain": "work",
            "top_k": 5,
        })
        assert r.status_code == 200
        data = r.json()
        assert "results" in data
        assert len(data["results"]) >= 1
        assert "trace_id" in data
        assert "knowledge_item_ids" in data

    def test_search_empty_db_returns_empty(self, client):
        r = client.post("/v1/knowledge/search", json={
            "query": "xyzzy nonexistent term",
            "domain": "work",
            "top_k": 5,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["results"] == []
        assert "trace_id" not in data

    def test_search_score_present(self, client):
        _upsert(client, content_markdown="python async programming guide")
        r = client.post("/v1/knowledge/search", json={
            "query": "python async",
            "domain": "work",
            "top_k": 5,
        })
        results = r.json()["results"]
        assert all("score" in item for item in results)


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

class TestAskEndpoint:
    def test_ask_without_llm_returns_chunks(self, client):
        _upsert(client, content_markdown="The answer is 42, always.")
        r = client.post("/v1/knowledge/ask", json={
            "question": "what is the answer",
            "domain": "work",
            "top_k": 3,
        })
        assert r.status_code == 200
        data = r.json()
        assert data["answer"] is None
        assert data["llm_available"] is False
        assert isinstance(data["chunks_used"], list)


# ---------------------------------------------------------------------------
# system config
# ---------------------------------------------------------------------------

class TestSystemConfigEndpoint:
    def test_get_returns_defaults(self, client):
        r = client.get("/v1/system/config")
        assert r.status_code == 200
        data = r.json()
        assert data["service_port"] == 18000
        assert data["llm_enabled"] is False
        assert data["restart_required"] is False
        assert data["runtime_port_managed_by"] is None

    def test_put_persists_and_returns_new_values(self, client):
        r = client.put("/v1/system/config", json={
            "ui_theme": "glass",
            "service_port": 19000,
            "api_base_url": "http://127.0.0.1:19000",
            "grafana_url": "http://127.0.0.1:3000",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["ui_theme"] == "glass"
        assert data["service_port"] == 19000
        assert data["restart_required"] is True
        assert data["runtime_port_managed_by"] is None
        assert data["updated_at"] is not None

    def test_put_missing_required_field_returns_422(self, client):
        r = client.put("/v1/system/config", json={"ui_theme": "glass"})
        assert r.status_code == 422

    def test_put_rolls_back_config_when_db_write_fails(self, client, monkeypatch):
        from app.repository_sqlite import SqliteKnowledgeRepo

        def _boom(_self, _payload):
            raise RuntimeError("db write failed")

        monkeypatch.setattr(SqliteKnowledgeRepo, "upsert_system_config", _boom)

        cfg_path = Path(os.environ["KB_CONFIG_TOML_PATH"])
        before = cfg_path.read_text(encoding="utf-8")

        with pytest.raises(RuntimeError, match="db write failed"):
            client.put("/v1/system/config", json={
                "ui_theme": "glass",
                "service_port": 19000,
                "api_base_url": "http://127.0.0.1:19000",
                "grafana_url": "http://127.0.0.1:3000",
            })

        after = cfg_path.read_text(encoding="utf-8")
        assert after == before


# ---------------------------------------------------------------------------
# MCP proxy HTTP endpoints
# ---------------------------------------------------------------------------

class TestMcpProxyHttpEndpoints:
    def test_import_incremental_endpoint(self, client, monkeypatch):
        from app.mcp_tools import KnowledgeMcpTools

        monkeypatch.setattr(
            KnowledgeMcpTools,
            "import_incremental_knowledge",
            lambda self, directory, project, domain, knowledge_type: {
                "ok": True,
                "op": "import_incremental_knowledge",
                "directory": directory,
                "project": project,
                "domain": domain,
                "knowledge_type": knowledge_type,
            },
        )
        r = client.post("/v1/knowledge/import-incremental", json={
            "directory": "/tmp/incr",
            "project": "proj-a",
            "domain": "work",
            "knowledge_type": "fact",
        })
        assert r.status_code == 200
        assert r.json()["op"] == "import_incremental_knowledge"

    def test_export_package_endpoint(self, client, monkeypatch):
        from app.mcp_tools import KnowledgeMcpTools

        monkeypatch.setattr(
            KnowledgeMcpTools,
            "export_knowledge_package",
            lambda self, export_dir=None: {"ok": True, "op": "export_knowledge_package", "export_dir": export_dir},
        )
        r = client.post("/v1/knowledge/export-package", json={"export_dir": "/tmp/exports"})
        assert r.status_code == 200
        assert r.json()["op"] == "export_knowledge_package"

    def test_import_package_endpoint(self, client, monkeypatch):
        from app.mcp_tools import KnowledgeMcpTools

        monkeypatch.setattr(
            KnowledgeMcpTools,
            "import_knowledge_package",
            lambda self, package_path, confirm=False: {
                "ok": True,
                "op": "import_knowledge_package",
                "package_path": package_path,
                "confirm": confirm,
            },
        )
        r = client.post("/v1/knowledge/import-package", json={"package_path": "/tmp/pkg.zip", "confirm": True})
        assert r.status_code == 200
        assert r.json()["op"] == "import_knowledge_package"

    def test_clear_knowledge_base_endpoint(self, client, monkeypatch):
        from app.mcp_tools import KnowledgeMcpTools

        monkeypatch.setattr(
            KnowledgeMcpTools,
            "clear_knowledge_base",
            lambda self, confirm=False, backup_dir=None: {
                "ok": True,
                "op": "clear_knowledge_base",
                "confirm": confirm,
                "backup_dir": backup_dir,
            },
        )
        r = client.post("/v1/knowledge/clear", json={"confirm": True, "backup_dir": "/tmp/backup"})
        assert r.status_code == 200
        assert r.json()["op"] == "clear_knowledge_base"

    def test_cleanup_expired_knowledge_endpoint(self, client, monkeypatch):
        from app.mcp_tools import KnowledgeMcpTools

        monkeypatch.setattr(
            KnowledgeMcpTools,
            "cleanup_expired_knowledge",
            lambda self, mode="archive", as_of=None, backup_dir=None, confirm=False: {
                "ok": True,
                "op": "cleanup_expired_knowledge",
                "mode": mode,
                "as_of": as_of,
                "backup_dir": backup_dir,
                "confirm": confirm,
            },
        )
        r = client.post("/v1/knowledge/cleanup-expired", json={
            "mode": "delete",
            "as_of": "2026-05-07",
            "backup_dir": "/tmp/expired",
            "confirm": True,
        })
        assert r.status_code == 200
        assert r.json()["op"] == "cleanup_expired_knowledge"


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------

def test_restart_returns_409_for_docker_mode(monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "postgres")
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/v1/system/restart")

    assert r.status_code == 409
    assert "docker compose restart" in r.json()["detail"]


def test_restart_requires_explicit_backend(monkeypatch):
    monkeypatch.delenv("KB_BACKEND", raising=False)
    from app.main import app

    with TestClient(app) as c:
        r = c.post("/v1/system/restart")

    assert r.status_code == 500
    assert "KB_BACKEND is not configured" in r.json()["detail"]


# --- mac restart 路径解析（直装版 scripts/ 优先 + 开发模式 mac-app/ fallback） ---

def _mac_restart_setup(monkeypatch, tmp_path, *, scripts_exists: bool, mac_app_exists: bool):
    """伪造 root_dir 布局让 main.restart 走对应分支，并 mock subprocess.Popen。"""
    import app.main as main_mod

    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")

    # restart_local_service 用 _resolve_data_root() 当 root_dir(优先 KB_APP_ROOT
    # 环境变量,fallback 到 APP_DIR.parent)。之前 test 只 patch APP_DIR 未设
    # KB_APP_ROOT,导致 fallback 到真项目根 → 拿到真 mac-app/restart.sh。
    fake_root = tmp_path / "root"
    (fake_root / "app").mkdir(parents=True)
    if scripts_exists:
        (fake_root / "scripts").mkdir()
        (fake_root / "scripts" / "restart.sh").write_text("#!/bin/sh\n")
    if mac_app_exists:
        (fake_root / "mac-app").mkdir()
        (fake_root / "mac-app" / "restart.sh").write_text("#!/bin/sh\n")

    monkeypatch.setattr(main_mod, "APP_DIR", fake_root / "app")
    monkeypatch.setenv("KB_APP_ROOT", str(fake_root))

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return type("P", (), {"pid": 12345})()

    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)
    return main_mod.app, fake_root, captured


def test_restart_mac_prefers_scripts_path(monkeypatch, tmp_path):
    """直装版：scripts/restart.sh 存在时，主路径用它（与 dmg payload 布局对齐）。"""
    app, fake_root, captured = _mac_restart_setup(
        monkeypatch, tmp_path, scripts_exists=True, mac_app_exists=True
    )
    with TestClient(app) as c:
        r = c.post("/v1/system/restart")

    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert captured["cmd"] == ["/bin/bash", str(fake_root / "scripts" / "restart.sh")]


def test_restart_mac_falls_back_to_mac_app_path(monkeypatch, tmp_path):
    """开发模式：scripts/restart.sh 不存在 → fallback 到 mac-app/restart.sh。"""
    app, fake_root, captured = _mac_restart_setup(
        monkeypatch, tmp_path, scripts_exists=False, mac_app_exists=True
    )
    with TestClient(app) as c:
        r = c.post("/v1/system/restart")

    assert r.status_code == 200
    assert captured["cmd"] == ["/bin/bash", str(fake_root / "mac-app" / "restart.sh")]


def test_restart_mac_returns_404_when_no_script(monkeypatch, tmp_path):
    """两条路径都不存在：404，不是 501（避免误导成"未实现"）。"""
    app, _, _ = _mac_restart_setup(
        monkeypatch, tmp_path, scripts_exists=False, mac_app_exists=False
    )
    with TestClient(app) as c:
        r = c.post("/v1/system/restart")

    assert r.status_code == 404
    assert "restart script not found" in r.json()["detail"]


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "python_gc" in r.text
