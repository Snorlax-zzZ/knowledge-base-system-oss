"""Windows ProcessManager 单元测试（Phase 3 Batch A 起）。

跨平台运行（在 mac 上 pytest 直接跑过），不依赖 Windows 特定 API。
后续 Batch B/C/D/E/F 在本文件按 class 分组追加。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import pytest

# windows-app 不在默认 sys.path，注入一次
_WINDOWS_APP_DIR = Path(__file__).resolve().parent.parent / "windows-app"
if str(_WINDOWS_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WINDOWS_APP_DIR))

from embedding_process_manager import (  # noqa: E402
    ActionHandler,
    ActualStateSnapshot,
    CommandResult,
    CommandRunner,
    DefaultCommandRunner,
    DefaultHealthProbe,
    DefaultSubprocessSpawner,
    DesiredStateSnapshot,
    EmbeddingActionContext,
    EmbeddingActionHandler,
    EmbeddingProcessManager,
    HealthProbe,
    InstallExecutor,
    InstallSpec,
    InstallStatusWriter,
    KbApiClient,
    KbApiConflict,
    KbApiTransportError,
    KbApiUnauthorized,
    OwnerTokenSource,
    OwnerTokenUnavailable,
    ProcessHandle,
    StaleResidueCleaner,
    StartHandler,
    StartSpec,
    StopHandler,
    SubprocessSpawner,
    _PsCmdlineProbe,
    _is_model_dir_complete,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """注入的 monotonic 时钟，测试可控时间推进。"""
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeSleep:
    """sleep 替身：记录调用 + 推进 fake clock。"""
    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.now += seconds


class _ScriptedTransport:
    """按预设脚本响应的 transport，支持每条匹配 method+path。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        # script: list of (method, path_suffix, status, body_bytes)
        self.script: list[tuple[str, str, int, bytes]] = []

    def queue(self, method: str, path_suffix: str, status: int, body: dict | bytes | None = None) -> None:
        if isinstance(body, dict):
            payload = json.dumps(body).encode("utf-8")
        elif body is None:
            payload = b""
        else:
            payload = body
        self.script.append((method, path_suffix, status, payload))

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, dict(headers), body))
        if not self.script:
            raise AssertionError(f"unexpected request: {method} {url}")
        exp_method, exp_suffix, status, payload = self.script.pop(0)
        assert method == exp_method, f"expected {exp_method}, got {method}"
        assert url.endswith(exp_suffix), f"expected url end with {exp_suffix!r}, got {url!r}"
        return status, payload


# ---------------------------------------------------------------------------
# OwnerTokenSource
# ---------------------------------------------------------------------------


class TestOwnerTokenSource:
    def test_load_blocking_returns_token_when_file_exists(self, tmp_path):
        token_path = tmp_path / "owner_token"
        token_path.write_text("abc-xyz", encoding="utf-8")
        clock = _FakeClock()
        sleep = _FakeSleep(clock)

        src = OwnerTokenSource(token_path, sleep=sleep, clock=clock)
        assert src.load_blocking() == "abc-xyz"
        # 文件已存在 → 不应睡眠
        assert sleep.calls == []

    def test_load_blocking_caches_after_first_read(self, tmp_path):
        token_path = tmp_path / "owner_token"
        token_path.write_text("v1", encoding="utf-8")
        src = OwnerTokenSource(token_path)

        assert src.load_blocking() == "v1"
        # 文件被改了，但缓存命中 → 仍返 v1
        token_path.write_text("v2", encoding="utf-8")
        assert src.load_blocking() == "v1"

    def test_load_blocking_retries_then_finds(self, tmp_path):
        token_path = tmp_path / "owner_token"
        # 文件初始不存在；模拟壳层早于 kb-api 启动场景
        clock = _FakeClock()
        sleep = _FakeSleep(clock)

        # poll 第二次时"出现"
        original_sleep = sleep.__call__
        call_count = {"n": 0}

        def maybe_create(seconds):
            call_count["n"] += 1
            if call_count["n"] == 2:
                token_path.write_text("late-token", encoding="utf-8")
            original_sleep(seconds)
        sleep.__call__ = maybe_create  # type: ignore[assignment]

        src = OwnerTokenSource(
            token_path, boot_timeout_sec=30.0, poll_interval_sec=1.0,
            sleep=maybe_create, clock=clock,
        )
        assert src.load_blocking() == "late-token"
        assert call_count["n"] >= 2

    def test_load_blocking_times_out(self, tmp_path):
        token_path = tmp_path / "owner_token"
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        src = OwnerTokenSource(
            token_path, boot_timeout_sec=3.0, poll_interval_sec=1.0,
            sleep=sleep, clock=clock,
        )
        with pytest.raises(OwnerTokenUnavailable):
            src.load_blocking()

    def test_invalidate_forces_re_read(self, tmp_path):
        token_path = tmp_path / "owner_token"
        token_path.write_text("v1", encoding="utf-8")
        src = OwnerTokenSource(token_path)

        assert src.load_blocking() == "v1"
        token_path.write_text("v2", encoding="utf-8")
        src.invalidate()
        assert src.load_blocking() == "v2"

    def test_refresh_returns_latest_without_wait(self, tmp_path):
        token_path = tmp_path / "owner_token"
        token_path.write_text("vA", encoding="utf-8")
        src = OwnerTokenSource(token_path)
        assert src.load_blocking() == "vA"

        token_path.write_text("vB", encoding="utf-8")
        assert src.refresh() == "vB"
        # 刷新后缓存也更新
        assert src.load_blocking() == "vB"

    def test_refresh_raises_when_missing(self, tmp_path):
        src = OwnerTokenSource(tmp_path / "nope")
        with pytest.raises(OwnerTokenUnavailable):
            src.refresh()

    def test_empty_file_treated_as_missing(self, tmp_path):
        """壳层正在覆盖式写时可能瞬时读到空文件；视作 not yet ready。"""
        token_path = tmp_path / "owner_token"
        token_path.write_text("", encoding="utf-8")
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        src = OwnerTokenSource(
            token_path, boot_timeout_sec=2.0, poll_interval_sec=1.0,
            sleep=sleep, clock=clock,
        )
        with pytest.raises(OwnerTokenUnavailable):
            src.load_blocking()


# ---------------------------------------------------------------------------
# KbApiClient
# ---------------------------------------------------------------------------


def _make_client(tmp_path, transport):
    token_path = tmp_path / "owner_token"
    token_path.write_text("tok-1", encoding="utf-8")
    src = OwnerTokenSource(token_path)
    return KbApiClient(
        base_url="http://127.0.0.1:18000",
        token_source=src,
        transport=transport,
    ), src


class TestKbApiClientGetDesired:
    def test_returns_snapshot_on_200(self, tmp_path):
        t = _ScriptedTransport()
        t.queue("GET", "/v1/system/embedding-service/desired-state", 200, {
            "action": "install", "model_id": "bge-m3", "device": "cpu",
            "enabled": True, "generation": 5, "updated_at": 1234.5,
        })
        client, _ = _make_client(tmp_path, t)

        snap = client.get_desired()
        assert snap == DesiredStateSnapshot(
            action="install", model_id="bge-m3", device="cpu",
            enabled=True, generation=5, updated_at=1234.5,
        )
        # 请求头带 token
        _, _, hdrs, _ = t.requests[-1]
        assert hdrs["X-Embedding-Owner-Token"] == "tok-1"

    def test_401_invalidates_token_and_raises(self, tmp_path):
        t = _ScriptedTransport()
        t.queue("GET", "/desired-state", 401, {"detail": "bad token"})
        client, src = _make_client(tmp_path, t)

        with pytest.raises(KbApiUnauthorized):
            client.get_desired()
        # token 缓存被清；下次 load_blocking 会重读
        assert src._cached is None  # noqa: SLF001 — 测试需要看私有字段

    def test_5xx_raises_transport_error(self, tmp_path):
        t = _ScriptedTransport()
        t.queue("GET", "/desired-state", 502, b"bad gateway")
        client, _ = _make_client(tmp_path, t)
        with pytest.raises(KbApiTransportError):
            client.get_desired()

    def test_transport_exception_wrapped(self, tmp_path):
        def boom(*args, **kw):
            raise ConnectionRefusedError("nope")
        client, _ = _make_client(tmp_path, boom)
        with pytest.raises(KbApiTransportError):
            client.get_desired()


