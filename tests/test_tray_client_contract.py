"""跨端契约测试 (v2 P0-3 P0-4 P0-5 + P1-5): tray 客户端与 embedding manager 契约。

这些 pin 覆盖 Mac / Win 双端应共享的核心行为契约。改任一契约都可能同时影响
两端 tray, 因此在这里落 test 挡回归。

Pin 覆盖:
1. install→start chaining flag pattern (Mac Swift startAfterInstallRequested 对齐)
2. adopt resolved 绝对路径 + quoted / equals / space 三态支持
3. adopt uses cleaner 真实 port, 不用 start_spec.port
4. _verify_kb_api_exe wmic → Get-CimInstance fallback (Win 11 22H2+ wmic deprecated)
5. _PsCmdlineProbe wmic → Get-CimInstance fallback
6. local-restart-direct.ps1 PowerShell 5.1 ParseFile 通过

并发 restart Named Mutex 与安装副本 SHA 属于真机安装验收项，不在普通单测里
伪装成已覆盖。

以下 pin 已在 test_embedding_process_manager.py 覆盖, 不重复:
- completed 反映业务 bool (test_install_failure_propagates + test_install_success_marks_installed
  + test_switch_aborts_when_install_fails)
- warmup timeout 保留活 handle (test_warmup_timeout_preserves_live_handle_for_self_heal)
- warmup spawn 失败重试 (test_spawn_failure_sets_error)
- stop failure 保 pid/port (test_stop_hard_failure_retains_handle_for_retry)
- stop 连续失败不假 ack 活进程 (test_stop_hard_failure_never_acks_live_process_as_stopped)

restart 202 accepted + operation-id + client polling 属于 2.0 架构工作，不是 1.3.13
当前契约；本轮自动测试只 pin PS 5.1 可解析，Named Mutex 并发走真机验收。
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# 加 windows-app/ 到 sys.path (跟 test_embedding_process_manager.py 同款)
# ---------------------------------------------------------------------------
_WINDOWS_APP = Path(__file__).parent.parent / "windows-app"
if str(_WINDOWS_APP) not in sys.path:
    sys.path.insert(0, str(_WINDOWS_APP))


# ---------------------------------------------------------------------------
# Pin 1: install→start chaining flag pattern (对齐 Mac Swift startAfterInstallRequested)
# ---------------------------------------------------------------------------


class TestInstallStartChainingFlagPattern:
    """v2 P0-3: tray SHALL 设 flag → POST /install → 内部 tick dispatch install
    成功后 consume flag → CAS POST /start (对齐 Mac Swift EmbeddingProcessManager.swift:1126,
    1222-1230)。"""

    def test_manager_provides_set_and_consume_flag_methods(self):
        """manager 提供 set_start_after_install_requested + consume_start_after_install_request."""
        from embedding_process_manager import EmbeddingProcessManager

        # 确认方法存在 (不需要真实调, 只 check API 面)
        assert hasattr(EmbeddingProcessManager, "set_start_after_install_requested"), \
            "manager SHALL 提供 set_start_after_install_requested 方法 (对齐 Mac swift)"
        assert hasattr(EmbeddingProcessManager, "consume_start_after_install_request"), \
            "manager SHALL 提供 consume_start_after_install_request 方法 (对齐 Mac swift)"

    def test_consume_reads_and_clears_flag(self):
        """consume 读并清 flag: 第一次 True, 之后 False (对齐 Mac swift 单次消费语义)."""
        from embedding_process_manager import EmbeddingProcessManager

        mgr = EmbeddingProcessManager.__new__(EmbeddingProcessManager)  # 只测 flag 方法, 不完整构造
        import threading
        mgr._start_after_install_lock = threading.Lock()
        mgr._start_after_install_requested = False

        # 初始 False
        assert mgr.consume_start_after_install_request() is False

        # set True 后 consume 一次返 True
        mgr.set_start_after_install_requested(True)
        assert mgr.consume_start_after_install_request() is True

        # consume 之后自动清成 False (下次 consume 返 False)
        assert mgr.consume_start_after_install_request() is False

    def test_kb_api_client_provides_post_start_cas_method(self):
        """v2 P0-3: KbApiClient SHALL 提供 post_start_cas 方法 (携带 expected_generation)."""
        from embedding_process_manager import KbApiClient
        assert hasattr(KbApiClient, "post_start_cas"), \
            "KbApiClient SHALL 提供 post_start_cas 让 tick 层 chaining POST /start"

    @staticmethod
    def _controller_for_start(tmp_path, *, set_flag):
        from tray_app_local import LocalTrayController

        manager = MagicMock()
        manager.set_start_after_install_requested.side_effect = set_flag
        bundle = MagicMock()
        bundle.manager = manager
        bundle.snapshot.return_value = type(
            "Snapshot", (), {"running": True, "device": "cpu", "last_error": ""},
        )()
        controller = LocalTrayController.__new__(LocalTrayController)
        controller.root = tmp_path
        controller.port = 18000
        controller._embedding_bundle = bundle
        controller._embedding_starting = False
        controller._auto_bootstrap_attempted = True
        controller._alert = MagicMock()
        controller._notify = MagicMock()
        controller._rebuild_menu = MagicMock()
        runtime = tmp_path / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "owner_token").write_text("token", encoding="utf-8")
        return controller, manager

    @staticmethod
    def _urlopen_with_order(events):
        class Response:
            def __init__(self, payload=b"") -> None:
                self._payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self._payload

        def fake_urlopen(request, **_kwargs):
            if isinstance(request, str):
                events.append("config")
                return Response(
                    b'{"embedding_service_model_id":"bge-m3",'
                    b'"embedding_service_device":"cpu",'
                    b'"embedding_service_mode":"local"}'
                )
            events.append(request.full_url)
            return Response()

        return fake_urlopen

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Win-only: import tray_app_local 依赖 pystray, 非 Win 平台不装",
    )
    @pytest.mark.parametrize(
        "method_name", ["_do_start_embedding", "_do_auto_bootstrap_embedding"],
    )
    def test_tray_sets_chain_flag_before_install_post(
        self, monkeypatch, tmp_path, method_name,
    ):
        events: list[str] = []

        def set_flag(value):
            events.append(f"flag:{value}")

        controller, _manager = self._controller_for_start(
            tmp_path, set_flag=set_flag,
        )
        monkeypatch.setattr(
            "tray_app_local.urllib.request.urlopen", self._urlopen_with_order(events),
        )
        monkeypatch.setattr("tray_app_local.time.sleep", lambda _seconds: None)

        getattr(controller, method_name)()

        install_events = [
            event for event in events
            if event.endswith("/v1/system/embedding-service/install")
        ]
        assert install_events == [
            "http://127.0.0.1:18000/v1/system/embedding-service/install"
        ]
        assert events.index("flag:True") < events.index(install_events[0])
        assert not any(event.endswith("/start") for event in events)

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Win-only: import tray_app_local 依赖 pystray, 非 Win 平台不装",
    )
    @pytest.mark.parametrize(
        "method_name", ["_do_start_embedding", "_do_auto_bootstrap_embedding"],
    )
    def test_tray_does_not_install_when_chain_flag_cannot_be_armed(
        self, monkeypatch, tmp_path, method_name,
    ):
        events: list[str] = []
        controller, _manager = self._controller_for_start(
            tmp_path, set_flag=RuntimeError("manager stopping"),
        )
        monkeypatch.setattr(
            "tray_app_local.urllib.request.urlopen", self._urlopen_with_order(events),
        )

        getattr(controller, method_name)()

        assert not any(
            event.endswith("/v1/system/embedding-service/install")
            for event in events
        )
        if method_name == "_do_auto_bootstrap_embedding":
            assert controller._auto_bootstrap_attempted is False


# ---------------------------------------------------------------------------
# Pin 2: adopt resolved 绝对路径 + quoted / equals / space 三态支持
# ---------------------------------------------------------------------------


class TestAdoptResolvedAbsolutePathMatching:
    """v2 P0-4: adopt cmdline matcher SHALL 支持 resolved 绝对路径 +
    quoted / --key=value / --key value 三种形式。"""

    @staticmethod
    def _match(cmdline: str, name: str, value: str) -> bool:
        from embedding_process_manager import _cmdline_option_matches
        return _cmdline_option_matches(cmdline, name, value)

    def test_absolute_path_unquoted_with_space(self):
        """--model-id C:\\KB\\models\\bge-m3 (空格分隔, unquoted)."""
        cmdline = r"infinity --port 7687 --model-id C:\KB\models\bge-m3"
        assert self._match(cmdline, "--model-id", r"C:\KB\models\bge-m3")

    def test_absolute_path_unquoted_with_equals(self):
        """--model-id=D:\\KnowledgeBase\\models\\bge-m3 (等号分隔, unquoted)."""
        cmdline = r"infinity --port 7687 --model-id=D:\KnowledgeBase\models\bge-m3"
        assert self._match(cmdline, "--model-id", r"D:\KnowledgeBase\models\bge-m3")

    def test_quoted_absolute_path_with_spaces_and_space_separator(self):
        """--model-id "C:\\Program Files\\KB\\models\\bge-m3" (quoted, 含空格)."""
        cmdline = r'infinity --port 7687 --model-id "C:\Program Files\KB\models\bge-m3"'
        assert self._match(cmdline, "--model-id", r"C:\Program Files\KB\models\bge-m3")

    def test_quoted_absolute_path_with_equals_separator(self):
        """--model-id="C:\\Program Files\\KB\\models\\bge-m3" (quoted, 等号分隔)."""
        cmdline = r'infinity --port 7687 --model-id="C:\Program Files\KB\models\bge-m3"'
        assert self._match(cmdline, "--model-id", r"C:\Program Files\KB\models\bge-m3")

    def test_logical_key_still_matches_short_form(self):
        """--model-id bge-m3 (逻辑 key 短形, 向后兼容)."""
        cmdline = "infinity --port 7687 --model-id bge-m3"
        assert self._match(cmdline, "--model-id", "bge-m3")

    def test_port_edge_boundary_still_holds(self):
        """v2 P0-4 边界: --port 7687 不 match --port 76870 (regression pin)."""
        cmdline = "infinity --port 76870 --model-id bge-m3"
        assert not self._match(cmdline, "--port", "7687"), \
            "边界锚点 (?=\\s|$) SHALL 防 substring 匹配"


# ---------------------------------------------------------------------------
# Pin 3: StartSpec 携带 cmdline_model_value 让 adopt 用 resolved 路径
# ---------------------------------------------------------------------------


class TestStartSpecCarriesCmdlineModelValue:
    """v2 P0-4: StartSpec SHALL 有 cmdline_model_value 字段, spec_factory 填 resolved
    绝对路径, EmbeddingActionHandler.start 传给 adopt_or_clean 匹配 cmdline."""

    def test_start_spec_has_cmdline_model_value_field(self):
        from dataclasses import fields
        from embedding_process_manager import StartSpec

        field_names = {f.name for f in fields(StartSpec)}
        assert "cmdline_model_value" in field_names, \
            "StartSpec SHALL 有 cmdline_model_value 字段 (v2 P0-4)"

    def test_default_falls_back_to_none(self):
        """cmdline_model_value 默认 None, caller 未填时 handler fallback 到 desired.model_id (向后兼容)."""
        from embedding_process_manager import StartSpec
        # 不能真构造 StartSpec (需要 spawner 等复杂依赖); 只 check dataclass 默认
        from dataclasses import fields
        fs = {f.name: f.default for f in fields(StartSpec)}
        assert fs["cmdline_model_value"] is None


# ---------------------------------------------------------------------------
# Pin 4: _verify_kb_api_exe wmic → Get-CimInstance fallback (Win 11 22H2+ 兼容)
# ---------------------------------------------------------------------------


class TestVerifyKbApiExeWmicToCimFallback:
    """v2 P1-5: _verify_kb_api_exe SHALL wmic 失败时 fallback Get-CimInstance,
    Win 11 22H2+ wmic 已 deprecated 默认关闭 (
    https://learn.microsoft.com/windows/deployment/planning/windows-10-deprecated-features)"""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Win-only: import tray_app_local 依赖 pystray, 非 Win 平台不装",
    )
    def test_query_exe_path_via_wmic_helper_exists(self):
        from tray_app_local import _query_exe_path_via_wmic
        assert callable(_query_exe_path_via_wmic)

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Win-only: import tray_app_local 依赖 pystray, 非 Win 平台不装",
    )
    def test_query_exe_path_via_cim_helper_exists(self):
        """v2 P1-5: PowerShell Get-CimInstance fallback helper 存在."""
        from tray_app_local import _query_exe_path_via_cim
        assert callable(_query_exe_path_via_cim)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_verify_falls_back_to_cim_when_wmic_returns_empty(self, tmp_path):
        """wmic 返空 → 走 CIM fallback."""
        from tray_app_local import _verify_kb_api_exe
        api_exe = tmp_path / "kb-api.exe"
        api_exe.touch()

        with patch("tray_app_local._query_exe_path_via_wmic", return_value=""), \
             patch("tray_app_local._query_exe_path_via_cim", return_value=str(api_exe)):
            # wmic 返空, CIM 返正确路径 → 应 match
            assert _verify_kb_api_exe(pid=12345, api_exe=api_exe) is True


