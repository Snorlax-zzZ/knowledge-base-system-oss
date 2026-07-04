"""Embedding 服务安装进度的 SSE 转发器（design v1.2 §3.2 + AC21 + AC26）。

壳层（mac-app / windows-app 的 ProcessManager）执行安装计划时往两个文件写：

- ``runtime/install_status.json``：每 ≤2s 覆盖式 flush 最新进度快照
  （phase / progress / message / bytes_downloaded / ...）
- ``logs/pip.log``：pip wheel 输出 append-only

本模块 tail 这两个文件，把变更打包成 SSE 事件转发给前端：

- ``status``：``install_status.json`` mtime 变化时整段 emit
- ``pip_log``：``pip.log`` 长度增长时增量 emit 新追加内容
- ``keepalive``：≤15s 无任何事件兜底心跳（AC21 pip 安装不允许黑盒静默）

终止条件：

- ``install_status.json`` 的 ``phase`` 进入 ``completed`` / ``failed`` 之一
- 超过 ``max_duration_sec`` 硬上限（默认 30 分钟）

测试通过注入 ``clock`` + ``sleep`` 控制时序，无需真停 200ms。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


_TERMINAL_PHASES = frozenset({"completed", "failed"})


def _format_sse(event: str, data: dict[str, Any]) -> str:
    """SSE 事件帧；data 序列化成单行 JSON。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@dataclass
class _TailState:
    last_status_mtime: float = -1.0
    last_pip_size: int = 0
    last_event_at: float = 0.0


class InstallSseStreamer:
    """tail install_status.json + pip.log，按 SSE 协议 yield 事件。

    所有时间相关参数都可注入，便于测试控制：

    - ``heartbeat_sec``：无事件多久补一个 keepalive
    - ``tail_interval_sec``：两次轮询间隔
    - ``max_duration_sec``：硬性总时长上限
    - ``clock``：返回 monotonic 时间
    - ``sleep``：休眠函数
    """

    def __init__(
        self,
        *,
        status_path: Path,
        pip_log_path: Path,
        heartbeat_sec: float = 15.0,
        tail_interval_sec: float = 0.2,
        # 硬上限：cuda wheel 2.5GB 在国内慢网（500KB/s 直连 PyTorch CloudFront）
        # 实测 85 分钟才下完，1800s 远不够；提到 7200s (2h) 覆盖 99% 用户。
        # 真撞 2h 还没完的属网络问题（< 350KB/s），UI 给 timeout 提示后由前端
        # fallback 主动 poll status.json 兜底（防 UI 永远卡死，v1.3.12 实测 bug）。
        max_duration_sec: float = 7200.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._status_path = status_path
        self._pip_log_path = pip_log_path
        self._heartbeat_sec = heartbeat_sec
        self._tail_interval_sec = tail_interval_sec
        self._max_duration_sec = max_duration_sec
        self._clock = clock
        self._sleep = sleep

    def _read_status_snapshot(self) -> Optional[dict[str, Any]]:
        try:
            text = self._status_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 壳层正在覆盖式写时可能短暂读到半截 JSON；忽略本轮等下次
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _status_mtime(self) -> float:
        try:
            return self._status_path.stat().st_mtime
        except FileNotFoundError:
            return -1.0

    def _pip_size(self) -> int:
        try:
            return self._pip_log_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _tail_pip_log(self, start_offset: int, end_offset: int) -> str:
        if end_offset <= start_offset:
            return ""
        try:
            with self._pip_log_path.open("rb") as fp:
                fp.seek(start_offset)
                chunk = fp.read(end_offset - start_offset)
        except FileNotFoundError:
            return ""
        # 二进制读后按 utf-8 容错解码（pip 偶尔输出非 utf-8 字符）
        return chunk.decode("utf-8", errors="replace")

    def events(self) -> Iterator[str]:
        """主循环。生成器，由 FastAPI ``StreamingResponse`` 消费。"""
        start = self._clock()
        # 入口即抓基线 mtime / pip size，再读快照——避免 yield 之后外部修改文件
        # 但生成器尚未推进到记录指针，导致后续循环误判"没变化"。
        baseline_mtime = self._status_mtime()
        baseline_pip_size = self._pip_size()
        st = _TailState(
            last_event_at=start,
            last_status_mtime=baseline_mtime,
            last_pip_size=baseline_pip_size,
        )

        # 初始快照：若 status 文件已存在直接 emit 一次，避免前端要等第一次变化
        initial = self._read_status_snapshot()
        if initial is not None:
            yield _format_sse("status", initial)
            st.last_event_at = self._clock()
            if initial.get("phase") in _TERMINAL_PHASES:
                return

        while True:
            now = self._clock()
            if now - start > self._max_duration_sec:
                yield _format_sse(
                    "timeout",
                    {"duration_sec": now - start, "limit_sec": self._max_duration_sec},
                )
                return

            cur_mtime = self._status_mtime()
            if cur_mtime > 0 and cur_mtime != st.last_status_mtime:
                snap = self._read_status_snapshot()
                if snap is not None:
                    yield _format_sse("status", snap)
                    st.last_status_mtime = cur_mtime
                    st.last_event_at = self._clock()
                    if snap.get("phase") in _TERMINAL_PHASES:
                        return

            cur_size = self._pip_size()
            if cur_size > st.last_pip_size:
                chunk = self._tail_pip_log(st.last_pip_size, cur_size)
                if chunk:
                    yield _format_sse("pip_log", {"chunk": chunk})
                    st.last_event_at = self._clock()
                st.last_pip_size = cur_size

            now = self._clock()
            if now - st.last_event_at >= self._heartbeat_sec:
                yield _format_sse("keepalive", {"t": now})
                st.last_event_at = now

            self._sleep(self._tail_interval_sec)


def resolve_install_paths(data_root: str) -> tuple[Path, Path]:
    """从 data_root 推出 ``install_status.json`` + ``pip.log`` 路径。

    与 design §3.1 布局对齐：
    - ``{data_root}/runtime/install_status.json``
    - ``{data_root}/logs/pip.log``
    """
    root = Path(data_root)
    return root / "runtime" / "install_status.json", root / "logs" / "pip.log"
