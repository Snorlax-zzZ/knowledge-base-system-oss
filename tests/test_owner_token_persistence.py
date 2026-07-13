"""Owner token 落盘测试（Phase 3 前置，design v1.2 §3.2 + AC25）。

kb-api 启动钩子要把 ``EmbeddingServiceState.owner_token`` 写到
``{data_root}/runtime/owner_token``，供壳层（mac-app / windows-app
ProcessManager）启动后读出来塞到 ``X-Embedding-Owner-Token`` header。

本文件覆盖三件事：

1. ``write_owner_token_file`` 单元测试（独立于 FastAPI）：路径 / 权限 / 覆盖
2. 启动钩子集成测试（用 TestClient 触发 startup 事件，验证文件真写出来了）
3. 边界：data_root 不存在 / 已存在旧 token 文件 / 已存在符号链接
"""
from __future__ import annotations

import os
import socket
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services.embedding_service_state import (
    get_embedding_service_state,
    resolve_owner_token_path,
    write_owner_token_file,
)


def _unused_loopback_port() -> int:
    """Return a currently unused loopback port for startup-hook tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# 单元测试：write_owner_token_file
# ---------------------------------------------------------------------------


class TestWriteOwnerTokenFile:
    def test_writes_token_to_runtime_subdir(self, tmp_path):
        token = "test-token-abc"
        target = write_owner_token_file(str(tmp_path), token)

        assert target == tmp_path / "runtime" / "owner_token"
        assert target.read_text(encoding="utf-8") == token

    def test_creates_runtime_dir_if_missing(self, tmp_path):
        data_root = tmp_path / "fresh"
        # 故意不预建 runtime/，验证 mkdir(parents=True, exist_ok=True)
        target = write_owner_token_file(str(data_root), "xyz")

        assert target.exists()
        assert target.parent.is_dir()

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Windows chmod 不严格执行 POSIX 权限位",
    )
    def test_chmod_600_on_posix(self, tmp_path):
        target = write_owner_token_file(str(tmp_path), "tok")
        mode = stat.S_IMODE(target.stat().st_mode)
        # 0o600：仅 owner 可读写
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"

    def test_overwrites_existing_file(self, tmp_path):
        # 先写一份旧 token
        write_owner_token_file(str(tmp_path), "old")
        # 再写新 token：应当完全覆盖（kb-api 进程重启场景）
        target = write_owner_token_file(str(tmp_path), "new")
        assert target.read_text(encoding="utf-8") == "new"

    @pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="Windows 符号链接需要管理员权限，跳过",
    )
    def test_refuses_to_follow_symlink(self, tmp_path):
        """符号链接劫持防御：runtime/owner_token 若是 symlink 指向外部，
        write_owner_token_file 必须先 unlink 再用 O_EXCL 重建。
        """
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        attacker = tmp_path / "attacker_file"
        attacker.write_text("attacker")
        (runtime / "owner_token").symlink_to(attacker)

        write_owner_token_file(str(tmp_path), "real-token")

        # 真正的 token 落在 runtime/owner_token（不再是 symlink）
        target = runtime / "owner_token"
        assert not target.is_symlink()
        assert target.read_text(encoding="utf-8") == "real-token"
        # 外部 attacker 文件没被覆盖
        assert attacker.read_text(encoding="utf-8") == "attacker"


class TestResolveOwnerTokenPath:
    def test_path_layout(self, tmp_path):
        p = resolve_owner_token_path(str(tmp_path))
        assert p == tmp_path / "runtime" / "owner_token"


# ---------------------------------------------------------------------------
# 集成测试：FastAPI 启动钩子触发 owner_token 落盘
# ---------------------------------------------------------------------------


class TestStartupHookPersistsToken:
    def test_startup_writes_token_to_data_root(self, tmp_path, monkeypatch):
        """TestClient 作为 context manager 时触发 startup 事件。

        验证：钩子真的把当前进程的 owner_token 写到 KB_APP_ROOT/runtime/owner_token。
        """
        monkeypatch.setenv("KB_APP_ROOT", str(tmp_path))
        monkeypatch.setenv("KB_BACKEND", "sqlite")
        monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("VECTOR_ENABLED", "0")
        # TestClient 不会真的监听 TCP；显式使用空闲端口，避免本机 18000 上
        # 正在运行的已安装 kb-api 让 startup 防覆盖探针误判本用例。
        monkeypatch.setenv("KB_PORT_API", str(_unused_loopback_port()))

        # 每用例重置控制面单例，确保 token 是新生成的
        get_embedding_service_state().reset_for_tests()
        expected_token = get_embedding_service_state().owner_token

        from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
        _repo_singleton_sqlite.cache_clear()
        _repo_singleton_postgres.cache_clear()

        from app.main import app
        with TestClient(app) as _:  # noqa: F841 — context 触发 startup
            target = tmp_path / "runtime" / "owner_token"
            assert target.exists(), "startup 钩子没写出 owner_token 文件"
            assert target.read_text(encoding="utf-8") == expected_token

    def test_startup_does_not_fail_when_data_root_unwritable(
        self, tmp_path, monkeypatch, caplog
    ):
        """落盘失败只 warn，不阻塞 kb-api 启动（design §3.2 "壳层启动时找不到自己 retry"）。

        构造法：把 KB_APP_ROOT 指向一个只读父目录下的子路径。
        """
        if sys.platform.startswith("win") or os.geteuid() == 0:
            pytest.skip("Windows 或 root 用户无法构造可靠的 EACCES 场景")

        readonly_parent = tmp_path / "readonly"
        readonly_parent.mkdir()
        # 让父目录不可写：mkdir runtime/ 必失败
        os.chmod(str(readonly_parent), 0o500)
        try:
            data_root = readonly_parent / "app"
            monkeypatch.setenv("KB_APP_ROOT", str(data_root))
            monkeypatch.setenv("KB_BACKEND", "sqlite")
            monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.db"))
            monkeypatch.setenv("VECTOR_ENABLED", "0")
            monkeypatch.setenv("KB_PORT_API", str(_unused_loopback_port()))

            get_embedding_service_state().reset_for_tests()

            from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
            _repo_singleton_sqlite.cache_clear()
            _repo_singleton_postgres.cache_clear()

            from app.main import app
            # 不应抛异常（钩子内 try/except 吞）
            with TestClient(app) as client:
                # kb-api 仍能正常响应 /health
                assert client.get("/health").status_code == 200
        finally:
            # 还原权限以便 pytest 能清理 tmp_path
            os.chmod(str(readonly_parent), 0o700)

    def test_startup_keeps_winner_token_when_api_port_already_listens(
        self, tmp_path, monkeypatch, caplog
    ):
        """短命 loser 的 startup 不得覆盖已监听实例的 owner token。"""
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        token_path = runtime / "owner_token"
        token_path.write_text("winner-token", encoding="utf-8")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])

            monkeypatch.setenv("KB_APP_ROOT", str(tmp_path))
            monkeypatch.setenv("KB_PORT_API", str(port))
            get_embedding_service_state().reset_for_tests()

            from app.main import _persist_owner_token_on_startup

            _persist_owner_token_on_startup()

        assert token_path.read_text(encoding="utf-8") == "winner-token"
        assert "skip owner_token write" in caplog.text