# ---------------------------------------------------------------------------
# Pin 5: _PsCmdlineProbe wmic → Get-CimInstance fallback
# ---------------------------------------------------------------------------


class TestPsCmdlineProbeWmicToCimFallback:
    """v2 P1-5: 双点 fallback, tray _verify + manager _PsCmdlineProbe."""

    def test_probe_has_wmic_and_cim_helper_methods(self):
        from embedding_process_manager import _PsCmdlineProbe
        assert hasattr(_PsCmdlineProbe, "_try_wmic_cmdline"), \
            "_PsCmdlineProbe SHALL 有 _try_wmic_cmdline 方法 (v2 P1-5)"
        assert hasattr(_PsCmdlineProbe, "_try_cim_cmdline"), \
            "_PsCmdlineProbe SHALL 有 _try_cim_cmdline fallback 方法 (v2 P1-5)"

    def test_wmic_empty_commandline_value_is_unknown_not_nonempty_marker(self):
        from embedding_process_manager import CommandResult, _PsCmdlineProbe

        class Runner:
            def run(self, _cmd, **_kwargs):
                return CommandResult(returncode=0, stdout_tail="CommandLine=\r\n")

        probe = _PsCmdlineProbe(runner=Runner())
        assert probe._try_wmic_cmdline(12345) == ""  # noqa: SLF001

    def test_process_ownership_probes_always_have_finite_timeouts(self, monkeypatch):
        import embedding_process_manager as manager_module
        from embedding_process_manager import CommandResult, _PsCmdlineProbe

        class Runner:
            def __init__(self) -> None:
                self.timeouts: list[float | None] = []

            def run(self, _cmd, **kwargs):
                self.timeouts.append(kwargs.get("timeout"))
                return CommandResult(returncode=0, stdout_tail="")

        runner = Runner()
        probe = _PsCmdlineProbe(runner=runner)
        probe._try_wmic_cmdline(12345)  # noqa: SLF001
        probe._try_cim_cmdline(12345)  # noqa: SLF001
        monkeypatch.setattr(manager_module.os, "name", "posix")
        probe.cmdline(12345)

        assert runner.timeouts == [3.0, 5.0, 5.0]


