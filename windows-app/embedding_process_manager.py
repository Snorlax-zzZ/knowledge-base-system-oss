"""Windows 壳层 ProcessManager —— 内置 embedding 服务子进程生命周期。

完整契约见 ``docs/14-phase3-process-manager-contract.md``。本模块对应
``tasks.md §3a 共用 + §3c Windows 平台特性``。

设计要点：

- **纯 stdlib**（urllib / subprocess / threading / pathlib），不引入新三方依
  赖。要兼容 PyInstaller --onefile 打包；任何额外 wheel 都意味着 tcl/tk
  那套坑要重走一遍（windows-app/tray_app_local.py 顶部那段魔法注释）。
- **测试友好**：HTTP transport / 子进程 spawner / 时钟 / 文件路径全部可
  注入；不写"无法 mock 的硬编码 urllib.request 调用"。生产路径走默认实现。
- **职责单一**：本模块只管 infinity 子进程；kb-api 主服务 / tray icon 仍
  由 ``tray_app_local.py`` 管。``EmbeddingProcessManager.start()`` 派一个
  daemon 线程跑 reconcile loop，托盘进程退出时自动跟着死。

Batch 推进顺序（按 commit 拆批，每批含 TDD 测试）：

- Batch A：骨架 + OwnerTokenSource + KbApiClient + ActualStateSnapshot
- Batch B：reconcile loop + desired-state poller + generation 幂等
- Batch C：install action（venv / pip / snapshot_download）
- Batch D：start action + 健康探活 + 崩溃重启
- Batch E：stop action + SIGTERM→SIGKILL + switch_model
- Batch F：残留清理 + tray_app_local 集成

本文件当前为 Batch A，后续 batch 在同一类上扩展。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence


logger = logging.getLogger("embedding_process_manager")


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesiredStateSnapshot:
    """壳层从 kb-api 拉取的 desired-state（contract §3.1）。"""

    action: str = "none"            # none|install|start|stop|switch_model
    model_id: str = ""
    device: str = "cpu"
    enabled: bool = False
    # v1.3.12：cuda wheel 安装配置（device=cuda 时透传给 build_install_plan）。
    # 老版 kb-api 不返这两个字段 → get_desired 取默认（阿里 + cu121）。
    pytorch_mirror: str = "https://download.pytorch.org/whl/"
    cuda_version: str = "cu124"
    generation: int = 0
    updated_at: float = 0.0


@dataclass
class ActualStateSnapshot:
    """壳层维护的实况状态（contract §3.2）。

    ``frozen=False``：reconcile loop 会原地改字段；线程安全通过外层
    ``EmbeddingProcessManager._actual_lock`` 保护。
    """

    acknowledged_generation: int = 0
    installed: bool = False
    running: bool = False
    warming_up: bool = False
    model_id: str = ""
    port: int = 0
    pid: Optional[int] = None
    device: str = "cpu"
    restart_count: int = 0
    last_error: str = ""

    def to_payload(self) -> dict[str, Any]:
        """生成 POST /actual-state 的请求体（与 schemas.EmbeddingServiceActualStateRequest 对齐）。"""
        return asdict(self)


# ---------------------------------------------------------------------------
# 异常体系
# ---------------------------------------------------------------------------


class ProcessManagerError(Exception):
    """ProcessManager 抛出的所有业务异常基类。"""


class OwnerTokenUnavailable(ProcessManagerError):
    """``runtime/owner_token`` 在引导超时内没出现（kb-api 始终未启动）。"""


class KbApiUnauthorized(ProcessManagerError):
    """POST/GET 拿到 401 —— token 被 kb-api 重启后改了，需 re-read 文件。"""


class KbApiConflict(ProcessManagerError):
    """POST /actual-state 拿到 409 —— acknowledged_generation 落后，本批丢弃即可。"""


class KbApiTransportError(ProcessManagerError):
    """网络层 / 5xx 错；reconcile loop 会指数退避后重试。"""


# ---------------------------------------------------------------------------
# OwnerTokenSource —— 读 runtime/owner_token，按需 retry / refresh
# ---------------------------------------------------------------------------


class OwnerTokenSource:
    """从 ``{data_root}/runtime/owner_token`` 读 token，按需缓存 / 刷新。

    contract §2 行为：
    - 启动期 token 文件可能还没写（kb-api 比壳层晚启动）→ ``load_blocking``
      最多等 ``boot_timeout_sec`` 秒，期间每 1s 重读
    - 平时缓存在内存
    - kb-api 重启会改 token → 拿到 401 时调 ``invalidate()`` 触发下次重读
    """

    def __init__(
        self,
        token_path: Path,
        *,
        boot_timeout_sec: float = 60.0,
        poll_interval_sec: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._path = Path(token_path)
        self._boot_timeout_sec = boot_timeout_sec
        self._poll_interval_sec = poll_interval_sec
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[str] = None

    @property
    def token_path(self) -> Path:
        return self._path

    def _read_once(self) -> Optional[str]:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        if not text:
            return None
        return text

    def load_blocking(self) -> str:
        """阻塞等待 owner_token 出现；超时抛 OwnerTokenUnavailable。

        缓存命中直接返回，不再 IO。
        """
        with self._lock:
            if self._cached is not None:
                return self._cached
        deadline = self._clock() + self._boot_timeout_sec
        while True:
            token = self._read_once()
            if token is not None:
                with self._lock:
                    self._cached = token
                return token
            if self._clock() >= deadline:
                raise OwnerTokenUnavailable(
                    f"owner_token 在 {self._boot_timeout_sec}s 内未出现于 {self._path}"
                )
            self._sleep(self._poll_interval_sec)

    def invalidate(self) -> None:
        """清缓存；下次 ``load_blocking`` 重新读文件。

        典型场景：actual-state 回写拿到 401，怀疑 kb-api 重启改了 token。
        """
        with self._lock:
            self._cached = None

    def refresh(self) -> str:
        """强制重读 + 返回；不等待，文件不存在直接抛 OwnerTokenUnavailable。"""
        token = self._read_once()
        if token is None:
            raise OwnerTokenUnavailable(f"owner_token 文件 {self._path} 不可读")
        with self._lock:
            self._cached = token
        return token


# ---------------------------------------------------------------------------
# KbApiClient —— GET desired-state / POST actual-state 的薄包装
# ---------------------------------------------------------------------------


# 测试可注入的 transport 接口：(method, url, headers, body) -> (status, body_bytes)
# body 是已 json.dumps 的 bytes；返回 body_bytes 是原始响应体（utf-8 decode 由调用方做）
Transport = Callable[[str, str, dict[str, str], Optional[bytes]], tuple[int, bytes]]


def _default_urllib_transport(
    method: str, url: str, headers: dict[str, str], body: Optional[bytes],
) -> tuple[int, bytes]:
    """生产路径：用 urllib.request 发请求。"""
    req = urllib.request.Request(url=url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


class KbApiClient:
    """kb-api 控制平面 HTTP 客户端（contract §3）。

    职责：
    - 携带 ``X-Embedding-Owner-Token`` header
    - 401 → 抛 ``KbApiUnauthorized`` 让调用方 invalidate token
    - 409 → 抛 ``KbApiConflict``（actual-state 才会出）
    - 5xx / 网络错 → 抛 ``KbApiTransportError``
    - 200 → 返回解析后的 dict
    """

    def __init__(
        self,
        *,
        base_url: str,
        token_source: OwnerTokenSource,
        transport: Transport = _default_urllib_transport,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token_source
        self._transport = transport

    def _do(
        self, method: str, path: str, payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        token = self._token.load_blocking()
        headers = {"X-Embedding-Owner-Token": token, "Accept": "application/json"}
        body: Optional[bytes] = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{self._base_url}{path}"
        try:
            status, raw = self._transport(method, url, headers, body)
        except Exception as exc:  # noqa: BLE001 — 把任何底层错统一抽象成 transport 错
            raise KbApiTransportError(f"transport failure: {exc}") from exc

        if status in (401, 403):
            # 兼容 kb-api owner_token mismatch 返 403 的情况（app/main.py:878/1020）：
            # kb-api 单独重启后 token 会换，壳层缓存的旧 token 命中 403，必须 invalidate 触发下次重读。
            # 401 是"未认证"（缓存 token 缺失），403 是"认证过但 token 不对"（缓存脏了），
            # 两个 case 对壳层来说处理方式一致：清缓存 + 抛异常让 caller retry。
            self._token.invalidate()
            raise KbApiUnauthorized(f"{method} {path} -> {status}")
        if status == 409:
            raise KbApiConflict(f"{method} {path} -> 409 {raw.decode('utf-8', errors='replace')}")
        if status >= 500 or status < 200:
            raise KbApiTransportError(f"{method} {path} -> {status}")
        if status >= 400:
            # 4xx 非 401/409 视为 transport 异常（client 侧 bug 或 schema 不对）
            raise KbApiTransportError(
                f"{method} {path} -> {status} {raw.decode('utf-8', errors='replace')}"
            )

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise KbApiTransportError(f"bad json from {path}: {e}") from e

    def get_desired(self) -> DesiredStateSnapshot:
        body = self._do("GET", "/v1/system/embedding-service/desired-state")
        return DesiredStateSnapshot(
            action=body.get("action", "none"),
            model_id=body.get("model_id", ""),
            device=body.get("device", "cpu"),
            enabled=bool(body.get("enabled", False)),
            pytorch_mirror=body.get(
                "pytorch_mirror", "https://download.pytorch.org/whl/"
            ),
            cuda_version=body.get("cuda_version", "cu124"),
            generation=int(body.get("generation", 0)),
            updated_at=float(body.get("updated_at", 0.0)),
        )

    def post_actual(self, snapshot: ActualStateSnapshot) -> dict[str, Any]:
        return self._do(
            "POST",
            "/v1/system/embedding-service/actual-state",
            payload=snapshot.to_payload(),
        )


# ---------------------------------------------------------------------------
# Action handler 协议（Batch C/D/E 实现真正动作；Batch B 只定义接口）
# ---------------------------------------------------------------------------


class ActionHandler:
    """reconcile loop 分发到的动作执行器接口。

    Batch B 用作 mock seam；Batch C/D/E 各动作的真实实现继承本类（或独立类
    分别注入）。所有方法默认抛 NotImplementedError；未实现的 action 在
    reconcile loop 内被吃掉 + 写 last_error。

    方法返回新的 ActualStateSnapshot（reconcile loop 据此更新 + 回写）。
    """

    def install(
        self, desired: DesiredStateSnapshot, current: ActualStateSnapshot,
    ) -> ActualStateSnapshot:
        raise NotImplementedError("install handler 未实现（Batch C）")

    def start(
        self, desired: DesiredStateSnapshot, current: ActualStateSnapshot,
    ) -> ActualStateSnapshot:
        raise NotImplementedError("start handler 未实现（Batch D）")

    def stop(
        self, desired: DesiredStateSnapshot, current: ActualStateSnapshot,
    ) -> ActualStateSnapshot:
        raise NotImplementedError("stop handler 未实现（Batch E）")

    def switch_model(
        self, desired: DesiredStateSnapshot, current: ActualStateSnapshot,
    ) -> ActualStateSnapshot:
        raise NotImplementedError("switch_model handler 未实现（Batch E）")

    def self_heal_warmup_if_needed(
        self, actual: ActualStateSnapshot,
    ) -> Optional[ActualStateSnapshot]:
        """bug 2 自愈钩子（默认 no-op，让 EmbeddingActionHandler 覆盖）。

        约定：返回 ``None`` 表示无需自愈；返回新 ``ActualStateSnapshot`` 则
        manager 锁内替换当前 actual 并照常回写 kb-api。
        """
        return None

    def shutdown(self) -> None:
        """tray quit 时的强制清理钩子（默认 no-op，EmbeddingActionHandler 覆盖）。

        约定：发 SIGTERM → grace → SIGKILL 给当前 infinity handle + 清
        runtime/pid|port，避免 tray 退出后 infinity 成孤儿（kb-tray taskkill
        kb-api 进程树不带 infinity—— infinity 是 kb-tray 平级 spawn 的，不在
        kb-api 树里）。``manager.stop()`` 起手调用。
        """
        return


# ---------------------------------------------------------------------------
# EmbeddingProcessManager —— reconcile loop 主入口
# ---------------------------------------------------------------------------


class EmbeddingProcessManager:
    """Windows 壳层 ProcessManager 主类（contract §4）。

    线程模型：
    - ``start()`` 派一个 daemon 线程跑 ``_run_reconcile()``，托盘进程退出时
      自然终结（不阻塞 Windows 服务关闭流程）
    - ``stop()`` 设 ``_stop_event``，循环在下次唤醒时退出
    - 所有 actual-state 写都过 ``_actual_lock``，避免心跳与动作执行竞争

    幂等机制（contract §4）：
    - 每个 generation 至多执行一次；记录 ``_last_done_generation``
    - generation 倒退（kb-api 重启归零）= 重新执行
    - reconcile 内任何异常 catch + 写 ``last_error``，绝不杀 loop

    退避策略：
    - desired-state 拉取失败 → 1s/2s/4s/8s（capped 30s）
    - 拉到 200 → backoff 立即归零
    """

    # reconcile 主循环周期；正常 3s；可注入做测试加速
    DEFAULT_LOOP_PERIOD_SEC = 3.0
    # 无变化时也要心跳，默认 5s 一次（kb-api 用 last_health_check 判存活）
    DEFAULT_HEARTBEAT_SEC = 5.0
    # 退避上限
    DEFAULT_MAX_BACKOFF_SEC = 30.0

    def __init__(
        self,
        *,
        client: KbApiClient,
        handler: ActionHandler,
        loop_period_sec: float = DEFAULT_LOOP_PERIOD_SEC,
        heartbeat_sec: float = DEFAULT_HEARTBEAT_SEC,
        max_backoff_sec: float = DEFAULT_MAX_BACKOFF_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._client = client
        self._handler = handler
        self._loop_period_sec = loop_period_sec
        self._heartbeat_sec = heartbeat_sec
        self._max_backoff_sec = max_backoff_sec
        self._clock = clock
        self._sleep = sleep
        self._on_error = on_error or (lambda msg: logger.warning(msg))

        self._actual = ActualStateSnapshot()
        self._actual_lock = threading.Lock()
        self._last_done_generation = -1
        # None = 首次 tick 必发一次心跳（让 kb-api 立即看到壳层活着）
        self._last_heartbeat_at: Optional[float] = None
        self._backoff = 0.0

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 公共接口 ---------------------------------------------------------

    def start(self) -> None:
        """派 daemon 线程跑 reconcile loop；幂等，多次调用只起一个线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_reconcile,
            name="EmbeddingProcessManager",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """通知 reconcile loop 退出；等线程 join。

        顺带调 ``handler.shutdown()`` 强制带走 infinity 子进程，避免 tray quit
        后 orphan（对齐 Mac Swift ``EmbeddingProcessManager.stop:781`` 的
        "顺带关掉 infinity 子进程" 注释逻辑）。失败吞掉日志，不阻塞退出。
        """
        self._stop_event.set()
        try:
            self._handler.shutdown()
        except Exception:  # noqa: BLE001
            logger.warning("manager.stop: handler.shutdown failed", exc_info=True)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def snapshot_actual(self) -> ActualStateSnapshot:
        """供外部（托盘菜单 / 测试）只读窥探实况。"""
        with self._actual_lock:
            return ActualStateSnapshot(**asdict(self._actual))

    # ---- 主循环 -----------------------------------------------------------

    def _run_reconcile(self) -> None:
        """主循环；只在 stop_event 触发时退出。所有异常吞掉 + 日志。"""
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — 主循环必须不死
                logger.exception("reconcile tick crashed; continuing")
            self._sleep_until_next_tick()

    def _tick(self) -> None:
        """一轮 reconcile：拉 desired → diff → 执行 → 回写。

        切分到独立方法便于单测直接驱动单 tick，不必跑整个 loop。
        """
        # bug 2 自愈（与 Mac Swift ``selfHealWarmupIfNeeded`` 同步）：
        # StartHandler 在 warmup 超时时返回 (handle, ready=false, "warmup
        # timeout")，actual.warming_up 会卡 true。但 infinity 实际可能在超时
        # 后才完成 model load——此时进程仍在跑、/health 真返 200，只是 actual
        # 状态没人重置。shouldSkip 又会因 generation 没涨直接跳过 dispatch，
        # 永远不会进 doStart 重写 actual。每 tick 起手让 handler 自检：handle
        # 仍活 + /health 200 + actual 仍脏 → 立即清状态并回写一次。
        self._self_heal_warmup()

        try:
            desired = self._client.get_desired()
            self._backoff = 0.0
        except KbApiUnauthorized:
            # token invalidate 已在 client 内完成；下一 tick 会重读 → 不退避
            return
        except KbApiTransportError as e:
            self._on_error(f"get_desired transport error: {e}")
            self._bump_backoff()
            return

        if self._should_skip(desired):
            self._maybe_heartbeat(desired)
            return

        # 执行动作
        new_actual = self._dispatch(desired)
        with self._actual_lock:
            self._actual = new_actual

        # 标记 done（即使 dispatch 内部失败也要标记，避免死循环重试无效 action）
        self._last_done_generation = desired.generation
        self._write_actual(desired)

    def _self_heal_warmup(self) -> None:
        """委托 handler 检查是否需要清 warmup 脏态；脏 → 锁内替换并立即回写。

        独立成方法是为了：(a) 把 handler 拒绝自愈（返回 None）的快路径放循环
        热区；(b) 异常吞掉，handler 实现里 raise 不能挂掉主 loop。
        """
        with self._actual_lock:
            snap = ActualStateSnapshot(**asdict(self._actual))
        try:
            healed = self._handler.self_heal_warmup_if_needed(snap)
        except Exception:  # noqa: BLE001
            logger.exception("self_heal_warmup_if_needed crashed; ignoring")
            return
        if healed is None:
            return
        with self._actual_lock:
            # 二次校验：另一个 tick 可能已经改了 warming_up，避免覆盖正确值
            if self._actual.warming_up or "warmup timeout" in self._actual.last_error:
                self._actual = healed
                logger.info(
                    "selfHeal: warmup state cleared (pid=%s port=%s)",
                    healed.pid, healed.port,
                )
                # 立即心跳一次，让 kb-api 第一时间看到状态翻绿。带 acknowledged
                # generation = handler 持有 → 用 actual 自身已有的 ack 值即可。
                try:
                    self._client.post_actual(healed)
                    self._last_heartbeat_at = self._clock()
                except (KbApiUnauthorized, KbApiConflict):
                    pass
                except KbApiTransportError as e:
                    self._on_error(f"self_heal post_actual transport error: {e}")

    def _should_skip(self, desired: DesiredStateSnapshot) -> bool:
        """是否跳过执行：generation 已 done + action=none 时仅心跳。"""
        if desired.action == "none":
            # action=none 本身不需要执行任何动作；但 generation 仍然要 ack
            if self._last_done_generation < desired.generation:
                self._last_done_generation = desired.generation
            return True
        return desired.generation <= self._last_done_generation

    def _dispatch(self, desired: DesiredStateSnapshot) -> ActualStateSnapshot:
        """按 action 分发到 handler；handler 异常吞 + 写 last_error。"""
        with self._actual_lock:
            current = ActualStateSnapshot(**asdict(self._actual))
        try:
            if desired.action == "install":
                new_actual = self._handler.install(desired, current)
            elif desired.action == "start":
                new_actual = self._handler.start(desired, current)
            elif desired.action == "stop":
                new_actual = self._handler.stop(desired, current)
            elif desired.action == "switch_model":
                new_actual = self._handler.switch_model(desired, current)
            else:
                current.last_error = f"unknown action: {desired.action}"
                return current
        except NotImplementedError as e:
            current.last_error = f"handler not implemented: {e}"
            return current
        except Exception as e:  # noqa: BLE001
            logger.exception("action handler %s failed", desired.action)
            current.last_error = f"{desired.action} failed: {e}"[:512]
            return current
        # handler 必须填好 acknowledged_generation；这里兜底
        new_actual.acknowledged_generation = desired.generation
        return new_actual

    def _maybe_heartbeat(self, desired: DesiredStateSnapshot) -> None:
        """无动作执行时仍按 ``heartbeat_sec`` 心跳回写 actual-state。

        首次（``_last_heartbeat_at is None``）立即心跳，让 kb-api 第一时间
        看到壳层活着；之后按 ``heartbeat_sec`` 节流。
        """
        now = self._clock()
        if self._last_heartbeat_at is not None and now - self._last_heartbeat_at < self._heartbeat_sec:
            return
        self._write_actual(desired)

    def _write_actual(self, desired: DesiredStateSnapshot) -> None:
        """带最新 generation 回写 actual-state；409/401 吃掉，5xx 退避。"""
        with self._actual_lock:
            snap = ActualStateSnapshot(**asdict(self._actual))
        snap.acknowledged_generation = max(
            snap.acknowledged_generation, self._last_done_generation, desired.generation,
        )
        try:
            self._client.post_actual(snap)
            self._last_heartbeat_at = self._clock()
            self._backoff = 0.0
        except KbApiConflict:
            # 心跳时 generation 落后 → 下次 tick 拉到新 desired 后会重写
            logger.debug("actual-state 409 conflict; dropping this heartbeat")
        except KbApiUnauthorized:
            # token invalidate 已在 client 内完成
            return
        except KbApiTransportError as e:
            self._on_error(f"post_actual transport error: {e}")
            self._bump_backoff()

    # ---- 时序工具 ---------------------------------------------------------

    def _bump_backoff(self) -> None:
        if self._backoff <= 0:
            self._backoff = 1.0
        else:
            self._backoff = min(self._backoff * 2.0, self._max_backoff_sec)

    def _sleep_until_next_tick(self) -> None:
        """根据是否退避，确定本轮 sleep 时长；提前 wake 在 stop_event 触发时。"""
        delay = self._backoff if self._backoff > 0 else self._loop_period_sec
        # stop_event.wait 接受小数秒，触发时立即返；自然唤醒返 False
        self._stop_event.wait(delay)


