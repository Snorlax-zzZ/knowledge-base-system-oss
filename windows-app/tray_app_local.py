"""直装版 Windows 托盘 App，管理本地 kb-api.exe 进程。"""
from __future__ import annotations

import datetime as _dt
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
import webbrowser
from pathlib import Path

# PyInstaller --onefile 解压到临时目录，Windows 默认不搜索该目录的 DLL
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _meipass = sys._MEIPASS  # type: ignore[attr-defined]
    os.add_dll_directory(_meipass)
    os.environ["PATH"] = _meipass + os.pathsep + os.environ.get("PATH", "")
    # Anaconda 的 tcl/tk 布局非标，PyInstaller hook 找不到 → build 时
    # 显式 add-data 到 _MEIPASS/_tcl 和 _MEIPASS/_tk，运行时手动注入
    # 这两个环境变量。否则 tkinter.Tk() 会抛 TclError: Can't find init.tcl
    _tcl_dir = os.path.join(_meipass, "_tcl")
    _tk_dir = os.path.join(_meipass, "_tk")
    if os.path.isdir(_tcl_dir):
        os.environ["TCL_LIBRARY"] = _tcl_dir
    if os.path.isdir(_tk_dir):
        os.environ["TK_LIBRARY"] = _tk_dir

from PIL import Image, ImageDraw
import pystray
from pystray import Menu, MenuItem

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


def _install_root() -> Path:
    if getattr(sys, "frozen", False):
        # onedir: bin\kb-tray\kb-tray.exe → 项目根 = parent.parent.parent
        # onefile: bin\kb-tray.exe        → 项目根 = parent.parent
        exe_dir = Path(sys.executable).parent
        if exe_dir.name == "kb-tray":
            return exe_dir.parent.parent
        return exe_dir.parent
    return Path(__file__).parent.parent


def _assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets"  # type: ignore[attr-defined]
    return Path(__file__).parent / "assets"


_ICON_CACHE: dict[str, Image.Image] = {}

_STATE_FILES = {
    "running": "menu-running-64.png",
    "stopped": "menu-stopped-64.png",
    "busy": "menu-busy-64.png",
    "unknown": "menu-stopped-64.png",
}


def _load_port(root: Path) -> int:
    cfg_path = root / "config" / "config.toml"
    if cfg_path.exists():
        try:
            with open(cfg_path, "rb") as f:
                cfg = tomllib.load(f)
            return int(cfg.get("server", {}).get("port", 18000))
        except Exception:
            pass
    return 18000


def _make_icon(state: str) -> Image.Image:
    """返回 64×64 托盘图标（从文件加载或程序生成）。"""
    if state not in _ICON_CACHE:
        filename = _STATE_FILES.get(state, "menu-stopped-64.png")
        img_path = _assets_dir() / filename
        if img_path.exists():
            _ICON_CACHE[state] = Image.open(img_path).convert("RGBA")
        else:
            color = {
                "running": (36, 182, 95),
                "stopped": (245, 107, 86),
                "busy": (67, 145, 255),
            }.get(state, (160, 160, 160))
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.rounded_rectangle((6, 6, 58, 58), radius=14, fill=color)
            draw.rectangle((18, 18, 46, 46), fill=(255, 255, 255, 240))
            _ICON_CACHE[state] = img
    return _ICON_CACHE[state]




def _find_pid_by_port(port: int) -> int | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(("127.0.0.1", port))
    except Exception:
        return None

    cmd = ["cmd.exe", "/c", f"netstat -ano | findstr :{port}"]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        return None

    for line in (proc.stdout or "").splitlines():
        t = line.strip()
        if "LISTENING" not in t:
            continue
        parts = t.split()
        if len(parts) < 5:
            continue
        local_addr = parts[1]
        pid_text = parts[-1]
        if local_addr.endswith(f":{port}") and pid_text.isdigit():
            return int(pid_text)
    return None