# ---------------------------------------------------------------------------
# Pin 6: local-restart-direct.ps1 PowerShell 5.1 ParseFile 通过
# ---------------------------------------------------------------------------


class TestLocalRestartDirectPs1PowerShell51Parseable:
    """v2 P0-5: PowerShell 5.1 SHALL 能 parse local-restart-direct.ps1
    以避免 fire-and-forget /v1/system/restart 假报成功。"""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only PowerShell 5.1 test")
    def test_powershell_5_1_can_parse_local_restart_direct_ps1(self):
        script = Path(__file__).parent.parent / "scripts" / "local-restart-direct.ps1"
        assert script.exists(), "local-restart-direct.ps1 SHALL 存在"

        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"$errs = $null; $tokens = $null; "
                f"$null = [System.Management.Automation.Language.Parser]::ParseFile("
                f"'{str(script)}', [ref]$tokens, [ref]$errs); "
                f"Write-Output $errs.Count",
            ],
            capture_output=True, text=True, timeout=15.0,
        )
        assert result.returncode == 0, f"PowerShell 调用失败: {result.stderr}"
        error_count = (result.stdout or "").strip().splitlines()[-1] if result.stdout else "?"
        assert error_count == "0", \
            f"PS 5.1 ParseFile SHALL 0 error, got {error_count}\nstderr: {result.stderr}"

    def test_local_restart_direct_ps1_starts_with_utf8_bom(self):
        """v2 P0-5: PS 5.1 硬要求 UTF-8 with BOM 才能正确解析中文注释附近的多行表达式."""
        script = Path(__file__).parent.parent / "scripts" / "local-restart-direct.ps1"
        assert script.exists()
        with open(script, "rb") as f:
            first_bytes = f.read(3)
        assert first_bytes == b"\xef\xbb\xbf", \
            f"local-restart-direct.ps1 SHALL 以 UTF-8 BOM (EF BB BF) 开头, 实际首 3 字节: {first_bytes.hex()}"

    def test_restart_script_has_no_standalone_carriage_return(self):
        script = Path(__file__).parent.parent / "scripts" / "local-restart-direct.ps1"
        data = script.read_bytes()
        assert b"\r" not in data.replace(b"\r\n", b""), \
            "restart 脚本路径不得含真实 CR 控制字符"

    def test_restart_script_status_path_and_errors_are_explicit(self):
        script = Path(__file__).parent.parent / "scripts" / "local-restart-direct.ps1"
        content = script.read_text(encoding="utf-8-sig")
        assert 'Join-Path $RootDir "runtime\\restart-status.json"' in content
        assert '$ErrorActionPreference = "Stop"' in content
        assert "Start-Process -FilePath $ApiExe" in content
        assert "-ErrorAction Stop" in content


