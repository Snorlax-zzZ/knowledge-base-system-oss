"""`/setup` 页面向系统配置端点发送的模式切换契约。"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP_HTML = ROOT / "app" / "static" / "setup" / "index.html"
CONFIRM_LINE = 'confirm_reindex: "I-CONFIRM-REINDEX"'


def _source_block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_setup_skip_sends_reindex_confirmation():
    source = SETUP_HTML.read_text(encoding="utf-8")
    block = _source_block(source, 'if (mode === "skip")', 'if (mode === "external")')
    assert CONFIRM_LINE in block
    assert "embedding_enabled: false" in block


def test_setup_external_sends_reindex_confirmation():
    source = SETUP_HTML.read_text(encoding="utf-8")
    block = _source_block(
        source,
        'document.getElementById("btn-save-external")',
        '// === 第 3 步:安装',
    )
    assert CONFIRM_LINE in block
    assert "embedding_enabled: true" in block