class TestKbApiClientPostActual:
    def test_posts_snapshot_payload(self, tmp_path):
        t = _ScriptedTransport()
        t.queue("POST", "/actual-state", 200, {
            "accepted": True, "acknowledged_generation": 7, "updated_at": 1.0,
        })
        client, _ = _make_client(tmp_path, t)

        snap = ActualStateSnapshot(
            acknowledged_generation=7, installed=True, running=True,
            warming_up=False, model_id="bge-m3", port=7687, pid=123,
            device="cpu", restart_count=0, last_error="",
        )
        resp = client.post_actual(snap)
        assert resp["accepted"] is True
        assert resp["acknowledged_generation"] == 7

        # body 正确序列化
        _, _, _, body = t.requests[-1]
        assert body is not None
        payload = json.loads(body.decode("utf-8"))
        assert payload["acknowledged_generation"] == 7
        assert payload["pid"] == 123
        assert payload["model_id"] == "bge-m3"

    def test_409_raises_conflict(self, tmp_path):
        t = _ScriptedTransport()
        t.queue("POST", "/actual-state", 409, {"detail": "stale generation"})
        client, _ = _make_client(tmp_path, t)
        snap = ActualStateSnapshot(acknowledged_generation=1, installed=True, running=True, warming_up=False)
        with pytest.raises(KbApiConflict):
            client.post_actual(snap)

    def test_401_invalidates_token(self, tmp_path):
        t = _ScriptedTransport()
        t.queue("POST", "/actual-state", 401, {"detail": "bad token"})
        client, src = _make_client(tmp_path, t)
        snap = ActualStateSnapshot(acknowledged_generation=1, installed=True, running=True, warming_up=False)
        with pytest.raises(KbApiUnauthorized):
            client.post_actual(snap)
        assert src._cached is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# ActualStateSnapshot 序列化
# ---------------------------------------------------------------------------


class TestActualStateSnapshotPayload:
    def test_to_payload_matches_schema_fields(self):
        """字段必须与 app/schemas.py:EmbeddingServiceActualStateRequest 对齐。

        若漏字段 / 多字段，kb-api 会返 422 → 壳层 reconcile 卡死。
        """
        snap = ActualStateSnapshot(
            acknowledged_generation=2, installed=True, running=True,
            warming_up=True, model_id="bge-m3", port=7687, pid=99,
            device="cpu", restart_count=1, last_error="hiccup",
        )
        payload = snap.to_payload()
        expected_keys = {
            "acknowledged_generation", "installed", "running", "warming_up",
            "model_id", "port", "pid", "device", "restart_count", "last_error",
        }
        assert set(payload.keys()) == expected_keys

    def test_pid_none_serializes_as_null(self):
        snap = ActualStateSnapshot(acknowledged_generation=0)
        payload = snap.to_payload()
        assert payload["pid"] is None


# ---------------------------------------------------------------------------
# EmbeddingProcessManager —— Batch B reconcile loop
# ---------------------------------------------------------------------------


class _FakeKbApi:
    """KbApiClient 测试替身：脚本化 desired-state 序列 + 收集 actual posts。"""

    def __init__(self) -> None:
        self.desired_queue: list[DesiredStateSnapshot] = []
        self.desired_errors: list[BaseException | None] = []
        self.actual_posts: list[ActualStateSnapshot] = []
        # actual post 副作用：默认返 200；可塞异常
        self.actual_errors: list[BaseException | None] = []

    def get_desired(self) -> DesiredStateSnapshot:
        if self.desired_errors:
            err = self.desired_errors.pop(0)
            if err is not None:
                raise err
        if not self.desired_queue:
            # 重复使用最后一个 desired，模拟稳态
            return DesiredStateSnapshot()
        return self.desired_queue.pop(0)

    def post_actual(self, snap: ActualStateSnapshot) -> dict:
        # 拷贝一份 snapshot 落进历史（防外部继续改动同一对象）
        self.actual_posts.append(ActualStateSnapshot(**{
            "acknowledged_generation": snap.acknowledged_generation,
            "installed": snap.installed,
            "running": snap.running,
            "warming_up": snap.warming_up,
            "model_id": snap.model_id,
            "port": snap.port,
            "pid": snap.pid,
            "device": snap.device,
            "restart_count": snap.restart_count,
            "last_error": snap.last_error,
        }))
        if self.actual_errors:
            err = self.actual_errors.pop(0)
            if err is not None:
                raise err
        return {
            "accepted": True,
            "acknowledged_generation": snap.acknowledged_generation,
            "updated_at": 0.0,
        }


