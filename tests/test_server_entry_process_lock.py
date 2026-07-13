"""直装 kb-api 进程级单实例锁回归测试。"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import app.server_entry as server_entry


ROOT = Path(__file__).resolve().parents[1]

WORKER = r"""
import sys
import time
from pathlib import Path

from app.server_entry import _acquire_process_lock, _release_process_lock

root = Path(sys.argv[1])
mode = sys.argv[2]
lock = _acquire_process_lock(root)
if lock is None:
    print("BUSY", flush=True)
    raise SystemExit(0)

try:
    if mode == "hold":
        (root / "holder.ready").write_text("ready", encoding="utf-8")
        while not (root / "holder.release").exists():
            time.sleep(0.02)
    else:
        print("ACQUIRED", flush=True)
finally:
    _release_process_lock(lock)
"""


def _wait_for(path: Path, process: subprocess.Popen[str], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"holder exited before ready: rc={process.returncode}, "
                f"stdout={stdout!r}, stderr={stderr!r}"
            )
        time.sleep(0.02)
    raise AssertionError("timed out waiting for holder.ready")


def _run_worker(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-c", WORKER, str(root), mode],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )


def test_process_lock_rejects_contender_then_allows_after_release(tmp_path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    holder = subprocess.Popen(
        [sys.executable, "-c", WORKER, str(tmp_path), "hold"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(tmp_path / "holder.ready", holder)

        contender = _run_worker(tmp_path, "once")
        assert contender.returncode == 0, contender.stderr
        assert contender.stdout.strip() == "BUSY"
        assert not (tmp_path / "runtime" / "owner_token").exists()

        (tmp_path / "holder.release").write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        assert holder.returncode == 0, (holder_stdout, holder_stderr)

        successor = _run_worker(tmp_path, "once")
        assert successor.returncode == 0, successor.stderr
        assert successor.stdout.strip() == "ACQUIRED"
        assert (tmp_path / "runtime" / "kb-api-process.lock").exists()
    finally:
        (tmp_path / "holder.release").touch()
        if holder.poll() is None:
            holder.terminate()
            try:
                holder.wait(timeout=3)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=3)


def test_main_exits_before_probe_when_process_lock_is_busy(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(server_entry, "_install_root", lambda: tmp_path)
    monkeypatch.setattr(server_entry, "_acquire_process_lock", lambda _root: None)

    def unexpected_probe(_path):
        raise AssertionError("busy contender must exit before dependency probe")

    monkeypatch.setattr(server_entry, "_write_dependency_probe", unexpected_probe)

    server_entry.main()

    captured = capsys.readouterr()
    assert "another kb-api process is already running" in captured.err
