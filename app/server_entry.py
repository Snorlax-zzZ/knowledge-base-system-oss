"""直装版 FastAPI 服务入口，供 PyInstaller 打包为 kb-api.exe。"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import BinaryIO

# PyInstaller --onefile 解压到临时目录，Windows 默认不搜索该目录的 DLL
# 必须在任何可能触发 _ctypes 的 import 之前注册
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(sys._MEIPASS)
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")

import portalocker  # noqa: E402 — frozen DLL path must be registered first

# PyInstaller --noconsole 模式下 sys.stdout/stderr 为 None
# uvicorn DefaultFormatter.__init__ 会调用 stream.isatty() 导致 AttributeError
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _install_root() -> Path:
    # 优先 KB_APP_ROOT env(tray spawn kb-api 时注入,见 tray_app_local._do_start);
    # 这是最可靠的来源,跟 app/main.py:_resolve_data_root 同款约定。
    env_root = os.environ.get("KB_APP_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    if getattr(sys, "frozen", False):
        # PyInstaller onedir (1.3.12+): sys.executable = <install_root>\bin\kb-api\kb-api.exe
        # PyInstaller onefile (1.3.11-): sys.executable = <install_root>\bin\kb-api.exe
        # onedir 时 exe_dir.name 是 "kb-api",需多 parent 一层
        exe_dir = Path(sys.executable).parent
        if exe_dir.name == "kb-api":
            return exe_dir.parent.parent
        return exe_dir.parent
    # 开发模式：app/server_entry.py 位于 <project_root>/app/
    return Path(__file__).parent.parent


def _load_config(root: Path) -> dict:
    cfg_path = root / "config" / "config.toml"
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            return tomllib.load(f)
    return {}


def _acquire_process_lock(root: Path) -> BinaryIO | None:
    """Try to become the sole kb-api process for this install root."""
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    handle = (runtime_dir / "kb-api-process.lock").open("a+b")
    try:
        portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except portalocker.exceptions.LockException:
        handle.close()
        return None
    return handle


def _release_process_lock(handle: BinaryIO) -> None:
    try:
        portalocker.unlock(handle)
    finally:
        handle.close()


def _write_dependency_probe(log_path: Path) -> None:
    """冷启动早期跑 qdrant_client + 关键传递依赖 probe，把结果落到日志文件。

    2026-07-01 方案 4：不用等 rebuild 才触发才能看到 import 失败。
    kb-api 每次启动都写一次，覆盖式。写失败任何异常吞掉——probe 是诊断用，
    不能反过来阻断 kb-api 启动。
    """
    import importlib
    import importlib.util
    import sys
    import traceback

    def _spec_origin(mod_name: str) -> dict:
        try:
            spec = importlib.util.find_spec(mod_name)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        if spec is None:
            return {"found": False}
        return {"found": True, "origin": spec.origin}

    def _mod_version(mod_name: str) -> str:
        try:
            m = importlib.import_module(mod_name)
        except Exception as exc:
            return f"IMPORT_FAILED: {type(exc).__name__}: {exc}"
        return str(getattr(m, "__version__", "?"))

    report: dict = {
        "frozen": getattr(sys, "frozen", False),
        "meipass": getattr(sys, "_MEIPASS", None),
        "executable": sys.executable,
        "sys_path_head": sys.path[:16],
        "specs": {
            name: _spec_origin(name)
            for name in ("qdrant_client", "grpc", "pydantic", "google.protobuf", "numpy", "portalocker")
        },
        "versions": {
            name: _mod_version(name)
            for name in ("pydantic", "grpc", "numpy", "portalocker")
        },
    }

    qdrant_ok = False
    qdrant_error: str | None = None
    try:
        import qdrant_client  # noqa: F401
        from qdrant_client import QdrantClient, models  # noqa: F401
        qdrant_ok = True
    except Exception as exc:
        qdrant_error = f"{type(exc).__name__}: {exc}\n" + traceback.format_exc()
    report["qdrant_import_ok"] = qdrant_ok
    report["qdrant_import_error"] = qdrant_error

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        log_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        # 落盘失败也不阻断启动
        pass


def main() -> None:
    root = _install_root()
    # 必须早于 dependency probe 和 app.main import：直接双击/绕过 shell 并发
    # 启动时，loser 在 FastAPI startup 写 owner_token 之前就干净退出。
    process_lock = _acquire_process_lock(root)
    if process_lock is None:
        print(
            "[server-entry] another kb-api process is already running; exiting",
            file=sys.stderr,
        )
        return

    try:
        cfg = _load_config(root)
        # 方案 4：frozen 冷启动早期 probe qdrant_client + 关键传递依赖，落 log
        # 让用户一启动就有依赖状态证据，不用等 rebuild 才触发才能看到 import 失败。
        _write_dependency_probe(root / "logs" / "qdrant-import-probe.log")

        # 构建机 pre-ship 自测入口（build_direct_install.ps1 里 setenv KB_PROBE_ONLY=1
        # 起一次 kb-api.exe → probe 落盘 → 立即退出）。生产不会设这个 env。
        if os.environ.get("KB_PROBE_ONLY") == "1":
            print("[server-entry] KB_PROBE_ONLY=1, exited after probe")
            sys.exit(0)

        server_cfg = cfg.get("server", {})
        data_cfg = cfg.get("data", {})

        host: str = server_cfg.get("host", "127.0.0.1")
        port: int = int(server_cfg.get("port", 18000))

        sqlite_path = str(root / data_cfg.get("sqlite_path", "data/knowledge.db"))
        qdrant_path = str(root / data_cfg.get("qdrant_local_path", "data/qdrant_local"))
        vector_enabled = "1" if data_cfg.get("vector_enabled", True) else "0"

        os.environ.setdefault("KB_BACKEND", "sqlite")
        os.environ["SQLITE_PATH"] = sqlite_path
        os.environ["VECTOR_ENABLED"] = vector_enabled
        os.environ["QDRANT_MODE"] = "local"
        os.environ["QDRANT_LOCAL_PATH"] = qdrant_path

        print(f"[server-entry] KB_BACKEND={os.environ.get('KB_BACKEND', 'sqlite')}")
        print(f"[server-entry] PORT={port} (source=config.toml)")
        print(f"[server-entry] SQLITE_PATH={sqlite_path}")

        # 确保数据目录存在
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        Path(qdrant_path).mkdir(parents=True, exist_ok=True)

        import uvicorn
        from app.main import app as fastapi_app  # noqa: F401

        uvicorn.run(fastapi_app, host=host, port=port, workers=1)
    finally:
        _release_process_lock(process_lock)


if __name__ == "__main__":
    main()