class _RecordingHandler(ActionHandler):
    """记录被调用 + 可注入返回值；任何方法未配返回值则用 NotImplementedError."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, DesiredStateSnapshot]] = []
        self.responses: dict[str, ActualStateSnapshot] = {}
        self.exceptions: dict[str, BaseException] = {}

    def _do(self, name: str, desired: DesiredStateSnapshot, current: ActualStateSnapshot) -> ActualStateSnapshot:
        self.calls.append((name, desired))
        if name in self.exceptions:
            raise self.exceptions[name]
        if name in self.responses:
            return self.responses[name]
        raise NotImplementedError(f"no canned response for {name}")

    def install(self, desired, current):
        return self._do("install", desired, current)

    def start(self, desired, current):
        return self._do("start", desired, current)

    def stop(self, desired, current):
        return self._do("stop", desired, current)

    def switch_model(self, desired, current):
        return self._do("switch_model", desired, current)


def _make_manager(
    *, api: _FakeKbApi, handler: ActionHandler | None = None, **kwargs: object,
) -> EmbeddingProcessManager:
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    return EmbeddingProcessManager(
        client=api,  # type: ignore[arg-type]
        handler=handler or _RecordingHandler(),
        loop_period_sec=0.01,
        heartbeat_sec=5.0,
        clock=clock,
        sleep=sleep,
        **kwargs,
    )


class TestReconcileTick:
    def test_action_none_skips_dispatch_but_acks_generation(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(action="none", generation=3))
        mgr = _make_manager(api=api)

        mgr._tick()  # noqa: SLF001
        # action=none → handler 未被调；初次也要心跳一次 actual
        assert api.actual_posts, "首次 tick 应触发心跳回写 actual-state"
        assert api.actual_posts[-1].acknowledged_generation == 3

    def test_dispatches_install_action(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(
            action="install", model_id="bge-m3", generation=1, enabled=True,
        ))
        handler = _RecordingHandler()
        handler.responses["install"] = ActualStateSnapshot(
            acknowledged_generation=0, installed=True, running=False, warming_up=False,
            model_id="bge-m3",
        )
        mgr = _make_manager(api=api, handler=handler)
        mgr._tick()  # noqa: SLF001

        assert [c[0] for c in handler.calls] == ["install"]
        # actual 被回写 + acknowledged_generation = 1
        assert api.actual_posts[-1].acknowledged_generation == 1
        assert api.actual_posts[-1].installed is True
        assert api.actual_posts[-1].model_id == "bge-m3"

    def test_skips_duplicate_generation(self):
        """同一 generation 已 done → 后续 tick 不再 dispatch（contract §4 幂等）。"""
        api = _FakeKbApi()
        api.desired_queue.extend([
            DesiredStateSnapshot(action="install", model_id="m", generation=2),
            DesiredStateSnapshot(action="install", model_id="m", generation=2),
        ])
        handler = _RecordingHandler()
        handler.responses["install"] = ActualStateSnapshot(installed=True)
        mgr = _make_manager(api=api, handler=handler)

        mgr._tick()  # noqa: SLF001
        mgr._tick()  # noqa: SLF001

        # install 只被调一次
        assert [c[0] for c in handler.calls] == ["install"]

    def test_new_generation_triggers_new_dispatch(self):
        api = _FakeKbApi()
        api.desired_queue.extend([
            DesiredStateSnapshot(action="install", model_id="a", generation=1),
            DesiredStateSnapshot(action="install", model_id="b", generation=2),
        ])
        handler = _RecordingHandler()
        handler.responses["install"] = ActualStateSnapshot(installed=True)
        mgr = _make_manager(api=api, handler=handler)
        mgr._tick()  # noqa: SLF001
        mgr._tick()  # noqa: SLF001
        assert [c[0] for c in handler.calls] == ["install", "install"]

    def test_handler_exception_writes_last_error(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(action="install", model_id="m", generation=1))
        handler = _RecordingHandler()
        handler.exceptions["install"] = RuntimeError("disk full")
        mgr = _make_manager(api=api, handler=handler)
        mgr._tick()  # noqa: SLF001

        assert api.actual_posts[-1].last_error.startswith("install failed: disk full")
        # generation 仍被 ack，避免 reconcile 死循环重试
        assert api.actual_posts[-1].acknowledged_generation == 1

    def test_unknown_action_records_error_no_handler_call(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(action="bogus", generation=4))
        handler = _RecordingHandler()
        mgr = _make_manager(api=api, handler=handler)
        mgr._tick()  # noqa: SLF001
        assert handler.calls == []
        assert "unknown action: bogus" in api.actual_posts[-1].last_error

    def test_handler_not_implemented_treated_like_known_error(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(action="start", generation=2))
        # 默认 _RecordingHandler.start 抛 NotImplementedError（没塞 responses）
        mgr = _make_manager(api=api, handler=_RecordingHandler())
        mgr._tick()  # noqa: SLF001
        assert "handler not implemented" in api.actual_posts[-1].last_error


class TestReconcileBackoff:
    def test_transport_error_triggers_backoff_growth(self):
        api = _FakeKbApi()
        # get_desired 连续 4 次 transport error
        for _ in range(4):
            api.desired_errors.append(KbApiTransportError("conn refused"))
        mgr = _make_manager(api=api)

        backoffs: list[float] = []
        for _ in range(4):
            mgr._tick()  # noqa: SLF001
            backoffs.append(mgr._backoff)  # noqa: SLF001
        # 1s → 2s → 4s → 8s
        assert backoffs == [1.0, 2.0, 4.0, 8.0]

    def test_backoff_capped_at_max(self):
        api = _FakeKbApi()
        for _ in range(20):
            api.desired_errors.append(KbApiTransportError("nope"))
        mgr = _make_manager(api=api, max_backoff_sec=5.0)
        for _ in range(20):
            mgr._tick()  # noqa: SLF001
        assert mgr._backoff == 5.0  # noqa: SLF001

    def test_backoff_resets_on_success(self):
        api = _FakeKbApi()
        api.desired_errors.append(KbApiTransportError("err"))
        api.desired_queue.append(DesiredStateSnapshot(action="none", generation=1))
        mgr = _make_manager(api=api)
        mgr._tick()  # noqa: SLF001
        assert mgr._backoff == 1.0  # noqa: SLF001
        mgr._tick()  # noqa: SLF001
        assert mgr._backoff == 0.0  # noqa: SLF001


class TestReconcileHeartbeat:
    def test_heartbeat_only_after_interval(self):
        api = _FakeKbApi()
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        for _ in range(3):
            api.desired_queue.append(DesiredStateSnapshot(action="none", generation=1))
        mgr = EmbeddingProcessManager(
            client=api,  # type: ignore[arg-type]
            handler=_RecordingHandler(),
            loop_period_sec=0.0,
            heartbeat_sec=5.0,
            clock=clock,
            sleep=sleep,
        )
        # tick 1：首次必然心跳一次
        mgr._tick()  # noqa: SLF001
        assert len(api.actual_posts) == 1
        # 时间只走了 0s → 下一 tick 不应再心跳
        mgr._tick()  # noqa: SLF001
        assert len(api.actual_posts) == 1
        # 时间推进 6s → 应该再心跳
        clock.now += 6.0
        mgr._tick()  # noqa: SLF001
        assert len(api.actual_posts) == 2

    def test_409_on_post_actual_swallowed(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(action="none", generation=1))
        api.actual_errors.append(KbApiConflict("stale"))
        mgr = _make_manager(api=api)
        # 不应抛
        mgr._tick()  # noqa: SLF001


class TestStartStop:
    def test_start_is_idempotent(self):
        api = _FakeKbApi()
        mgr = _make_manager(api=api)
        mgr.start()
        first_thread = mgr._thread  # noqa: SLF001
        mgr.start()
        assert mgr._thread is first_thread  # noqa: SLF001
        mgr.stop(timeout=1.0)

    def test_stop_returns_quickly_when_loop_blocked_in_sleep(self):
        api = _FakeKbApi()
        api.desired_queue.append(DesiredStateSnapshot(action="none", generation=1))
        # 用真 time.sleep + 短周期；stop 必须能让 wait 立即返回
        mgr = EmbeddingProcessManager(
            client=api,  # type: ignore[arg-type]
            handler=_RecordingHandler(),
            loop_period_sec=30.0,    # 故意拉长，验证 stop_event 提前唤醒
            heartbeat_sec=5.0,
        )
        mgr.start()
        import time as _real_time
        _real_time.sleep(0.1)  # 给 loop 一点跑起来时间
        t0 = _real_time.monotonic()
        mgr.stop(timeout=2.0)
        elapsed = _real_time.monotonic() - t0
        assert elapsed < 1.0, f"stop 应秒回，实际 {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Batch C: InstallStatusWriter
# ---------------------------------------------------------------------------


class TestInstallStatusWriter:
    def test_flush_writes_json_with_required_fields(self, tmp_path):
        path = tmp_path / "runtime" / "install_status.json"
        clock = _FakeClock()
        clock.now = 100.0
        w = InstallStatusWriter(path, clock=clock)
        clock.now = 105.0
        w.flush(phase="preparing", progress=0.1, message="hi", total_bytes=999)

        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["phase"] == "preparing"
        assert body["progress"] == 0.1
        assert body["message"] == "hi"
        assert body["total_bytes"] == 999
        assert body["started_at"] == 100.0
        assert body["updated_at"] == 105.0
        assert body["error"] == ""

    def test_flush_creates_parent_dir(self, tmp_path):
        path = tmp_path / "a" / "b" / "c" / "install_status.json"
        w = InstallStatusWriter(path)
        w.flush(phase="preparing")
        assert path.exists()

    def test_flush_overwrites_atomically(self, tmp_path):
        """两次 flush 之间不应留下 .tmp 残留。"""
        path = tmp_path / "install_status.json"
        w = InstallStatusWriter(path)
        w.flush(phase="preparing")
        w.flush(phase="downloading")
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["phase"] == "downloading"
        # 确认没 .tmp 残留
        assert not list(tmp_path.glob("*.tmp"))

    def test_progress_clamped_to_unit_range(self, tmp_path):
        path = tmp_path / "install_status.json"
        w = InstallStatusWriter(path)
        w.flush(phase="downloading", progress=-0.5)
        assert json.loads(path.read_text(encoding="utf-8"))["progress"] == 0.0
        w.flush(phase="downloading", progress=2.0)
        assert json.loads(path.read_text(encoding="utf-8"))["progress"] == 1.0

    def test_unknown_phase_rejected(self, tmp_path):
        w = InstallStatusWriter(tmp_path / "install_status.json")
        with pytest.raises(ValueError):
            w.flush(phase="bogus")


# ---------------------------------------------------------------------------
# Batch C: InstallExecutor
# ---------------------------------------------------------------------------


class _RecordingRunner(CommandRunner):
    """脚本化 CommandRunner：按调用次序返回预设结果，并记录所有调用。

    ``auto_stub_weights_on_success``：下载命令(cmd 含 snapshot_download)返 rc=0 时，
    从 python -c script 里 grep 出 ``local_dir=`` 参数，在该目录写 config.json +
    50MB+1 字节的 pytorch_model.bin，模拟真下载成功让 InstallExecutor post-check
    _is_model_dir_complete 命中。silent-success 护栏测试要显式关掉这个 flag。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.results: list[CommandResult] = []
        self.auto_stub_weights_on_success = True
        # _venv_deps_ready 探测的默认结果；None → 默认 rc=1（探测失败，走原 pip 路径）。
        # 让老 happy path 断言不用感知 probe 步骤；新 skip 测试显式 override。
        self.probe_default_result: Optional[CommandResult] = None
        self.probe_calls: list[list[str]] = []

    def queue(self, returncode: int, stdout_tail: str = "") -> None:
        self.results.append(CommandResult(returncode=returncode, stdout_tail=stdout_tail))

    def run(self, cmd, *, cwd=None, log_path=None, env=None, timeout=None):  # type: ignore[override]
        joined_args = " ".join(str(a) for a in cmd)
        # _venv_deps_ready 探测（venv_python -c "import infinity_emb, torch, ..."）
        # 走独立返回通道，不进 self.calls / self.results 消耗，保护老 happy path
        # 三 queue（venv/pip/download）断言不变。
        if "import infinity_emb, torch, huggingface_hub" in joined_args:
            self.probe_calls.append(list(cmd))
            if self.probe_default_result is not None:
                return self.probe_default_result
            return CommandResult(returncode=1, stdout_tail="probe default fail\n")
        self.calls.append((list(cmd), log_path))
        if not self.results:
            raise AssertionError(f"no canned result for {cmd}")
        result = self.results.pop(0)
        if (
            self.auto_stub_weights_on_success
            and result.returncode == 0
            and any("snapshot_download" in str(a) for a in cmd)
        ):
            self._stub_weights_from_cmd(cmd)
        return result

    @staticmethod
    def _stub_weights_from_cmd(cmd) -> None:
        script = " ".join(str(a) for a in cmd)
        # cmd 里的 script 形如 ``...local_dir='C:/...',endpoint=...``
        import re
        m = re.search(r"local_dir=(['\"])(.+?)\1", script)
        if not m:
            return
        local_dir = Path(m.group(2))
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "config.json").write_text("{}", encoding="utf-8")
        weight = local_dir / "pytorch_model.bin"
        min_bytes = 50 * 1024 * 1024 + 1024
        with weight.open("wb") as f:
            f.seek(min_bytes - 1)
            f.write(b"\0")