# ---------------------------------------------------------------------------
# InstallStatusWriter —— 原子覆盖式写 runtime/install_status.json (contract §3.3)
# ---------------------------------------------------------------------------


# install_status.json 的 phase 枚举（contract §3.3）
_INSTALL_PHASES = (
    "preparing", "downloading", "pip_installing", "warming", "completed", "failed",
)


class InstallStatusWriter:
    """原子写 ``runtime/install_status.json``。

    contract §3.3：先写临时文件再 ``os.replace`` 覆盖，避免 kb-api SSE
    读到半截 JSON。所有字段单调推进（caller 保证 phase 不倒退）。
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._started_at = self._clock()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def flush(
        self,
        *,
        phase: str,
        progress: float = 0.0,
        message: str = "",
        bytes_downloaded: int = 0,
        total_bytes: int = 0,
        error: str = "",
    ) -> None:
        if phase not in _INSTALL_PHASES:
            raise ValueError(f"unknown phase: {phase}")
        payload = {
            "phase": phase,
            "progress": max(0.0, min(1.0, float(progress))),
            "message": message,
            "bytes_downloaded": int(bytes_downloaded),
            "total_bytes": int(total_bytes),
            "started_at": self._started_at,
            "updated_at": self._clock(),
            "error": error,
        }
        text = json.dumps(payload, ensure_ascii=False)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# CommandRunner —— 抽象子进程执行，便于测试 mock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """同步命令执行结果。"""
    returncode: int
    stdout_tail: str = ""    # 仅末尾几行，避免日志爆炸
    stderr_tail: str = ""


class CommandRunner:
    """抽象子进程执行接口。

    生产实现走 ``subprocess.run`` + tee 到日志文件；测试用记录器替换。
    """

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        log_path: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        raise NotImplementedError


class DefaultCommandRunner(CommandRunner):
    """生产路径：subprocess.run + 同步 tee log_path。

    简化策略（够用即可，pip 安装是同步阻塞动作，不需要异步流）：
    - stdout/stderr 合并；append 到 log_path（若给）
    - 不限大小；pip 最长几分钟，install_status.json 的 keepalive 由 InstallExecutor
      在主流程里按时间维度维持
    """

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        log_path: Optional[Path] = None,
        env: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        # Windows 上避免弹黑框；mac/linux flag 不存在则忽略
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        # 2026-07-02 P2-1 fix: 原逻辑 `for line in proc.stdout` 阻塞读到 EOF 才
        # 走到 proc.wait(timeout=), timeout 完全不生效(子进程 hang 时主线程永
        # 远卡在 stdout 迭代)。改成后台线程读 stdout, 主线程 wait(timeout=),
        # 超时先 terminate → 3s 再 kill, 兜底防 install probe hang 死锁 reconcile。
        # 用 deque(maxlen=50) 替代 list + append/pop, 线程安全。
        from collections import deque
        tail_dq: deque = deque(maxlen=50)
        log_handle = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")

        def _reader() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    if log_handle is not None:
                        try:
                            log_handle.write(line)
                            log_handle.flush()
                        except Exception:
                            pass
                    tail_dq.append(line)
            except Exception:
                pass

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()
        timed_out = False
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                rc = proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
        finally:
            reader_thread.join(timeout=2.0)
            if log_handle is not None:
                log_handle.close()
        if timed_out:
            tail_dq.append(f"\n[timeout {timeout}s: process terminated]\n")
        return CommandResult(returncode=rc, stdout_tail="".join(tail_dq))


# ---------------------------------------------------------------------------
# InstallExecutor —— 安装计划执行器
# ---------------------------------------------------------------------------


# 简化的 InstallPlan 接口：只依赖契约里固定字段，不直接 import build_install_plan，
# 让本模块与 app/services/embedding_install.py 解耦（壳层运行时 kb-api 在另一进程，
# 不共享 Python 对象，通过 desired-state 字段重建即可）。
@dataclass(frozen=True)
class InstallSpec:
    """壳层执行安装计划所需的最小字段集。

    生产路径：kb-api 把 ``build_install_plan(...)`` 的关键字段塞到 desired
    body 里，或壳层从 ``GET /v1/system/embedding-service/install-plan?model_id=...``
    主动拉。Batch C 阶段先让壳层调用 build_install_plan，因为 kb-api 与壳层
    都跑在同一台机器、共享文件系统。
    """

    model_id: str             # HuggingFace repo id
    venv_dir: str
    model_dir: str
    device: str
    create_venv_cmd: list[str]
    pip_install_cmd: list[str]
    download_args: dict[str, str]   # {repo_id, local_dir, endpoint}
    mirror_chain: tuple[str, ...] = ("https://hf-mirror.com", "https://huggingface.co")


class InstallExecutor:
    """执行安装计划：建 venv → pip 装 infinity-emb → 下模型。

    设计：
    - 各阶段失败立即 phase=failed + 写 last_error，**不**重试整体（只在
      下载阶段重试 mirror chain）
    - 进度通过 ``status_writer.flush`` 报告，对外可见
    - venv 命令在 PATH 内的 python，pip 走 venv/bin/pip（Windows: venv/Scripts/pip.exe）
    - 下载阶段：在 venv 内拉一个 Python 子进程跑 snapshot_download；通过
      ``--endpoint`` 参数尝试 mirror_chain；任一成功即结束
    """

    def __init__(
        self,
        *,
        status_writer: InstallStatusWriter,
        pip_log_path: Path,
        runner: CommandRunner,
    ) -> None:
        self._status = status_writer
        self._pip_log_path = Path(pip_log_path)
        self._runner = runner

    # ---- 主入口 -----------------------------------------------------------

    def execute(self, spec: InstallSpec) -> bool:
        """执行整套安装；返回 True=完成，False=失败（status 已 flush）。"""
        try:
            self._prepare(spec)
            self._create_venv(spec)
            self._pip_install(spec)
            self._download_model(spec)
        except _InstallStepFailed as e:
            self._status.flush(
                phase="failed", progress=e.progress, message=e.message, error=e.error,
            )
            return False
        self._status.flush(
            phase="completed", progress=1.0, message="安装完成",
        )
        return True

    # ---- 各阶段 -----------------------------------------------------------

    def _prepare(self, spec: InstallSpec) -> None:
        self._status.flush(
            phase="preparing", progress=0.05,
            message=f"准备安装 {spec.model_id}",
        )

    def _create_venv(self, spec: InstallSpec) -> None:
        result = self._runner.run(spec.create_venv_cmd, log_path=self._pip_log_path)
        if result.returncode != 0:
            raise _InstallStepFailed(
                progress=0.05,
                message="创建 embedding venv 失败",
                error=_tail(result.stdout_tail, result.stderr_tail),
            )
        self._status.flush(
            phase="pip_installing", progress=0.15,
            message="安装 infinity-emb 依赖",
        )

    def _pip_install(self, spec: InstallSpec) -> None:
        # 2026-07-01 加短路：venv 已可用（infinity_emb + torch + huggingface_hub
        # 都 import 得动）时 skip 整个 pip 步骤。触发场景：
        # - installer 选"保留"覆盖装（venv 保留），Inno 只清 bin/ 下 exe
        # - 跨机拷贝 venv、多份 installer 重复装同版本
        # 平级于 _download_model 里的 _is_model_dir_complete skip；老逻辑漏了这
        # 一步 → 保留 venv 场景 pip resolver 空跑十几分钟（numpy 反复降级、torch
        # cu124 index-url resolver 检查 4.4G wheel 依赖）—— 2026-07-01
        # audit 之后老大要求补齐。
        if self._venv_deps_ready(spec):
            logger.info(
                "venv deps already importable; skipping pip install (venv=%s)",
                spec.venv_dir,
            )
            self._status.flush(
                phase="downloading", progress=0.35,
                message="检测到 venv 依赖已就绪，跳过 pip 安装",
            )
            return

        # spec.pip_install_cmd 是 build_install_plan 出的单条 venv_python -c
        # "<script>" 命令；script 内部 subprocess.call 串两步 pip（先装 infinity-emb
        # 全套，后 force-upgrade numpy>=2.1 + click<8.2 兼容 Python 3.13）。详见
        # app/services/embedding_install.py:build_install_plan 注释。
        result = self._runner.run(spec.pip_install_cmd, log_path=self._pip_log_path)
        if result.returncode != 0:
            raise _InstallStepFailed(
                progress=0.15,
                message="pip install infinity-emb 失败",
                error=_tail(result.stdout_tail, result.stderr_tail),
            )

        self._status.flush(
            phase="downloading", progress=0.35,
            message="开始下载模型",
        )

    def _venv_deps_ready(self, spec: InstallSpec) -> bool:
        """强 probe：探 5 个 import + click / numpy 版本校验，识别"Step1 装成
        Step2 失败"的半装 venv（2026-07-02 P0 flag）。

        返 True → pip 步骤 skip；False → 走正常 pip 路径。任何异常（venv 不存
        在 / probe 超时 / import 报错）都返 False 走 pip 兜底，不影响正确性。

        exit code 分级（便于日志诊断）：
          0 = 全过（venv 完整可跑 infinity），skip pip
          1 = 关键 import 失败（infinity_emb / torch / huggingface_hub / click / typer）
          2 = click 版本 >= 8.2（需 Step2 pin click<8.2 修 typer 兼容）
          3 = numpy 版本不符合 py 版本区间约束（py>=3.10 需 numpy>=2.1；
              py<3.10 需 numpy<2 因为 infinity-emb 0.0.77 metadata 硬 upper <2）

        20s timeout：venv python 冷启 1-3s + import torch 首次 5-10s（cu124 需
        探 CUDA），保守留 20s 覆盖慢机器。命中场景下秒回。跟 Mac Swift 端
        EmbeddingProcessManager.swift:venvDepsReady 完全同源，两端一致。
        """
        venv_python = _find_venv_python(spec.venv_dir)
        if venv_python is None:
            return False
        # click / numpy 版本用 tuple(int, int) 比较，避字符串比较坑（8.10 vs 8.2）。
        probe_script = (
            "import sys\n"
            "try:\n"
            "    import infinity_emb, torch, huggingface_hub, click, typer\n"
            "    import numpy\n"
            "except ImportError:\n"
            "    sys.exit(1)\n"
            "def _v(pkg):\n"
            "    parts = pkg.split('.')[:2]\n"
            "    try:\n"
            "        return tuple(int(p) for p in parts)\n"
            "    except ValueError:\n"
            "        return (0, 0)\n"
            "if _v(click.__version__) >= (8, 2):\n"
            "    sys.exit(2)\n"
            "np_v = _v(numpy.__version__)\n"
            "if sys.version_info >= (3, 10):\n"
            "    if np_v < (2, 1):\n"
            "        sys.exit(3)\n"
            "else:\n"
            "    if np_v >= (2, 0):\n"
            "        sys.exit(3)\n"
            "sys.exit(0)\n"
        )
        probe_cmd = [venv_python, "-c", probe_script]
        try:
            result = self._runner.run(
                probe_cmd, log_path=self._pip_log_path, timeout=20.0,
            )
        except Exception:  # noqa: BLE001 — 探测失败退回 pip 路径，不影响正确性
            return False
        if result.returncode != 0:
            logger.info(
                "venv_deps_ready: probe failed (exit=%d, venv=%s)",
                result.returncode, spec.venv_dir,
            )
        return result.returncode == 0

    def _download_model(self, spec: InstallSpec) -> None:
        """跑 venv 内 Python 调 snapshot_download；按 mirror_chain 顺序兜底。"""
        # bug 1 修复（与 Mac Swift InstallExecutor.execute 同步）：snapshot_download
        # 前先检测 local_dir 是否已含完整权重。命中即跳过整段下载（避免重装 /
        # 升级链路把 4GB 模型重下一遍）。判定 = config.json 存在 + 至少一份
        # ≥50MB 权重；不命中走标准 resume 路径。
        local_dir = spec.download_args.get("local_dir", "")
        if local_dir and _is_model_dir_complete(local_dir):
            logger.info("model dir already complete at %s; skipping snapshot_download", local_dir)
            self._status.flush(
                phase="downloading", progress=0.95,
                message="检测到本地模型权重已完整，跳过下载",
            )
            return

        last_err = ""
        # mirror_chain 优先级：spec.download_args["endpoint"]（若有）置最前 + spec.mirror_chain 其余
        chain: list[str] = []
        primary = spec.download_args.get("endpoint", "")
        if primary:
            chain.append(primary)
        for ep in spec.mirror_chain:
            if ep and ep not in chain:
                chain.append(ep)
        if not chain:
            chain = ["https://huggingface.co"]

        for endpoint in chain:
            cmd = _build_download_cmd(spec, endpoint)
            self._status.flush(
                phase="downloading", progress=0.5,
                message=f"下载模型（{endpoint}）",
            )
            result = self._runner.run(cmd, log_path=self._pip_log_path)
            # 护栏：huggingface_hub 撞 403 后会静默 "Returning existing local_dir"
            # 让 returncode=0 但权重根本没下（实测 bytes_downloaded=0，只留元数据壳）。
            # returncode=0 不再等于成功，必须 post-check 权重完整性。
            if result.returncode == 0:
                if local_dir and _is_model_dir_complete(local_dir):
                    return
                last_err = (
                    f"snapshot_download rc=0 but weights missing at {local_dir};"
                    " likely hub silent-fallback (mirror 403 → returned local_dir as success)."
                    " tail=" + _tail(result.stdout_tail, result.stderr_tail)
                )
                logger.warning(
                    "download via %s: silent-success detected (no weights); trying next mirror",
                    endpoint,
                )
                continue
            # 空 tail 不覆盖已有 last_err（防前一个 silent-success 详细描述
            # 被后续 rc!=0 但 stdout/stderr 都空的失败覆盖成 "all mirrors exhausted"）
            new_err = _tail(result.stdout_tail, result.stderr_tail)
            if new_err:
                last_err = new_err
            logger.warning("download via %s failed (rc=%s); trying next mirror",
                           endpoint, result.returncode)
        # 全部 mirror 都失败
        raise _InstallStepFailed(
            progress=0.5,
            message="所有镜像下载失败",
            error=last_err or "all mirrors exhausted",
        )


# ---- 内部异常 ---------------------------------------------------------


class _InstallStepFailed(Exception):
    def __init__(self, *, progress: float, message: str, error: str) -> None:
        super().__init__(error)
        self.progress = progress
        self.message = message
        self.error = error


def _tail(stdout_tail: str, stderr_tail: str) -> str:
    """合并 stdout/stderr 尾部输出做 error 字段；硬截到 512 char。"""
    combined = (stdout_tail + stderr_tail).strip()
    return combined[-512:]


def _find_venv_python(venv_dir: str) -> Optional[str]:
    """在 venv_dir 下找 python 解释器绝对路径；跨平台探测。
    Win = Scripts/python.exe, Mac/Linux = bin/python 或 bin/python3。
    找不到返 None。
    """
    if not venv_dir:
        return None
    base = Path(venv_dir)
    for rel in ("Scripts/python.exe", "bin/python", "bin/python3"):
        p = base / rel
        if p.exists():
            return str(p)
    return None


def _is_model_dir_complete(local_dir: str) -> bool:
    """检测本地 model 目录是否含完整权重，命中即可跳过 snapshot_download。

    判定规则（保守，对齐 Mac Swift ``InstallExecutor.isModelDirComplete``）：
    ``config.json`` 存在 且 至少一份 **PyTorch** 权重文件（pytorch_model.bin /
    model.safetensors）单文件 >=50MB（防只下了元数据壳就被误判为完整）。

    **不认 ONNX 权重**：本项目 infinity 启动模板默认 ``engine=torch``，必须
    PyTorch 权重；若误把 onnx/model.onnx_data 当完整，会导致跳过 snapshot_download
    后 infinity 启动撞 ``OSError: no file named pytorch_model.bin, model.safetensors,
    ...``（exit code 3）。bge-m3 仓库同时含 onnx 副本，部分网络环境下 LFS 拉取顺序
    会先到 onnx 后到 pt 权重，中间任何中断都可能留下"只有 onnx"的局部目录。

    不命中（包括只缺一份权重）就放弃跳过，让 snapshot_download 走标准 resume 路径。
    """
    if not local_dir:
        return False
    base = Path(local_dir)
    if not (base / "config.json").is_file():
        return False
    weight_candidates = (
        "pytorch_model.bin",
        "model.safetensors",
    )
    min_weight_bytes = 50 * 1024 * 1024
    for rel in weight_candidates:
        try:
            size = (base / rel).stat().st_size
        except OSError:
            continue
        if size >= min_weight_bytes:
            return True
    return False


def _build_download_cmd(spec: InstallSpec, endpoint: str) -> list[str]:
    """生成 venv 内 Python 子进程命令：调 snapshot_download 下模型。

    用 Python -c 内联脚本，无需 ship 额外 .py 文件；resume_download 自带断点续传。
    """
    # 用 _find_venv_python helper 探测；venv 没建好用 PATH 兜底
    python = _find_venv_python(spec.venv_dir) or "python"

    # ignore_patterns：hf-mirror 对某些非权重文件（imgs/.DS_Store 等）返 403，
    # huggingface_hub 抓到 403 会 fallback 到 huggingface.co(国内 timeout)→
    # 判定 "remote repo cannot be accessed" → **返回 local_dir 当作成功**，
    # 但实际权重根本没下(bytes_downloaded=0)，install phase=completed 是假的。
    # 跳过这些无关紧要文件避开 403，让 LFS 权重正常拉。
    script = (
        "from huggingface_hub import snapshot_download;"
        f"snapshot_download(repo_id={spec.download_args['repo_id']!r},"
        f"local_dir={spec.download_args['local_dir']!r},"
        f"endpoint={endpoint!r},"
        "ignore_patterns=('imgs/*', '*.DS_Store', '.gitattributes'),"
        "resume_download=True)"
    )
    return [python, "-c", script]


# ---------------------------------------------------------------------------
# Batch D: SubprocessSpawner + HealthProbe + StartHandler
# ---------------------------------------------------------------------------


class ProcessHandle:
    """已 spawn infinity 子进程的引用（contract §6）。

    暴露 ``poll()`` / ``pid`` / ``terminate()`` / ``kill()`` 四个接口；生产
    实现用 ``subprocess.Popen``，测试用替身。``poll()`` 返 None 表示仍存活，
    返 int 是退出码。
    """

    pid: int

    def poll(self) -> Optional[int]:
        raise NotImplementedError

    def terminate(self) -> None:
        raise NotImplementedError

    def kill(self) -> None:
        raise NotImplementedError


class _PopenProcessHandle(ProcessHandle):
    """生产实现：包一层 ``subprocess.Popen`` 暴露 ProcessHandle 接口。"""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc

    @property
    def pid(self) -> int:  # type: ignore[override]
        return self._proc.pid

    def poll(self) -> Optional[int]:
        return self._proc.poll()

    def terminate(self) -> None:
        # Windows：subprocess.terminate 默认调 TerminateProcess（粗暴），
        # 我们要的是先 SIGTERM 等价 → 让子进程有机会清理。Win32 上调
        # GenerateConsoleCtrlEvent 太复杂，simply send SIGBREAK / SIGTERM。
        try:
            self._proc.terminate()
        except OSError:
            pass

    def kill(self) -> None:
        try:
            self._proc.kill()
        except OSError:
            pass


class SubprocessSpawner:
    """抽象子进程拉起接口；生产走 subprocess.Popen，测试用替身。"""

    def spawn(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        log_path: Optional[Path] = None,
    ) -> ProcessHandle:
        raise NotImplementedError


class DefaultSubprocessSpawner(SubprocessSpawner):
    """生产实现：subprocess.Popen + 可选 stdout/stderr → 日志文件。

    Windows 上加 CREATE_NEW_PROCESS_GROUP 让 stop 阶段能向进程组发信号；
    JobObject 整合留到 tray_app_local 集成层（Batch F）。
    """

    def spawn(
        self,
        cmd: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        log_path: Optional[Path] = None,
    ) -> ProcessHandle:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]

        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        log_file = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("a", encoding="utf-8")
            stdout = log_file
            stderr = subprocess.STDOUT

        proc = subprocess.Popen(
            list(cmd),
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdout=stdout,
            stderr=stderr,
            text=True,
            creationflags=creationflags,
        )
        # log_file 不在这里关；进程结束时由 supervisor 关
        return _PopenProcessHandle(proc)


class HealthProbe:
    """抽象 infinity 健康探活接口。

    返回 ``True`` = ready（warming 完成），``False`` = 还在 warming 或不可达。
    """

    def is_ready(self, port: int, *, timeout_sec: float = 2.0) -> bool:
        raise NotImplementedError


class DefaultHealthProbe(HealthProbe):
    """生产实现：GET http://127.0.0.1:{port}/health；200 = ready。"""

    def is_ready(self, port: int, *, timeout_sec: float = 2.0) -> bool:
        url = f"http://127.0.0.1:{port}/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
                return 200 <= resp.status < 300
        except Exception:  # noqa: BLE001 — 任何错都视为未 ready
            return False


