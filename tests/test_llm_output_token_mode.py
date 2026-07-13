"""LLM 输出 Token 自动模式、升级迁移与截断可观测性。"""
from __future__ import annotations

import sqlite3
from typing import Any

from app.model_settings import llm_config_from_env
from app.repository_sqlite import SqliteKnowledgeRepo
from app.schemas import AskRequest
from app.service import KnowledgeService
from app.services.backup_service import BackupService


def _config_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "api_base_url": "http://127.0.0.1:18000",
        "service_port": 18000,
        "grafana_url": "http://127.0.0.1:3000",
        "ui_theme": "neo",
        "llm_max_tokens": 1024,
    }
    payload.update(overrides)
    return payload


def test_new_sqlite_database_defaults_to_automatic_output_limit(tmp_path):
    repo = SqliteKnowledgeRepo(sqlite_path=str(tmp_path / "new.db"))

    assert repo.get_system_config()["llm_max_tokens_auto"] is True


def test_legacy_sqlite_migration_preserves_token_value_and_manual_mode(tmp_path):
    db_path = tmp_path / "legacy.db"
    repo = SqliteKnowledgeRepo(sqlite_path=str(db_path))
    repo.upsert_system_config(_config_payload(llm_max_tokens=2345))

    # 测试同时兼容 RED 前（列尚不存在）与 GREEN 后（先构造新表再移除新列）：
    # 重新初始化时必须把“缺少模式列”识别为升级库，而不是新安装。
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(system_config)").fetchall()
        }
        if "llm_max_tokens_auto" in columns:
            conn.execute("ALTER TABLE system_config DROP COLUMN llm_max_tokens_auto")
        conn.execute("UPDATE system_config SET llm_max_tokens = 2345 WHERE id = 1")

    upgraded = SqliteKnowledgeRepo(sqlite_path=str(db_path))
    config = upgraded.get_system_config()

    assert config["llm_max_tokens"] == 2345
    assert config["llm_max_tokens_auto"] is False


def test_restoring_legacy_backup_keeps_saved_token_limit_in_manual_mode(tmp_path):
    backup_db = tmp_path / "legacy-backup.db"
    with sqlite3.connect(backup_db) as conn:
        conn.execute(
            "CREATE TABLE system_config ("
            "id INTEGER PRIMARY KEY, "
            "api_base_url TEXT NOT NULL, "
            "grafana_url TEXT NOT NULL, "
            "llm_max_tokens INTEGER NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO system_config "
            "(id, api_base_url, grafana_url, llm_max_tokens) "
            "VALUES (1, 'http://127.0.0.1:18000', "
            "'http://127.0.0.1:3000', 2345)"
        )

    restored_db = tmp_path / "restored.db"
    restored_repo = SqliteKnowledgeRepo(sqlite_path=str(restored_db))
    service = BackupService(
        repo=restored_repo,
        sqlite_path=str(restored_db),
        qdrant_local_path=str(tmp_path / "qdrant"),
        on_qdrant_close=lambda: None,
        on_qdrant_reinit=lambda: None,
    )

    service._restore_system_config_from(backup_db)
    config = restored_repo.get_system_config()

    assert config["llm_max_tokens"] == 2345
    assert config["llm_max_tokens_auto"] is False


def test_env_defaults_auto_but_preserves_explicit_legacy_token_limit(monkeypatch):
    monkeypatch.delenv("KB_LLM_MAX_TOKENS_AUTO", raising=False)
    monkeypatch.delenv("KB_LLM_MAX_TOKENS", raising=False)
    assert llm_config_from_env().max_tokens_auto is True

    monkeypatch.setenv("KB_LLM_MAX_TOKENS", "3072")
    legacy = llm_config_from_env()
    assert legacy.max_tokens == 3072
    assert legacy.max_tokens_auto is False

    monkeypatch.setenv("KB_LLM_MAX_TOKENS_AUTO", "true")
    assert llm_config_from_env().max_tokens_auto is True


def test_env_only_llm_config_is_used_when_sqlite_has_no_saved_settings(
    tmp_path,
    monkeypatch,
):
    repo = SqliteKnowledgeRepo(sqlite_path=str(tmp_path / "env-only.db"))
    monkeypatch.setattr(
        repo,
        "search_chunks_for_ask",
        lambda **_: [
            {
                "knowledge_item_id": "item-env",
                "title": "Environment",
                "chunk_text": "Context",
                "version": 1,
            }
        ],
    )
    monkeypatch.setenv("KB_LLM_ENABLED", "true")
    monkeypatch.setenv("KB_LLM_API_KEY", "env-key")
    monkeypatch.setenv("KB_LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("KB_LLM_MODEL", "env-model")
    monkeypatch.setenv("KB_LLM_MAX_TOKENS", "3072")
    monkeypatch.delenv("KB_LLM_MAX_TOKENS_AUTO", raising=False)

    capture: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.service.httpx.Client",
        lambda **_: _CapturingClient(capture),
    )

    response = KnowledgeService(repo).ask(
        AskRequest(question="q", domain="work")
    )

    assert response["llm_available"] is True
    assert capture["url"] == "https://env.example/v1/chat/completions"
    assert capture["payload"]["model"] == "env-model"
    assert capture["payload"]["max_tokens"] == 3072


