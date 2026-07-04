"""Embedding service 控制面 API 测试（design v1.2 §3.2 + AC25 + AC26）。

Batch A 覆盖：status + desired-state + actual-state 三端点 + owner token + generation 校验。
后续 Batch B/C/D/E/F 在本文件按 class 分组追加。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """复用 test_api.py 同款 fixture（SQLite 临时库 + 关闭 vector）。"""
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[server]\nport = 18000\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_TOML_PATH", str(cfg_path))

    from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
    _repo_singleton_sqlite.cache_clear()
    _repo_singleton_postgres.cache_clear()

    from app.services.embedding_service_state import get_embedding_service_state
    # 每个测试给 fresh 控制面状态（避免跨用例的 generation / actual 残留）。
    get_embedding_service_state().reset_for_tests()

    from app.main import app
    return TestClient(app)


def _owner_token() -> str:
    from app.services.embedding_service_state import get_embedding_service_state
    return get_embedding_service_state().owner_token


# ---------------------------------------------------------------------------
# GET /v1/system/embedding-models —— Phase 4 /setup 用模型注册表
# ---------------------------------------------------------------------------


class TestEmbeddingModelsList:
    def test_returns_registry_items(self, client):
        r = client.get("/v1/system/embedding-models")
        assert r.status_code == 200
        body = r.json()
        assert "models" in body and "default_key" in body
        assert len(body["models"]) >= 1
        keys = {m["key"] for m in body["models"]}
        assert "bge-m3" in keys, "MODEL_REGISTRY 默认应含 bge-m3"

    def test_default_model_marked_recommended(self, client):
        body = client.get("/v1/system/embedding-models").json()
        default_key = body["default_key"]
        recommended = [m for m in body["models"] if m["recommended"]]
        assert len(recommended) == 1
        assert recommended[0]["key"] == default_key

    def test_item_fields_complete(self, client):
        body = client.get("/v1/system/embedding-models").json()
        m = body["models"][0]
        for field in ("key", "model_id", "display_name", "dim", "size_bytes",
                      "ram_bytes", "multilingual", "recommended"):
            assert field in m, f"列表项缺字段 {field}"


# ---------------------------------------------------------------------------
# GET /v1/system/reindex-preview —— Phase 4 reindex 确认对话框数据
# ---------------------------------------------------------------------------


class TestReindexPreview:
    def test_returns_zero_on_empty_kb(self, client):
        r = client.get("/v1/system/reindex-preview")
        assert r.status_code == 200
        body = r.json()
        assert body["active_chunks"] == 0
        assert body["threshold_blocked_writes"] is False
        assert body["threshold"] > 0

    def test_estimated_seconds_present(self, client):
        body = client.get("/v1/system/reindex-preview").json()
        assert body["estimated_seconds"] >= 1  # 即使 0 chunk 也给 1s 兜底


# ---------------------------------------------------------------------------
# GET /v1/system/embedding-service/status —— 汇总视图，所有人可读
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_returns_defaults_on_fresh_db(self, client):
        r = client.get("/v1/system/embedding-service/status")
        assert r.status_code == 200
        body = r.json()
        # 默认 DB 未配 embedding service → mode=disabled，actual 全空
        assert body["mode"] == "disabled"
        assert body["installed"] is False
        assert body["running"] is False
        assert body["warming_up"] is False
        assert body["model_id"] == ""
        assert body["port"] == 0
        assert body["pid"] is None
        assert body["device"] == "cpu"
        assert body["restart_count"] == 0

    def test_reflects_db_config_when_actual_unset(self, client):
        """壳层还没回写 actual-state 时，model_id/port/device 退回到 DB 配置。"""
        # 先把 DB 切到 local 模式（mode/model 变 → 必须 confirm_reindex）
        cfg = client.get("/v1/system/config").json()
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["embedding_service_port"] = 7687
        cfg["embedding_service_device"] = "cpu"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200

        body = client.get("/v1/system/embedding-service/status").json()
        assert body["mode"] == "local"
        assert body["model_id"] == "bge-m3"
        assert body["port"] == 7687
        assert body["installed"] is False  # actual 还没回写
        assert body["running"] is False

    def test_actual_state_overrides_db_config(self, client):
        """壳层回写后，status 用 actual 的 model_id/port 覆盖 DB 默认。"""
        token = _owner_token()
        r = client.post(
            "/v1/system/embedding-service/actual-state",
            headers={"X-Embedding-Owner-Token": token},
            json={
                "acknowledged_generation": 0,
                "installed": True,
                "running": True,
                "warming_up": False,
                "model_id": "bge-large-zh-v1.5",
                "port": 7688,
                "pid": 12345,
                "device": "cpu",
                "restart_count": 1,
            },
        )
        assert r.status_code == 200

        body = client.get("/v1/system/embedding-service/status").json()
        assert body["installed"] is True
        assert body["running"] is True
        assert body["model_id"] == "bge-large-zh-v1.5"
        assert body["port"] == 7688
        assert body["pid"] == 12345
        assert body["restart_count"] == 1


# ---------------------------------------------------------------------------
# GET /v1/system/embedding-service/desired-state —— 内部，owner token 必须
# ---------------------------------------------------------------------------

class TestDesiredStateEndpoint:
    def test_requires_owner_token(self, client):
        r = client.get("/v1/system/embedding-service/desired-state")
        assert r.status_code == 403

    def test_rejects_wrong_owner_token(self, client):
        r = client.get(
            "/v1/system/embedding-service/desired-state",
            headers={"X-Embedding-Owner-Token": "wrong-token"},
        )
        assert r.status_code == 403

    def test_returns_initial_desired_state(self, client):
        token = _owner_token()
        r = client.get(
            "/v1/system/embedding-service/desired-state",
            headers={"X-Embedding-Owner-Token": token},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "none"
        assert body["generation"] == 0
        assert body["enabled"] is False


# ---------------------------------------------------------------------------
# POST /v1/system/embedding-service/actual-state —— 壳层回写，owner token + gen
# ---------------------------------------------------------------------------

class TestActualStateEndpoint:
    def _post_actual(self, client, token, *, generation=0, **overrides):
        payload = {
            "acknowledged_generation": generation,
            "installed": False,
            "running": False,
            "warming_up": False,
            "model_id": "",
            "port": 0,
            "pid": None,
            "device": "cpu",
            "restart_count": 0,
        }
        payload.update(overrides)
        return client.post(
            "/v1/system/embedding-service/actual-state",
            headers={"X-Embedding-Owner-Token": token},
            json=payload,
        )

    def test_requires_owner_token(self, client):
        r = self._post_actual(client, "")
        assert r.status_code == 403

    def test_rejects_wrong_owner_token(self, client):
        r = self._post_actual(client, "evil-token")
        assert r.status_code == 403

    def test_accepts_valid_token(self, client):
        r = self._post_actual(client, _owner_token(), running=True)
        assert r.status_code == 200
        body = r.json()
        assert body["accepted"] is True
        assert body["acknowledged_generation"] == 0

    def test_rejects_stale_generation(self, client):
        """壳层先回写 gen=2，再用 gen=1 回写应被拒（防旧覆盖新）。"""
        token = _owner_token()
        assert self._post_actual(client, token, generation=2).status_code == 200
        r = self._post_actual(client, token, generation=1)
        assert r.status_code == 409

    def test_accepts_same_generation(self, client):
        """同一 generation 多次回写应被接受（壳层周期性 heartbeat）。"""
        token = _owner_token()
        assert self._post_actual(client, token, generation=3).status_code == 200
        assert self._post_actual(client, token, generation=3).status_code == 200

    def test_payload_validation_rejects_bad_port(self, client):
        token = _owner_token()
        r = client.post(
            "/v1/system/embedding-service/actual-state",
            headers={"X-Embedding-Owner-Token": token},
            json={
                "acknowledged_generation": 0,
                "installed": False,
                "running": False,
                "warming_up": False,
                "port": 99999,
            },
        )
        assert r.status_code == 422

    def test_running_actual_syncs_port_and_dim_to_db_config(self, client):
        """2026-07-01 bug fix：actual-state 回写 running=True + port>0 时，
        必须自动 sync 到 system_config，让 kb-api VectorIndex 走真 embedding
        而不是 hash fallback（老 bug：install 后 port 保持 0/dim 保持 384，
        VectorIndex 每次 embed 都撞 base_url 空 → 静默 HashEmbedding，
        install_status.json completed 但语义搜索假 work）。
        """
        # seed DB config：mode=local + model=bge-m3；port/dim 保留默认 0 / 384
        cfg = client.get("/v1/system/config").json()
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"  # mode/model 变触发 §2.9
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        pre = client.get("/v1/system/config").json()
        assert pre["embedding_service_port"] == 0
        assert pre["embedding_dim"] == 384
        assert pre["embedding_enabled"] is False

        # 壳层回写 running=True port=7687（infinity 起来后的心跳）
        r = self._post_actual(
            client, _owner_token(),
            generation=1, installed=True, running=True,
            model_id="D:\\KnowledgeBase\\models\\bge-m3", port=7687,
            device="cuda",
        )
        assert r.status_code == 200

        # 期望：port/dim/enabled 全被 sync
        after = client.get("/v1/system/config").json()
        assert after["embedding_service_port"] == 7687
        assert after["embedding_dim"] == 1024, "bge-m3 的 dim 必须从 MODEL_REGISTRY 拿到 1024"
        assert after["embedding_enabled"] is True

    def test_actual_state_not_running_does_not_touch_config(self, client):
        """running=False 时不能动 DB config（防未 ready 时 flapping 反复覆盖）。"""
        cfg = client.get("/v1/system/config").json()
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        r = self._post_actual(
            client, _owner_token(),
            generation=1, installed=True, running=False,
            port=7687,  # 有 port 但 running=False
            last_error="infinity exited during warmup with code 3",
        )
        assert r.status_code == 200

        after = client.get("/v1/system/config").json()
        assert after["embedding_service_port"] == 0
        assert after["embedding_dim"] == 384
        assert after["embedding_enabled"] is False

    def test_running_actual_sync_is_idempotent(self, client):
        """同样的 running actual 多次回写 config 不应重复变化（幂等）。"""
        cfg = client.get("/v1/system/config").json()
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        token = _owner_token()
        for gen in (1, 2, 3):
            r = self._post_actual(
                client, token,
                generation=gen, installed=True, running=True,
                port=7687, device="cuda",
            )
            assert r.status_code == 200

        after = client.get("/v1/system/config").json()
        assert after["embedding_service_port"] == 7687
        assert after["embedding_dim"] == 1024
        assert after["embedding_enabled"] is True


# ---------------------------------------------------------------------------
# POST install / start / stop —— 编排端点
# ---------------------------------------------------------------------------

class TestInstallEndpoint:
    def test_install_rejects_unknown_model(self, client):
        r = client.post(
            "/v1/system/embedding-service/install",
            json={"model_id": "no-such-model"},
        )
        assert r.status_code == 400

    def test_install_bumps_desired_state(self, client, tmp_path, monkeypatch):
        """install 写期望状态后应可在 desired-state 端点读到。

        预置一个 ``phase=completed`` 的状态文件让 SSE 立刻终止，避免 TestClient
        被 streamer 默认 30 分钟硬上限阻塞。
        """
        import json as _json
        monkeypatch.setenv("KB_APP_ROOT", str(tmp_path))
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "install_status.json").write_text(
            _json.dumps({"phase": "completed"})
        )

        r = client.post(
            "/v1/system/embedding-service/install",
            json={"model_id": "bge-m3", "device": "cpu"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")

        # desired 应已被 bump：action=install, model_id=bge-m3, generation=1
        token = _owner_token()
        body = client.get(
            "/v1/system/embedding-service/desired-state",
            headers={"X-Embedding-Owner-Token": token},
        ).json()
        assert body["action"] == "install"
        assert body["model_id"] == "bge-m3"
        assert body["device"] == "cpu"
        assert body["enabled"] is True
        assert body["generation"] == 1

    def test_install_sse_emits_initial_status(self, client, tmp_path, monkeypatch):
        """壳层已写 install_status.json 时 SSE 第一帧应转发该快照。"""
        import json as _json
        monkeypatch.setenv("KB_APP_ROOT", str(tmp_path))
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        status_file = runtime_dir / "install_status.json"
        # phase=completed 让 streamer 立刻终止，TestClient 才不会无限等
        status_file.write_text(_json.dumps({"phase": "completed", "progress": 1.0}))

        r = client.post(
            "/v1/system/embedding-service/install",
            json={"model_id": "bge-m3"},
        )
        body = r.text
        assert "event: status" in body
        assert '"phase": "completed"' in body


class TestStartStopEndpoints:
    def test_start_bumps_action_to_start(self, client):
        r = client.post("/v1/system/embedding-service/start", json={"model_id": "bge-m3"})
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "start"
        assert body["model_id"] == "bge-m3"
        assert body["enabled"] is True
        assert body["generation"] == 1

    def test_start_inherits_previous_model_id(self, client):
        # 先 install 把 model_id 记到 desired
        client.post("/v1/system/embedding-service/start", json={"model_id": "bge-m3"})
        # 再 stop 不传 model_id，应保留 bge-m3
        r = client.post("/v1/system/embedding-service/stop")
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "stop"
        assert body["model_id"] == "bge-m3"
        assert body["enabled"] is False
        assert body["generation"] == 2

    def test_generation_monotonic_across_endpoints(self, client):
        """连续 start → stop → start 应让 generation 单调递增。"""
        g1 = client.post("/v1/system/embedding-service/start",
                         json={"model_id": "bge-m3"}).json()["generation"]
        g2 = client.post("/v1/system/embedding-service/stop").json()["generation"]
        g3 = client.post("/v1/system/embedding-service/start").json()["generation"]
        assert g1 == 1 and g2 == 2 and g3 == 3


# ---------------------------------------------------------------------------
# POST rebuild-vector-index + abort + status —— design v1.2 §4.5 / AC10 / AC23
# ---------------------------------------------------------------------------

@pytest.fixture
def rebuild_client(client, monkeypatch):
    """rebuild 端点 fixture：注入 stub rebuild_fn 绕开真实 embedding 服务。"""
    import app.main as main_mod
    from app.services.rebuild_runner import get_rebuild_runner

    # 每次 reset runner 单例 + maintenance flag
    get_rebuild_runner().reset_for_tests()

    def stub_rebuild(repo, vi, *, batch_size, progress_cb):
        progress_cb(batch_size, batch_size)

    monkeypatch.setattr(main_mod, "_REBUILD_FN_OVERRIDE", stub_rebuild)
    # backup / restore 也 stub 掉，避免真去拷贝 data/qdrant_local，跨测试触发
    # FileExistsError（同一秒钟两次默认 TS 相同）
    monkeypatch.setattr(main_mod, "_BACKUP_FN_OVERRIDE", lambda s, d: None)
    monkeypatch.setattr(main_mod, "_RESTORE_FN_OVERRIDE", lambda b, q: None)
    return client


class TestRebuildEndpoint:
    def test_rebuild_requires_confirm_token(self, rebuild_client):
        r = rebuild_client.post("/v1/system/rebuild-vector-index",
                                json={"confirm": "yes"})
        assert r.status_code == 400

    def test_rebuild_accepts_correct_token(self, rebuild_client):
        r = rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["status"] in {"running", "completed"}
        assert body["task_id"]
        assert body["threshold_blocked_writes"] is False  # 空库 0 < 5000

    def test_rebuild_status_endpoint_reflects_runner(self, rebuild_client):
        # 起一次 rebuild 让它完成
        rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )
        from app.services.rebuild_runner import get_rebuild_runner
        get_rebuild_runner().join(timeout=2.0)

        r = rebuild_client.get("/v1/system/rebuild-vector-index/status")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] in {"completed", "running"}

    def test_concurrent_rebuild_returns_409(self, rebuild_client, monkeypatch):
        """已有 rebuild 在跑 → 第二次 POST 拿 409。"""
        import threading
        import app.main as main_mod

        release = threading.Event()

        def slow_rebuild(repo, vi, *, batch_size, progress_cb):
            release.wait(2.0)
            progress_cb(1, 1)

        monkeypatch.setattr(main_mod, "_REBUILD_FN_OVERRIDE", slow_rebuild)

        r1 = rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r1.status_code == 202

        r2 = rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r2.status_code == 409

        release.set()
        from app.services.rebuild_runner import get_rebuild_runner
        get_rebuild_runner().join(timeout=2.0)


class TestAbortEndpoint:
    def test_abort_when_idle_returns_idle(self, rebuild_client):
        r = rebuild_client.post("/v1/system/rebuild-vector-index/abort")
        assert r.status_code == 200
        assert r.json()["status"] == "idle"

    def test_abort_running_rebuild_marks_aborted(self, rebuild_client, monkeypatch):
        import threading
        import time as _time
        import app.main as main_mod

        def long_rebuild(repo, vi, *, batch_size, progress_cb):
            for i in range(1000):
                progress_cb(i + 1, 1000)
                _time.sleep(0.005)

        monkeypatch.setattr(main_mod, "_REBUILD_FN_OVERRIDE", long_rebuild)

        r = rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r.status_code == 202

        _time.sleep(0.05)  # 让 worker 起来 + 跑两轮 progress_cb
        r = rebuild_client.post("/v1/system/rebuild-vector-index/abort")
        assert r.status_code == 200
        assert r.json()["status"] == "aborted"


class TestSwitchModelEndpoint:
    """POST /v1/system/embedding-service/switch-model（AC22 + 与 rebuild 互斥）。"""

    def test_switch_requires_confirm_token(self, client):
        r = client.post(
            "/v1/system/embedding-service/switch-model",
            json={"model_id": "bge-m3", "confirm": "yes"},
        )
        assert r.status_code == 400

    def test_switch_rejects_unknown_model(self, client):
        r = client.post(
            "/v1/system/embedding-service/switch-model",
            json={"model_id": "no-such", "confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r.status_code == 400

    def test_switch_bumps_desired_with_action(self, client):
        r = client.post(
            "/v1/system/embedding-service/switch-model",
            json={
                "model_id": "bge-large-zh-v1.5",
                "device": "cpu",
                "confirm": "I-CONFIRM-OVERWRITE",
            },
        )
        assert r.status_code == 202
        body = r.json()
        assert body["action"] == "switch_model"
        assert body["model_id"] == "bge-large-zh-v1.5"
        assert body["generation"] == 1
        assert "rebuild-vector-index" in body["next_action"]

        # 验证 desired-state 端点看得到
        token = _owner_token()
        d = client.get(
            "/v1/system/embedding-service/desired-state",
            headers={"X-Embedding-Owner-Token": token},
        ).json()
        assert d["action"] == "switch_model"
        assert d["model_id"] == "bge-large-zh-v1.5"

    def test_switch_blocked_while_rebuild_running(self, rebuild_client, monkeypatch):
        """rebuild 在跑时切模型应 409，避免向量空间混用。"""
        import threading
        import app.main as main_mod

        release = threading.Event()

        def slow_rebuild(repo, vi, *, batch_size, progress_cb):
            release.wait(2.0)
            progress_cb(1, 1)

        monkeypatch.setattr(main_mod, "_REBUILD_FN_OVERRIDE", slow_rebuild)

        rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )

        r = rebuild_client.post(
            "/v1/system/embedding-service/switch-model",
            json={"model_id": "bge-m3", "confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r.status_code == 409

        release.set()
        from app.services.rebuild_runner import get_rebuild_runner
        get_rebuild_runner().join(timeout=2.0)


class TestPutConfigEmbeddingService:
    """PUT /v1/system/config 联动（tasks §2.9 + §2.10）。"""

    def _get_cfg(self, client) -> dict:
        return client.get("/v1/system/config").json()

    def test_mode_local_rejects_new_base_url_409(self, client):
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["embedding_base_url"] = "https://api.openai.com/v1"   # 不应允许
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 409
        assert "mode=local" in r.json()["detail"]

    def test_mode_local_rejects_new_embedding_model_409(self, client):
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["embedding_model"] = "text-embedding-3-small"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 409

    def test_mode_local_allows_empty_external_fields(self, client):
        """mode=local 但 base_url/model 都为空 → 允许（fresh 切换）。"""
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["embedding_base_url"] = ""
        cfg["embedding_model"] = ""
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200

    def test_mode_change_requires_confirm_reindex_400(self, client):
        """disabled → local 是 mode 变更，必须带 I-CONFIRM-REINDEX。"""
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        # 故意不传 confirm_reindex
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"].lower()

    def test_mode_change_with_correct_confirm_succeeds(self, client):
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "external"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200
        assert r.json()["embedding_service_mode"] == "external"

    def test_model_id_change_requires_confirm_reindex(self, client):
        # 先把 mode 切 local + model bge-m3
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        # 再改 model_id 到 bge-large-zh-v1.5（不传 confirm）
        cfg = self._get_cfg(client)
        cfg["embedding_service_model_id"] = "bge-large-zh-v1.5"
        # 不传 confirm_reindex
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 400

    def test_unrelated_field_change_no_confirm_needed(self, client):
        """改 grafana_url 这种无关字段不需要 confirm_reindex。"""
        cfg = self._get_cfg(client)
        cfg["grafana_url"] = "http://localhost:9999"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200

    def test_enter_local_mode_bumps_desired_install(self, client):
        """bug4 回归：disabled → local 必须 bump_desired(install)，否则壳层不会拉起 infinity。"""
        from app.services.embedding_service_state import get_embedding_service_state
        state = get_embedding_service_state()
        gen_before = state.desired().generation

        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["embedding_service_device"] = "cpu"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200

        desired = state.desired()
        assert desired.action == "install"
        assert desired.model_id == "bge-m3"
        assert desired.enabled is True
        assert desired.generation > gen_before

    def test_leave_local_mode_bumps_desired_stop(self, client):
        """bug4 回归：local → external 必须 bump_desired(stop)，否则 infinity 在后台浪费 1.5GB 内存。"""
        from app.services.embedding_service_state import get_embedding_service_state
        state = get_embedding_service_state()

        # 先进入 local 状态
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        gen_mid = state.desired().generation

        # 切回 external
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "external"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200

        desired = state.desired()
        assert desired.action == "stop"
        assert desired.model_id == "bge-m3"  # 保留旧 model_id 给壳层定位 process
        assert desired.enabled is False
        assert desired.generation > gen_mid

    def test_switch_model_within_local_bumps_desired_switch_model(self, client):
        """bug4 回归：local→local 改 model_id 触发 switch_model（壳层 stop→install→start）。"""
        from app.services.embedding_service_state import get_embedding_service_state
        state = get_embedding_service_state()

        # 先进入 local bge-m3
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "local"
        cfg["embedding_service_model_id"] = "bge-m3"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        gen_mid = state.desired().generation

        # 切到 bge-large-zh-v1.5
        cfg = self._get_cfg(client)
        cfg["embedding_service_model_id"] = "bge-large-zh-v1.5"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        r = client.put("/v1/system/config", json=cfg)
        assert r.status_code == 200

        desired = state.desired()
        assert desired.action == "switch_model"
        assert desired.model_id == "bge-large-zh-v1.5"
        assert desired.enabled is True
        assert desired.generation > gen_mid

    def test_external_to_disabled_no_bump(self, client):
        """bug4 回归：external↔disabled 之间切换不动 desired（infinity 本来就没跑）。"""
        from app.services.embedding_service_state import get_embedding_service_state
        state = get_embedding_service_state()

        # 进入 external
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "external"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200
        gen_mid = state.desired().generation

        # 切到 disabled
        cfg = self._get_cfg(client)
        cfg["embedding_service_mode"] = "disabled"
        cfg["confirm_reindex"] = "I-CONFIRM-REINDEX"
        assert client.put("/v1/system/config", json=cfg).status_code == 200

        assert state.desired().generation == gen_mid


class TestMaintenanceMiddlewareReindex:
    """AC10：REINDEX flag 置位时，写类 API 返 202（非 503）。"""

    def test_reindex_flag_returns_202_for_write(self, client):
        from app.services.maintenance import (
            MaintenanceReason, get_maintenance_flag,
        )
        flag = get_maintenance_flag()
        flag.clear()
        flag.set(MaintenanceReason.REINDEX, "test rebuild in progress")
        try:
            # 写类请求（upsert）应 202 + Retry-After
            r = client.post(
                "/v1/knowledge/items/upsert",
                json={
                    "title": "x", "domain": "d", "project": "p",
                    "type": "decision", "content_markdown": "c",
                    "summary": "s", "author": "a", "change_note": "n",
                },
            )
            assert r.status_code == 202
            assert r.headers.get("retry-after") == "30"
            body = r.json()
            assert body["reason"] == "reindex"
        finally:
            flag.clear()

    def test_backup_import_flag_still_returns_503(self, client):
        """非 REINDEX 原因仍走 503（原有行为兜底）。"""
        from app.services.maintenance import (
            MaintenanceReason, get_maintenance_flag,
        )
        flag = get_maintenance_flag()
        flag.clear()
        flag.set(MaintenanceReason.BACKUP_IMPORT, "import in progress")
        try:
            r = client.post(
                "/v1/knowledge/items/upsert",
                json={
                    "title": "x", "domain": "d", "project": "p",
                    "type": "decision", "content_markdown": "c",
                    "summary": "s", "author": "a", "change_note": "n",
                },
            )
            assert r.status_code == 503
            assert r.headers.get("retry-after") == "60"
        finally:
            flag.clear()


# ---------------------------------------------------------------------------
# warming_up 期间语义检索返 202（AC19）
# ---------------------------------------------------------------------------

class TestWarmingUpMiddleware:
    def _set_warming(self, client, warming: bool) -> None:
        """通过 actual-state 端点把 warming_up 翻成给定值。"""
        token = _owner_token()
        r = client.post(
            "/v1/system/embedding-service/actual-state",
            headers={"X-Embedding-Owner-Token": token},
            json={
                "acknowledged_generation": 0,
                "installed": True,
                "running": True,
                "warming_up": warming,
                "model_id": "bge-m3",
            },
        )
        assert r.status_code == 200

    def test_warming_up_returns_202_for_search(self, client):
        self._set_warming(client, True)
        r = client.post(
            "/v1/knowledge/search",
            json={"query": "x", "domain": "work"},
        )
        assert r.status_code == 202
        assert r.headers.get("retry-after") == "5"
        assert "warming up" in r.json()["detail"].lower()

    def test_warming_up_does_not_block_get_endpoints(self, client):
        """warming_up 不应影响非语义检索接口（health / config 等）。"""
        self._set_warming(client, True)
        assert client.get("/health").status_code == 200
        assert client.get("/v1/system/config").status_code == 200

    def test_search_works_when_not_warming(self, client):
        self._set_warming(client, False)
        # VECTOR_ENABLED=0 → 走 hash fallback；只验证不被 202 拦
        r = client.post(
            "/v1/knowledge/search",
            json={"query": "x", "domain": "work"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# 端到端流程：install → start → actual-state → switch-model → rebuild
# ---------------------------------------------------------------------------

class TestEndToEndFlow:
    """覆盖 §2.12 综合场景：多端点串起来的真实使用路径。"""

    def test_install_then_start_then_actual_state_visible_in_status(
        self, client, tmp_path, monkeypatch,
    ):
        import json as _json
        monkeypatch.setenv("KB_APP_ROOT", str(tmp_path))
        (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
        (tmp_path / "runtime" / "install_status.json").write_text(
            _json.dumps({"phase": "completed"})
        )

        # 1) install bumps desired (action=install, gen=1)
        r = client.post(
            "/v1/system/embedding-service/install",
            json={"model_id": "bge-m3"},
        )
        assert r.status_code == 200

        # 2) start bumps desired (action=start, gen=2)
        r = client.post("/v1/system/embedding-service/start", json={})
        assert r.json()["generation"] == 2

        # 3) 壳层回写 actual-state（gen=2 acknowledged）
        token = _owner_token()
        r = client.post(
            "/v1/system/embedding-service/actual-state",
            headers={"X-Embedding-Owner-Token": token},
            json={
                "acknowledged_generation": 2,
                "installed": True, "running": True, "warming_up": False,
                "model_id": "bge-m3", "port": 7687, "pid": 99999,
                "device": "cpu", "restart_count": 0,
            },
        )
        assert r.status_code == 200

        # 4) status 端点汇总 actual
        body = client.get("/v1/system/embedding-service/status").json()
        assert body["installed"] is True
        assert body["running"] is True
        assert body["model_id"] == "bge-m3"
        assert body["port"] == 7687
        assert body["pid"] == 99999

    def test_switch_then_rebuild_then_status_full_loop(
        self, rebuild_client, monkeypatch,
    ):
        # 1) switch-model bumps desired
        r = rebuild_client.post(
            "/v1/system/embedding-service/switch-model",
            json={
                "model_id": "bge-large-zh-v1.5",
                "confirm": "I-CONFIRM-OVERWRITE",
            },
        )
        assert r.status_code == 202

        # 2) rebuild kicks off
        r = rebuild_client.post(
            "/v1/system/rebuild-vector-index",
            json={"confirm": "I-CONFIRM-OVERWRITE"},
        )
        assert r.status_code == 202

        # 3) wait for completion
        from app.services.rebuild_runner import get_rebuild_runner
        get_rebuild_runner().join(timeout=2.0)

        # 4) rebuild status 返回 completed
        r = rebuild_client.get("/v1/system/rebuild-vector-index/status")
        assert r.json()["status"] in {"completed", "running"}


# ---------------------------------------------------------------------------
# GET /v1/system/embedding-service/install-plan —— 壳层拉安装计划（单一真源）
# ---------------------------------------------------------------------------

class TestInstallPlanEndpoint:
    """壳层（Mac Swift / Windows Python ProcessManager）拉 install plan。

    设计动机：避免 Swift 端复刻 build_install_plan，单一真源走 HTTP。
    """

    URL = "/v1/system/embedding-service/install-plan"

    def test_requires_owner_token(self, client):
        r = client.get(f"{self.URL}?model_id=bge-m3")
        assert r.status_code == 403

    def test_rejects_wrong_owner_token(self, client):
        r = client.get(
            f"{self.URL}?model_id=bge-m3",
            headers={"X-Embedding-Owner-Token": "wrong-token"},
        )
        assert r.status_code == 403

    def test_returns_plan_for_bge_m3(self, client):
        token = _owner_token()
        r = client.get(
            f"{self.URL}?model_id=bge-m3",
            headers={"X-Embedding-Owner-Token": token},
        )
        assert r.status_code == 200
        body = r.json()
        # 核心字段全：壳层 ProcessManager 执行链 venv → pip → 下载 → 起进程
        assert body["model_id"] == "BAAI/bge-m3"
        assert body["model_key"] == "bge-m3"
        assert body["dim"] == 1024
        assert body["device"] == "cpu"  # 默认 cpu，与 query 不传 device 一致
        # Windows 用反斜杠，Mac/Linux 用正斜杠——归一化后断言
        assert body["venv_dir"].replace("\\", "/").endswith("embedding-service/venv")
        assert body["model_dir"].replace("\\", "/").endswith("models/bge-m3")
        # 命令是 list[str]，Swift JSONDecoder 直接 [String]
        assert isinstance(body["create_venv_cmd"], list)
        assert isinstance(body["pip_install_cmd"], list)
        assert isinstance(body["start_cmd"], list)
        # download_args 是 dict（snapshot_download 入参）
        assert body["download_args"]["repo_id"] == "BAAI/bge-m3"

    def test_unknown_model_id_returns_400(self, client):
        token = _owner_token()
        r = client.get(
            f"{self.URL}?model_id=does-not-exist",
            headers={"X-Embedding-Owner-Token": token},
        )
        assert r.status_code == 400
        assert "未知模型 key" in r.json()["detail"]

    def test_device_query_overrides_default(self, client):
        token = _owner_token()
        r = client.get(
            f"{self.URL}?model_id=bge-m3&device=mps",
            headers={"X-Embedding-Owner-Token": token},
        )
        assert r.status_code == 200
        assert r.json()["device"] == "mps"
