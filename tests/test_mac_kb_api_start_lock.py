"""macOS/Linux kb-api 生命周期锁回归测试。"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _lifecycle_lock_supported() -> bool:
    if sys.platform == "darwin":
        return os.access("/usr/bin/lockf", os.X_OK)
    if sys.platform.startswith("linux"):
        return shutil.which("flock") is not None
    return False


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pid_runs_executable(pid: int, executable: Path) -> bool:
    inspected = subprocess.run(
        ["/bin/ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return inspected.returncode == 0 and str(executable) in inspected.stdout


def _terminate_recorded_children(spawn_log: Path, fake_executable: Path) -> None:
    if not spawn_log.exists():
        return
    pids = {
        int(line.strip())
        for line in spawn_log.read_text(encoding="utf-8").splitlines()
        if line.strip().isdigit()
    }
    for pid in pids:
        # PID 可能在测试退出路径被系统复用；只允许终止命令行仍包含本用例唯一
        # fake executable 绝对路径的进程，绝不凭旧 PID 盲杀。
        if not _pid_runs_executable(pid, fake_executable):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        alive = []
        for pid in pids:
            if _pid_runs_executable(pid, fake_executable):
                alive.append(pid)
        if not alive:
            return
        time.sleep(0.05)
    for pid in pids:
        if not _pid_runs_executable(pid, fake_executable):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.skipif(
    not _lifecycle_lock_supported(),
    reason="需要 macOS /usr/bin/lockf 或 Linux flock",
)
def test_concurrent_kb_start_spawns_exactly_one_api(tmp_path):
    install_root = tmp_path / "KnowledgeBase"
    scripts_dir = install_root / "scripts"
    fake_bin = install_root / "bin" / "kb-api" / "kb-api"
    scripts_dir.mkdir(parents=True)
    fake_bin.parent.mkdir(parents=True)

    for name in ("kb-start.sh", "kb-ports.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts_dir / name)

    spawn_log = install_root / "fake-spawns.log"
    fake_bin.write_text(
        f"""#!{sys.executable}
import http.server
import os
import time

with open(os.environ["FAKE_SPAWN_LOG"], "a", encoding="utf-8") as fh:
    fh.write(f"{{os.getpid()}}\\n")
    fh.flush()

# 扩大两个 kb-start 都通过初始 health check 的竞态窗口。
time.sleep(0.8)

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        pass

server = http.server.HTTPServer(
    ("127.0.0.1", int(os.environ["KB_PORT_API"])), Handler
)
server.serve_forever()
""",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "KB_PORT_API": str(_unused_loopback_port()),
            "FAKE_SPAWN_LOG": str(spawn_log),
        }
    )
    start_script = scripts_dir / "kb-start.sh"
    processes: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            processes.append(
                subprocess.Popen(
                    ["/bin/bash", str(start_script)],
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            results.append((process.returncode, stdout, stderr))

        assert all(code == 0 for code, _, _ in results), results
        spawned = spawn_log.read_text(encoding="utf-8").splitlines()
        assert len(spawned) == 1, (
            f"expected one fake API spawn, got {spawned}; shell results={results}"
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
        _terminate_recorded_children(spawn_log, fake_bin)


def test_restart_delegates_to_locked_start_and_keeps_frozen_logs():
    restart = (ROOT / "mac-app" / "restart.sh").read_text(encoding="utf-8")
    start = (ROOT / "scripts" / "kb-start.sh").read_text(encoding="utf-8")

    assert 'exec "$ROOT_DIR/scripts/kb-start.sh"' in restart
    assert "KB_API_LIFECYCLE_LOCK_HELD" in restart
    assert "runtime/kb-api-lifecycle.lock" in restart
    assert "runtime/kb-api-lifecycle.lock" in start
    assert "/usr/bin/lockf -s -t 120 9" in restart
    assert restart.index("/usr/bin/lockf") < restart.index("kb-stop.sh")
    assert restart.index("kb-stop.sh") < restart.index(
        'exec "$ROOT_DIR/scripts/kb-start.sh"'
    )
    assert 'nohup "$KB_API_BIN"' not in restart
    assert (
        'nohup "$KB_API_BIN" >"$LOG_OUT" 2>"$LOG_ERR" 9>&- &' in start
    )


def test_kb_start_has_cross_platform_lock_and_validates_held_fd():
    start = (ROOT / "scripts" / "kb-start.sh").read_text(encoding="utf-8")

    assert "/usr/bin/lockf -s -t 120 9" in start
    assert 'elif command -v flock >/dev/null 2>&1; then' in start
    assert "flock -w 120 9" in start
    assert "no supported file-lock utility" in start
    assert "fd_is_open 9" in start
    assert "unset KB_API_LIFECYCLE_LOCK_HELD" in start
