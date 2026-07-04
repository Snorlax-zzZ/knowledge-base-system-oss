"""``_drop_repo_singletons_for_tests``（原名 ``_invalidate_repo_singletons``）
释放 qdrant_local 锁的回归测试（bug B）。

历史 bug：基于 ``lru_cache`` 的 repo 单例池只 ``cache_clear`` 不能拿回旧实例，
mode 切换 / PUT /v1/system/config 触发的 invalidate 之后，旧 ``VectorIndex`` 仍持有
``qdrant_local`` portalocker 文件锁；下次 ``get_repo`` 重建时同进程内
``QdrantClient(path=...)`` ``AlreadyLocked``，新 VectorIndex 静默
``enabled=False`` 退化关键词检索。

修法：单例池改 ``dict + Lock``，drop 时遍历旧实例 ``vector_index.pause()``
释放锁再 clear。这个测试断言 ``pause`` 被调到。

2026-07-01 方案 E：生产 config hot reload 不再走 clear + pause 路径
（走 ``_refresh_live_repo_vector_indexes`` 原地热刷新），这里的 ``drop`` 语义
仅测试 fixture 与硬失效场景使用。
"""
from __future__ import annotations

import pytest


class FakeVectorIndex:
    def __init__(self) -> None:
        self.pause_calls = 0
        self.resumed = False

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resumed = True


class FakeRepo:
    def __init__(self, vi: FakeVectorIndex) -> None:
        self.vector_index = vi


@pytest.fixture
def isolated_main(monkeypatch):
    """隔离 module 全局 dict，避免污染其他用例。"""
    from app import main as main_module

    monkeypatch.setattr(main_module, "_repo_singletons", {})
    return main_module


def test_invalidate_calls_pause_on_old_vector_index(isolated_main):
    main_module = isolated_main
    vi = FakeVectorIndex()
    repo = FakeRepo(vi)
    main_module._repo_singletons[("sqlite", "/tmp/fake.db")] = repo

    main_module._drop_repo_singletons_for_tests()

    assert vi.pause_calls == 1, "invalidate 必须 pause 旧 vector_index 释放 qdrant_local 锁"
    assert main_module._repo_singletons == {}, "dict 必须清空"


def test_invalidate_handles_multiple_singletons(isolated_main):
    """sqlite + postgres 两个 backend 同时存活时都要 pause。"""
    main_module = isolated_main
    vi_sqlite = FakeVectorIndex()
    vi_pg = FakeVectorIndex()
    main_module._repo_singletons[("sqlite", "/tmp/a.db")] = FakeRepo(vi_sqlite)
    main_module._repo_singletons[("postgres", "postgresql://x")] = FakeRepo(vi_pg)

    main_module._drop_repo_singletons_for_tests()

    assert vi_sqlite.pause_calls == 1
    assert vi_pg.pause_calls == 1
    assert main_module._repo_singletons == {}


def test_invalidate_swallows_pause_errors(isolated_main):
    """单个 pause 抛异常不能阻断后续 invalidate（dict 清空 + 其他实例 pause 仍要跑）。"""
    main_module = isolated_main

    class BrokenVI:
        def pause(self) -> None:
            raise RuntimeError("simulated qdrant close failure")

    healthy_vi = FakeVectorIndex()
    main_module._repo_singletons[("sqlite", "/tmp/broken.db")] = FakeRepo(BrokenVI())
    main_module._repo_singletons[("postgres", "postgresql://ok")] = FakeRepo(healthy_vi)

    main_module._drop_repo_singletons_for_tests()

    assert main_module._repo_singletons == {}
    assert healthy_vi.pause_calls == 1, "broken 实例抛异常不能影响 healthy 实例 pause"


def test_invalidate_tolerates_missing_vector_index(isolated_main):
    """repo.vector_index 是 None（早期 init 失败场景）不应抛 AttributeError。"""
    main_module = isolated_main

    class RepoWithoutVI:
        vector_index = None

    main_module._repo_singletons[("sqlite", "/tmp/x.db")] = RepoWithoutVI()
    main_module._drop_repo_singletons_for_tests()  # 不抛即过
    assert main_module._repo_singletons == {}


def test_singleton_reused_within_same_key(isolated_main, monkeypatch):
    """同 key 两次 get 返回同一个 repo（dict 缓存生效）。"""
    main_module = isolated_main

    instances = []

    class StubRepo:
        def __init__(self, sqlite_path, vector_index=None):
            self.sqlite_path = sqlite_path
            self.vector_index = vector_index or FakeVectorIndex()
            instances.append(self)

    monkeypatch.setattr(
        "app.repository_sqlite.SqliteKnowledgeRepo",
        StubRepo,
    )
    monkeypatch.setattr(
        main_module.VectorIndex,
        "from_repo",
        classmethod(lambda cls, repo: FakeVectorIndex()),
    )

    r1 = main_module._repo_singleton_sqlite("/tmp/same.db")
    r2 = main_module._repo_singleton_sqlite("/tmp/same.db")
    assert r1 is r2
    assert len(instances) == 1, "dict 缓存命中应只构造一次"

    main_module._drop_repo_singletons_for_tests()
    r3 = main_module._repo_singleton_sqlite("/tmp/same.db")
    assert r3 is not r1, "invalidate 后必须重建新实例"
    assert len(instances) == 2
