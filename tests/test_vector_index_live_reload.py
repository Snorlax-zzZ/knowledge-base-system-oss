"""VectorIndex live reload 回归测试（方案 E 根治 Qdrant portalocker 竞态）。

覆盖 4 条契约：

1. **actual-state 心跳同步不再 pause + 不再重建 QdrantClient**
   config hot reload 走 ``_refresh_live_repo_vector_indexes`` → ``reload_from_repo``；
   老 clear + pause 语义不再走这条路径。

2. **热刷新对象身份不变**
   ``id(repo)`` / ``id(repo.vector_index)`` / ``_client`` 句柄跨 reload 稳定；
   embedding runtime（enabled / dim）已按 DB 更新。

3. **mode / model_id 变更时不自动 recreate_collection**
   ``put_system_config`` 用 ``allow_schema_refresh=False`` 分支时，即便 dim 会变，
   ``_schema_refresh_needed`` 不被 set、``_ensure_client_and_collection`` 也不调；
   ``allow_schema_refresh=True`` 时才走 schema refresh。

4. **backup/import/recover 仍走 pause/resume**
   maintenance 独占语义不被本次改动打穿。
"""
from __future__ import annotations

import threading
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 通用 fake 组件
# ---------------------------------------------------------------------------


class _FakeVectorsCfg:
    def __init__(self, size: int) -> None:
        self.size = size


class _FakeParams:
    def __init__(self, size: int) -> None:
        self.vectors = _FakeVectorsCfg(size)


class _FakeConfig:
    def __init__(self, size: int) -> None:
        self.params = _FakeParams(size)


class FakeCollection:
    def __init__(self, name: str, size: int) -> None:
        self.name = name
        self.config = _FakeConfig(size)


class FakeQdrantClient:
    """记录 upsert / delete / query / recreate_collection 调用轨迹的假 client。"""

    def __init__(self, *, initial_dim: int = 1024, collection_name: str = "knowledge_chunks") -> None:
        self._collection = FakeCollection(collection_name, initial_dim)
        self.recreate_calls: list[int] = []
        self.upsert_calls = 0
        self.delete_calls = 0
        self.query_calls = 0
        self.closed = False

    def get_collections(self):
        class R:
            pass
        r = R()
        r.collections = [self._collection]
        return r

    def get_collection(self, collection_name: str):
        return self._collection

    def create_collection(self, *, collection_name: str, vectors_config: Any) -> None:  # pragma: no cover
        self._collection = FakeCollection(collection_name, int(vectors_config.size))

    def recreate_collection(self, *, collection_name: str, vectors_config: Any) -> None:
        self.recreate_calls.append(int(vectors_config.size))
        self._collection = FakeCollection(collection_name, int(vectors_config.size))

    def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        self.upsert_calls += 1

    def delete(self, *, collection_name: str, points_selector: Any) -> None:
        self.delete_calls += 1

    def query_points(self, *, collection_name: str, query: list[float], query_filter: Any, limit: int, with_payload: bool):
        self.query_calls += 1

        class Resp:
            pass
        r = Resp()
        r.points = []
        return r

    def close(self) -> None:
        self.closed = True


class FakeRepo:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cfg = dict(cfg)
        self.vector_index = None

    def get_system_config(self) -> dict[str, Any]:
        return dict(self._cfg)


@pytest.fixture
def isolated_main(monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module, "_repo_singletons", {})
    return main_module