# ---------------------------------------------------------------------------
# Pin 7: restart API 预检（1.3.13 当前契约）
# ---------------------------------------------------------------------------


class TestRestartApiParseFilePrecheck:
    """v2 P0-5: /v1/system/restart SHALL 在启动脚本前 ParseFile 预检, 失败返 500 不假绿.

    202 accepted + operation-id + client polling 若未来采用，须在 2.0 重新设计并单独
    落契约；1.3.13 仅 pin 预检行为消灭假绿。
    """

    def test_app_main_has_parse_precheck_before_popen(self):
        """静态检查: app/main.py 的 restart_local_service 有 ParseFile 预检代码路径."""
        main_py = Path(__file__).parent.parent / "app" / "main.py"
        content = main_py.read_text(encoding="utf-8")
        assert "ParseFile" in content, \
            "v2 P0-5 SHALL 在 restart API 里加 ParseFile 预检 (防假绿)"
        assert "$null = [System.Management.Automation.Language.Parser]::ParseFile(" in content, \
            "restart API SHALL 丢弃 ParseFile 返回的 AST，避免中文脚本输出触发本地编码解码失败"
        assert "restart script parse failed" in content, \
            "v2 P0-5 SHALL 在 ParseFile 失败时返 500 + 明确 err msg"


class TestLocalStopProcessOwnership:
    def test_local_stop_never_kills_global_python_or_unverified_pid(self):
        script = Path(__file__).parent.parent / "scripts" / "local-stop.ps1"
        content = script.read_text(encoding="utf-8-sig")

        assert "param([switch]$IncludeTray)" in content
        assert "function Test-OwnedKbApiProcess" in content
        assert "function Test-OwnedInfinityProcess" in content
        assert 'embedding-service\\venv\\Scripts\\infinity_emb.exe' in content
        assert "$IncludeTray -and (Test-OwnedInfinityProcess $_)" in content
        assert "Get-CimInstance Win32_Process -Filter" in content
        assert "Test-OwnedKbApiProcess $proc" in content
        assert "uvicorn\\s+app.main:app" not in content
        assert "-match '^python" not in content
        assert 'Write-Error "kb-api process is still running' in content

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Win-only: import tray_app_local 依赖 pystray, 非 Win 平台不装",
    )
    def test_force_stop_refuses_unowned_port_pid(self, tmp_path):
        from tray_app_local import LocalTrayController

        controller = LocalTrayController.__new__(LocalTrayController)
        controller._proc = None
        controller.port = 18000
        controller._api_exe = tmp_path / "kb-api.exe"
        controller._busy = True
        controller._alert = MagicMock()
        controller._refresh = MagicMock()

        with patch("tray_app_local._find_pid_by_port", return_value=4242), \
             patch("tray_app_local._verify_kb_api_exe", return_value=False), \
             patch("tray_app_local.subprocess.run") as run:
            controller._do_force_stop()

        run.assert_not_called()
        title, message = controller._alert.call_args.args
        assert "拦截" in title
        assert "4242" in message