def _make_install_spec(tmp_path) -> InstallSpec:
    venv_dir = tmp_path / "embedding-service" / "venv"
    model_dir = tmp_path / "models" / "bge-m3"
    # 模拟 venv 存在（让 _build_download_cmd 找到 venv python）
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    return InstallSpec(
        model_id="BAAI/bge-m3",
        venv_dir=str(venv_dir),
        model_dir=str(model_dir),
        device="cpu",
        create_venv_cmd=["python", "-m", "venv", str(venv_dir)],
        pip_install_cmd=["/bin/sh", "-c", f"{venv_dir}/bin/python -m pip install --upgrade pip && {venv_dir}/bin/python -m pip install 'infinity-emb[server,torch]' 'huggingface_hub<1.0'"],
        download_args={
            "repo_id": "BAAI/bge-m3",
            "local_dir": str(model_dir),
            "endpoint": "https://hf-mirror.com",
        },
    )


def _make_executor(tmp_path, runner):
    status_path = tmp_path / "runtime" / "install_status.json"
    pip_log = tmp_path / "logs" / "pip.log"
    writer = InstallStatusWriter(status_path)
    return InstallExecutor(
        status_writer=writer, pip_log_path=pip_log, runner=runner,
    ), status_path


class TestInstallExecutorHappyPath:
    def test_runs_three_commands_in_order(self, tmp_path):
        runner = _RecordingRunner()
        runner.queue(0)  # venv
        runner.queue(0)  # pip
        runner.queue(0)  # download
        executor, status_path = _make_executor(tmp_path, runner)
        ok = executor.execute(_make_install_spec(tmp_path))
        assert ok is True

        # 三步顺序：venv → pip → download
        cmds = [c[0] for c in runner.calls]
        assert cmds[0][0] == "python" and cmds[0][1] == "-m" and cmds[0][2] == "venv"
        assert "pip" in cmds[1][0] or "pip" in " ".join(cmds[1])
        assert "snapshot_download" in cmds[2][2]  # python -c "...snapshot_download..."

        # status 最终 = completed
        body = json.loads(status_path.read_text(encoding="utf-8"))
        assert body["phase"] == "completed"
        assert body["progress"] == 1.0

    def test_phase_progression(self, tmp_path):
        runner = _RecordingRunner()
        for _ in range(3):
            runner.queue(0)
        # 用 patched writer 来捕获所有 phase 序列
        phases: list[str] = []
        original_flush = InstallStatusWriter.flush

        def capturing_flush(self, **kw):  # type: ignore[no-redef]
            phases.append(kw.get("phase", ""))
            return original_flush(self, **kw)
        InstallStatusWriter.flush = capturing_flush  # type: ignore[assignment]
        try:
            executor, _ = _make_executor(tmp_path, runner)
            assert executor.execute(_make_install_spec(tmp_path))
        finally:
            InstallStatusWriter.flush = original_flush  # type: ignore[assignment]

        # 必须按 contract §3.3 顺序推进
        assert phases == ["preparing", "pip_installing", "downloading", "downloading", "completed"]

    def test_pip_log_path_passed_to_runner(self, tmp_path):
        runner = _RecordingRunner()
        for _ in range(3):
            runner.queue(0)
        executor, _ = _make_executor(tmp_path, runner)
        executor.execute(_make_install_spec(tmp_path))
        # 所有 3 步都 tee 到 logs/pip.log
        for _cmd, log_path in runner.calls:
            assert log_path is not None and log_path.name == "pip.log"


class TestInstallExecutorFailures:
    def test_venv_failure_writes_failed_phase(self, tmp_path):
        runner = _RecordingRunner()
        runner.queue(1, stdout_tail="python: command not found\n")
        executor, status_path = _make_executor(tmp_path, runner)
        assert executor.execute(_make_install_spec(tmp_path)) is False

        body = json.loads(status_path.read_text(encoding="utf-8"))
        assert body["phase"] == "failed"
        assert "venv" in body["message"]
        assert "command not found" in body["error"]
        # 后续步骤未执行
        assert len(runner.calls) == 1

    def test_pip_failure_writes_failed_phase(self, tmp_path):
        runner = _RecordingRunner()
        runner.queue(0)
        runner.queue(1, stdout_tail="ERROR: No matching distribution\n")
        executor, status_path = _make_executor(tmp_path, runner)
        assert executor.execute(_make_install_spec(tmp_path)) is False
        body = json.loads(status_path.read_text(encoding="utf-8"))
        assert body["phase"] == "failed"
        assert "pip install" in body["message"]


