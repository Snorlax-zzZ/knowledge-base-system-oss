"""Windows 托盘 App 与 EmbeddingProcessManager 的桥接层。

把 ``embedding_process_manager`` 的零散件（OwnerTokenSource / KbApiClient /
InstallExecutor / StartHandler / StopHandler / StaleResidueCleaner /
EmbeddingActionHandler / EmbeddingProcessManager）组装成一个可直接交给
``tray_app_local.LocalTrayController`` 用的 ``EmbeddingTrayBundle``。

把 wiring 单独成模块的理由：

- ``tray_app_local.py`` 依赖 ``pystray`` / Windows-only API，开发机（mac）
  上 import 即报错；本模块仅依赖 stdlib + ``embedding_process_manager`` +
  ``app.services.embedding_install``（纯逻辑层），mac 上能跑单测
- 把 ``InstallSpec`` / ``StartSpec`` 工厂集中在一处，下次 Mac Swift 实现
  时可以照搬本文件的字段映射，不必再读契约文档

依赖图：

    tray_app_local.py
            ↓
    embedding_tray_bridge.py  ← 本模块
            ↓
    embedding_process_manager.py（manager + handlers）
            ↓
    app.services.embedding_install.build_install_plan（计划生成器）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.services.embedding_install import (
    DEFAULT_EMBEDDING_PORT,
    build_install_plan,
    find_free_port,
)

from embedding_process_manager import (
    ActualStateSnapshot,
    DefaultCommandRunner,
    DefaultHealthProbe,
    DefaultSubprocessSpawner,
    DesiredStateSnapshot,
    EmbeddingActionContext,
    EmbeddingActionHandler,
    EmbeddingProcessManager,
    InstallExecutor,
    InstallSpec,
    InstallStatusWriter,
    KbApiClient,
    OwnerTokenSource,
    StaleResidueCleaner,
    StartHandler,
    StartSpec,
    StopHandler,
)


logger = logging.getLogger("embedding_tray_bridge")


# ---------------------------------------------------------------------------
# 字段映射：把 build_install_plan(InstallPlan) 转成壳层用的 InstallSpec
# ---------------------------------------------------------------------------


def _install_plan_to_spec(plan, model_id: str) -> InstallSpec:
    """从 ``app.services.embedding_install.InstallPlan`` 转 ``InstallSpec``。

    ``model_id`` 用 desired 里的 key（如 ``bge-m3``），不用 plan.model_spec.model_id
    （那是 HF repo id）。``is_owned_infinity`` 的 model_id 匹配规则按 plan 内
    --model-id 字段是 ``model_dir`` 路径，cmdline 含 ``--model-id {model_dir}``
    —— 当残留识别用，model_id 字段同时充当 path 比对。

    **Win 平台路径翻译**：``build_install_plan`` 按 design.md §3.3 给的是逻辑
    入口名 ``venv/bin/python``（Mac/Linux 风格），Win venv 实际在
    ``venv\\Scripts\\python.exe``。Win 壳层这里翻译 ``pip_install_cmd[0]`` +
    ``create_venv_cmd``。``start_cmd`` 翻译另走 ``_translate_start_cmd_to_win``。
    """
    return InstallSpec(
        model_id=plan.model_dir,  # 与 start_cmd 内 --model-id 保持一致，便于残留判定
        venv_dir=plan.venv_dir,
        model_dir=plan.model_dir,
        device=plan.device,
        create_venv_cmd=list(plan.create_venv_cmd),
        pip_install_cmd=_translate_venv_python_cmd_to_win(
            list(plan.pip_install_cmd), plan.venv_dir,
        ),
        download_args=dict(plan.download_args),
    )


def _translate_venv_python_cmd_to_win(cmd: list[str], venv_dir: str) -> list[str]:
    """把命令里 ``venv/bin/python`` 替换成 Win 的 ``venv\\Scripts\\python.exe``。

    ``build_install_plan`` 的 ``pip_install_cmd`` 是 ``[venv_python, "-c",
    "<script>"]`` 形式，Win 上 ``venv_python`` 是 Mac/Linux 风格路径
    ``D:\\KnowledgeBase\\embedding-service\\venv\\bin\\python``，文件不存在
    → ``subprocess.Popen`` 抛 ``[WinError 2]``。这里只动 ``cmd[0]``，其余参数
    不动（``-c`` 后的 script 里用 ``sys.executable`` 取当前进程 Python，自动
    指向 venv Python，不需要硬替换）。
    """
    if not cmd:
        return cmd
    out = list(cmd)
    first = out[0]
    if first.endswith("python") or first.endswith("python.exe"):
        out[0] = str(Path(venv_dir) / "Scripts" / "python.exe")
    return out


def _start_cmd_with_port(plan_start_cmd: list[str], port: int) -> list[str]:
    """build_install_plan 给的 start_cmd 不含 ``--port``；壳层选完空闲端口后追加。

    注意：``plan_start_cmd`` 的 ``--port`` 实际上在 ``build_install_plan`` 已经
    硬编码了 ``DEFAULT_EMBEDDING_PORT``（见 embedding_install.py:258）。这里再
    追加一次是 contract 设计冗余——双 ``--port`` 时 infinity 取最后一个，所以
    实际端口仍是壳层选的 ``port``。保留追加以兜底未来 plan 不再硬编码的情况。
    """
    cmd = list(plan_start_cmd)
    cmd.extend(["--port", str(port)])
    return cmd


def _translate_start_cmd_to_win(plan_start_cmd: list[str], venv_dir: str) -> list[str]:
    """Win 平台 start_cmd 路径翻译：plan.start_cmd[0] 是 ``venv/bin/infinity_emb``
    (Mac/Linux)，Windows venv 实际是 ``venv\\Scripts\\infinity_emb.exe``。

    不翻译的话 ``subprocess.Popen`` 找不到可执行文件 → ``[WinError 2] 系统找
    不到指定的文件``，infinity 永远起不来，``runtime/pid`` + ``runtime/port``
    都不写入，前端看到 ``running=false`` + ``spawn failed``。

    与 ``_install_plan_to_spec`` 里的 pip_install_cmd 翻译同源（sh-c → Win
    native），都是因为 ``app/services/embedding_install.py`` 按 design.md §3.3
    保持"零平台分支"，Windows 路径硬编码 ``venv/bin/...`` 需要在壳层翻译。
    """
    if not plan_start_cmd:
        return plan_start_cmd
    cmd = list(plan_start_cmd)
    first = cmd[0]
    # 兼容 "venv/bin/infinity_emb" 或绝对路径 ".../venv/bin/infinity_emb"
    if first.endswith("infinity_emb") and "Scripts" not in first:
        cmd[0] = str(Path(venv_dir) / "Scripts" / "infinity_emb.exe")
    return cmd


# ---------------------------------------------------------------------------
# Bundle —— 把零散件拼成完整可用对象
# ---------------------------------------------------------------------------


@dataclass
class EmbeddingTrayBundle:
    """组装产物：托盘 app 主线只跟本对象打交道。

    用法：

        bundle = build_default_bundle(data_root=Path("..."), kb_api_port=18000)
        bundle.start()    # 启动 reconcile 后台线程
        ...
        bundle.stop()     # 退出时清理
        snap = bundle.snapshot()   # 给托盘菜单显示状态用
    """

    manager: EmbeddingProcessManager
    action_handler: EmbeddingActionHandler
    token_source: OwnerTokenSource
    data_root: Path

    def start(self) -> None:
        self.manager.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.manager.stop(timeout=timeout)

    def snapshot(self) -> ActualStateSnapshot:
        return self.manager.snapshot_actual()


# ---------------------------------------------------------------------------
# 默认 Bundle 工厂
# ---------------------------------------------------------------------------


def build_default_bundle(
    *,
    data_root: Path,
    kb_api_port: int,
    loop_period_sec: float = 3.0,
    heartbeat_sec: float = 5.0,
    on_error: Optional[Callable[[str], None]] = None,
) -> EmbeddingTrayBundle:
    """生产路径的标准组装；所有零散件都用 Default*** 实现。

    ``data_root`` 即 ``KB_APP_ROOT``，与 kb-api 一致；壳层用同一目录读
    ``runtime/owner_token`` + 写 ``runtime/install_status.json`` /
    ``runtime/pid`` 等。
    """
    data_root = Path(data_root)
    runtime_dir = data_root / "runtime"
    logs_dir = data_root / "logs"

    token_source = OwnerTokenSource(runtime_dir / "owner_token")
    client = KbApiClient(
        base_url=f"http://127.0.0.1:{kb_api_port}",
        token_source=token_source,
    )

    runner = DefaultCommandRunner()
    install_executor = InstallExecutor(
        status_writer=InstallStatusWriter(runtime_dir / "install_status.json"),
        pip_log_path=logs_dir / "pip.log",
        runner=runner,
    )
    start_handler = StartHandler(
        spawner=DefaultSubprocessSpawner(),
        probe=DefaultHealthProbe(),
    )
    stop_handler = StopHandler()
    residue_cleaner = StaleResidueCleaner(runtime_dir=runtime_dir)

    spec_factory = make_default_spec_factory(data_root)
    action_handler = EmbeddingActionHandler(
        install_executor=install_executor,
        start_handler=start_handler,
        stop_handler=stop_handler,
        residue_cleaner=residue_cleaner,
        spec_factory=spec_factory,
    )

    manager = EmbeddingProcessManager(
        client=client,
        handler=action_handler,
        loop_period_sec=loop_period_sec,
        heartbeat_sec=heartbeat_sec,
        on_error=on_error,
    )
    return EmbeddingTrayBundle(
        manager=manager,
        action_handler=action_handler,
        token_source=token_source,
        data_root=data_root,
    )


def make_default_spec_factory(data_root: Path) -> Callable[
    [DesiredStateSnapshot, ActualStateSnapshot], EmbeddingActionContext,
]:
    """生成 EmbeddingActionHandler 用的 spec_factory。

    对每个 action 类型生产对应 spec：
    - install / switch_model：build_install_plan + 转 InstallSpec
    - start / switch_model：build_install_plan 得到 start_cmd → 追加端口
    - 端口选择：复用 actual 里的端口（已 spawn 过）或 find_free_port
    """
    runtime_dir = Path(data_root) / "runtime"
    infinity_log = Path(data_root) / "logs" / "infinity.log"

    def factory(
        desired: DesiredStateSnapshot, actual: ActualStateSnapshot,
    ) -> EmbeddingActionContext:
        if not desired.model_id:
            return EmbeddingActionContext(runtime_dir=runtime_dir)

        try:
            plan = build_install_plan(
                desired.model_id, str(data_root), device=desired.device,
                pytorch_mirror=desired.pytorch_mirror,
                cuda_version=desired.cuda_version,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("build_install_plan failed for %s: %s", desired.model_id, e)
            return EmbeddingActionContext(runtime_dir=runtime_dir)

        install_spec = _install_plan_to_spec(plan, desired.model_id)

        port = actual.port or 0
        if port <= 0:
            try:
                port = find_free_port(start_port=DEFAULT_EMBEDDING_PORT)
            except Exception as e:  # noqa: BLE001
                logger.warning("find_free_port failed: %s; fallback default", e)
                port = DEFAULT_EMBEDDING_PORT

        start_spec = StartSpec(
            model_id=plan.model_dir,  # 与 cmdline --model-id 字段一致
            device=plan.device,
            start_cmd=_start_cmd_with_port(
                _translate_start_cmd_to_win(plan.start_cmd, plan.venv_dir),
                port,
            ),
            port=port,
            runtime_dir=runtime_dir,
            infinity_log_path=infinity_log,
            # 透传 plan.env（INFINITY_BETTERTRANSFORMER=false 等），缺这一步
            # infinity 启动撞 NameError(BetterTransformerManager) → exit 3
            env=dict(plan.env or {}),
            cmdline_model_value=plan.model_dir,
        )

        return EmbeddingActionContext(
            install_spec=install_spec,
            start_spec=start_spec,
            runtime_dir=runtime_dir,
        )

    return factory
