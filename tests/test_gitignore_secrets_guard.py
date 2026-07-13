"""锁定 .gitignore 中的关键保护项。

任何一项被误删都会导致本机密钥 / 数据 / 日志 / build 产物流入 git。
本测试由 v1.3.13 收口阶段落地兜底。
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = ROOT / ".gitignore"


@pytest.fixture(scope="module")
def gitignore_lines() -> list[str]:
    return [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.parametrize(
    "pattern",
    [
        # 密钥 / 环境变量
        ".env",
        ".env.local",
        ".env.*.local",
        "**/.env",
        "**/.env.*",
        # 运行时数据 (含 DB 里的 api_key / owner_token)
        "data/",
        "runtime/",
        "logs/",
        "backups/",
        "exports/",
        "*.log",
        # 虚拟环境 / build 产物 (含临时的敏感缓存)
        ".venv/",
        "venv/",
        "bin/",
        "build/",
        "dist/",
    ],
)
def test_gitignore_contains_required_secret_pattern(
    gitignore_lines: list[str], pattern: str,
) -> None:
    """关键保护项必须在 .gitignore 里。缺失 = 潜在密钥泄漏。"""
    assert pattern in gitignore_lines, (
        f".gitignore 缺失关键保护 pattern: {pattern!r}; "
        "误删这一行会导致对应目录 / 文件被 git 追踪。"
    )


def test_gitignore_does_not_exclude_version_file(gitignore_lines: list[str]) -> None:
    """VERSION 文件已从 gitignore 移出加入 git 追踪 (v1.3.13 决策),
    保证 dev / build / installer 三处版本号一致。误加回 gitignore 会破坏
    test_release_contract 的四处一致断言。"""
    assert "/VERSION" not in gitignore_lines, (
        "VERSION 文件不应 gitignore (v1.3.13 决策), 否则 dev 运行时 APP_VERSION "
        "与 installer 版本会漂移。"
    )
    assert "VERSION" not in gitignore_lines, (
        "同上, 不带前缀的 VERSION pattern 也应缺席。"
    )