class TestInstallExecutorMirrorFallback:
    def test_tries_next_mirror_on_failure(self, tmp_path):
        """download 步：第一个 endpoint 失败 → 走 mirror_chain 第二个。"""
        runner = _RecordingRunner()
        runner.queue(0)  # venv
        runner.queue(0)  # pip
        runner.queue(1, stdout_tail="hf-mirror DNS fail\n")  # download mirror 1
        runner.queue(0)  # download mirror 2
        executor, status_path = _make_executor(tmp_path, runner)
        ok = executor.execute(_make_install_spec(tmp_path))
        assert ok is True

        # 两次下载，endpoint 不同
        download_cmds = [c[0] for c in runner.calls[2:]]
        assert len(download_cmds) == 2
        assert "hf-mirror.com" in download_cmds[0][2]
        assert "huggingface.co" in download_cmds[1][2]
        # 最终 completed
        assert json.loads(status_path.read_text(encoding="utf-8"))["phase"] == "completed"

    def test_all_mirrors_fail_writes_failed(self, tmp_path):
        runner = _RecordingRunner()
        runner.queue(0)  # venv
        runner.queue(0)  # pip
        runner.queue(1)  # mirror 1
        runner.queue(1)  # mirror 2
        executor, status_path = _make_executor(tmp_path, runner)
        assert executor.execute(_make_install_spec(tmp_path)) is False
        body = json.loads(status_path.read_text(encoding="utf-8"))
        assert body["phase"] == "failed"
        assert "镜像下载失败" in body["message"]

    def test_rc0_but_weights_missing_treated_as_silent_success(self, tmp_path):
        """护栏（2026-07-01 加）：huggingface_hub 撞 mirror 403 后会兜底 fallback
        到 huggingface.co → 国内 timeout → 静默返回 local_dir 当作成功
        (bytes_downloaded=0, returncode=0)。老大 v1.3.12 装机踩过这坑，
        install phase=completed 但 infinity 启动撞 OSError(no pytorch_model.bin)。

        修复：rc=0 但权重缺失 → 视为 silent-success → 走下一个 mirror。
        """
        runner = _RecordingRunner()
        runner.auto_stub_weights_on_success = False  # 关掉 auto stub 模拟 silent-success
        runner.queue(0)  # venv
        runner.queue(0)  # pip
        runner.queue(0)  # mirror 1 - rc=0 但没建权重（silent-success）
        runner.queue(1)  # mirror 2 - 真失败
        executor, status_path = _make_executor(tmp_path, runner)
        assert executor.execute(_make_install_spec(tmp_path)) is False
        # 两个 mirror 都被访问（silent-success 也算失败继续 fallback）
        download_cmds = [c[0] for c in runner.calls[2:]]
        assert len(download_cmds) == 2
        body = json.loads(status_path.read_text(encoding="utf-8"))
        assert body["phase"] == "failed"
        # error 提示这是 silent fallback，方便运维定位
        assert "silent-fallback" in body["error"] or "silent" in body["error"].lower()

    def test_pip_skipped_when_venv_deps_ready(self, tmp_path):
        """2026-07-01 加：venv 里 infinity_emb+torch+huggingface_hub 都 import
        得动时 → _pip_install 直接 return，不再跑 pip_install_cmd。
        触发场景：installer 选保留覆盖装（venv 保留）、跨机拷贝 venv。
        """
        from embedding_process_manager import CommandResult
        runner = _RecordingRunner()
        runner.queue(0)  # venv
        # 不 queue pip（应该被 skip）
        runner.queue(0)  # download
        runner.probe_default_result = CommandResult(returncode=0, stdout_tail="")

        executor, status_path = _make_executor(tmp_path, runner)
        assert executor.execute(_make_install_spec(tmp_path)) is True

        # 只有 2 个非探测 call：venv、download。pip 被 skip 了
        cmds = [c[0] for c in runner.calls]
        assert len(cmds) == 2
        assert cmds[0][1] == "-m" and cmds[0][2] == "venv"
        assert "snapshot_download" in cmds[1][2]
        # 探测确实跑过（走独立通道 probe_calls）
        assert len(runner.probe_calls) == 1
        assert "import infinity_emb" in " ".join(str(a) for a in runner.probe_calls[0])
        # phase 序列跳过 pip_installing 是 acceptable（走 downloading 消息）
        body = json.loads(status_path.read_text(encoding="utf-8"))
        assert body["phase"] == "completed"

    def test_pip_runs_when_venv_probe_fails(self, tmp_path):
        """探测 rc!=0（venv 缺 infinity_emb / 装了但破了）时必须走原 pip 路径。
        防 skip 逻辑被过度触发导致漏装。
        """
        from embedding_process_manager import CommandResult
        runner = _RecordingRunner()
        runner.queue(0)  # venv
        runner.queue(0)  # pip
        runner.queue(0)  # download
        # probe 默认 fail（rc=1）→ 走原 pip 路径

        executor, _ = _make_executor(tmp_path, runner)
        assert executor.execute(_make_install_spec(tmp_path)) is True
        cmds = [c[0] for c in runner.calls]
        assert len(cmds) == 3  # venv + pip + download
        assert "pip" in cmds[1][0] or "pip" in " ".join(str(a) for a in cmds[1])
        # probe 也跑了但 fail
        assert len(runner.probe_calls) == 1

    def test_download_cmd_includes_ignore_patterns(self, tmp_path):
        """hf-mirror 对 imgs/.DS_Store 等文件返 403，snapshot_download 必须
        带 ignore_patterns 跳过；不带就会撞 403 → 静默 fallback → silent-success。
        """
        runner = _RecordingRunner()
        runner.queue(0)  # venv
        runner.queue(0)  # pip
        runner.queue(0)  # download
        executor, _ = _make_executor(tmp_path, runner)
        executor.execute(_make_install_spec(tmp_path))
        download_script = runner.calls[2][0][2]  # python -c "<script>"
        assert "ignore_patterns" in download_script
        # 必须涵盖已知踩坑三项
        for pat in ("imgs/*", ".DS_Store", ".gitattributes"):
            assert pat in download_script, f"missing ignore pattern: {pat}"


# ---------------------------------------------------------------------------
# Batch D: StartHandler / SubprocessSpawner / HealthProbe
# ---------------------------------------------------------------------------


class _FakeHandle(ProcessHandle):
    """ProcessHandle 测试替身。"""
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._exit: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self._exit

    def set_exited(self, code: int) -> None:
        self._exit = code

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _RecordingSpawner(SubprocessSpawner):
    def __init__(self) -> None:
        self.calls: list[tuple[list, Path | None]] = []
        self.env_calls: list[dict[str, str] | None] = []
        self.next_handle: ProcessHandle | None = _FakeHandle(pid=12345)
        self.raise_exc: Exception | None = None

    def spawn(self, cmd, *, cwd=None, env=None, log_path=None):  # type: ignore[override]
        self.calls.append((list(cmd), log_path))
        self.env_calls.append(dict(env) if env else None)
        if self.raise_exc:
            raise self.raise_exc
        h = self.next_handle
        assert h is not None
        return h


class _ScriptedProbe(HealthProbe):
    """每次 is_ready 按队列返回。"""
    def __init__(self, sequence: list[bool]) -> None:
        self._seq = list(sequence)
        self.calls = 0

    def is_ready(self, port, *, timeout_sec=2.0):
        self.calls += 1
        if not self._seq:
            return False
        return self._seq.pop(0)


def _make_start_spec(tmp_path) -> StartSpec:
    runtime_dir = tmp_path / "runtime"
    return StartSpec(
        model_id="bge-m3",
        device="cpu",
        start_cmd=["infinity_emb", "v2", "--port", "7687", "--model-id", "bge-m3", "--device", "cpu"],
        port=7687,
        runtime_dir=runtime_dir,
        infinity_log_path=tmp_path / "logs" / "infinity.log",
    )


def _make_handler(probe: HealthProbe, spawner: SubprocessSpawner | None = None, **kw):
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    return StartHandler(
        spawner=spawner or _RecordingSpawner(),
        probe=probe,
        warmup_timeout_sec=kw.pop("warmup_timeout_sec", 10.0),
        probe_interval_sec=kw.pop("probe_interval_sec", 1.0),
        clock=clock,
        sleep=sleep,
        **kw,
    )


class TestStartHandlerSpawn:
    def test_spawn_writes_pid_and_port_files(self, tmp_path):
        spawner = _RecordingSpawner()
        probe = _ScriptedProbe([True])  # 立即 ready
        handler = _make_handler(probe, spawner=spawner)
        spec = _make_start_spec(tmp_path)

        handle, ready, err = handler.spawn_and_wait_ready(spec)
        assert handle is not None and ready is True and err == ""
        assert (spec.runtime_dir / "pid").read_text(encoding="utf-8") == "12345"
        assert (spec.runtime_dir / "port").read_text(encoding="utf-8") == "7687"

    def test_spawn_exception_returns_error(self, tmp_path):
        spawner = _RecordingSpawner()
        spawner.raise_exc = OSError("no exec")
        handler = _make_handler(_ScriptedProbe([]), spawner=spawner)

        handle, ready, err = handler.spawn_and_wait_ready(_make_start_spec(tmp_path))
        assert handle is None and ready is False
        assert "spawn failed" in err

    def test_passes_log_path_to_spawner(self, tmp_path):
        spawner = _RecordingSpawner()
        handler = _make_handler(_ScriptedProbe([True]), spawner=spawner)
        spec = _make_start_spec(tmp_path)
        handler.spawn_and_wait_ready(spec)
        _cmd, log_path = spawner.calls[0]
        assert log_path == spec.infinity_log_path

    def test_passes_env_to_spawner(self, tmp_path):
        """plan.env 必须透传到 spawn 子进程；否则 infinity 启动撞
        NameError(BetterTransformerManager) → exit 3（v1.3.13 修复）。"""
        spawner = _RecordingSpawner()
        handler = _make_handler(_ScriptedProbe([True]), spawner=spawner)
        runtime_dir = tmp_path / "runtime"
        spec = StartSpec(
            model_id="bge-m3",
            device="cpu",
            start_cmd=["infinity_emb", "v2", "--port", "7687"],
            port=7687,
            runtime_dir=runtime_dir,
            infinity_log_path=tmp_path / "logs" / "infinity.log",
            env={"INFINITY_BETTERTRANSFORMER": "false"},
        )
        handler.spawn_and_wait_ready(spec)
        assert spawner.env_calls[0] == {"INFINITY_BETTERTRANSFORMER": "false"}

    def test_empty_env_passed_as_none(self, tmp_path):
        """spec.env 空时 spawn 收到 env=None（不强制传空 dict 污染 os.environ 合并）。"""
        spawner = _RecordingSpawner()
        handler = _make_handler(_ScriptedProbe([True]), spawner=spawner)
        handler.spawn_and_wait_ready(_make_start_spec(tmp_path))
        assert spawner.env_calls[0] is None


