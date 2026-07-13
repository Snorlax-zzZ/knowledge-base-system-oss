"""macOS 菜单栏 App 组装后的签名完整性测试。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "darwin", reason="codesign 仅在 macOS 可用")
def test_build_menubar_app_refreshes_adhoc_signature(tmp_path):
    if shutil.which("codesign") is None:
        pytest.skip("codesign 不可用")

    output = tmp_path / "KnowledgeBaseMenuBar.app"
    subprocess.run(
        [
            str(ROOT / "scripts" / "build_menubar_app.sh"),
            "--output", str(output),
            "--project-root", "/Applications/KnowledgeBase",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    verified = subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", "--verbose=4", str(output)],
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr


def test_build_menubar_app_recompiles_when_binary_missing():
    """契约: build_menubar_app.sh 必须独立检查 template binary, 不能只依赖 plist
    缺失作为编译触发条件. 覆盖"骨架 + plist 已存在但 binary 缺失"半成品场景
    (前次 build 失败留下的状态), 避免后续重试仍报 executable not found."""
    script = (ROOT / "scripts" / "build_menubar_app.sh").read_text()
    # 独立 binary 检查 block, 不在 plist fallback if 内
    assert 'TEMPLATE_BIN=' in script, "缺少独立的 template binary 变量声明"
    assert 'if [[ ! -x "$TEMPLATE_BIN" ]]' in script, (
        "缺少独立的 binary 缺失判定 (只有 plist fallback 内的调用不够)"
    )
    # binary 缺失时必须调 swift.sh 编译
    assert 'scripts/build_menubar_swift.sh' in script


def test_open_menubar_app_checks_binary_not_just_dir():
    """契约: open_menubar_app.sh 必须检查 binary 存在, 不能只 check 目录.
    只 check 目录会 open 半成品 App (骨架 + plist 存在但 binary 缺失)."""
    script = (ROOT / "scripts" / "open_menubar_app.sh").read_text()
    assert 'BIN_PATH=' in script, "缺少 binary 路径变量"
    assert '! -x "$BIN_PATH"' in script, (
        "缺少 binary 可执行检查, 只 check 目录会 open 半成品"
    )
    # 缺失时要调 builder 重建
    assert 'build_menubar_app.sh' in script