def test_saved_sqlite_llm_config_keeps_precedence_over_environment(
    tmp_path,
    monkeypatch,
):
    repo = SqliteKnowledgeRepo(sqlite_path=str(tmp_path / "saved.db"))
    repo.upsert_system_config(
        _config_payload(
            llm_enabled=True,
            llm_api_key="saved-key",
            llm_base_url="https://saved.example/v1",
            llm_model="saved-model",
            llm_max_tokens=2345,
            llm_max_tokens_auto=False,
        )
    )
    monkeypatch.setattr(
        repo,
        "search_chunks_for_ask",
        lambda **_: [
            {
                "knowledge_item_id": "item-saved",
                "title": "Saved",
                "chunk_text": "Context",
                "version": 1,
            }
        ],
    )
    monkeypatch.setenv("KB_LLM_ENABLED", "true")
    monkeypatch.setenv("KB_LLM_API_KEY", "env-key")
    monkeypatch.setenv("KB_LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("KB_LLM_MODEL", "env-model")
    monkeypatch.setenv("KB_LLM_MAX_TOKENS", "3072")
    monkeypatch.setenv("KB_LLM_MAX_TOKENS_AUTO", "true")

    capture: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.service.httpx.Client",
        lambda **_: _CapturingClient(capture),
    )

    response = KnowledgeService(repo).ask(
        AskRequest(question="q", domain="work")
    )

    assert response["llm_available"] is True
    assert capture["url"] == "https://saved.example/v1/chat/completions"
    assert capture["payload"]["model"] == "saved-model"
    assert capture["payload"]["max_tokens"] == 2345


class _FakeResponse:
    def __init__(self, *, content: str, finish_reason: str | None) -> None:
        self._body = {
            "choices": [
                {
                    "message": {"content": content},
                    "finish_reason": finish_reason,
                }
            ]
        }

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _CapturingClient:
    def __init__(
        self,
        capture: dict[str, Any],
        *,
        content: str = "complete answer",
        finish_reason: str | None = "stop",
    ) -> None:
        self.capture = capture
        self.response = _FakeResponse(
            content=content,
            finish_reason=finish_reason,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
        self.capture.update({"url": url, "headers": headers, "payload": json})
        return self.response


def _llm_cfg(*, auto: bool, max_tokens: int = 1024) -> dict[str, Any]:
    return {
        "api_key": "test-key",
        "base_url": "https://llm.example/v1",
        "model": "test-model",
        "timeout_sec": 30.0,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "max_tokens_auto": auto,
    }


def test_call_llm_automatic_mode_omits_project_side_token_limit(monkeypatch):
    capture: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.service.httpx.Client",
        lambda **_: _CapturingClient(capture),
    )

    result = KnowledgeService._call_llm(
        _llm_cfg(auto=True),
        "question",
        [{"title": "T", "chunk_text": "context"}],
    )

    assert "max_tokens" not in capture["payload"]
    assert result == {"answer": "complete answer", "finish_reason": "stop"}


def test_call_llm_manual_mode_sends_saved_limit(monkeypatch):
    capture: dict[str, Any] = {}
    monkeypatch.setattr(
        "app.service.httpx.Client",
        lambda **_: _CapturingClient(capture),
    )

    result = KnowledgeService._call_llm(
        _llm_cfg(auto=False, max_tokens=4096),
        "question",
        [{"title": "T", "chunk_text": "context"}],
    )

    assert capture["payload"]["max_tokens"] == 4096
    assert result["finish_reason"] == "stop"


class _AskRepo:
    def get_system_config(self) -> dict[str, Any]:
        return {
            "llm_enabled": True,
            "llm_api_key": "test-key",
            "llm_base_url": "https://llm.example/v1",
            "llm_model": "test-model",
            "llm_timeout_sec": 30.0,
            "llm_temperature": 0.2,
            "llm_max_tokens": 1024,
            "llm_max_tokens_auto": False,
        }

    def search_chunks_for_ask(self, **_: Any) -> list[dict[str, Any]]:
        return [
            {
                "knowledge_item_id": "item-1",
                "title": "Title",
                "chunk_text": "Context",
                "version": 1,
            }
        ]


def test_ask_marks_finish_reason_length_as_truncated(monkeypatch):
    monkeypatch.setattr(
        KnowledgeService,
        "_call_llm",
        staticmethod(
            lambda *_args, **_kwargs: {
                "answer": "partial answer",
                "finish_reason": "length",
            }
        ),
    )

    response = KnowledgeService(_AskRepo()).ask(
        AskRequest(question="q", domain="work")
    )

    assert response["answer"] == "partial answer"
    assert response["finish_reason"] == "length"
    assert response["truncated"] is True
