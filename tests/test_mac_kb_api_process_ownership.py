"""kb-api shell 生命周期脚本不得误杀其他安装或外部 Python。"""
from __future__ import annotations

import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pid_command_contains(pid: int, unique_path: Path) -> bool:
    inspected = subprocess.run(
        ["/bin/ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return inspected.returncode == 0 and str(unique_path) in inspected.stdout


def _safe_cleanup(process: subprocess.Popen[bytes], unique_path: Path) -> None:
    if process.poll() is not None:
        process.wait(timeout=3)
        return
    if not _pid_command_contains(process.pid, unique_path):
        raise AssertionError(
            f"refusing to signal reused/unexpected pid {process.pid}: {unique_path}"
        )
    process.terminate()
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    if not _pid_command_contains(process.pid, unique_path):
        raise AssertionError(
            f"refusing to SIGKILL reused/unexpected pid {process.pid}: {unique_path}"
        )
    process.kill()
    process.wait(timeout=3)


def _copy_scripts(install_root: Path, *names: str) -> None:
    scripts_dir = install_root / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in names:
        shutil.copy2(ROOT / "scripts" / name, scripts_dir / name)


def _make_command_stubs(tmp_path: Path, *names: str) -> Path:
    stub_dir = tmp_path / "command-stubs"
    stub_dir.mkdir()
    for name in names:
        target = stub_dir / name
        target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        target.chmod(0o755)
    return stub_dir


def _spawn_external_sleeper(tmp_path: Path) -> tuple[subprocess.Popen[bytes], Path]:
    script = tmp_path / "external-owner-sleeper.py"
    script.write_text(
        "import time\nwhile True:\n    time.sleep(1)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("external sleeper exited unexpectedly")
        if _pid_command_contains(process.pid, script):
            return process, script
        time.sleep(0.02)
    raise AssertionError("external sleeper command line did not become visible")


def _base_env(port: int, path_prefix: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["KB_PORT_API"] = str(port)
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return env


@pytest.mark.skipif(sys.platform != "darwin", reason="kb-stop.sh 是 mac 直装脚本，Windows 用 local-stop.ps1 走对应 N11 sweep 测试")
def test_kb_stop_does_not_signal_external_pid_from_stale_pid_file(tmp_path):
    install_root = tmp_path / "KnowledgeBase"
    _copy_scripts(install_root, "kb-stop.sh", "kb-ports.sh")
    (install_root / "data").mkdir()
    external, unique_script = _spawn_external_sleeper(tmp_path)
    (install_root / "data" / ".local_api.pid").write_text(
        str(external.pid), encoding="utf-8"
    )
    stubs = _make_command_stubs(tmp_path, "lsof", "pgrep")

    try:
        completed = subprocess.run(
            ["/bin/bash", str(install_root / "scripts" / "kb-stop.sh")],
            env=_base_env(_unused_loopback_port(), stubs),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert external.poll() is None, "kb-stop killed an external PID-file process"
        assert not (install_root / "data" / ".local_api.pid").exists()
    finally:
        _safe_cleanup(external, unique_script)


@pytest.mark.skipif(sys.platform != "darwin", reason="primary script lock uses macOS lockf")
def test_kb_start_does_not_signal_external_pid_from_stale_pid_file(tmp_path):
    install_root = tmp_path / "KnowledgeBase"
    _copy_scripts(install_root, "kb-start.sh", "kb-ports.sh")
    (install_root / "data").mkdir()
    external, unique_script = _spawn_external_sleeper(tmp_path)
    (install_root / "data" / ".local_api.pid").write_text(
        str(external.pid), encoding="utf-8"
    )
    stubs = _make_command_stubs(tmp_path, "lsof")

    try:
        completed = subprocess.run(
            ["/bin/bash", str(install_root / "scripts" / "kb-start.sh")],
            env=_base_env(_unused_loopback_port(), stubs),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 1, (completed.stdout, completed.stderr)
        assert external.poll() is None, "kb-start killed an external PID-file process"
        assert not (install_root / "data" / ".local_api.pid").exists()
    finally:
        _safe_cleanup(external, unique_script)


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("lsof") is None,
    reason="需要 macOS lockf 与真实 lsof 端口归属探测",
)
def test_kb_start_does_not_kill_external_python_http_listener(tmp_path):
    install_root = tmp_path / "KnowledgeBase"
    _copy_scripts(install_root, "kb-start.sh", "kb-ports.sh")
    port = _unused_loopback_port()
    script = tmp_path / "external-python-http-server.py"
    script.write_text(
        """import http.server
import sys

server = http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])),
                                http.server.SimpleHTTPRequestHandler)
server.serve_forever()
""",
        encoding="utf-8",
    )
    server = subprocess.Popen(
        [sys.executable, str(script), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError("external HTTP server exited before test")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("external HTTP server did not listen")

        completed = subprocess.run(
            ["/bin/bash", str(install_root / "scripts" / "kb-start.sh")],
            env=_base_env(port),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 1, (completed.stdout, completed.stderr)
        assert server.poll() is None, "kb-start killed an unrelated Python listener"
    finally:
        _safe_cleanup(server, script)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 直装脚本正向验证")
def test_kb_stop_terminates_owned_onedir_binary_with_spaced_root(tmp_path):
    install_root = tmp_path / "Knowledge Base Owned Onedir"
    _copy_scripts(install_root, "kb-stop.sh", "kb-ports.sh")
    (install_root / "data").mkdir()
    owned_binary = install_root / "bin" / "kb-api" / "kb-api"
    owned_binary.parent.mkdir(parents=True)
    # macOS 26 会把复制到临时路径的 Apple 平台 /bin/sleep 直接 SIGKILL
    #（rc=137），无法用于 TERM 语义验证。复制当前 Python Mach-O 是等价的
    # 真实子进程 harness，argv[0] 仍是目标 frozen onedir 绝对路径。
    shutil.copyfile(sys.executable, owned_binary)
    owned_binary.chmod(0o755)

    owned = subprocess.Popen(
        [str(owned_binary), "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # pytest 是该进程的 parent；后台 wait 模拟 launchd/真实父进程及时 reap，
    # 避免已因 TERM 退出的 zombie 被 kill -0 误判成仍运行而进入 KILL 分支。
    reaper = threading.Thread(target=owned.wait, daemon=True)
    reaper.start()
    (install_root / "data" / ".local_api.pid").write_text(
        str(owned.pid), encoding="utf-8"
    )
    stubs = _make_command_stubs(tmp_path, "lsof", "pgrep")

    try:
        assert _pid_command_contains(owned.pid, owned_binary)
        completed = subprocess.run(
            ["/bin/bash", str(install_root / "scripts" / "kb-stop.sh")],
            env=_base_env(_unused_loopback_port(), stubs),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        reaper.join(timeout=3)
        assert not reaper.is_alive(), "owned onedir process was not reaped"
        assert owned.returncode == -signal.SIGTERM
        assert "Knowledge base stopped" in completed.stdout
    finally:
        _safe_cleanup(owned, owned_binary)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 直装脚本正向验证")
def test_kb_stop_sigkills_owned_venv_uvicorn_after_ignored_term(tmp_path):
    install_root = tmp_path / "Knowledge Base Owned Venv"
    _copy_scripts(install_root, "kb-stop.sh", "kb-ports.sh")
    (install_root / "data").mkdir()
    owned_python = install_root / ".venv" / "bin" / "python"
    owned_python.parent.mkdir(parents=True)
    owned_python.symlink_to(sys.executable)

    module_dir = tmp_path / "fake-uvicorn-module"
    module_dir.mkdir()
    ready = tmp_path / "owned-uvicorn.ready"
    (module_dir / "uvicorn.py").write_text(
        """import os
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["OWNED_UVICORN_READY"], "w", encoding="utf-8") as fh:
    fh.write("ready")
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    owned_env = os.environ.copy()
    owned_env["PYTHONPATH"] = str(module_dir)
    owned_env["OWNED_UVICORN_READY"] = str(ready)
    owned = subprocess.Popen(
        [str(owned_python), "-m", "uvicorn", "app.main:app"],
        env=owned_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ready.exists() and _pid_command_contains(owned.pid, owned_python):
            break
        if owned.poll() is not None:
            raise AssertionError("owned fake uvicorn exited before stop test")
        time.sleep(0.02)
    else:
        raise AssertionError("owned fake uvicorn did not become ready")

    (install_root / "data" / ".local_api.pid").write_text(
        str(owned.pid), encoding="utf-8"
    )
    stubs = _make_command_stubs(tmp_path, "lsof", "pgrep")

    try:
        completed = subprocess.run(
            ["/bin/bash", str(install_root / "scripts" / "kb-stop.sh")],
            env=_base_env(_unused_loopback_port(), stubs),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        owned.wait(timeout=3)
        assert owned.returncode == -signal.SIGKILL
        assert "Knowledge base stopped" in completed.stdout
    finally:
        _safe_cleanup(owned, owned_python)
