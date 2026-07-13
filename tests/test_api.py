"""HTTP API 端点集成测试（使用 FastAPI TestClient + SQLite 临时库）。"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

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
        assert data["finish_reason"] is None
        assert data["truncated"] is False
        assert isinstance(data["chunks_used"], list)

    def test_ask_exposes_provider_length_truncation(self, client, monkeypatch):
        from app.service import KnowledgeService

        configured = client.put(
            "/v1/system/config",
            json={
                "llm_enabled": True,
                "llm_api_key": "test-key",
                "llm_base_url": "https://llm.example/v1",
                "llm_model": "test-model",
                "llm_max_tokens_auto": True,
            },
        )
        assert configured.status_code == 200, configured.text
        _upsert(client, content_markdown="provider truncation contract")
        monkeypatch.setattr(
            KnowledgeService,
            "_call_llm",
            staticmethod(
                lambda *_args, **_kwargs: {
                    "answer": "partial answer",
                    "finish_reason": "length",
                }
            ),
        )

        response = client.post(
            "/v1/knowledge/ask",
            json={
                "question": "provider truncation",
                "domain": "work",
                "top_k": 3,
            },
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["answer"] == "partial answer"
        assert data["finish_reason"] == "length"
        assert data["truncated"] is True


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
        assert data["llm_max_tokens_auto"] is True
        assert data["restart_required"] is False
        assert data["runtime_port_managed_by"] is None

    def test_partial_put_preserves_llm_output_mode_and_token_value(self, client):
        configured = client.put(
            "/v1/system/config",
            json={"llm_max_tokens_auto": False, "llm_max_tokens": 2345},
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["llm_max_tokens_auto"] is False
        assert configured.json()["llm_max_tokens"] == 2345

        saved = client.put("/v1/system/config", json={"ui_theme": "glass"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["llm_max_tokens_auto"] is False
        assert saved.json()["llm_max_tokens"] == 2345

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

    def test_partial_put_accepts_missing_bootstrap_urls_and_preserves_them(self, client):
        """真实 partial PUT 必须先进入 handler，再由当前库值补齐启动字段。"""
        r0 = client.put("/v1/system/config", json={
            "api_base_url": "http://127.0.0.1:18000",
            "grafana_url": "http://grafana.internal:3000",
            "ui_theme": "neo",
        })
        assert r0.status_code == 200

        # /setup skip/external 当前都只发自己编辑的字段；修复前这里在进入
        # put_system_config 之前就因两个 URL 缺失而 422。
        r1 = client.put("/v1/system/config", json={"ui_theme": "glass"})
        assert r1.status_code == 200, r1.text
        data = r1.json()
        assert data["ui_theme"] == "glass"
        assert data["api_base_url"] == "http://127.0.0.1:18000"
        assert data["grafana_url"] == "http://grafana.internal:3000"

    def test_partial_put_rejects_explicit_empty_bootstrap_url(self, client):
        """允许省略不等于允许显式写入非法空值。"""
        r = client.put("/v1/system/config", json={"api_base_url": ""})
        assert r.status_code == 422

    def test_partial_put_revalidates_values_merged_from_database(
        self, client, monkeypatch
    ):
        """历史脏值不能借 model_fields_set 补齐绕过 Pydantic validator。"""
        from app.repository_sqlite import SqliteKnowledgeRepo

        original = SqliteKnowledgeRepo.get_system_config

        def _dirty_config(repo):
            cfg = original(repo)
            cfg["embedding_service_device"] = "not-a-device"
            return cfg

        monkeypatch.setattr(SqliteKnowledgeRepo, "get_system_config", _dirty_config)
        r = client.put("/v1/system/config", json={"ui_theme": "glass"})
        assert r.status_code == 422, r.text
        assert any(
            item.get("loc", [])[-1:] == ["embedding_service_device"]
            for item in r.json()["detail"]
        )

    def test_setup_external_partial_payload_with_confirm_succeeds(self, client):
        """模拟 /setup 外部 Embedding 原始 payload：无需先拼全量系统配置。"""
        r = client.put("/v1/system/config", json={
            "embedding_service_mode": "external",
            "embedding_enabled": True,
            "embedding_base_url": "https://embed.example/v1",
            "embedding_model": "embedding-v1",
            "embedding_api_key": "secret",
            "confirm_reindex": "I-CONFIRM-REINDEX",
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["embedding_service_mode"] == "external"
        assert data["embedding_enabled"] is True
        assert data["embedding_base_url"] == "https://embed.example/v1"
        assert data["embedding_model"] == "embedding-v1"

    def test_setup_skip_partial_payload_with_confirm_succeeds_from_local(self, client):
        """用户重新进入 /setup 选择跳过时，local → disabled 必须能保存。"""
        r0 = client.put("/v1/system/config", json={
            "api_base_url": "http://127.0.0.1:18000",
            "grafana_url": "http://127.0.0.1:3000",
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
            "embedding_enabled": True,
            "confirm_reindex": "I-CONFIRM-REINDEX",
        })
        assert r0.status_code == 200

        r1 = client.put("/v1/system/config", json={
            "embedding_service_mode": "disabled",
            "embedding_enabled": False,
            "confirm_reindex": "I-CONFIRM-REINDEX",
        })
        assert r1.status_code == 200, r1.text
        data = r1.json()
        assert data["embedding_service_mode"] == "disabled"
        assert data["embedding_enabled"] is False

    def test_explicit_disabled_mode_forces_embedding_off_when_flag_is_omitted(self, client):
        """mode 是三态总开关；旧客户端只切 mode 也不能留下 remote embedding。"""
        r0 = client.put("/v1/system/config", json={
            "embedding_service_mode": "external",
            "embedding_enabled": True,
            "embedding_base_url": "https://embed.example/v1",
            "embedding_model": "embedding-v1",
            "confirm_reindex": "I-CONFIRM-REINDEX",
        })
        assert r0.status_code == 200

        r1 = client.put("/v1/system/config", json={
            "embedding_service_mode": "disabled",
            "confirm_reindex": "I-CONFIRM-REINDEX",
        })
        assert r1.status_code == 200, r1.text
        assert r1.json()["embedding_enabled"] is False

    def test_explicit_external_mode_forces_embedding_on_when_flag_is_omitted(self, client):
        """只切到 external 时应真正启用已提交的远程配置。"""
        r = client.put("/v1/system/config", json={
            "embedding_service_mode": "external",
            "embedding_base_url": "https://embed.example/v1",
            "embedding_model": "embedding-v1",
            "confirm_reindex": "I-CONFIRM-REINDEX",
        })
        assert r.status_code == 200, r.text
        assert r.json()["embedding_enabled"] is True

    @pytest.mark.parametrize(
        ("mode", "submitted_managed", "expected_managed"),
        [
            ("local", False, True),
            ("external", True, False),
            ("disabled", True, False),
        ],
    )
    def test_embedding_service_mode_normalizes_conflicting_managed_flag(
        self,
        client,
        mode,
        submitted_managed,
        expected_managed,
    ):
        """managed 是 mode 的派生持久化值，客户端不能提交矛盾组合。"""
        payload = {
            "embedding_service_mode": mode,
            "embedding_service_managed": submitted_managed,
            "confirm_reindex": "I-CONFIRM-REINDEX",
        }
        if mode == "local":
            payload["embedding_service_model_id"] = "bge-m3"

        r = client.put("/v1/system/config", json=payload)

        assert r.status_code == 200, r.text
        assert r.json()["embedding_service_mode"] == mode
        assert r.json()["embedding_service_managed"] is expected_managed

    @pytest.mark.parametrize(
        ("mode", "canonical_managed", "conflicting_managed"),
        [
            ("local", True, False),
            ("external", False, True),
        ],
    )
    def test_partial_managed_only_put_is_normalized_from_current_mode(
        self,
        client,
        mode,
        canonical_managed,
        conflicting_managed,
    ):
        """只 PUT managed 也不能绕过当前 mode 的最终不变量。"""
        setup_payload = {
            "embedding_service_mode": mode,
            "embedding_service_managed": canonical_managed,
            "confirm_reindex": "I-CONFIRM-REINDEX",
        }
        if mode == "local":
            setup_payload["embedding_service_model_id"] = "bge-m3"
        setup = client.put("/v1/system/config", json=setup_payload)
        assert setup.status_code == 200, setup.text

        r = client.put(
            "/v1/system/config",
            json={"embedding_service_managed": conflicting_managed},
        )

        assert r.status_code == 200, r.text
        assert r.json()["embedding_service_mode"] == mode
        assert r.json()["embedding_service_managed"] is canonical_managed

    def test_partial_put_preserves_all_unedited_persistable_fields(self, client):
        """回归：PUT 是全量覆盖语义，但 /console 简版设置页 / /settings / 托盘菜单
        只 PUT 自己编辑过的部分字段。Pydantic 用 schema 默认值填充缺失字段会：

        1. 把 embedding_service_mode/model_id 误判成「用户想改 embedding 模型」→
           触发 I-CONFIRM-REINDEX 强制确认 → 保存任何配置都 400 报错；
        2. 静默清空 rerank / enrichment / embedding_service_* 等未编辑字段（True→False、
           有值→空、cuda→cpu、cu118→cu124 等）。

        修法：后端按 model_fields_set 统一兜底，凡是客户端没显式传入的可持久化字段，
        一律用当前库值补齐。本测试覆盖 P1 审核指出的全套字段丢失场景。
        """
        # 1. 建立一组非默认配置（覆盖 rerank / enrichment / embedding_service 全套）
        r0 = client.put("/v1/system/config", json={
            "ui_theme": "neo",
            "service_port": 18000,
            "api_base_url": "http://127.0.0.1:18000",
            "grafana_url": "http://127.0.0.1:3000",
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
            "embedding_service_managed": True,
            "embedding_service_port": 7997,
            "embedding_service_device": "cuda",
            "embedding_service_pytorch_mirror": "https://mirrors.example.com/whl/",
            "embedding_service_cuda_version": "cu118",
            "confirm_reindex": "I-CONFIRM-REINDEX",
            "rerank_enabled": True,
            "rerank_api_key": "rk-secret",
            "rerank_base_url": "https://rerank.example.com",
            "rerank_model": "bge-reranker-v2-m3",
            "rerank_path": "/v1/rerank",
            "rerank_timeout_sec": 15,
            "enrichment_enabled": True,
        })
        assert r0.status_code == 200

        # 2. 再 PUT：只改 ui_theme（模拟 /console / /settings 部分保存），完全不传
        #    embedding_service_* / rerank_* / enrichment_*
        r1 = client.put("/v1/system/config", json={
            "ui_theme": "glass",
            "service_port": 18000,
            "api_base_url": "http://127.0.0.1:18000",
            "grafana_url": "http://127.0.0.1:3000",
        })
        # 修前：400 confirm token（mode 误判）；修后：200
        assert r1.status_code == 200, r1.text
        d = r1.json()
        assert d["ui_theme"] == "glass"
        # embedding_service_* 全套保持不变（P1 #1）
        assert d["embedding_service_mode"] == "local"
        assert d["embedding_service_model_id"] == "bge-m3"
        assert d["embedding_service_managed"] is True
        assert d["embedding_service_port"] == 7997
        assert d["embedding_service_device"] == "cuda"
        assert d["embedding_service_pytorch_mirror"] == "https://mirrors.example.com/whl/"
        assert d["embedding_service_cuda_version"] == "cu118"
        # rerank / enrichment 不被清空（P1 #2）
        assert d["rerank_enabled"] is True
        assert d["rerank_api_key"] == "rk-secret"
        assert d["rerank_base_url"] == "https://rerank.example.com"
        assert d["rerank_model"] == "bge-reranker-v2-m3"
        assert d["rerank_path"] == "/v1/rerank"
        assert d["rerank_timeout_sec"] == 15
        assert d["enrichment_enabled"] is True

    def test_partial_put_does_not_break_explicit_embedding_change(self, client):
        """兜底不能过度：客户端显式传 embedding_service_mode 时（如 /setup 引导切
        local），必须仍走 confirm_reindex 校验，不能被兜底吞掉变 old==new。
        """
        # 先建立 disabled 默认态（无需 confirm）
        client.put("/v1/system/config", json={
            "ui_theme": "neo", "service_port": 18000,
            "api_base_url": "http://127.0.0.1:18000", "grafana_url": "http://127.0.0.1:3000",
        })
        # 显式切 local 但不带 confirm → 仍应 400
        r = client.put("/v1/system/config", json={
            "ui_theme": "neo", "service_port": 18000,
            "api_base_url": "http://127.0.0.1:18000", "grafana_url": "http://127.0.0.1:3000",
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
        })
        assert r.status_code == 400
        assert "I-CONFIRM-REINDEX" in r.json()["detail"]

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


def test_restart_windows_passes_script_path_via_environment(monkeypatch, tmp_path):
    """含撇号/空格的安装路径不得拼进 PowerShell 单引号命令。"""
    import app.main as main_mod

    fake_root = tmp_path / "Bob's Knowledge Base"
    scripts_dir = fake_root / "scripts"
    scripts_dir.mkdir(parents=True)
    restart_script = scripts_dir / "local-restart-direct.ps1"
    restart_script.write_text("# test", encoding="utf-8")

    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    monkeypatch.setattr(main_mod.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("KB_APP_ROOT", str(fake_root))

    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["run_cmd"] = cmd
        captured["run_kwargs"] = kwargs
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    def fake_popen(cmd, **kwargs):
        captured["popen_cmd"] = cmd
        captured["popen_kwargs"] = kwargs
        return type("P", (), {"pid": 12345})()

    monkeypatch.setattr(main_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(main_mod.subprocess, "Popen", fake_popen)

    assert main_mod.restart_local_service() == {"ok": True}

    for key in ("run_kwargs", "popen_kwargs"):
        env = captured[key]["env"]  # type: ignore[index]
        assert env["KB_RESTART_SCRIPT"] == str(restart_script)
    assert "$env:KB_RESTART_SCRIPT" in " ".join(captured["run_cmd"])  # type: ignore[arg-type]
    assert "$env:KB_RESTART_SCRIPT" in " ".join(captured["popen_cmd"])  # type: ignore[arg-type]
    assert str(restart_script) not in " ".join(captured["popen_cmd"])  # type: ignore[arg-type]


def test_restart_windows_parse_failure_never_starts_script(monkeypatch, tmp_path):
    """ParseFile 预检失败必须返回 500，且 fire-and-forget Popen 零调用。"""
    from fastapi import HTTPException
    import app.main as main_mod

    fake_root = tmp_path / "KnowledgeBase"
    scripts_dir = fake_root / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "local-restart-direct.ps1").write_text(
        "broken {", encoding="utf-8",
    )

    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    monkeypatch.setattr(main_mod.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("KB_APP_ROOT", str(fake_root))
    monkeypatch.setattr(
        main_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": 1, "stderr": "Missing closing '}'"},
        )(),
    )
    popen = MagicMock()
    monkeypatch.setattr(main_mod.subprocess, "Popen", popen)

    with pytest.raises(HTTPException) as exc_info:
        main_mod.restart_local_service()

    assert exc_info.value.status_code == 500
    assert "restart script parse failed" in str(exc_info.value.detail)
    popen.assert_not_called()


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
