"""Embedding service 控制面状态（design v1.2 §3.2 + AC25 + AC26）。

进程级单例。kb-api 各路由通过 ``get_embedding_service_state()`` 共享。

承担三件事：

1. **desired-state**：kb-api 决定要让壳层（mac-app / windows-app 的
   ``ProcessManager``）做什么——安装 / 启动 / 停止 / 切模型；带 ``generation``
   单调递增版本号，便于壳层判断"是不是新指令"
2. **actual-state**：壳层周期回写 infinity 进程的真实运行状况（installed /
   running / warming_up / pid / port / device / restart_count 等）
3. **owner_token**：kb-api 启动时一次性生成的随机串，壳层回写 actual-state
   必须在 header 携带；token 不符 → 视为本机其他进程伪造，拒（AC25）

generation 单调规则（AC25 防"旧覆盖新"）：
- desired-state 每次 ``bump_desired()`` → ``generation += 1``
- actual-state 回写携带的 ``acknowledged_generation`` 必须 **≥** 最后一次
  acknowledged 的 generation；否则视为壳层基于旧 desired 在回写，拒（409）

进程模型：单 worker 单进程；``threading.Lock`` 足够。未来多 worker 部署需迁
共享存储（Redis），届时只需重写本模块接口。
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class DesiredState:
    """kb-api 写入、壳层读取的期望状态。

    ``action`` 枚举：

    - ``none``：保持现状（默认）
    - ``install``：跑安装计划（建 venv / pip install / 下模型）
    - ``start``：起 infinity
    - ``stop``：停 infinity
    - ``switch_model``：停现有 + 安装 ``model_id`` + 起新进程
    """
    action: str = "none"
    model_id: str = ""        # 目标模型 key（如 "bge-m3"）
    device: str = "cpu"
    enabled: bool = False     # mode=local 时为 True，方便壳层快速判断"该不该跑"
    # v1.3.12：cuda wheel 安装配置。仅 device=cuda 时影响 pip 命令；cpu/mps 忽略。
    # 默认值与 SystemConfig / build_install_plan 内部默认保持一致。
    pytorch_mirror: str = "https://download.pytorch.org/whl/"
    cuda_version: str = "cu124"
    generation: int = 0
    updated_at: float = 0.0


@dataclass(frozen=True)
class ActualState:
    """壳层回写、kb-api 读取的实况状态。"""
    installed: bool = False
    running: bool = False
    warming_up: bool = False
    model_id: str = ""
    port: int = 0
    pid: Optional[int] = None
    device: str = "cpu"
    restart_count: int = 0
    last_health_check: Optional[float] = None
    last_error: str = ""
    # 壳层执行时基于的 desired generation；用作单调校验。
    acknowledged_generation: int = 0
    updated_at: float = 0.0


class GenerationConflict(Exception):
    """回写 actual-state 携带的 generation 落后于最后一次 acknowledged。"""


class OwnerTokenMismatch(Exception):
    """回写 actual-state 携带的 owner_token 不匹配。"""


class EmbeddingServiceState:
    """Embedding 控制面状态机（线程安全单例）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner_token = secrets.token_urlsafe(32)
        self._desired = DesiredState()
        self._actual = ActualState()

    @property
    def owner_token(self) -> str:
        return self._owner_token

    def desired(self) -> DesiredState:
        with self._lock:
            return self._desired

    def actual(self) -> ActualState:
        with self._lock:
            return self._actual

    def bump_desired(
        self,
        *,
        action: str,
        model_id: str = "",
        device: str = "cpu",
        enabled: bool = False,
        pytorch_mirror: str = "https://download.pytorch.org/whl/",
        cuda_version: str = "cu124",
    ) -> DesiredState:
        """更新 desired-state 并自增 generation。"""
        with self._lock:
            self._desired = DesiredState(
                action=action,
                model_id=model_id,
                device=device,
                enabled=enabled,
                pytorch_mirror=pytorch_mirror,
                cuda_version=cuda_version,
                generation=self._desired.generation + 1,
                updated_at=time.time(),
            )
            return self._desired

    def apply_actual(
        self,
        *,
        owner_token: str,
        acknowledged_generation: int,
        installed: bool,
        running: bool,
        warming_up: bool,
        model_id: str,
        port: int,
        pid: Optional[int],
        device: str,
        restart_count: int,
        last_error: str = "",
    ) -> ActualState:
        """壳层回写实况；做 owner token + generation 双校验。"""
        if owner_token != self._owner_token:
            raise OwnerTokenMismatch("owner token mismatch")
        with self._lock:
            if acknowledged_generation < self._actual.acknowledged_generation:
                raise GenerationConflict(
                    f"acknowledged_generation {acknowledged_generation} < "
                    f"last {self._actual.acknowledged_generation}; "
                    "refusing stale write"
                )
            now = time.time()
            self._actual = ActualState(
                installed=installed,
                running=running,
                warming_up=warming_up,
                model_id=model_id,
                port=port,
                pid=pid,
                device=device,
                restart_count=restart_count,
                last_health_check=now,
                last_error=last_error,
                acknowledged_generation=acknowledged_generation,
                updated_at=now,
            )
            return self._actual

    def reset_for_tests(self) -> None:
        """测试钩子：清状态 + 重新生成 owner_token。生产代码不应调用。"""
        with self._lock:
            self._owner_token = secrets.token_urlsafe(32)
            self._desired = DesiredState()
            self._actual = ActualState()


_singleton: Optional[EmbeddingServiceState] = None
_singleton_lock = threading.Lock()


def get_embedding_service_state() -> EmbeddingServiceState:
    """返回进程级 EmbeddingServiceState 单例。"""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = EmbeddingServiceState()
    return _singleton


def resolve_owner_token_path(data_root: str) -> Path:
    """返回壳层用来读 owner_token 的落盘路径（``{data_root}/runtime/owner_token``）。

    与 design §3.2 / AC25 一致：kb-api 启动时一次性写入，壳层启动后 read 一次
    然后调 ``POST actual-state`` 时塞 ``X-Embedding-Owner-Token`` 头。
    """
    return Path(data_root) / "runtime" / "owner_token"


def write_owner_token_file(data_root: str, token: str) -> Path:
    """把 owner_token 写到 ``{data_root}/runtime/owner_token``。

    - 父目录不存在则建（``parents=True, exist_ok=True``）
    - 已存在文件先 unlink 再写（避免符号链接攻击 / 旧文件残留权限）
    - chmod 0o600（仅 owner 可读写；Windows 上 chmod 不严格但仍调用）
    - **存在性 ≠ 一致性**：壳层每次回写都得带 token，kb-api 进程重启会生成新
      token + 覆盖文件；老 token 自动失效
    """
    target = resolve_owner_token_path(data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    # 用 O_CREAT | O_WRONLY | O_EXCL 创建，避免被预置符号链接劫持
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception:
        # 写失败要清掉半成品，下次启动重新写
        try:
            target.unlink()
        except OSError:
            pass
        raise
    try:
        os.chmod(str(target), 0o600)
    except OSError:
        # Windows / 部分 FS 不支持 chmod，吞掉
        pass
    return target