class TestStartHandlerHealth:
    def test_polls_until_ready(self, tmp_path):
        spawner = _RecordingSpawner()
        probe = _ScriptedProbe([False, False, True])
        handler = _make_handler(probe, spawner=spawner)

        handle, ready, err = handler.spawn_and_wait_ready(_make_start_spec(tmp_path))
        assert ready is True and err == ""
        assert probe.calls == 3

    def test_timeout_returns_handle_with_warming(self, tmp_path):
        """超时但子进程仍活着：返回 handle + ready=False + 超时错。
        让 manager 后续 tick 继续观测，不算 spawn 失败。"""
        spawner = _RecordingSpawner()
        probe = _ScriptedProbe([])  # 永远不 ready
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        handler = StartHandler(
            spawner=spawner,
            probe=probe,
            warmup_timeout_sec=3.0,
            probe_interval_sec=1.0,
            clock=clock,
            sleep=sleep,
        )

        handle, ready, err = handler.spawn_and_wait_ready(_make_start_spec(tmp_path))
        assert handle is not None
        assert ready is False
        assert "warmup timeout" in err

    def test_early_exit_detected(self, tmp_path):
        """子进程 spawn 后立即崩 → poll() 返非 None → 立即失败返回。"""
        spawner = _RecordingSpawner()
        dead = _FakeHandle(pid=999)
        dead.set_exited(137)  # 模拟 OOM killer
        spawner.next_handle = dead
        probe = _ScriptedProbe([False])  # probe 来不及成功
        handler = _make_handler(probe, spawner=spawner)

        handle, ready, err = handler.spawn_and_wait_ready(_make_start_spec(tmp_path))
        assert handle is None
        assert ready is False
        assert "exited during warmup with code 137" in err


# ---------------------------------------------------------------------------
# Batch E: StopHandler
# ---------------------------------------------------------------------------


class TestStopHandlerGraceful:
    def test_terminate_then_dies_within_grace(self, tmp_path):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "pid").write_text("123")
        (runtime_dir / "port").write_text("7687")
        handle = _FakeHandle(pid=123)
        # 模拟 terminate 后立即退出（grace 期内）
        clock = _FakeClock()
        sleep = _FakeSleep(clock)

        # 让 fake handle 在第一次 poll 时还没退；第二次起返回 0
        poll_calls = {"n": 0}
        orig_poll = handle.poll

        def poll_then_exit():
            poll_calls["n"] += 1
            if poll_calls["n"] >= 2:
                handle._exit = 0  # noqa: SLF001
            return orig_poll()
        handle.poll = poll_then_exit  # type: ignore[method-assign]

        stop = StopHandler(grace_sec=3.0, poll_interval_sec=0.1, clock=clock, sleep=sleep)
        graceful, err = stop.terminate_and_wait(handle, runtime_dir)
        assert graceful is True
        assert err == ""
        assert handle.terminate_calls == 1
        assert handle.kill_calls == 0
        # pid / port 文件被清
        assert not (runtime_dir / "pid").exists()
        assert not (runtime_dir / "port").exists()


class TestStopHandlerForceKill:
    def test_sigterm_ignored_falls_back_to_sigkill(self, tmp_path):
        """AC14a：3 秒不死 → kill。"""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        handle = _FakeHandle(pid=456)
        clock = _FakeClock()
        sleep = _FakeSleep(clock)

        # poll 始终返 None（grace 阶段），kill 后第二轮才退出
        poll_state = {"after_kill": False}
        orig_poll = handle.poll

        def stubborn_poll():
            if not poll_state["after_kill"]:
                return None
            handle._exit = -9  # noqa: SLF001
            return orig_poll()
        handle.poll = stubborn_poll  # type: ignore[method-assign]
        orig_kill = handle.kill

        def kill_marks_alive_then_dies():
            orig_kill()
            poll_state["after_kill"] = True
        handle.kill = kill_marks_alive_then_dies  # type: ignore[method-assign]

        stop = StopHandler(grace_sec=3.0, poll_interval_sec=0.5, clock=clock, sleep=sleep)
        graceful, err = stop.terminate_and_wait(handle, runtime_dir)
        assert graceful is False
        assert handle.terminate_calls == 1
        assert handle.kill_calls == 1
        # 已被 kill 杀死，err 应为空
        assert err == ""

    def test_kill_also_fails(self, tmp_path):
        """SIGKILL 后仍存活（极端情况，验证 last_error 报告）。"""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        handle = _FakeHandle(pid=789)  # 始终不退出
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        stop = StopHandler(grace_sec=2.0, poll_interval_sec=0.5, clock=clock, sleep=sleep)
        graceful, err = stop.terminate_and_wait(handle, runtime_dir)
        assert graceful is False
        assert handle.terminate_calls == 1
        assert handle.kill_calls == 1
        assert "did not respond to SIGKILL" in err


# ---------------------------------------------------------------------------
# Batch E: StaleResidueCleaner
# ---------------------------------------------------------------------------


class _FixedCmdlineProbe(_PsCmdlineProbe):
    def __init__(self, mapping: dict[int, str]) -> None:
        # 故意不调 super().__init__，避免 import DefaultCommandRunner
        self._mapping = mapping

    def cmdline(self, pid: int) -> str:  # type: ignore[override]
        return self._mapping.get(pid, "")


class TestStaleResidueCleaner:
    def test_no_pid_file_returns_none(self, tmp_path):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        cleaner = StaleResidueCleaner(
            runtime_dir=runtime_dir,
            cmdline_probe=_FixedCmdlineProbe({}),
            pid_alive_fn=lambda pid: False,
        )
        pid, port = cleaner.adopt_or_clean("bge-m3")
        assert pid is None and port is None

    def test_dead_pid_cleans_files(self, tmp_path):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "pid").write_text("9999")
        (runtime_dir / "port").write_text("7687")
        cleaner = StaleResidueCleaner(
            runtime_dir=runtime_dir,
            cmdline_probe=_FixedCmdlineProbe({}),
            pid_alive_fn=lambda pid: False,
        )
        pid, port = cleaner.adopt_or_clean("bge-m3")
        assert pid is None and port is None
        assert not (runtime_dir / "pid").exists()
        assert not (runtime_dir / "port").exists()

    def test_alive_pid_with_matching_cmdline_adopted(self, tmp_path):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "pid").write_text("12345")
        (runtime_dir / "port").write_text("7687")
        cmdline = "/path/to/infinity_emb v2 --port 7687 --model-id bge-m3"
        cleaner = StaleResidueCleaner(
            runtime_dir=runtime_dir,
            cmdline_probe=_FixedCmdlineProbe({12345: cmdline}),
            pid_alive_fn=lambda pid: pid == 12345,
        )
        pid, port = cleaner.adopt_or_clean("bge-m3")
        assert pid == 12345
        assert port == 7687
        # 文件保留（adopt 不清）
        assert (runtime_dir / "pid").exists()

    def test_alive_pid_foreign_returns_stale_port(self, tmp_path):
        """PID 复用：上次自己的 PID 被别的程序拿了 → 不动它，告诉 caller 换端口。"""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "pid").write_text("12345")
        (runtime_dir / "port").write_text("7687")
        cmdline = "/usr/bin/something-else --opt"
        cleaner = StaleResidueCleaner(
            runtime_dir=runtime_dir,
            cmdline_probe=_FixedCmdlineProbe({12345: cmdline}),
            pid_alive_fn=lambda pid: True,
        )
        pid, port = cleaner.adopt_or_clean("bge-m3")
        assert pid is None  # 不 adopt
        assert port == 7687  # 但告诉 caller stale 端口

    def test_alive_pid_wrong_model_treated_as_foreign(self, tmp_path):
        """cmdline 含 infinity 但 model_id 不匹配 → 视为外人（可能是切模型残留）。"""
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "pid").write_text("100")
        (runtime_dir / "port").write_text("7687")
        cmdline = "infinity_emb v2 --port 7687 --model-id qwen3-embedding-0.6b"
        cleaner = StaleResidueCleaner(
            runtime_dir=runtime_dir,
            cmdline_probe=_FixedCmdlineProbe({100: cmdline}),
            pid_alive_fn=lambda pid: True,
        )
        pid, port = cleaner.adopt_or_clean("bge-m3")
        assert pid is None
        assert port == 7687

    def test_corrupt_pid_file_treated_as_missing(self, tmp_path):
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        (runtime_dir / "pid").write_text("not-a-number")
        cleaner = StaleResidueCleaner(
            runtime_dir=runtime_dir,
            cmdline_probe=_FixedCmdlineProbe({}),
            pid_alive_fn=lambda pid: False,
        )
        pid, port = cleaner.adopt_or_clean("bge-m3")
        assert pid is None
        assert port is None