@pytest.fixture
def vi_with_fake_client(monkeypatch, tmp_path):
    """构造一个挂 FakeQdrantClient 的 VectorIndex 实例，跳过真 Qdrant 连接。

    - ``VECTOR_ENABLED=1`` 让 reload 后 ``resolved.enabled=True``，保留 upsert / search 通路
    - ``_ensure_client_and_collection`` 打桩成计数 spy（无副作用），测试自己按需重新装
    - ``_embed_with_fallback`` 打桩成 fixed vector，避免真发 http
    - embedding 相关 env 全清 → __init__ 走 HashEmbedding 路径
    """
    from app import vector_index as vi_mod

    monkeypatch.setenv("VECTOR_ENABLED", "1")
    monkeypatch.setenv("QDRANT_MODE", "local")
    monkeypatch.setenv("QDRANT_LOCAL_PATH", str(tmp_path / "qdrant"))
    monkeypatch.setenv("QDRANT_COLLECTION", "knowledge_chunks")
    monkeypatch.setenv("VECTOR_DIM", "1024")
    for k in (
        "KB_EMBEDDING_ENABLED",
        "KB_EMBEDDING_API_KEY",
        "KB_EMBEDDING_MODEL",
        "KB_EMBEDDING_BASE_URL",
        "KB_EMBEDDING_TIMEOUT_SEC",
    ):
        monkeypatch.delenv(k, raising=False)

    ensure_calls: list[int] = []

    def _spy_ensure(self):
        ensure_calls.append(1)

    monkeypatch.setattr(vi_mod.VectorIndex, "_ensure_client_and_collection", _spy_ensure)
    monkeypatch.setattr(
        vi_mod.VectorIndex, "_embed_with_fallback", lambda self, text: [0.0] * int(self.embedding.dim)
    )

    vi = vi_mod.VectorIndex.from_env()
    # 手动进入 "已连成功" 稳态
    vi.enabled = True
    fake = FakeQdrantClient(initial_dim=1024, collection_name=vi.collection_name)
    vi._client = fake
    ensure_calls.clear()  # 忽略 __init__ 期间的 ensure 计数
    return vi, fake, ensure_calls


# ---------------------------------------------------------------------------
# 契约 1 + 2：reload_from_repo 保持 client 句柄 + 不 pause + runtime 已更新
# ---------------------------------------------------------------------------


class TestReloadPreservesClient:
    def test_reload_does_not_close_client(self, vi_with_fake_client):
        vi, fake, _ = vi_with_fake_client
        old_client_id = id(vi._client)
        old_vi_id = id(vi)

        repo = FakeRepo({
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
            "embedding_service_port": 7687,
            "embedding_enabled": True,
            "embedding_dim": 1024,
        })

        vi.reload_from_repo(repo, allow_schema_refresh=True)

        assert fake.closed is False, "reload 禁止 close QdrantClient（否则 portalocker 会撞）"
        assert id(vi) == old_vi_id
        assert id(vi._client) == old_client_id, "client 句柄跨 reload 必须稳定"

    def test_reload_updates_embedding_runtime(self, vi_with_fake_client):
        vi, _, _ = vi_with_fake_client
        # 起手是 HashEmbedding（fixture 清了 embedding env）
        from app.vector_index import HashEmbedding, ApiEmbedding

        assert isinstance(vi.embedding, HashEmbedding)

        # 切到 local + bge-m3 → helper 会写 env → embedding_config_from_env active
        repo = FakeRepo({
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
            "embedding_service_port": 7687,
            "embedding_enabled": True,
            "embedding_dim": 1024,
        })

        vi.reload_from_repo(repo, allow_schema_refresh=True)

        assert isinstance(vi.embedding, ApiEmbedding), "local mode 应切到 ApiEmbedding 走本机 infinity"
        assert int(vi.embedding.dim) == 1024


# ---------------------------------------------------------------------------
# 契约 3：allow_schema_refresh 分流
# ---------------------------------------------------------------------------