@dataclass(frozen=True)
class StartSpec:
    """start action 所需最小字段集。

    壳层从 InstallSpec 派生（共享 venv_dir / model_dir / device / model_id），
    或从 desired-state 自带的 start_cmd（kb-api 直接给完整命令）拼。
    """

    model_id: str
    device: str
    start_cmd: list[str]    # 完整命令；缺 --port 时由 caller 追加
    port: int               # 已选端口
    runtime_dir: Path       # 用于落 pid / port 文件
    infinity_log_path: Path # tee infinity 日志
    # plan.env 透传：infinity 子进程必需的 env（如 INFINITY_BETTERTRANSFORMER=false
    # 关掉 bettertransformer 探测，否则会撞 NameError(BetterTransformerManager)）
    env: dict[str, str] = field(default_factory=dict)


# 健康探活默认超时（秒）。AC24 分级就绪：单次探活 ≤2s，整个 warmup 上限独立配。
DEFAULT_WARMUP_TIMEOUT_SEC = 120.0
DEFAULT_PROBE_INTERVAL_SEC = 1.0


class StartHandler:
    """start action 的工作流（contract §6.1 + §6.2）。

    步骤：
    1. spawn infinity 子进程
    2. 落盘 ``runtime/pid`` + ``runtime/port``
    3. 循环 ``is_ready`` 探活，warming → ready 翻转
    4. 超时仍未 ready → terminate + 返回 warming_up=True + last_error
    """

    def __init__(
        self,
        *,
        spawner: SubprocessSpawner,
        probe: HealthProbe,
        warmup_timeout_sec: float = DEFAULT_WARMUP_TIMEOUT_SEC,
        probe_interval_sec: float = DEFAULT_PROBE_INTERVAL_SEC,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._spawner = spawner
        self._probe = probe
        self._warmup_timeout_sec = warmup_timeout_sec
        self._probe_interval_sec = probe_interval_sec
        self._clock = clock
        self._sleep = sleep

    @property
    def probe(self) -> HealthProbe:
        """供 EmbeddingActionHandler 在 self-heal 路径里复用同一份 probe。"""
        return self._probe

    def spawn_and_wait_ready(self, spec: StartSpec) -> tuple[Optional[ProcessHandle], bool, str]:
        """spawn + 探活；返回 ``(handle, ready, last_error)``。

        - ``handle`` is None 表示 spawn 直接失败
        - ``ready`` 即 warming_up 是否已结束
        - ``last_error`` 为空串表示正常
        """
        try:
            handle = self._spawner.spawn(
                spec.start_cmd,
                log_path=spec.infinity_log_path,
                env=dict(spec.env) if spec.env else None,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("spawn infinity failed")
            return None, False, f"spawn failed: {e}"[:512]

        # 写 pid / port（壳层下次启动靠这两份文件清残留）
        try:
            spec.runtime_dir.mkdir(parents=True, exist_ok=True)
            (spec.runtime_dir / "pid").write_text(str(handle.pid), encoding="utf-8")
            (spec.runtime_dir / "port").write_text(str(spec.port), encoding="utf-8")
        except OSError as e:
            logger.warning("write runtime/pid|port failed: %s", e)

        deadline = self._clock() + self._warmup_timeout_sec
        while self._clock() < deadline:
            # 子进程异常早夭 → 立即返回 spawn 失败
            exit_code = handle.poll()
            if exit_code is not None:
                return None, False, f"infinity exited during warmup with code {exit_code}"
            if self._probe.is_ready(spec.port):
                return handle, True, ""
            self._sleep(self._probe_interval_sec)
        # 超时：进程还活着但还在 warming → 返回 handle，让 manager 后续 tick 继续观测
        return handle, False, f"warmup timeout after {self._warmup_timeout_sec}s"


# ---------------------------------------------------------------------------
# Batch E: StopHandler + StaleResidueCleanup
# ---------------------------------------------------------------------------


# stop 退出契约（AC14a）：先 SIGTERM，3 秒不死 SIGKILL
DEFAULT_STOP_GRACE_SEC = 3.0


class StopHandler:
    """stop action 工作流（contract §8 / AC14a）。

    步骤：
    1. ``handle.terminate()`` 发 SIGTERM（Windows = subprocess.terminate）
    2. 轮询 ``poll()``，最多等 ``grace_sec`` 秒
    3. 仍存活 → ``handle.kill()`` 强杀
    4. 清 ``runtime/pid`` + ``runtime/port``

    返回 ``(graceful: bool, error: str)``，让 manager 决定 actual_state 字段。
    """

    def __init__(
        self,
        *,
        grace_sec: float = DEFAULT_STOP_GRACE_SEC,
        poll_interval_sec: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._grace_sec = grace_sec
        self._poll_interval_sec = poll_interval_sec
        self._clock = clock
        self._sleep = sleep

    def terminate_and_wait(
        self, handle: ProcessHandle, runtime_dir: Path,
    ) -> tuple[bool, str]:
        """对已 spawn 进程发起优雅停止；返回 (graceful, last_error)。"""
        # Phase 1：SIGTERM
        try:
            handle.terminate()
        except Exception as e:  # noqa: BLE001
            logger.warning("terminate raised: %s", e)

        deadline = self._clock() + self._grace_sec
        graceful = False
        while self._clock() < deadline:
            if handle.poll() is not None:
                graceful = True
                break
            self._sleep(self._poll_interval_sec)

        error = ""
        if not graceful:
            try:
                handle.kill()
            except Exception as e:  # noqa: BLE001
                logger.warning("kill raised: %s", e)
            # 再宽限 1 秒等 SIGKILL 真正生效
            deadline2 = self._clock() + 1.0
            while self._clock() < deadline2:
                if handle.poll() is not None:
                    break
                self._sleep(self._poll_interval_sec)
            else:
                error = "process did not respond to SIGKILL"

        # 清残留 pid / port 文件
        for fname in ("pid", "port"):
            try:
                (runtime_dir / fname).unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("unlink runtime/%s failed: %s", fname, e)

        return graceful, error


# ---------------------------------------------------------------------------
# StaleResidueCleanup —— 启动时清残留 (contract §7 / AC14b)
# ---------------------------------------------------------------------------


class _PsCmdlineProbe:
    """读取指定 PID 的 cmdline；可被替身覆盖，用于测试。

    生产路径：
    - Linux / mac：``/proc/{pid}/cmdline`` 或 ``ps -p {pid} -o command=``
    - Windows：``wmic process where ProcessId={pid} get CommandLine`` 或
      ``psutil``（不引入第三方）
    本类用 ``ps`` 命令实现 mac/linux 通用版本；Windows 实现走 wmic 兜底。
    """

    def __init__(self, runner: Optional[CommandRunner] = None) -> None:
        self._runner = runner or DefaultCommandRunner()

    def cmdline(self, pid: int) -> str:
        if os.name == "nt":
            # Windows: wmic (Win10 之前) / pwsh Get-CimInstance (Win11+)。这里
            # 用 wmic，缺则返空串（让 caller 走"未知 cmdline → 视为外人 PID"分支）
            cmd = ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"]
        else:
            # mac / linux: ps -p {pid} -o command= 返回完整命令行
            cmd = ["ps", "-p", str(pid), "-o", "command="]
        try:
            result = self._runner.run(cmd)
        except Exception:  # noqa: BLE001
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout_tail or "").strip()


def _pid_alive(pid: int) -> bool:
    """跨平台 PID 存活探测。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: OpenProcess 太麻烦，用 taskkill /T(测试模式) 不合适；
        # 简化：用 ps 退出码（Windows 上 ps 可能没有）→ subprocess.run tasklist
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=2.0,
            )
            return str(pid) in out.stdout
        except Exception:  # noqa: BLE001
            return False
    # POSIX: kill -0 不实际发信号，只校验
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但是别的用户的；视作存活
        return True
    except OSError:
        return False


class StaleResidueCleaner:
    """壳层启动时清残留（contract §7 / AC14b）。

    扫 ``runtime/pid`` + ``runtime/port``：
    - 文件缺 → no-op
    - PID 已死 → 删 pid + port，回收端口
    - PID 仍活：
      - cmdline 含 ``infinity --port {port} --model-id {model_id}`` → 自家进程
        →  ``adopt()`` 返 True，告诉 manager 直接管这个 PID
      - 否则 → 外人进程占了 PID（PID 复用 / 用户其他程序），不动它，让 caller
        换端口
    """

    def __init__(
        self,
        *,
        runtime_dir: Path,
        cmdline_probe: Optional[_PsCmdlineProbe] = None,
        pid_alive_fn: Callable[[int], bool] = _pid_alive,
    ) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._probe = cmdline_probe or _PsCmdlineProbe()
        self._pid_alive = pid_alive_fn

    def adopt_or_clean(self, expected_model_id: str) -> tuple[Optional[int], Optional[int]]:
        """返回 ``(pid_to_adopt, stale_port)``。

        - ``pid_to_adopt`` is None：没有需要 adopt 的；caller 自由 spawn 新进程
        - ``pid_to_adopt`` is int：上次自己留下的 infinity，caller 可直接管
        - ``stale_port`` 给 caller 提示"端口可能被外人占着，找下一个空闲端口"
        """
        pid = self._read_int("pid")
        port = self._read_int("port")
        if pid is None:
            return None, port

        if not self._pid_alive(pid):
            # PID 已死：清残留
            self._unlink("pid")
            self._unlink("port")
            return None, None

        # PID 活着：检查是不是自己人
        cmdline = self._probe.cmdline(pid)
        # 复用 app/services/embedding_install.is_owned_infinity 的判定，但
        # 壳层不直接 import kb-api 包，简化为内联实现
        if cmdline and "infinity" in cmdline and (
            port is not None and f"--port {port}" in cmdline
        ) and f"--model-id {expected_model_id}" in cmdline:
            # 自家 infinity，adopt
            return pid, port
        # 外人占了：不动它，让 caller 换端口
        return None, port

    def _read_int(self, fname: str) -> Optional[int]:
        try:
            text = (self._runtime_dir / fname).read_text(encoding="utf-8").strip()
            return int(text) if text else None
        except (FileNotFoundError, ValueError, OSError):
            return None

    def _unlink(self, fname: str) -> None:
        try:
            (self._runtime_dir / fname).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("unlink runtime/%s failed: %s", fname, e)


# ---------------------------------------------------------------------------
# Batch F: EmbeddingActionHandler —— ActionHandler 协议的串联实现
# ---------------------------------------------------------------------------


# 子进程崩溃保活上限（contract §6.3 / AC14a 配套）：超限放弃 + 等用户手动重置
DEFAULT_MAX_RESTART_COUNT = 3


class EmbeddingActionHandler(ActionHandler):
    """ActionHandler 协议的串联实现（contract §4 主链路）。

    把 StartHandler / StopHandler / InstallExecutor / StaleResidueCleaner
    粘到一起，给 EmbeddingProcessManager 用。

    职责：
    - install: 调 InstallExecutor.execute（不 spawn 进程，仅准备 venv + 下载）
    - start: StaleResidueCleaner.adopt_or_clean → 决定 adopt 已有进程 / 换端口
      → StartHandler.spawn_and_wait_ready
    - stop: StopHandler.terminate_and_wait → 清状态
    - switch_model: stop + install（若 model 变）+ start

    内部状态：
    - ``_current_handle``：当前管理的 ProcessHandle，None = 没在跑
    - ``_restart_count``：崩溃保活计数；超 3 不再自动重启

    所有状态变更走 ``_lock`` 保护，避免 reconcile 与 supervise 并发。
    """

    def __init__(
        self,
        *,
        install_executor: InstallExecutor,
        start_handler: StartHandler,
        stop_handler: StopHandler,
        residue_cleaner: StaleResidueCleaner,
        spec_factory: Callable[[DesiredStateSnapshot, ActualStateSnapshot], "EmbeddingActionContext"],
        max_restart_count: int = DEFAULT_MAX_RESTART_COUNT,
    ) -> None:
        self._installer = install_executor
        self._starter = start_handler
        self._stopper = stop_handler
        self._cleaner = residue_cleaner
        self._spec_factory = spec_factory
        self._max_restart_count = max_restart_count

        self._lock = threading.Lock()
        self._current_handle: Optional[ProcessHandle] = None
        self._restart_count = 0

    # ---- 当前句柄查询（manager supervise 用） ---------------------------

    @property
    def current_handle(self) -> Optional[ProcessHandle]:
        with self._lock:
            return self._current_handle

    @property
    def restart_count(self) -> int:
        with self._lock:
            return self._restart_count

    def is_running(self) -> bool:
        with self._lock:
            return self._current_handle is not None and self._current_handle.poll() is None

    # ---- ActionHandler 协议实现 ---------------------------------------

    def install(self, desired, current):
        ctx = self._spec_factory(desired, current)
        if ctx.install_spec is None:
            current.last_error = "install spec missing"
            return current
        ok = self._installer.execute(ctx.install_spec)
        current.installed = ok
        current.model_id = desired.model_id
        current.device = desired.device
        current.last_error = "" if ok else "install failed (see install_status.json)"
        return current

    def start(self, desired, current):
        # 1) 启动前清残留
        ctx = self._spec_factory(desired, current)
        start_spec = ctx.start_spec
        if start_spec is None:
            current.last_error = "start spec missing"
            return current

        adopt_pid, stale_port = self._cleaner.adopt_or_clean(desired.model_id)
        if adopt_pid is not None:
            # 上次自己留下的 infinity 仍在跑 → 直接 adopt，不重 spawn
            with self._lock:
                self._current_handle = _AdoptedHandle(pid=adopt_pid)
            current.running = True
            current.warming_up = False  # adopt 来的视作已就绪（adopt 前 cmdline 校验过）
            current.pid = adopt_pid
            current.port = start_spec.port
            current.model_id = desired.model_id
            # adopt 能拿到 infinity handle 说明模型 + venv 都在盘上：filesystem
            # 层面必然装好，回补 installed=True 让 kb-api 视图跟真实盘状一致。
            # 防御 install handler 因 SSE 立即关闭而被跳过（2026-07-03 P0 事故）导致
            # _actual.installed 永远停在 False，UI 显示"未安装" 但服务已在跑。
            current.installed = True
            current.last_error = ""
            return current

        # 2) 端口 stale → caller 应在 spec_factory 里替换；这里再次保守提示
        if stale_port is not None and start_spec.port == stale_port:
            current.last_error = f"port {stale_port} stale; spec_factory 应选其他端口"
            return current

        # 3) spawn + 等 ready
        handle, ready, err = self._starter.spawn_and_wait_ready(start_spec)
        if handle is None:
            current.running = False
            current.warming_up = False
            current.last_error = err
            return current

        with self._lock:
            self._current_handle = handle
            preserved_restart = self._restart_count
        current.running = True
        current.warming_up = not ready
        current.pid = handle.pid
        current.port = start_spec.port
        current.model_id = desired.model_id
        current.device = desired.device
        # spawn_and_wait_ready 能拿到 handle 说明 infinity_emb.exe 用 model + venv
        # 起来了；filesystem 层面必然装好，回补 installed=True 让 kb-api 视图跟盘
        # 状一致。防御 install handler 因 SSE 立即关闭而被跳过（2026-07-03 P0 事故）
        # 导致 _actual.installed 永远停在 False，UI 显示"未安装" 但服务已在跑。
        current.installed = True
        # restart_count 只在 stop 时归零;start 期间保留累计,supervise 触发的重启
        # 能继续往上加;用户手动 stop 后再 start 也能从 0 重新计
        current.restart_count = preserved_restart
        current.last_error = err if not ready else ""
        return current

    def stop(self, desired, current):
        with self._lock:
            handle = self._current_handle
        if handle is None:
            current.running = False
            current.warming_up = False
            current.pid = None
            current.last_error = ""
            return current

        ctx = self._spec_factory(desired, current)
        runtime_dir = ctx.runtime_dir or Path(".")
        graceful, err = self._stopper.terminate_and_wait(handle, runtime_dir)
        with self._lock:
            self._current_handle = None
            self._restart_count = 0
        current.running = False
        current.warming_up = False
        current.pid = None
        current.restart_count = 0
        current.last_error = err if err else ("" if graceful else "force-killed after grace")
        return current

    def shutdown(self) -> None:
        """tray quit 时强制带走 infinity（与 Mac Swift 同步）。

        manager.stop() 起手调用：set stop_event 后 reconcile loop 不会再
        dispatch，但 currentHandle 持有的 infinity 子进程仍在跑——必须在
        tray 退出前显式 SIGTERM → grace → SIGKILL，否则 infinity 成孤儿
        （kb-tray 退出后 Windows 不会自动回收子进程；taskkill kb-api 树也
        不带 infinity，因为 infinity 是 kb-tray 平级 spawn 的）。

        runtime_dir 通过 spec_factory 传入 empty desired/actual 拿（factory
        实现保证 model_id=空时回退到固定 runtime_dir）。stopper 自身就清
        ``runtime/pid|port``。
        """
        with self._lock:
            handle = self._current_handle
            self._current_handle = None
        if handle is None:
            return
        try:
            ctx = self._spec_factory(DesiredStateSnapshot(), ActualStateSnapshot())
            runtime_dir = ctx.runtime_dir or Path(".")
        except Exception:  # noqa: BLE001
            logger.warning(
                "shutdown: spec_factory failed; runtime_dir fallback to cwd",
                exc_info=True,
            )
            runtime_dir = Path(".")
        try:
            self._stopper.terminate_and_wait(handle, runtime_dir)
        except Exception:  # noqa: BLE001
            logger.warning("shutdown: terminate_and_wait failed", exc_info=True)

    def self_heal_warmup_if_needed(self, actual):
        """bug 2 自愈实现（与 Mac Swift ``selfHealWarmupIfNeeded`` 同步）。

        触发条件（全部满足）：
        - 当前持有 handle 且 ``poll()`` is None（进程仍活）
        - ``actual.warming_up`` 为 true 或 ``last_error`` 含 "warmup timeout"
        - 探活 ``/health`` 返回 200

        触发后清掉 warming_up + last_error，把 running 翻 true。否则返回 None。
        """
        with self._lock:
            handle = self._current_handle
        if handle is None or handle.poll() is not None:
            return None
        stuck = actual.warming_up or "warmup timeout" in (actual.last_error or "")
        if not stuck:
            return None
        port = actual.port if actual.port > 0 else 7687
        if not self._starter.probe.is_ready(port):
            return None

        healed = ActualStateSnapshot(**asdict(actual))
        healed.running = True
        healed.warming_up = False
        healed.last_error = ""
        return healed

    def switch_model(self, desired, current):
        # stop -> install -> start;任一阶段失败立即返回，不再向下
        current = self.stop(desired, current)
        if current.running:
            # stop 没成功停掉旧的，不切
            current.last_error = current.last_error or "stop before switch failed"
            return current

        current = self.install(desired, current)
        if not current.installed:
            return current

        current = self.start(desired, current)
        return current

    # ---- 崩溃保活（manager supervise 调） --------------------------------

    def supervise_tick(self, desired: DesiredStateSnapshot, current: ActualStateSnapshot) -> ActualStateSnapshot:
        """每个 reconcile tick 调一次：检查子进程健康，必要时触发重启。

        - 期望 running 且子进程已死 → restart_count += 1，重 spawn（≤ 3 次）
        - 期望 running 且仍活 → no-op
        - 期望 stopped 已死 → no-op
        """
        if not desired.enabled or desired.action == "stop":
            return current
        with self._lock:
            handle = self._current_handle
            cnt = self._restart_count
        if handle is None:
            return current

        exit_code = handle.poll()
        if exit_code is None:
            return current  # 仍存活

        # 已死
        if cnt >= self._max_restart_count:
            with self._lock:
                self._current_handle = None
            current.running = False
            current.warming_up = False
            current.pid = None
            current.last_error = f"restart_limit_exceeded (exit={exit_code})"
            current.restart_count = cnt
            return current

        # 尝试重启
        with self._lock:
            self._restart_count = cnt + 1
        new_current = self.start(desired, current)
        new_current.restart_count = cnt + 1
        return new_current


@dataclass(frozen=True)
class EmbeddingActionContext:
    """spec_factory 返回的上下文束：动作执行所需的全部 spec。

    生产路径：壳层在 ``spec_factory`` 内根据 desired/actual 拼 InstallSpec /
    StartSpec / runtime_dir，可灵活换 port / 路径。
    """

    install_spec: Optional[InstallSpec] = None
    start_spec: Optional[StartSpec] = None
    runtime_dir: Optional[Path] = None


class _AdoptedHandle(ProcessHandle):
    """adopt 来的 PID 没有 Popen 对象；用 ``_pid_alive`` 做 poll 兜底。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> Optional[int]:
        return None if _pid_alive(self.pid) else -1

    def terminate(self) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(self.pid)], capture_output=True)
        else:
            try:
                os.kill(self.pid, 15)  # SIGTERM
            except OSError:
                pass

    def kill(self) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.pid)], capture_output=True)
        else:
            try:
                os.kill(self.pid, 9)  # SIGKILL
            except OSError:
                pass
