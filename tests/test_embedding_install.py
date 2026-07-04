"""验证内置 Embedding 服务安装计划与纯逻辑层（app/services/embedding_install）。

覆盖：模型解析 / 设备裁决 / 端口探测 / owner 凭证判定 / reindex 阈值 /
磁盘预检 / 安装计划生成。本层刻意不含下载与 spawn（AC27），故全部可单测。
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from app.services import embedding_install as ei
from app.services.disk_space import InsufficientDiskSpaceError


class TestResolveModel:
    def test_default_model_in_registry(self) -> None:
        assert ei.DEFAULT_MODEL_KEY in ei.MODEL_REGISTRY
        spec = ei.resolve_model(ei.DEFAULT_MODEL_KEY)
        assert spec.model_id == "BAAI/bge-m3"
        assert spec.dim == 1024

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(ei.EmbeddingInstallError):
            ei.resolve_model("does-not-exist")


class TestResolveDevice:
    def test_configured_wins(self) -> None:
        assert ei.resolve_device("cuda") == "cuda"
        assert ei.resolve_device("mps", detected_cuda=False) == "mps"

    def test_invalid_configured_raises(self) -> None:
        with pytest.raises(ei.EmbeddingInstallError):
            ei.resolve_device("tpu")

    def test_detected_cuda_used_when_unconfigured(self) -> None:
        assert ei.resolve_device(None, detected_cuda=True) == "cuda"

    def test_cpu_fallback(self) -> None:
        assert ei.resolve_device(None, detected_cuda=False) == "cpu"
        assert ei.resolve_device(None) == "cpu"


class TestFindFreePort:
    def test_returns_start_port_when_free(self) -> None:
        # 先占一个端口，确认 find_free_port 会避让到下一个。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", 0))
            taken = occupied.getsockname()[1]
            port = ei.find_free_port(start_port=taken)
            assert port != taken
            assert port > taken

    def test_exhausted_raises(self) -> None:
        with pytest.raises(ei.EmbeddingInstallError):
            # max_tries=0 → 无可尝试范围，立即耗尽。
            ei.find_free_port(start_port=7687, max_tries=0)


class TestIsOwnedInfinity:
    def test_match(self) -> None:
        cmd = "/x/venv/bin/infinity_emb v2 --model-id /d/models/bge-m3 --port 7687 --device cpu"
        assert ei.is_owned_infinity(cmd, port=7687, model_id="/d/models/bge-m3") is True

    def test_wrong_port_rejected(self) -> None:
        cmd = "infinity_emb v2 --model-id /d/models/bge-m3 --port 9999 --device cpu"
        assert ei.is_owned_infinity(cmd, port=7687, model_id="/d/models/bge-m3") is False

    def test_wrong_model_rejected(self) -> None:
        cmd = "infinity_emb v2 --model-id /d/models/other --port 7687 --device cpu"
        assert ei.is_owned_infinity(cmd, port=7687, model_id="/d/models/bge-m3") is False

    def test_foreign_process_rejected(self) -> None:
        assert ei.is_owned_infinity("python -m http.server 7687", port=7687, model_id="x") is False

    def test_empty_cmdline(self) -> None:
        assert ei.is_owned_infinity("", port=7687, model_id="x") is False


class TestReindexThreshold:
    def test_below_threshold_no_block(self) -> None:
        assert ei.should_block_writes_for_reindex(ei.REINDEX_MAINTENANCE_THRESHOLD - 1) is False

    def test_at_threshold_blocks(self) -> None:
        assert ei.should_block_writes_for_reindex(ei.REINDEX_MAINTENANCE_THRESHOLD) is True

    def test_above_threshold_blocks(self) -> None:
        assert ei.should_block_writes_for_reindex(ei.REINDEX_MAINTENANCE_THRESHOLD + 10000) is True


class TestRequireModelDiskSpace:
    def test_enough_space_passes(self, tmp_path) -> None:
        # tmp_path 所在卷正常有几 GB 空闲，bge-m3 ×1.5 ≈ 3.5GB 一般够。
        # 若 CI 卷过小会抛 InsufficientDiskSpaceError，属真实环境约束非逻辑错。
        try:
            ei.require_model_disk_space(ei.DEFAULT_MODEL_KEY, str(tmp_path))
        except InsufficientDiskSpaceError:
            pytest.skip("测试卷剩余空间不足，跳过（非逻辑错误）")

    def test_unknown_model_raises(self, tmp_path) -> None:
        with pytest.raises(ei.EmbeddingInstallError):
            ei.require_model_disk_space("nope", str(tmp_path))


class TestBuildInstallPlan:
    def test_plan_paths_and_device(self, tmp_path) -> None:
        plan = ei.build_install_plan("bge-m3", str(tmp_path), device="cpu")
        assert plan.model_spec.model_id == "BAAI/bge-m3"
        assert plan.device == "cpu"
        # Win backslash / Mac forward-slash 路径分隔符兼容，用 Path 比对
        assert Path(plan.venv_dir).parts[-2:] == ("embedding-service", "venv")
        assert Path(plan.model_dir).parts[-2:] == ("models", "bge-m3")
        assert plan.download_args["repo_id"] == "BAAI/bge-m3"
        assert plan.download_args["local_dir"] == plan.model_dir

    def test_start_cmd_pins_localhost_and_device(self, tmp_path) -> None:
        plan = ei.build_install_plan("bge-m3", str(tmp_path), device="cpu")
        assert "127.0.0.1" in plan.start_cmd       # AC15 不暴露 0.0.0.0
        assert "--device" in plan.start_cmd
        assert "cpu" in plan.start_cmd

    def test_plan_has_no_download_execution(self, tmp_path) -> None:
        # 计划只给参数，不含实际执行入口（AC27 安装归属在壳层）。
        plan = ei.build_install_plan("bge-m3", str(tmp_path))
        assert isinstance(plan.download_args, dict)  # 仅参数
        assert plan.device in ei.VALID_DEVICES

    def test_default_device_cpu_when_unspecified(self, tmp_path) -> None:
        plan = ei.build_install_plan("bge-m3", str(tmp_path))
        assert plan.device == "cpu"

    def test_mirror_propagated(self, tmp_path) -> None:
        plan = ei.build_install_plan("bge-m3", str(tmp_path), mirror="https://hf-mirror.com")
        assert plan.download_args["endpoint"] == "https://hf-mirror.com"

    def test_pip_install_extras_and_hf_hub_pin(self, tmp_path) -> None:
        """锁住 pip 装 [server,torch] + huggingface_hub<1.0 + numpy/click 兼容 pin。

        踩坑全记录（按时间顺序）：
        - [all]：拉 vision/ct2/audio/tensorrt/onnxruntime-gpu，pip resolver
          backtrack 几十分钟卡死（1.3.5 实测）
        - 只 [server]：torch 不在主依赖（pip show 显示 Requires: numpy,
          huggingface_hub），起 infinity 时 ImportError: torch.nn not available
        - [server,torch] 起来后 BetterTransformerManager NameError：infinity
          acceleration.py:46 引用未定义符号（infinity 自己代码 bug）
        - 试装 [optimum]：pip 21 老 resolver backtrack 45 分钟没装出来
        - 改用 env INFINITY_BETTERTRANSFORMER=false 关掉 BetterTransformer 探测，
          acceleration.py:36 第一行直接 return False 不走 optimum 代码
        - huggingface_hub<1.0：infinity 代码 `from huggingface_hub import
          HfFolder`，hf_hub 1.0 移除该 API
        - numpy>=2.1 + click<8.2（1.3.12 实装期补）：Python 3.13 + Win 上 numpy
          1.x longdouble 初始化 OverflowError；click 8.2+ 跟 typer 0.12 撞
          Secondary flag 校验，infinity CLI 启动崩。force-upgrade 绕开
          infinity-emb 0.0.77 老 metadata 锁死的 numpy<2 约束（runtime 跟
          numpy 2.5 实测兼容）。
        """
        plan = ei.build_install_plan("bge-m3", str(tmp_path))
        # 新 plan 形态：[venv_python, "-c", "<内联 Python script>"]，断言 script 文本内容
        assert len(plan.pip_install_cmd) >= 3 and plan.pip_install_cmd[1] == "-c", (
            f"pip_install_cmd 应该是 [venv_python, '-c', script] 形态；当前={plan.pip_install_cmd}"
        )
        script = plan.pip_install_cmd[2]
        assert "infinity-emb[server,torch]" in script, (
            f"必须含 [server,torch] 双 extras（漏了 infinity 起不来）；script={script}"
        )
        assert "[server,torch,optimum]" not in script and "[optimum]" not in script, (
            f"不要装 optimum（pip 21 backtrack 45min + 版本地狱）；script={script}"
        )
        assert "huggingface_hub<1.0" in script, (
            f"必须 pin huggingface_hub<1.0 避开 HfFolder ImportError；script={script}"
        )
        # 2026-07-02 P2-2 补:Step 2 numpy pin 按 py 版本分叉,断言两条路径都在
        assert "sys.version_info >= (3, 10)" in script, (
            f"Step 2 必须按 python 版本分叉(py<3.10 不能强升 numpy);script={script}"
        )
        assert "numpy>=2.1" in script, (
            f"py>=3.10 分支必须 force-upgrade numpy>=2.1 兼容 Py 3.13 longdouble;script={script}"
        )
        # py<3.10 分支:**不动 numpy**(Step 1 拉的 1.26.x 天然符合 infinity-emb 0.0.77
        # metadata `numpy<2`;强升会破坏 metadata + py3.9 天花板 numpy 2.0.2 也可能撞
        # No matching distribution found)
        assert "numpy<2" not in script, (
            f"py<3.10 分支不能 pin numpy(会破坏 infinity-emb metadata);script={script}"
        )
        assert "click<8.2" in script, (
            f"必须 pin click<8.2 兼容 typer 0.12(典 Secondary flag 校验);script={script}"
        )
        # 镜像源（清华主 + PyPI 兜底）
        assert "pypi.tuna.tsinghua.edu.cn" in script, (
            f"必须配清华镜像主源（国内 torch 直连 PyPI 易断流）；script={script}"
        )

    def test_env_disables_bettertransformer(self, tmp_path) -> None:
        """plan.env 必须含 INFINITY_BETTERTRANSFORMER=false。

        否则 infinity acceleration.py 模块顶层 from optimum.bettertransformer
        import 或者 check_if_bettertransformer_possible() 内部引用未定义的
        BetterTransformerManager → 进程崩。Swift StartHandler 启动时把 plan.env
        merge 进 Process.environment。
        """
        plan = ei.build_install_plan("bge-m3", str(tmp_path))
        assert plan.env.get("INFINITY_BETTERTRANSFORMER") == "false", (
            f"plan.env 必须有 INFINITY_BETTERTRANSFORMER=false；当前={plan.env}"
        )

    def test_cpu_plan_has_no_cuda_step(self, tmp_path) -> None:
        """device=cpu 时 pip script 不应包含任何 cuda wheel 替换步骤。

        回归保护：v1.3.12 引入 cuda 分支后，要防 cpu 用户被多塞 force-reinstall
        torch 步骤（白白走一遍 PyTorch 索引拉 CPU wheel 再覆盖一遍）。
        """
        plan = ei.build_install_plan("bge-m3", str(tmp_path), device="cpu")
        script = plan.pip_install_cmd[2]
        assert "--force-reinstall" not in script, (
            f"cpu plan 不应含 --force-reinstall（cuda 分支专属）；script={script}"
        )
        assert "pytorch-wheels" not in script and "download.pytorch.org" not in script, (
            f"cpu plan 不应含 PyTorch CUDA 索引；script={script}"
        )

    def test_mps_plan_has_no_cuda_step(self, tmp_path) -> None:
        """device=mps 同 cpu，mac 走 torch 自带 mps backend，不需要单独拉 cuda wheel。"""
        plan = ei.build_install_plan("bge-m3", str(tmp_path), device="mps")
        script = plan.pip_install_cmd[2]
        assert "--force-reinstall" not in script
        assert "pytorch-wheels" not in script

    def test_cuda_plan_includes_force_reinstall_torch(self, tmp_path) -> None:
        """device=cuda 必须在 pip script 里插 force-reinstall torch 走 cuda 索引。

        根因：infinity-emb[server,torch] 默认拉 CPU torch wheel，cuda 用户启动
        infinity 时 torch.cuda.is_available()=False → autodevice_string=[] →
        IndexError。1.5 步用 --index-url 替换默认 PyPI 走 {mirror}{cuda_version}
        拉 CUDA wheel；--index-url 替换默认 PyPI 索引，pip 不会再选回 PyPI 上的
        高版本 CPU wheel 而 mask 掉 cuda 修复。
        """
        plan = ei.build_install_plan("bge-m3", str(tmp_path), device="cuda")
        script = plan.pip_install_cmd[2]
        assert "--force-reinstall" in script, (
            f"cuda plan 必须 --force-reinstall 替换 Step 1 的 CPU wheel；script={script}"
        )
        assert "'--index-url'" in script, (
            f"cuda plan 必须用 --index-url 替换默认 PyPI 索引；script={script}"
        )
        assert "torch" in script and "torchvision" in script
        # 默认走 PyTorch 官方 + cu124（v1.3.12 第二轮：阿里 HTML 门户页 pip 不兼容；
        # cu121 没 cp313 + win_amd64 wheel）
        assert "download.pytorch.org/whl/cu124" in script, (
            f"默认应是 PyTorch 官方 cu124；script={script}"
        )

    def test_cuda_plan_custom_mirror_and_version(self, tmp_path) -> None:
        """pytorch_mirror + cuda_version 必须如实透传到 pip script，覆盖默认。"""
        plan = ei.build_install_plan(
            "bge-m3", str(tmp_path), device="cuda",
            pytorch_mirror="https://mirror.example.com/pypi/",
            cuda_version="cu118",
        )
        script = plan.pip_install_cmd[2]
        assert "mirror.example.com/pypi/cu118" in script, (
            f"自定义 mirror+version 必须如实出现在 script；script={script}"
        )
        # 默认值不应偷溜
        assert "download.pytorch.org" not in script
        assert "cu124" not in script

    def test_cuda_plan_falls_back_on_invalid_input(self, tmp_path) -> None:
        """非法 mirror / cuda_version 回落默认（不流到 pip script，避免脏值）。"""
        plan = ei.build_install_plan(
            "bge-m3", str(tmp_path), device="cuda",
            pytorch_mirror="not-a-url",
            cuda_version="cuFOO",
        )
        script = plan.pip_install_cmd[2]
        assert "download.pytorch.org/whl/cu124" in script
        assert "not-a-url" not in script
        assert "cuFOO" not in script

    def test_start_cmd_includes_explicit_port(self, tmp_path) -> None:
        """start_cmd 必须显式传 --port，不然 infinity v2 用自己默认 7997。

        Swift StartHandler 按 plan.port 探活 /health，端口对不上 warmup
        必 timeout。Phase 2 设计漏了 --port，1.3.0~1.3.6 dmg 装机后 install
        阶段过了，start 阶段直接卡 warmup 不报错（Phase 3b checkpoint 标的
        follow-up）。
        """
        plan = ei.build_install_plan("bge-m3", str(tmp_path))
        assert "--port" in plan.start_cmd, f"start_cmd 缺 --port；当前={plan.start_cmd}"
        port_idx = plan.start_cmd.index("--port")
        port_val = int(plan.start_cmd[port_idx + 1])
        assert port_val == plan.port, (
            f"--port 参数 ({port_val}) 必须等于 plan.port ({plan.port})"
        )