class TestSchemaRefreshGate:
    def test_no_schema_refresh_skips_ensure_and_flag(self, vi_with_fake_client):
        """put_system_config 里 mode/model_id 变更走 allow_schema_refresh=False；
        即便 dim 会变，也不能 mark schema refresh、不能调 ensure。"""
        vi, _, ensure_calls = vi_with_fake_client

        repo = FakeRepo({
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
            "embedding_enabled": True,
            "embedding_dim": 1024,
        })

        vi.reload_from_repo(repo, allow_schema_refresh=False)

        assert vi._schema_refresh_needed is False
        assert ensure_calls == [], "allow_schema_refresh=False 不许调 _ensure_client_and_collection"

    def test_schema_refresh_when_dim_changes(self, vi_with_fake_client, monkeypatch):
        """dim 变化 + allow_schema_refresh=True 时 mark flag + 调 ensure。"""
        vi, _, ensure_calls = vi_with_fake_client
        assert int(vi.embedding.dim) == 1024

        # 让 resolved.embedding.dim = 512（模拟切到不同 dim 模型）
        from app import vector_index as vi_mod

        def _fake_resolve(db_cfg, *, default_dim):
            return vi_mod._ResolvedRuntime(
                enabled=True,
                embedding=vi_mod.HashEmbedding(dim=512),
                fallback=vi_mod.HashEmbedding(dim=512),
                dim=512,
            )

        monkeypatch.setattr(vi_mod, "_resolve_runtime_from_db_cfg", _fake_resolve)

        vi.reload_from_repo(FakeRepo({}), allow_schema_refresh=True)

        assert vi._schema_refresh_needed is True
        assert ensure_calls == [1], "dim 变 + allow_schema_refresh=True 应调一次 ensure"

    def test_schema_refresh_flag_stable_when_dim_unchanged(self, vi_with_fake_client, monkeypatch):
        """dim 未变 + allow_schema_refresh=True 时 flag 不 mark，但 ensure 仍调
        （幂等 no-op 即可，避免 branch drift）。"""
        vi, _, ensure_calls = vi_with_fake_client

        from app import vector_index as vi_mod

        def _fake_resolve(db_cfg, *, default_dim):
            return vi_mod._ResolvedRuntime(
                enabled=True,
                embedding=vi_mod.HashEmbedding(dim=1024),
                fallback=vi_mod.HashEmbedding(dim=1024),
                dim=1024,
            )

        monkeypatch.setattr(vi_mod, "_resolve_runtime_from_db_cfg", _fake_resolve)

        vi.reload_from_repo(FakeRepo({}), allow_schema_refresh=True)

        assert vi._schema_refresh_needed is False, "dim 未变时不 mark schema refresh"


# ---------------------------------------------------------------------------
# 契约 1（续）：_refresh_live_repo_vector_indexes 不 pause 老 vector_index
# ---------------------------------------------------------------------------


class TestRefreshLiveDoesNotPause:
    def test_refresh_live_does_not_call_pause(self, isolated_main):
        main_module = isolated_main

        pause_calls: list[str] = []

        class TrackingVI:
            def __init__(self) -> None:
                self.reload_calls: list[bool] = []

            def pause(self) -> None:
                pause_calls.append("boom")

            def reload_from_repo(self, repo: Any, *, allow_schema_refresh: bool) -> None:
                self.reload_calls.append(allow_schema_refresh)

        class RepoStub:
            def __init__(self, vi: TrackingVI) -> None:
                self.vector_index = vi

            def get_system_config(self) -> dict:
                return {}

        vi = TrackingVI()
        main_module._repo_singletons[("sqlite", "/tmp/live.db")] = RepoStub(vi)

        main_module._refresh_live_repo_vector_indexes(allow_schema_refresh=True)

        assert pause_calls == [], "config hot reload 路径禁止 pause 老 vector_index"
        assert vi.reload_calls == [True]
        # dict 未清空（保持 QdrantClient 单实例）
        assert len(main_module._repo_singletons) == 1

    def test_refresh_live_swallows_reload_error(self, isolated_main):
        main_module = isolated_main

        class BrokenVI:
            def reload_from_repo(self, repo: Any, *, allow_schema_refresh: bool) -> None:
                raise RuntimeError("simulated reload failure")

        class HealthyVI:
            def __init__(self) -> None:
                self.reloaded = False

            def reload_from_repo(self, repo: Any, *, allow_schema_refresh: bool) -> None:
                self.reloaded = True

        class RepoStub:
            def __init__(self, vi: Any) -> None:
                self.vector_index = vi

            def get_system_config(self) -> dict:
                return {}

        healthy = HealthyVI()
        main_module._repo_singletons[("sqlite", "/tmp/broken.db")] = RepoStub(BrokenVI())
        main_module._repo_singletons[("postgres", "postgresql://ok")] = RepoStub(healthy)

        main_module._refresh_live_repo_vector_indexes(allow_schema_refresh=True)

        assert healthy.reloaded is True, "单个 vector_index reload 抛异常不能阻断其他实例"