# ---------------------------------------------------------------------------
# Batch F: EmbeddingActionHandler 串联
# ---------------------------------------------------------------------------


class _StubInstaller(InstallExecutor):
    """InstallExecutor 测试替身：可控成功/失败 + 记录调用。"""
    def __init__(self, ok: bool = True) -> None:
        # 不调 super().__init__；不需要 status_writer / runner
        self.ok = ok
        self.calls: list[InstallSpec] = []

    def execute(self, spec: InstallSpec) -> bool:  # type: ignore[override]
        self.calls.append(spec)
        return self.ok


class _StubStarter(StartHandler):
    def __init__(self) -> None:
        self.handle_to_return: ProcessHandle | None = _FakeHandle(pid=1111)
        self.ready: bool = True
        self.err: str = ""
        self.calls: list[StartSpec] = []

    def spawn_and_wait_ready(self, spec):  # type: ignore[override]
        self.calls.append(spec)
        return self.handle_to_return, self.ready, self.err


class _StubStopper(StopHandler):
    def __init__(self) -> None:
        self.graceful = True
        self.err = ""
        self.calls: list[tuple[ProcessHandle, Path]] = []

    def terminate_and_wait(self, handle, runtime_dir):  # type: ignore[override]
        self.calls.append((handle, runtime_dir))
        return self.graceful, self.err


class _StubResidue(StaleResidueCleaner):
    def __init__(self, adopt_pid=None, stale_port=None):
        self.adopt_pid = adopt_pid
        self.stale_port = stale_port
        self.calls = 0

    def adopt_or_clean(self, expected_model_id):  # type: ignore[override]
        self.calls += 1
        return self.adopt_pid, self.stale_port


def _make_action_handler(
    *, installer=None, starter=None, stopper=None, cleaner=None,
    install_spec=None, start_spec=None, runtime_dir=None,
):
    def spec_factory(desired, current):
        return EmbeddingActionContext(
            install_spec=install_spec, start_spec=start_spec,
            runtime_dir=runtime_dir,
        )
    return EmbeddingActionHandler(
        install_executor=installer or _StubInstaller(),
        start_handler=starter or _StubStarter(),
        stop_handler=stopper or _StubStopper(),
        residue_cleaner=cleaner or _StubResidue(),
        spec_factory=spec_factory,
    )


class TestEmbeddingActionHandlerInstall:
    def test_install_success_marks_installed(self, tmp_path):
        installer = _StubInstaller(ok=True)
        h = _make_action_handler(
            installer=installer,
            install_spec=_make_install_spec(tmp_path),
        )
        desired = DesiredStateSnapshot(action="install", model_id="bge-m3", device="cpu", generation=1)
        out = h.install(desired, ActualStateSnapshot())
        assert out.installed is True
        assert out.model_id == "bge-m3"
        assert out.last_error == ""
        assert len(installer.calls) == 1

    def test_install_failure_propagates(self, tmp_path):
        h = _make_action_handler(
            installer=_StubInstaller(ok=False),
            install_spec=_make_install_spec(tmp_path),
        )
        desired = DesiredStateSnapshot(action="install", model_id="bge-m3", generation=1)
        out = h.install(desired, ActualStateSnapshot())
        assert out.installed is False
        assert "install failed" in out.last_error

    def test_install_missing_spec_short_circuits(self):
        h = _make_action_handler(install_spec=None)
        out = h.install(DesiredStateSnapshot(action="install"), ActualStateSnapshot())
        assert "install spec missing" in out.last_error


