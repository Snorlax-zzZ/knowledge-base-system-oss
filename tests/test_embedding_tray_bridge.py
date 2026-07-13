"""windows-app/embedding_tray_bridge.py 单元测试。

只覆盖纯组装逻辑（spec_factory 字段映射 / bundle 工厂注入实例正确），不
真发 HTTP / 不真 spawn 子进程——那些路径由 test_embedding_process_manager
的端到端测试 cover。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WINDOWS_APP_DIR = Path(__file__).resolve().parent.parent / "windows-app"
if str(_WINDOWS_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WINDOWS_APP_DIR))

from embedding_process_manager import (  # noqa: E402
    ActualStateSnapshot,
    DesiredStateSnapshot,
    EmbeddingProcessManager,
)
from embedding_tray_bridge import (  # noqa: E402
    EmbeddingTrayBundle,
    build_default_bundle,
    make_default_spec_factory,
)


class TestSpecFactory:
    def test_empty_model_id_returns_only_runtime_dir(self, tmp_path):
        factory = make_default_spec_factory(tmp_path)
        ctx = factory(DesiredStateSnapshot(action="none"), ActualStateSnapshot())
        assert ctx.install_spec is None
        assert ctx.start_spec is None
        assert ctx.runtime_dir == tmp_path / "runtime"

    def test_known_model_produces_install_and_start_spec(self, tmp_path):
        factory = make_default_spec_factory(tmp_path)
        desired = DesiredStateSnapshot(
            action="install", model_id="bge-m3", device="cpu", enabled=True,
        )
        ctx = factory(desired, ActualStateSnapshot())
        assert ctx.install_spec is not None
        assert ctx.start_spec is not None
        # InstallSpec 字段映射
        assert ctx.install_spec.venv_dir == str(tmp_path / "embedding-service" / "venv")
        assert ctx.install_spec.model_dir == str(tmp_path / "models" / "bge-m3")
        assert ctx.install_spec.device == "cpu"
        # download_args 镜像默认 hf-mirror
        assert ctx.install_spec.download_args["repo_id"] == "BAAI/bge-m3"
        # StartSpec 含 --port 追加
        assert "--port" in ctx.start_spec.start_cmd
        assert ctx.start_spec.port > 0
        assert ctx.start_spec.cmdline_model_value == str(tmp_path / "models" / "bge-m3")

    def test_actual_port_reused_when_set(self, tmp_path):
        """已有 actual.port → 不重新 find_free_port，直接复用。"""
        factory = make_default_spec_factory(tmp_path)
        desired = DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True)
        actual = ActualStateSnapshot(port=29999)
        ctx = factory(desired, actual)
        assert ctx.start_spec is not None
        assert ctx.start_spec.port == 29999
        # cmdline 也是这个端口
        assert "29999" in " ".join(ctx.start_spec.start_cmd)

    def test_unknown_model_returns_no_spec(self, tmp_path):
        factory = make_default_spec_factory(tmp_path)
        desired = DesiredStateSnapshot(
            action="install", model_id="bogus-model-xyz", device="cpu",
        )
        ctx = factory(desired, ActualStateSnapshot())
        assert ctx.install_spec is None
        assert ctx.start_spec is None
        assert ctx.runtime_dir == tmp_path / "runtime"

    def test_residue_match_with_generated_cmdline(self, tmp_path):
        """生成的 start_cmd 必须能被 StaleResidueCleaner 的 cmdline 匹配规则识别。

        规则：cmdline 含 "infinity" + "--port {port}" + "--model-id {model_id}"
        因 InstallSpec.model_id 我们设为 plan.model_dir，故 cmdline 内
        "--model-id <path>" 跟 InstallSpec.model_id 比对必然一致。
        """
        factory = make_default_spec_factory(tmp_path)
        desired = DesiredStateSnapshot(action="start", model_id="bge-m3", enabled=True)
        actual = ActualStateSnapshot(port=7687)
        ctx = factory(desired, actual)
        assert ctx.start_spec is not None
        cmdline = " ".join(ctx.start_spec.start_cmd)
        # 模拟 is_owned_infinity 的判定
        assert "infinity" in cmdline
        assert "--port 7687" in cmdline
        assert f"--model-id {ctx.start_spec.model_id}" in cmdline


class TestBuildDefaultBundle:
    def test_bundle_components_wired(self, tmp_path):
        bundle = build_default_bundle(data_root=tmp_path, kb_api_port=18000)
        assert isinstance(bundle, EmbeddingTrayBundle)
        assert isinstance(bundle.manager, EmbeddingProcessManager)
        assert bundle.token_source.token_path == tmp_path / "runtime" / "owner_token"
        assert bundle.data_root == tmp_path

    def test_snapshot_returns_actual_state(self, tmp_path):
        bundle = build_default_bundle(data_root=tmp_path, kb_api_port=18000)
        snap = bundle.snapshot()
        assert isinstance(snap, ActualStateSnapshot)
        # 默认状态：所有 false / 0
        assert snap.running is False
        assert snap.installed is False
        assert snap.acknowledged_generation == 0

    def test_stop_idle_bundle_does_not_hang(self, tmp_path):
        """bundle.stop() 不应阻塞，即使 manager 从未 start()。"""
        bundle = build_default_bundle(data_root=tmp_path, kb_api_port=18000)
        # 不调 start，直接 stop
        bundle.stop(timeout=2.0)  # 应秒回（_thread is None）