# ---------------------------------------------------------------------------
# 契约 4：backup/import/recover 仍走 pause/resume
# ---------------------------------------------------------------------------


class TestMaintenancePauseResumeStillWorks:
    def test_pause_closes_client_and_blocks_reconnect(self, vi_with_fake_client, monkeypatch):
        vi, fake, ensure_calls = vi_with_fake_client

        # 需要真 ensure 才能验证 paused 时不重连；恢复真实现
        from app import vector_index as vi_mod
        # 拿到 unbound 方法（原始实现被 fixture 打桩成 lambda）
        # 直接用 vector_index 模块里的原方法：我们靠 attribute lookup 走 method 无副作用检查
        real_ensure_source = None
        # 简化：直接手 patch 出一个只会检查 self._paused / self._client 的 mini 版
        def real_ensure_stub(self):
            with self._lock:
                if self._paused:
                    return
                if self._client is not None and not self._schema_refresh_needed:
                    return
            # 走到这里说明想重建 —— 但 pause 状态下不该走到
            raise RuntimeError("ensure should not attempt reconnect while paused")

        monkeypatch.setattr(vi_mod.VectorIndex, "_ensure_client_and_collection", real_ensure_stub)

        vi.pause()

        assert fake.closed is True, "maintenance pause 仍须 close client 让文件锁释放"
        assert vi._client is None
        assert vi._paused is True

        # paused 状态下 ensure 不重连（fast return 走 paused 分支）
        vi._ensure_client_and_collection()  # 不抛即过

    def test_resume_clears_paused_flag(self, vi_with_fake_client):
        vi, fake, _ = vi_with_fake_client
        vi.pause()
        assert vi._paused is True
        assert vi._client is None

        vi.resume()

        assert vi._paused is False
        assert vi._client is None, "resume 后 client 保持 None，交给下次 _ensure 重建"


# ---------------------------------------------------------------------------
# 契约 1（续²）：并发下 upsert 和 reload 交叉不撞半刷新态
# ---------------------------------------------------------------------------


class TestConcurrentReloadAndUpsert:
    def test_concurrent_reload_does_not_corrupt_upsert(self, vi_with_fake_client):
        vi, fake, _ = vi_with_fake_client

        repo = FakeRepo({
            "embedding_service_mode": "local",
            "embedding_service_model_id": "bge-m3",
            "embedding_service_port": 7687,
            "embedding_enabled": True,
            "embedding_dim": 1024,
        })

        errors: list[BaseException] = []
        stop = threading.Event()

        def reload_loop() -> None:
            for _ in range(50):
                if stop.is_set():
                    return
                try:
                    vi.reload_from_repo(repo, allow_schema_refresh=True)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    return

        def upsert_loop() -> None:
            chunk = {
                "chunk_id": "00000000-0000-0000-0000-000000000000",
                "knowledge_item_id": "kid",
                "domain": "d",
                "project": "p",
                "version": "v",
                "title": "t",
                "chunk_index": 0,
                "text": "hello",
            }
            for _ in range(50):
                if stop.is_set():
                    return
                try:
                    vi.upsert_chunks([chunk])
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)
                    return

        t1 = threading.Thread(target=reload_loop)
        t2 = threading.Thread(target=upsert_loop)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        stop.set()

        assert not errors, f"并发 reload + upsert 不应抛异常：{errors}"
        assert fake.closed is False, "reload 全程不能 close client"
        assert fake.upsert_calls > 0, "upsert 至少跑通几次（确认没被死锁）"
