"""控制台与独立设置页的 LLM 输出长度交互契约。"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_HTML = ROOT / "app" / "static" / "console" / "index.html"
CONSOLE_JS = ROOT / "app" / "static" / "console" / "console.js"
SETTINGS_HTML = ROOT / "app" / "static" / "settings" / "index.html"


def test_console_has_auto_toggle_and_disables_manual_token_input():
    html = CONSOLE_HTML.read_text(encoding="utf-8")
    js = CONSOLE_JS.read_text(encoding="utf-8")

    assert 'id="cfg_llm_max_tokens_auto"' in html
    assert "自动控制最大输出" in html
    assert "cfg_llm_max_tokens_auto" in js
    assert "cfg_llm_max_tokens" in js
    assert ".disabled" in js
    assert "llm_max_tokens_auto" in js


def test_console_surfaces_length_truncation_warning():
    html = CONSOLE_HTML.read_text(encoding="utf-8")
    js = CONSOLE_JS.read_text(encoding="utf-8")

    assert 'id="ask-answer-warning"' in html
    assert "res.truncated" in js
    assert "回答可能不完整" in js
    assert "configCache?.llm_max_tokens_auto" in js
    assert "当前已是自动模式" in js
    assert "启用自动控制或调大 Max Tokens" in js


def test_standalone_settings_has_auto_toggle_and_disabled_state_logic():
    html = SETTINGS_HTML.read_text(encoding="utf-8")

    assert 'id="llm_max_tokens_auto"' in html
    assert "自动控制最大输出" in html
    assert '"llm_max_tokens_auto"' in html
    assert "llm_max_tokens.disabled" in html