class TestEmbeddingActionHandlerStart:
    def test_fresh_start_spawns(self, tmp_path):
        starter = _StubStarter()
        h = _make_action_handler(
            starter=starter,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        desired = DesiredStateSnapshot(
            action="start", model_id="bge-m3", device="cpu", enabled=True, generation=2,
        )
        out = h.start(desired, ActualStateSnapshot())
        assert out.running is True
        assert out.warming_up is False
        assert out.pid == 1111
        assert h.current_handle is not None

    def test_warming_up_when_probe_times_out(self, tmp_path):
        starter = _StubStarter()
        starter.ready = False
        starter.err = "warmup timeout"
        h = _make_action_handler(
            starter=starter,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        out = h.start(DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True), ActualStateSnapshot())
        assert out.running is True
        assert out.warming_up is True
        assert out.last_error == "warmup timeout"

    def test_adopts_existing_pid_skips_spawn(self, tmp_path):
        starter = _StubStarter()
        h = _make_action_handler(
            starter=starter,
            cleaner=_StubResidue(adopt_pid=5555, stale_port=7687),
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        out = h.start(DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True), ActualStateSnapshot())
        assert starter.calls == [], "adopt 时不应 spawn 新进程"
        assert out.running is True
        assert out.pid == 5555
        assert out.warming_up is False

    def test_spawn_failure_sets_error(self, tmp_path):
        starter = _StubStarter()
        starter.handle_to_return = None
        starter.ready = False
        starter.err = "spawn failed: no exec"
        h = _make_action_handler(
            starter=starter,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        out = h.start(DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True), ActualStateSnapshot())
        assert out.running is False
        assert "spawn failed" in out.last_error
        assert h.current_handle is None


class TestEmbeddingActionHandlerStop:
    def test_stop_when_no_handle_is_noop(self, tmp_path):
        h = _make_action_handler(runtime_dir=tmp_path / "runtime")
        out = h.stop(DesiredStateSnapshot(action="stop"), ActualStateSnapshot(running=True))
        assert out.running is False
        assert out.pid is None

    def test_stop_graceful(self, tmp_path):
        starter = _StubStarter()
        stopper = _StubStopper()
        h = _make_action_handler(
            starter=starter, stopper=stopper,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        # 先 start 让 _current_handle 装上
        h.start(DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True), ActualStateSnapshot())
        assert h.current_handle is not None

        out = h.stop(DesiredStateSnapshot(action="stop"), ActualStateSnapshot(running=True))
        assert out.running is False
        assert h.current_handle is None
        assert stopper.calls and stopper.calls[0][1] == tmp_path / "runtime"

    def test_stop_force_kill_records_error(self, tmp_path):
        starter = _StubStarter()
        stopper = _StubStopper()
        stopper.graceful = False
        stopper.err = ""
        h = _make_action_handler(
            starter=starter, stopper=stopper,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        h.start(DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True), ActualStateSnapshot())
        out = h.stop(DesiredStateSnapshot(action="stop"), ActualStateSnapshot(running=True))
        assert "force-killed" in out.last_error


class TestEmbeddingActionHandlerSwitchModel:
    def test_switch_orchestrates_stop_install_start(self, tmp_path):
        installer = _StubInstaller(ok=True)
        starter = _StubStarter()
        stopper = _StubStopper()
        h = _make_action_handler(
            installer=installer, starter=starter, stopper=stopper,
            install_spec=_make_install_spec(tmp_path),
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        # 模拟"先 start 一次"作为旧状态
        h.start(DesiredStateSnapshot(action="start", model_id="old", enabled=True), ActualStateSnapshot())
        starter.calls.clear()  # 重置计数,关注 switch 内的 start

        desired = DesiredStateSnapshot(action="switch_model", model_id="new", enabled=True, generation=5)
        out = h.switch_model(desired, ActualStateSnapshot(running=True))
        assert len(stopper.calls) == 1
        assert len(installer.calls) == 1
        assert len(starter.calls) == 1
        assert out.running is True
        assert out.model_id == "new"

    def test_switch_aborts_when_install_fails(self, tmp_path):
        installer = _StubInstaller(ok=False)
        starter = _StubStarter()
        h = _make_action_handler(
            installer=installer, starter=starter,
            install_spec=_make_install_spec(tmp_path),
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        out = h.switch_model(DesiredStateSnapshot(action="switch_model", model_id="new"), ActualStateSnapshot())
        assert installer.calls
        assert starter.calls == []  # install 失败,start 不应被调
        assert out.installed is False


class TestEmbeddingActionHandlerSupervise:
    def test_alive_subprocess_noop(self, tmp_path):
        starter = _StubStarter()
        h = _make_action_handler(
            starter=starter,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        h.start(DesiredStateSnapshot(action="start", model_id="m", enabled=True), ActualStateSnapshot())
        out = h.supervise_tick(
            DesiredStateSnapshot(action="none", model_id="m", enabled=True),
            ActualStateSnapshot(running=True, pid=1111),
        )
        # 没崩 → 不变
        assert out.running is True

    def test_dead_subprocess_triggers_restart(self, tmp_path):
        starter = _StubStarter()
        dead_handle = _FakeHandle(pid=1111)
        starter.handle_to_return = dead_handle
        h = _make_action_handler(
            starter=starter,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        h.start(DesiredStateSnapshot(action="start", model_id="m", enabled=True), ActualStateSnapshot())
        # 让当前 handle 显示已死
        dead_handle.set_exited(137)
        # 后续 start 给个新 handle
        starter.handle_to_return = _FakeHandle(pid=2222)

        desired = DesiredStateSnapshot(action="start", model_id="m", enabled=True, generation=2)
        out = h.supervise_tick(desired, ActualStateSnapshot(running=True))
        # 触发了一次 restart
        assert h.restart_count == 1
        assert out.running is True
        assert out.pid == 2222

    def test_restart_limit_gives_up(self, tmp_path):
        starter = _StubStarter()
        h = _make_action_handler(
            starter=starter,
            start_spec=_make_start_spec(tmp_path),
            runtime_dir=tmp_path / "runtime",
        )
        h.start(DesiredStateSnapshot(action="start", model_id="m", enabled=True), ActualStateSnapshot())
        # 手动撑爆 restart_count
        h._restart_count = 3  # noqa: SLF001
        h._current_handle._exit = 137  # type: ignore[attr-defined,union-attr]  # noqa: SLF001

        desired = DesiredStateSnapshot(action="start", model_id="m", enabled=True)
        out = h.supervise_tick(desired, ActualStateSnapshot(running=True))
        assert out.running is False
        assert "restart_limit_exceeded" in out.last_error

    def test_supervise_noop_when_desired_disabled(self):
        h = _make_action_handler()
        # 没有 current_handle 也行；早返
        out = h.supervise_tick(
            DesiredStateSnapshot(action="stop", enabled=False),
            ActualStateSnapshot(running=False),
        )
        assert out.running is False


# ---------------------------------------------------------------------------
# Batch F: 真子进程端到端测试（fake_infinity）
# ---------------------------------------------------------------------------


class TestEndToEndWithFakeInfinity:
    """用真 DefaultSubprocessSpawner + DefaultHealthProbe + fake_infinity 验证
    生产路径无问题。跑得相对慢（~3s）但只跑一次。
    """

    def test_start_real_subprocess_health_probe_succeeds(self, tmp_path):
        import socket
        # 找一个空闲端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        fake_path = Path(__file__).resolve().parent.parent / "tools" / "fake_infinity.py"
        assert fake_path.exists(), "fake_infinity.py 缺失"

        runtime_dir = tmp_path / "runtime"
        start_spec = StartSpec(
            model_id="bge-m3",
            device="cpu",
            start_cmd=[
                sys.executable, str(fake_path), "v2",
                "--port", str(port),
                "--model-id", "bge-m3",
                "--device", "cpu",
                "--warmup-seconds", "0.5",
                "--sigterm-mode", "normal",
            ],
            port=port,
            runtime_dir=runtime_dir,
            infinity_log_path=tmp_path / "logs" / "infinity.log",
        )

        spawner = DefaultSubprocessSpawner()
        probe = DefaultHealthProbe()
        handler = StartHandler(
            spawner=spawner, probe=probe,
            warmup_timeout_sec=8.0, probe_interval_sec=0.3,
        )
        handle = None
        try:
            handle, ready, err = handler.spawn_and_wait_ready(start_spec)
            assert handle is not None, f"spawn 失败: {err}"
            assert ready is True, f"探活超时: {err}"
            assert handle.poll() is None
            # runtime 文件落盘
            assert (runtime_dir / "pid").read_text(encoding="utf-8") == str(handle.pid)
            assert (runtime_dir / "port").read_text(encoding="utf-8") == str(port)

            # 跑一遍 stop（验证 SIGTERM normal 正常收尾）
            stopper = StopHandler(grace_sec=3.0, poll_interval_sec=0.2)
            graceful, err2 = stopper.terminate_and_wait(handle, runtime_dir)
            assert graceful is True
            assert err2 == ""
            assert not (runtime_dir / "pid").exists()
        finally:
            # 兜底清理
            if handle is not None:
                try:
                    handle.kill()
                except Exception:
                    pass

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows TerminateProcess 无法被进程忽略，fake_infinity --sigterm-mode ignore 在 Win 上无意义；强杀路径走 test_force_kill 的 taskkill 路径覆盖",
    )
    def test_stop_force_kills_when_sigterm_ignored(self, tmp_path):
        """AC14a 端到端：fake_infinity --sigterm-mode ignore → 3s 内被 SIGKILL。"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        fake_path = Path(__file__).resolve().parent.parent / "tools" / "fake_infinity.py"
        runtime_dir = tmp_path / "runtime"
        start_spec = StartSpec(
            model_id="bge-m3",
            device="cpu",
            start_cmd=[
                sys.executable, str(fake_path), "v2",
                "--port", str(port),
                "--model-id", "bge-m3",
                "--device", "cpu",
                "--warmup-seconds", "0.2",
                "--sigterm-mode", "ignore",
            ],
            port=port,
            runtime_dir=runtime_dir,
            infinity_log_path=tmp_path / "logs" / "infinity.log",
        )
        spawner = DefaultSubprocessSpawner()
        probe = DefaultHealthProbe()
        starter = StartHandler(
            spawner=spawner, probe=probe,
            warmup_timeout_sec=5.0, probe_interval_sec=0.3,
        )
        handle, ready, _err = starter.spawn_and_wait_ready(start_spec)
        assert handle is not None and ready is True

        import time as _t
        t0 = _t.monotonic()
        stopper = StopHandler(grace_sec=2.0, poll_interval_sec=0.2)
        graceful, err = stopper.terminate_and_wait(handle, runtime_dir)
        elapsed = _t.monotonic() - t0

        assert graceful is False, "SIGTERM 被忽略时不应 graceful"
        assert handle.poll() is not None, "kill 应让进程退出"
        # AC14a：必须在 grace+宽限 (~3s) 内强杀完
        assert elapsed < 4.0, f"强杀耗时过长: {elapsed:.2f}s"


class TestIsModelDirComplete:
    """``_is_model_dir_complete`` 必须只认 PyTorch 权重，不认 ONNX。

    原因：infinity 启动模板默认 ``engine=torch``，必须 pytorch_model.bin /
    model.safetensors。若误把 onnx/model.onnx_data 当完整，会跳过 snapshot_download
    后 infinity 启动撞 ``OSError: no file named pytorch_model.bin, model.safetensors,
    ...`` → exit code 3。
    """

    MIN_BYTES = 50 * 1024 * 1024 + 1024  # 略大于 50MB 阈值

    @staticmethod
    def _touch(p: Path, size: int = 0) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as f:
            if size > 0:
                f.seek(size - 1)
                f.write(b"\0")

    def test_empty_local_dir_returns_false(self):
        assert _is_model_dir_complete("") is False

    def test_missing_config_returns_false(self, tmp_path):
        self._touch(tmp_path / "model.safetensors", self.MIN_BYTES)
        assert _is_model_dir_complete(str(tmp_path)) is False

    def test_only_onnx_weights_returns_false(self, tmp_path):
        """老大本机踩到的坑：onnx_data 在但 PyTorch 权重缺，必须返 False
        才能触发 snapshot_download 补 pytorch 权重。"""
        self._touch(tmp_path / "config.json")
        self._touch(tmp_path / "onnx" / "model.onnx_data", self.MIN_BYTES)
        self._touch(tmp_path / "onnx" / "model.onnx", self.MIN_BYTES)
        assert _is_model_dir_complete(str(tmp_path)) is False

    def test_pytorch_bin_returns_true(self, tmp_path):
        self._touch(tmp_path / "config.json")
        self._touch(tmp_path / "pytorch_model.bin", self.MIN_BYTES)
        assert _is_model_dir_complete(str(tmp_path)) is True

    def test_safetensors_returns_true(self, tmp_path):
        self._touch(tmp_path / "config.json")
        self._touch(tmp_path / "model.safetensors", self.MIN_BYTES)
        assert _is_model_dir_complete(str(tmp_path)) is True

    def test_undersized_weight_returns_false(self, tmp_path):
        """只有元数据壳（<50MB）不算完整。"""
        self._touch(tmp_path / "config.json")
        self._touch(tmp_path / "pytorch_model.bin", 1024)  # 1KB
        assert _is_model_dir_complete(str(tmp_path)) is False