class LocalTrayController:
    POLL_INTERVAL = 10  # seconds

    def __init__(self) -> None:
        self.root = _install_root()
        self.port = _load_port(self.root)
        # kb-api.exe 位置:1.3.12 起 onedir 模式(bin/kb-api/kb-api.exe + _internal/),
        # 1.3.11 及以前 onefile(bin/kb-api.exe)。升级到 1.3.12 时 [InstallDelete]
        # 已清旧 onefile,但兜底两种都探测,旧用户在过渡期也能跑。
        api_onedir = self.root / "bin" / "kb-api" / "kb-api.exe"
        api_onefile = self.root / "bin" / "kb-api.exe"
        self._api_exe = api_onedir if api_onedir.exists() else api_onefile
        self._proc: subprocess.Popen | None = None
        self._state = "unknown"
        self._status_text = "检查中..."
        self._busy = False
        self._stop_event = threading.Event()

        # Embedding 服务壳层 manager（懒加载;kb-api 健康后才 start）
        self._embedding_bundle = None
        self._embedding_started = False
        # 手动"启动 Embedding 服务" 的 in-progress 标志（menu-scoped busy）——
        # 2026-07-01 P1：90 秒轮询期间没设 busy 会被重复点击起多个 /install
        # 并发；这里独立于全局 self._busy，避免冻结整个托盘。
        self._embedding_starting = False
        # kb-api 冷启后 desired-state 内存态归零导致 embedding 不会自动起（坑
        # D-deep）——托盘 bundle 首次 healthy 时做一次 auto-bootstrap 静默
        # POST /install，代替用户点菜单。一次性标志避免反复触发。
        self._auto_bootstrap_attempted = False

        # bug 8 配套：重建向量索引的后台 poller 单实例锁
        # 用 threading.Lock 包一个 bool；防止用户连点菜单触发多个 poller 并发刷屏
        self._rebuild_poller_lock = threading.Lock()
        self._rebuild_poller_active = False

        self.icon = pystray.Icon("KBLocal")
        self.icon.icon = _make_icon("unknown")
        self.icon.title = "百变怪芝士包"
        self._rebuild_menu()

    # ── 状态工具 ─────────────────────────────────────────────────────────────

    def _set_state(self, state: str, text: str) -> None:
        self._state = state
        self._status_text = text
        if not self._busy:
            self.icon.icon = _make_icon(state)
        self._rebuild_menu()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.icon.icon = _make_icon("busy" if busy else self._state)
        self._rebuild_menu()

    def _rebuild_menu(self) -> None:
        can_start = (not self._busy) and self._state != "running"
        can_stop = (not self._busy) and self._state == "running"
        kb_actions_enabled = (not self._busy) and self._state == "running"

        kb_manage = Menu(
            MenuItem("导入知识包", self._on_import_package,
                     enabled=lambda _: kb_actions_enabled),
            MenuItem("导出知识包", self._on_export_package,
                     enabled=lambda _: kb_actions_enabled),
            MenuItem("打开导出目录", self._open_exports_dir),
            Menu.SEPARATOR,
            MenuItem("重建向量索引", self._on_rebuild_vector_index,
                     enabled=lambda _: kb_actions_enabled),
        )

        # Embedding 状态菜单项：显示当前状态；已装未跑时用户可点手动启动
        # (unblock kb-api 重启后 desired-state 内存态归零的边缘 case)
        embed_text, embed_enabled, embed_action = self._embedding_menu_state()

        self.icon.menu = Menu(
            MenuItem(f"状态: {self._status_text}", lambda *_: None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("启动知识库", self._on_start, enabled=lambda _: can_start),
            MenuItem("停止知识库", self._on_stop, enabled=lambda _: can_stop),
            # 兜底项：无视 busy/state，按端口 PID 强杀。永远可点
            # 用途：菜单卡在 busy 或 state 误判时还能停服务
            MenuItem("强制停止服务（兜底）", self._on_force_stop),
            Menu.SEPARATOR,
            MenuItem(embed_text, embed_action or (lambda *_: None),
                     enabled=lambda _: embed_enabled),
            Menu.SEPARATOR,
            MenuItem("打开控制台", self._open_console),
            MenuItem("打开系统配置", self._open_settings),
            MenuItem("打开 API 文档", self._open_docs),
            MenuItem("诊断状态", self._show_debug_status),
            Menu.SEPARATOR,
            MenuItem("知识库管理", kb_manage),
            Menu.SEPARATOR,
            MenuItem("退出", self._on_quit),
        )
        self.icon.update_menu()

    def _embedding_menu_state(self):
        """按 embedding bundle snapshot 决定菜单项 (文本, enabled, 点击回调)。

        状态分支（每次 _rebuild_menu 求值一次；触发点见 _refresh / _set_state）：
        - bundle 未起 / kb-api 未健康 → 灰: 未就绪
        - _embedding_starting=True (手动触发中) → 灰: 启动中…（防重复点击）
        - warming_up=True                  → 灰: 启动中
        - running=True                     → 灰: 运行中 (device)
        - installed=True + running=False   → **可点: 启动 Embedding 服务**
        - last_error 非空                  → 灰: 错误摘要
        - 其余（未装）                     → 灰: 未安装引导语
        """
        bundle = self._embedding_bundle
        if bundle is None:
            return ("Embedding: 未就绪", False, None)
        # 手动触发中（menu-scoped busy）优先于 snapshot——即便 warming_up 还没翻，
        # 也把菜单锁上，防用户 90 秒轮询窗口内重复点起多个 /install。
        if self._embedding_starting:
            return ("⏳ Embedding 启动中… (手动触发)", False, None)
        snap = bundle.snapshot()
        if snap.warming_up:
            return ("⏳ Embedding 启动中…", False, None)
        if snap.running:
            dev = snap.device or "?"
            return (f"✓ Embedding: 运行中 ({dev})", False, None)
        # 磁盘兜底：即便 in-process actual.installed=False（触发场景：kb-api
        # 冷启后内存 desired 归零，install action 从没跑过，actual 保持默认），
        # 只要磁盘上 venv + 至少一个模型完整就允许用户手动启动——避免
        # "venv/模型明明都在，托盘却说未安装→请走引导页" 的误导。
        # (2026-07-01 老大反馈：截图里 UI 说未安装但磁盘 venv+model 都在)
        if snap.installed or self._filesystem_says_installed():
            return ("▶ 启动 Embedding 服务", not self._busy, self._on_start_embedding)
        if snap.last_error:
            return (f"⚠ Embedding: {snap.last_error[:30]}", False, None)
        return ("Embedding: 未安装（请走引导页）", False, None)

    def _filesystem_says_installed(self) -> bool:
        """磁盘兜底探测：embedding-service/venv 有 python + models/ 至少一个
        目录含 PyTorch 权重（复用 _is_model_dir_complete）。
        任何异常都返 False 走原语义，不影响正确性。
        """
        try:
            from embedding_process_manager import (
                _find_venv_python, _is_model_dir_complete,
            )
            venv_dir = self.root / "embedding-service" / "venv"
            if _find_venv_python(str(venv_dir)) is None:
                return False
            models_dir = self.root / "models"
            if not models_dir.exists():
                return False
            for entry in models_dir.iterdir():
                if entry.is_dir() and _is_model_dir_complete(str(entry)):
                    return True
        except Exception:  # noqa: BLE001 — 探测失败不能拖累菜单
            pass
        return False

    def _on_start_embedding(self, _icon=None, _item=None) -> None:
        # 双重互斥：全局 busy（其他菜单动作跑着）+ menu-scoped starting
        # （本 embedding 启动流程已在跑）。任一命中直接忽略点击。
        if self._busy or self._embedding_starting:
            return
        threading.Thread(target=self._safe_run,
                         args=(self._do_start_embedding, "启动 Embedding 失败"),
                         daemon=True).start()

    def _do_start_embedding(self) -> None:
        """串两步 POST /install + POST /start 让 kb-api bump desired-state：
        1) /install 让 reconcile 跑 install(skip if complete)——保证半装场景 repair
        2) /start 显式 promote 到 action=start——reconcile 才会 spawn infinity

        kb-api 侧 install handler 不会 auto-promote 到 start，跟 setup wizard 一致
        （app/static/setup/index.html:475）。缺 /start 表现：desired 卡在 install，
        reconcile 反复跑 install(skip)，infinity 永不 spawn（2026-07-02 P0 事故根因）。

        用户可见反馈:
        - 立即 icon.notify 气球
        - _embedding_starting=True 锁菜单显示 "启动中…（手动触发）"
        - 每 3s 轮询 snapshot 直到 running / last_error / 前端超时
        - 结束 _alert 强弹结果 (toast 可能被系统专注助手吞)
        - finally 里清 _embedding_starting，防异常留下菜单永久锁死
        """
        if self._embedding_bundle is None:
            self._alert("启动 Embedding 失败",
                        "kb-api 尚未健康，无法启动 embedding 壳层；请稍候再试。")
            return

        # 2026-07-01 P1：菜单 scoped busy，防 90 秒轮询期间用户重复点击
        # 起并发 /install。set 后立刻 _rebuild_menu 让菜单文案 / enabled 翻掉；
        # finally 里清标志避免异常路径永久卡死。
        self._embedding_starting = True
        self._rebuild_menu()
        try:
            # 1) 读 config 拿 model_id / device
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/v1/system/config", timeout=8
                ) as r:
                    cfg = json.loads(r.read())
            except Exception as e:
                self._alert("启动 Embedding 失败", f"读 kb-api /v1/system/config 失败: {e}")
                return
            model_id = str(cfg.get("embedding_service_model_id") or "").strip() or "bge-m3"
            device = str(cfg.get("embedding_service_device") or "").strip() or "cpu"

            # 2) 读 owner_token
            token_path = self.root / "runtime" / "owner_token"
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError as e:
                self._alert("启动 Embedding 失败", f"读 owner_token 失败: {e}")
                return

            # 3) POST /install 触发 desired bump（install action 会在模型完整时 skip 下载）
            payload = json.dumps({"model_id": model_id, "device": device}).encode("utf-8")
            install_req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/system/embedding-service/install",
                data=payload, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Embedding-Owner-Token": token,
                },
            )
            try:
                with urllib.request.urlopen(install_req, timeout=20) as r:
                    r.read()  # 消费掉 SSE stream 尾部 event
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")[:300]
                self._alert("启动 Embedding 失败",
                            f"kb-api HTTP {e.code}\n{body}")
                return
            except Exception as e:
                self._alert("启动 Embedding 失败", f"网络错误: {e}")
                return

            # 4) POST /start 显式 promote desired.action=install→start（跟 setup wizard 一致）。
            # kb-api install handler 完成后不会 auto-promote，必须再 bump 一次；
            # 缺这一步 reconcile 卡在 install(skip)，infinity 永不 spawn。
            # 幂等——bump 失败只 warn，warmup 轮询继续（用户仍可看到最终结果）。
            start_req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/system/embedding-service/start",
                data=json.dumps({}).encode("utf-8"), method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Embedding-Owner-Token": token,
                },
            )
            try:
                with urllib.request.urlopen(start_req, timeout=10) as r:
                    r.read()
            except Exception:
                pass  # 幂等失败不阻塞主流程；下游 warmup 轮询兜底

            self._notify("Embedding", "已请求启动，预计 30-60 秒完成 warmup…")
            self._rebuild_menu()

            # 4) 轮询 snapshot 直到 running / last_error / 超时。
            # 2026-07-01 P1：超时必须 >= embedding_process_manager 里
            # DEFAULT_WARMUP_TIMEOUT_SEC (120s)，否则慢机上前端先报"超时"但
            # 后台 manager 可能仍在 warming → 用户被误导为失败。这里用
            # StartHandler 常量 + 30s buffer 覆盖 install(skip)→ start→ warmup
            # 全链路。
            try:
                from embedding_process_manager import DEFAULT_WARMUP_TIMEOUT_SEC
                poll_deadline_sec = float(DEFAULT_WARMUP_TIMEOUT_SEC) + 30.0
            except Exception:
                poll_deadline_sec = 150.0  # 兜底
            deadline = time.time() + poll_deadline_sec
            last_err = ""
            while time.time() < deadline:
                time.sleep(3.0)
                snap = self._embedding_bundle.snapshot()
                if snap.running:
                    self._notify("Embedding", f"服务已启动（{snap.device or '?'}）")
                    return
                if snap.last_error and snap.last_error != last_err:
                    # 记但不立刻退 —— reconcile 可能重试成功；只在最终失败时报
                    last_err = snap.last_error

            # 5) 超时
            tail = f"\n最后错误: {last_err[:200]}" if last_err else ""
            self._alert(
                "Embedding 启动超时",
                f"{int(poll_deadline_sec)} 秒内未 warmup 就绪。请查 "
                "D:/KnowledgeBase/logs/infinity.log。" + tail,
            )
        finally:
            self._embedding_starting = False
            try:
                self._rebuild_menu()
            except Exception:
                pass

    def _notify(self, title: str, msg: str) -> None:
        import ctypes
        try:
            self.icon.notify(msg, title=title)
        except Exception:
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0)

    def _alert(self, title: str, msg: str) -> None:
        """模态对话框——用于"用户主动点了菜单要看结果"的反馈。
        toast 在某些 Windows 通知设置下不弹（专注助手 / 通知关闭），
        强弹保证看见。导入 / 导出 / 强制停止之类操作走这条。
        """
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)  # MB_ICONINFORMATION

    def _show_debug_status(self, _icon=None, _item=None) -> None:
        threading.Thread(target=self._do_show_debug_status, daemon=True).start()

    def _do_show_debug_status(self) -> None:
        import ctypes
        health_ok = self._check_health()
        pid_by_port = _find_pid_by_port(self.port)
        own_pid = self._proc.pid if self._proc is not None else None
        own_alive = (self._proc is not None and self._proc.poll() is None)
        embed_lines = self._embedding_debug_lines()
        msg = (
            f"state    : {self._state}\n"
            f"busy     : {self._busy}\n"
            f"status   : {self._status_text}\n"
            f"port     : {self.port}\n"
            f"port pid : {pid_by_port if pid_by_port is not None else 'none'}\n"
            f"own pid  : {own_pid if own_pid is not None else 'none'} "
            f"(alive={own_alive})\n"
            f"health   : {'OK' if health_ok else 'FAIL'}\n"
            f"root     : {self.root}\n"
            f"--- embedding ---\n{embed_lines}"
        )
        ctypes.windll.user32.MessageBoxW(0, msg, "诊断状态", 0)

    def _embedding_debug_lines(self) -> str:
        if self._embedding_bundle is None:
            return "manager: not started\n(kb-api 未健康前不拉起)"
        snap = self._embedding_bundle.snapshot()
        return (
            f"installed       : {snap.installed}\n"
            f"running         : {snap.running}\n"
            f"warming_up      : {snap.warming_up}\n"
            f"model_id        : {snap.model_id or '-'}\n"
            f"port            : {snap.port}\n"
            f"pid             : {snap.pid if snap.pid is not None else '-'}\n"
            f"device          : {snap.device}\n"
            f"restart_count   : {snap.restart_count}\n"
            f"last_error      : {snap.last_error or '-'}"
        )

    def _check_health(self) -> bool:
        try:
            url = f"http://127.0.0.1:{self.port}/health"
            with urllib.request.urlopen(url, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _refresh(self) -> None:
        if self._busy:
            return
        alive = self._check_health()
        if alive:
            self._set_state("running", "运行中")
            # kb-api 一旦健康就拉起 embedding 壳层 manager(只拉一次)
            self._ensure_embedding_manager_started()
        else:
            if self._proc is not None and self._proc.poll() is not None:
                self._proc = None
            self._set_state("stopped", "未运行")

    # ── Embedding 服务壳层管理 ──────────────────────────────────────────────

    def _ensure_embedding_manager_started(self) -> None:
        """kb-api 第一次健康时拉起 EmbeddingProcessManager。

        失败不阻塞主流程:embedding 服务是可选能力,kb-api 仍可单独用关键词
        检索。grace:本函数任何错都吞,只 log。
        """
        if self._embedding_started:
            # bundle 已起过：check auto-bootstrap（多轮 poll 内幂等）
            self._maybe_auto_bootstrap_embedding()
            return
        try:
            from embedding_tray_bridge import build_default_bundle
            self._embedding_bundle = build_default_bundle(
                data_root=self.root, kb_api_port=self.port,
            )
            self._embedding_bundle.start()
            self._embedding_started = True
        except Exception:
            # 失败容忍:用户仍可用关键词检索,embedding 设置失败 UI 自己处理
            import traceback
            traceback.print_exc()
            return
        # bundle 首次起后触发 auto-bootstrap 判定
        self._maybe_auto_bootstrap_embedding()

    def _maybe_auto_bootstrap_embedding(self) -> None:
        """kb-api 冷启后 desired-state 内存态归零 → 托盘 reconcile 拉 desired
        看 action=none 就什么都不做 → infinity 不会自动起（坑 D-deep）。

        本方法在 kb-api 首次 healthy 时判定：
          - 磁盘上 venv + 至少一个模型完整（说明用户之前配过 embedding）
          - kb-api 侧 desired.action=none / enabled=false（说明是新起进程）
          - actual 里 running=false（不覆盖已经跑起来的服务）
          - 且本次会话内还没 attempt 过（一次性标志）
        全命中 → 静默 POST /install 一次代替用户点菜单，reconcile 自动跑起来。

        长期修根：desired-state 落盘持久化（[[kb-desired-state-persistence]]
        followup）。本方法只是保险丝，覆盖用户可见 UX 症状。
        """
        if self._auto_bootstrap_attempted:
            return
        if self._embedding_bundle is None:
            return
        # 1) 磁盘装了吗
        if not self._filesystem_says_installed():
            return
        # 2) actual 里已经跑起来了就别动
        try:
            snap = self._embedding_bundle.snapshot()
        except Exception:
            return
        if snap.running or snap.warming_up:
            self._auto_bootstrap_attempted = True  # 已经好了，不需要 bootstrap
            return
        # 3) 拉 desired-state 判是不是内存归零态
        token_path = self.root / "runtime" / "owner_token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/system/embedding-service/desired-state",
                headers={"X-Embedding-Owner-Token": token},
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                desired = json.loads(r.read())
        except Exception:
            return
        if desired.get("action") != "none" or desired.get("enabled"):
            self._auto_bootstrap_attempted = True  # 有活跃 desired，不覆盖
            return
        # 4) 触发静默 auto-bootstrap（后台线程，不阻塞 poll_loop）
        self._auto_bootstrap_attempted = True
        threading.Thread(
            target=self._safe_run,
            args=(self._do_auto_bootstrap_embedding, "自动启动 Embedding 失败"),
            daemon=True,
        ).start()

    def _do_auto_bootstrap_embedding(self) -> None:
        """跟 _do_start_embedding 类似但静默：不弹 alert、只放一次气球通知。
        串两步：先 POST /install（bump 到 install，reconcile 跑 install skip 或
        真装），再 POST /start（bump 到 start，reconcile spawn infinity）。
        两步必须都发——kb-api 侧 install handler 不会 auto-promote 到 start，
        跟 setup wizard 一致（app/static/setup/index.html:475）。
        缺 /start 表现：desired 卡在 install，reconcile 反复跑 install(skip)，
        infinity 永不 spawn，横幅显示"已安装但未运行"（2026-07-02 P0 事故根因）。
        用户可见就是横幅平滑消失 + 菜单翻绿。
        """
        if self._embedding_bundle is None or self._embedding_starting:
            return
        self._embedding_starting = True
        self._rebuild_menu()
        try:
            # 拉 config 拿 model_id / device
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/v1/system/config", timeout=8
                ) as r:
                    cfg = json.loads(r.read())
            except Exception:
                return
            model_id = str(cfg.get("embedding_service_model_id") or "").strip() or "bge-m3"
            device = str(cfg.get("embedding_service_device") or "").strip() or "cpu"

            token_path = self.root / "runtime" / "owner_token"
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                return

            payload = json.dumps({"model_id": model_id, "device": device}).encode("utf-8")
            install_req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/system/embedding-service/install",
                data=payload, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Embedding-Owner-Token": token,
                },
            )
            try:
                with urllib.request.urlopen(install_req, timeout=20) as r:
                    r.read()
            except Exception:
                return  # 静默失败——菜单/横幅会保持"未运行"状态，用户可以手动点

            # 显式 POST /start promote desired.action=install→start（跟 setup wizard 一致）。
            # kb-api install handler 完成后不会自己 promote，必须再 bump 一次；否则
            # reconcile 反复跑 install(skip)，infinity 永不 spawn（2026-07-02 P0 事故）。
            # 幂等——bump 失败不阻塞 warmup 轮询（用户可从菜单手动重试）。
            start_req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/system/embedding-service/start",
                data=json.dumps({}).encode("utf-8"), method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Embedding-Owner-Token": token,
                },
            )
            try:
                with urllib.request.urlopen(start_req, timeout=10) as r:
                    r.read()
            except Exception:
                pass

            # 一次性气球提示（不弹 alert，避免打扰）
            try:
                self._notify("Embedding", "检测到已配置服务，正在自动启动…")
            except Exception:
                pass

            # 观察 warmup 完成：跟 _do_start_embedding 用同款超时（DEFAULT_WARMUP_TIMEOUT_SEC + 30s）
            try:
                from embedding_process_manager import DEFAULT_WARMUP_TIMEOUT_SEC
                poll_deadline_sec = float(DEFAULT_WARMUP_TIMEOUT_SEC) + 30.0
            except Exception:
                poll_deadline_sec = 150.0
            deadline = time.time() + poll_deadline_sec
            while time.time() < deadline:
                time.sleep(3.0)
                snap = self._embedding_bundle.snapshot()
                if snap.running:
                    try:
                        self._notify("Embedding", f"服务已自动启动（{snap.device or '?'}）")
                    except Exception:
                        pass
                    return
            # 超时静默——不弹框，让用户按需右键"▶ 启动 Embedding 服务"重试
            # (auto path 尽量少骚扰；手动路径承担确切失败反馈)
        finally:
            self._embedding_starting = False
            try:
                self._rebuild_menu()
            except Exception:
                pass

    def _stop_embedding_manager(self) -> None:
        """退出托盘前关掉 embedding manager,顺带 SIGTERM/SIGKILL infinity 子进程。"""
        if self._embedding_bundle is None:
            return
        try:
            self._embedding_bundle.stop(timeout=5.0)
        except Exception:
            pass

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            self._refresh()
            self._stop_event.wait(self.POLL_INTERVAL)

    # ── 操作 ─────────────────────────────────────────────────────────────────

    def _on_start(self, _icon=None, _item=None) -> None:
        if self._busy:
            return
        threading.Thread(target=self._do_start, daemon=True).start()

    def _do_start(self) -> None:
        self._set_busy(True)
        try:
            if not self._api_exe.exists():
                self._notify("启动失败", f"找不到 kb-api.exe:\n{self._api_exe}")
                return

            # 注入 KB_APP_ROOT 让 kb-api：
            # (a) 从 {root}\VERSION 读到正确 APP_VERSION
            # (b) 把 {root} 加入 _allowed_data_roots()，导出/导入不被路径边界拦
            env = os.environ.copy()
            env["KB_APP_ROOT"] = str(self.root)
            self._proc = subprocess.Popen(
                [str(self._api_exe)],
                cwd=str(self.root),
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for _ in range(20):
                time.sleep(1)
                if self._check_health():
                    self._notify("已启动", "Knowledge Base 已就绪")
                    return
            self._notify("启动超时", "服务未在 20 秒内就绪，请查看日志")
        finally:
            self._set_busy(False)
            self._refresh()

    def _on_stop(self, _icon=None, _item=None) -> None:
        if self._busy:
            return
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _do_stop(self) -> None:
        # PyInstaller --onefile：self._proc.pid 是 bootloader，terminate 漏 child。
        # 跟 _on_quit 同样用 taskkill /T 杀进程树，并按端口兜底。
        self._set_busy(True)
        try:
            pids_to_kill: list[int] = []
            if self._proc is not None and self._proc.poll() is None:
                pids_to_kill.append(self._proc.pid)
            port_pid = _find_pid_by_port(self.port)
            if port_pid is not None and port_pid not in pids_to_kill:
                pids_to_kill.append(port_pid)

            if not pids_to_kill:
                self._notify("未运行", "服务当前未在运行")
                return

            for pid in pids_to_kill:
                subprocess.run(
                    ["cmd.exe", "/c", f"taskkill /PID {pid} /T /F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            self._proc = None
            # 等服务真停下来再回报，避免菜单刷新时 health 还瞬时 OK 让状态卡 running
            for _ in range(10):
                if _find_pid_by_port(self.port) is None and not self._check_health():
                    break
                time.sleep(0.3)
            self._notify("已停止", "Knowledge Base 已停止")
        finally:
            self._set_busy(False)
            self._refresh()

    def _open_url(self, path: str) -> None:
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            ok = webbrowser.open(url)
            if not ok:
                subprocess.run(["cmd.exe", "/c", "start", "", url], check=False)
        except Exception:
            subprocess.run(["cmd.exe", "/c", "start", "", url], check=False)

    def _open_console(self, _icon=None, _item=None) -> None:
        self._open_url("/console")

    def _open_settings(self, _icon=None, _item=None) -> None:
        self._open_url("/settings")

    def _open_docs(self, _icon=None, _item=None) -> None:
        self._open_url("/docs")

    # ── 知识库管理 ──────────────────────────────────────────────────────────
    # 仅依赖 stdlib：tkinter 选文件 / 弹窗，urllib + 自构 multipart 调 HTTP。
    # PyInstaller --collect-all app 默认带 tkinter；不引入 requests 等外部依赖。

    _IMPORT_PRESETS = [
        ("个人默认（personal / personal / fact）", "personal", "personal", "fact"),
        ("工作默认（work / work / fact）", "work", "work", "fact"),
        ("个人-经验（personal / personal / lesson）", "personal", "personal", "lesson"),
        ("工作-手册（work / work / runbook）", "work", "work", "runbook"),
    ]

    def _exports_dir(self) -> Path:
        d = self.root / "exports"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _open_exports_dir(self, _icon=None, _item=None) -> None:
        d = self._exports_dir()
        try:
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception:
            subprocess.run(["cmd.exe", "/c", "start", "", str(d)], check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    def _on_import_package(self, _icon=None, _item=None) -> None:
        if self._busy or self._state != "running":
            self._alert("无法导入", "请先启动知识库后再操作")
            return
        threading.Thread(target=self._safe_run,
                         args=(self._do_import_package, "导入异常"),
                         daemon=True).start()

    def _on_export_package(self, _icon=None, _item=None) -> None:
        if self._busy or self._state != "running":
            self._alert("无法导出", "请先启动知识库后再操作")
            return
        threading.Thread(target=self._safe_run,
                         args=(self._do_export_package, "导出异常"),
                         daemon=True).start()

    def _safe_run(self, fn, title: str) -> None:
        """daemon thread 顶层兜底——把任何未捕获异常通过 _alert 强弹,
        避免 PyInstaller --noconsole 下 stderr 被吞、用户看不到为什么没反应。
        """
        import traceback
        try:
            fn()
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            self._alert(title, f"{type(e).__name__}: {e}\n\n{tb[-400:]}")
            # busy 万一被卡也复位一下
            try:
                self._set_busy(False)
            except Exception:
                pass

    def _do_export_package(self) -> None:
        self._set_busy(True)
        try:
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = self._exports_dir() / f"kb-export-{ts}.tar.gz"
            url = f"http://127.0.0.1:{self.port}/v1/system/backup/export"
            req = urllib.request.Request(url, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=600) as resp, \
                     open(out_path, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                detail = self._extract_detail(body) or f"HTTP {e.code}"
                self._alert("导出失败", detail)
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return
            size_mb = out_path.stat().st_size / (1024 * 1024)
            self._alert("导出完成",
                        f"已生成：{out_path.name}\n大小：{size_mb:.1f} MB\n\n位置：{out_path.parent}")
        except Exception as e:
            self._alert("导出失败", str(e))
        finally:
            self._set_busy(False)

    def _do_import_package(self) -> None:
        path = self._tk_choose_open_file(
            title="选择要导入的文件（.tar.gz 备份包 / .md / .txt / .docx / .pdf）",
            filetypes=[
                ("全部支持的类型", "*.tar.gz *.tgz *.md *.markdown *.txt *.docx *.pdf"),
                ("备份包", "*.tar.gz *.tgz"),
                ("Markdown", "*.md *.markdown"),
                ("文本", "*.txt"),
                ("Word", "*.docx"),
                ("PDF", "*.pdf"),
            ],
        )
        if not path:
            return
        lower = path.lower()
        if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
            self._do_import_backup(path)
        else:
            self._do_import_single_file(path)

    def _do_import_backup(self, path: str) -> None:
        if not self._tk_confirm(
            title="确认覆盖恢复",
            message=(
                f"即将用以下备份包覆盖当前知识库数据：\n\n{path}\n\n"
                "原数据会被新版本自动备份到 auto-backup/{时间戳}/。\n"
                "确定继续？"
            ),
        ):
            return
        self._set_busy(True)
        try:
            url = f"http://127.0.0.1:{self.port}/v1/system/backup/import"
            body, content_type = self._build_multipart(
                fields={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
                files={"file": (Path(path).name, path)},
            )
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            )
            try:
                with urllib.request.urlopen(req, timeout=1800) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                self._alert("导入完成", self._extract_detail(resp_body) or "已覆盖恢复")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                self._alert("导入失败",
                            f"HTTP {e.code}：{self._extract_detail(body) or '详见日志'}")
        except Exception as e:
            self._alert("导入失败", str(e))
        finally:
            self._set_busy(False)
            self._refresh()

    def _do_import_single_file(self, path: str) -> None:
        preset = self._tk_choose_preset()
        if preset is None:
            return
        _label, project, domain, kn_type = preset
        self._set_busy(True)
        try:
            url = f"http://127.0.0.1:{self.port}/v1/knowledge/import-file"
            body, content_type = self._build_multipart(
                fields={
                    "project": project,
                    "domain": domain,
                    "knowledge_type": kn_type,
                    "actor": "windows-tray",
                },
                files={"file": (Path(path).name, path)},
            )
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": content_type, "Content-Length": str(len(body))},
            )
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                self._alert("导入完成",
                            f"{Path(path).name} → {project}/{domain}/{kn_type}")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace") if e.fp else ""
                self._alert("导入失败",
                            f"HTTP {e.code}：{self._extract_detail(body) or '详见日志'}")
        except Exception as e:
            self._alert("导入失败", str(e))
        finally:
            self._set_busy(False)

    # ── 重建向量索引（bug 7 + 8，对齐 Mac Swift main.swift:374-511）──────────
    # 入口：知识库管理 → 重建向量索引；NSAlert 二次确认 → POST /v1/system/
    # rebuild-vector-index + I-CONFIRM-OVERWRITE token；HTTP 200/202 都视为
    # 进入 rebuild 状态；后台 3s/次 poll status，completed/failed 弹通知；
    # _rebuild_poller_active 单实例锁防连点 + 30 分钟兜底超时。

    def _on_rebuild_vector_index(self, _icon=None, _item=None) -> None:
        if self._busy or self._state != "running":
            self._alert("无法重建", "请先启动知识库后再操作")
            return
        threading.Thread(
            target=self._safe_run,
            args=(self._do_rebuild_vector_index, "重建异常"),
            daemon=True,
        ).start()

    def _do_rebuild_vector_index(self) -> None:
        if not self._tk_confirm(
            title="重建向量索引",
            message=(
                "将清空现有向量集合并重新走 embedding 算所有 chunk 的向量。\n\n"
                "- 切换 embedding 模型 / 维度后必须重建\n"
                "- 期间不能写入知识库（自动维护态）\n"
                "- 时长视 chunk 数与模型而定：约 1100 chunk · CPU bge-m3 约 1-3 分钟\n\n"
                "进度会在「打开控制台」页面顶部横幅实时显示。是否继续？"
            ),
        ):
            return

        url = f"http://127.0.0.1:{self.port}/v1/system/rebuild-vector-index"
        body = json.dumps(
            {"confirm": "I-CONFIRM-OVERWRITE", "batch_size": 100}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                resp_body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            status = e.code
            resp_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        except Exception as e:  # noqa: BLE001
            self._alert("重建启动失败", f"网络错误：{e}")
            return

        # 200 = 同步完成（小批量秒回）；202 = 异步启动（rebuild_runner 后台线程）。
        # 都视为"成功进入 rebuild 状态"，差别仅在前端要不要 poll；这里统一 poll。
        if status in (200, 202):
            self._notify(
                "向量索引重建已启动",
                "后台进行中，跑完会弹通知。也可在「打开控制台」页面顶部横幅实时看进度。",
            )
            self._start_rebuild_poller()
            return
        if status == 409:
            self._notify("已有重建在跑", "请等当前 rebuild 完成或先到设置页面 abort")
            return
        if status == 400:
            # 典型场景：embedding 是 HashEmbedding 兜底（mode=disabled 或外部 API 不可达）
            detail = self._extract_detail(resp_body) or "embedding 服务未就绪"
            self._notify(
                "重建无法启动",
                f"请先在「设置」页配置可用的 embedding 服务。详情：{detail[:160]}",
            )
            return
        if status == 503:
            # embedding 服务还没就绪（infinity 装机后 warmup 30-60s 才 LISTEN）。
            # 后端拒在进 RUNNING 之前，旧索引完好；提示用户等 banner ready。
            # 对齐 Mac main.swift case 503（commit bc8f9e6）。
            self._notify(
                "embedding 服务尚未就绪",
                "请等托盘 banner 显示 ready 后再点重建（通常装机后 30-60 秒）。",
            )
            return
        self._notify(
            "重建启动失败",
            f"HTTP {status}：{(self._extract_detail(resp_body) or resp_body)[:200]}",
        )

    def _start_rebuild_poller(self) -> None:
        """启动单实例后台 poller，3s/次 poll 直到 completed/failed/30 分钟超时。"""
        with self._rebuild_poller_lock:
            if self._rebuild_poller_active:
                return
            self._rebuild_poller_active = True
        threading.Thread(
            target=self._poll_rebuild_status_until_done,
            daemon=True,
        ).start()

    def _poll_rebuild_status_until_done(self) -> None:
        """3s/次 poll /status；completed/failed 弹通知；30 分钟兜底超时。"""
        poll_interval_sec = 3.0
        max_duration_sec = 30 * 60
        status_url = (
            f"http://127.0.0.1:{self.port}/v1/system/rebuild-vector-index/status"
        )
        started_at = time.monotonic()
        try:
            while True:
                if time.monotonic() - started_at > max_duration_sec:
                    self._notify(
                        "重建状态轮询超时",
                        "已超 30 分钟未收到完成信号；请到「设置」页确认 rebuild 状态。",
                    )
                    return
                time.sleep(poll_interval_sec)

                # 拉一次 status；失败不报错，下轮重试（kb-api 临时抖动可恢复）
                try:
                    with urllib.request.urlopen(status_url, timeout=5) as resp:
                        raw = resp.read()
                except Exception:  # noqa: BLE001
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                status = obj.get("status")
                if status == "completed":
                    processed = obj.get("processed", 0)
                    total = obj.get("total", 0)
                    elapsed = int(time.monotonic() - started_at)
                    self._notify(
                        "向量索引重建完成",
                        f"处理 {processed}/{total} chunk，用时 {elapsed} 秒。语义检索已生效，可直接问答。",
                    )
                    return
                if status == "failed":
                    err_text = str(obj.get("error", ""))[:220]
                    self._notify(
                        "向量索引重建失败",
                        err_text or "未知错误，请查看「设置」页详情",
                    )
                    return
                # running / idle / 其他过渡态：继续 poll
        finally:
            with self._rebuild_poller_lock:
                self._rebuild_poller_active = False

    @staticmethod
    def _extract_detail(body: str) -> str:
        if not body:
            return ""
        try:
            obj = json.loads(body)
        except ValueError:
            return body[:200]
        if isinstance(obj, dict):
            d = obj.get("detail")
            if isinstance(d, str):
                return d
        return body[:200]

    @staticmethod
    def _build_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
        boundary = f"----kbtray{uuid.uuid4().hex}"
        crlf = b"\r\n"
        parts: list[bytes] = []
        for name, value in fields.items():
            parts.append(f"--{boundary}".encode())
            parts.append(
                f'Content-Disposition: form-data; name="{name}"'.encode()
            )
            parts.append(b"")
            parts.append(str(value).encode("utf-8"))
        for name, (filename, filepath) in files.items():
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            parts.append(f"--{boundary}".encode())
            parts.append(
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"'.encode()
            )
            parts.append(f"Content-Type: {mime}".encode())
            parts.append(b"")
            with open(filepath, "rb") as f:
                parts.append(f.read())
        parts.append(f"--{boundary}--".encode())
        parts.append(b"")
        return crlf.join(parts), f"multipart/form-data; boundary={boundary}"

    @staticmethod
    def _tk_choose_open_file(title: str, filetypes: list) -> str | None:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askopenfilename(parent=root, title=title,
                                              filetypes=filetypes)
        finally:
            root.destroy()
        return path or None

    @staticmethod
    def _tk_confirm(title: str, message: str) -> bool:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return bool(messagebox.askyesno(title, message, parent=root, icon="warning"))
        finally:
            root.destroy()

    def _tk_choose_preset(self) -> tuple | None:
        import tkinter as tk
        from tkinter import ttk
        result: dict = {}
        root = tk.Tk()
        root.title("选择导入参数")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        ttk.Label(root, text="为这份单文件选择 project / domain / 知识类型：",
                  padding=12).pack(anchor="w")
        var = tk.IntVar(value=0)
        for idx, preset in enumerate(self._IMPORT_PRESETS):
            ttk.Radiobutton(root, text=preset[0], variable=var, value=idx,
                            padding=(20, 2)).pack(anchor="w")
        btn_frame = ttk.Frame(root, padding=12)
        btn_frame.pack(fill="x")

        def _on_ok() -> None:
            result["picked"] = self._IMPORT_PRESETS[var.get()]
            root.destroy()

        def _on_cancel() -> None:
            root.destroy()

        ttk.Button(btn_frame, text="确定", command=_on_ok).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="取消", command=_on_cancel).pack(side="right")
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
        root.mainloop()
        return result.get("picked")

    # ── 兜底强制停止 ────────────────────────────────────────────────────────
    # 不受 self._busy / self._state 任何条件约束，永远可点。
    # 用于：托盘菜单"启动 / 停止"因状态卡死灰掉时还能干净停服务。

    def _on_force_stop(self, _icon=None, _item=None) -> None:
        threading.Thread(target=self._do_force_stop, daemon=True).start()

    def _do_force_stop(self) -> None:
        killed: list[str] = []
        try:
            if self._proc is not None:
                try:
                    self._proc.kill()
                    killed.append(f"child PID {self._proc.pid}")
                except Exception:
                    pass
                self._proc = None
            pid = _find_pid_by_port(self.port)
            if pid is not None:
                subprocess.run(
                    ["cmd.exe", "/c", f"taskkill /PID {pid} /T /F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                killed.append(f"port {self.port} → PID {pid}")
            # 一并重置 busy（如果是它卡死导致 stop 灰）
            self._busy = False
            if killed:
                self._alert("强制停止", "已停止：" + " | ".join(killed))
            else:
                self._alert("强制停止", "没有找到正在跑的服务")
        finally:
            self._refresh()

    def _on_quit(self, _icon=None, _item=None) -> None:
        self._stop_event.set()
        # 先停 embedding manager(它会 SIGTERM→3s→SIGKILL infinity 子进程,
        # 清 runtime/pid + runtime/port,避免下次启动残留)
        self._stop_embedding_manager()
        # 退出托盘时彻底停掉 kb-api（跟 macOS applicationWillTerminate 对齐）。
        #
        # PyInstaller --onefile 坑：self._proc.pid 拿到的是 *bootloader* PID，
        # 真正监听端口的 uvicorn 跑在 bootloader 的 child 进程里。
        # self._proc.terminate() 只杀 bootloader、child 漏网 → 端口仍占用。
        # 必须用 `taskkill /T /F`（/T = 杀进程树带走 child）。
        #
        # 同时按端口找 PID 兜底，覆盖"tray 自己没 spawn 但 kb-api 在跑"
        # （比如开机自启、上次会话残留）的情况。
        try:
            pids_to_kill: list[int] = []
            if self._proc is not None and self._proc.poll() is None:
                pids_to_kill.append(self._proc.pid)
            port_pid = _find_pid_by_port(self.port)
            if port_pid is not None and port_pid not in pids_to_kill:
                pids_to_kill.append(port_pid)
            for pid in pids_to_kill:
                subprocess.run(
                    ["cmd.exe", "/c", f"taskkill /PID {pid} /T /F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            self._proc = None
        except Exception:
            pass
        self.icon.stop()

    def run(self) -> None:
        self._refresh()
        poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        poll_thread.start()
        self.icon.run()


_SINGLE_INSTANCE_MUTEX_NAME = "Local\\KnowledgeBaseTray-SingletonLock"


def _acquire_single_instance_lock() -> bool:
    """获取用户会话级 Windows 命名 mutex。

    返回 True = 成功（本进程是首启）；False = 已有实例在跑。
    使用 ctypes 直调 kernel32.CreateMutexW，不依赖 pywin32。
    句柄不释放——进程退出由 OS 自动清理，跨进程可见性正确。
    """
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(
        None, True, _SINGLE_INSTANCE_MUTEX_NAME
    )
    if not handle:
        # 创建失败（极端情况，比如权限），保守起见放行不阻塞
        return True
    last_error = ctypes.windll.kernel32.GetLastError()
    if last_error == ERROR_ALREADY_EXISTS:
        # 另一个实例已持有，本次启动放弃
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    # 故意不持有 handle 引用——进程退出 OS 清理
    return True


def main() -> int:
    if sys.platform != "win32":
        print("This app is for Windows only.")
        return 1
    if not _acquire_single_instance_lock():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0,
            "百变怪芝士包已经在运行中。\n请查看屏幕右下角托盘图标。",
            "重复启动",
            0x40,  # MB_ICONINFORMATION
        )
        return 0
    try:
        from win32_menu_icons import patch_menu_icons
        patch_menu_icons()
    except Exception:
        pass
    LocalTrayController().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
