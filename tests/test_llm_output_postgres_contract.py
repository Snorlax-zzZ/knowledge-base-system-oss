"""Postgres 的 LLM 输出模式迁移与 upsert SQL 契约。"""
from __future__ import annotations

from typing import Any

from app.repository_postgres import PostgresKnowledgeRepo


class _Cursor:
    def __init__(self, statements: list[tuple[str, Any]], row: Any = None) -> None:
        self.statements = statements
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, statements: list[tuple[str, Any]], row: Any = None) -> None:
        self.statements = statements
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.statements, self.row)


def _repo_with_fake_connection(
    statements: list[tuple[str, Any]],
    *,
    row: Any = None,
) -> PostgresKnowledgeRepo:
    repo = object.__new__(PostgresKnowledgeRepo)
    repo.database_url = "postgresql://unused"
    repo.vector_index = None
    repo._connect = lambda: _Connection(statements, row)  # type: ignore[method-assign]
    return repo


def test_postgres_migration_defaults_new_install_auto_and_upgrade_manual():
    statements: list[tuple[str, Any]] = []
    repo = _repo_with_fake_connection(statements)

    repo._ensure_schema_extensions()

    sql = [" ".join(statement.split()) for statement, _ in statements]
    create_system_config = next(
        statement
        for statement in sql
        if "CREATE TABLE IF NOT EXISTS system_config" in statement
    )
    add_output_mode = next(
        statement
        for statement in sql
        if "ADD COLUMN IF NOT EXISTS llm_max_tokens_auto" in statement
    )
    assert "llm_max_tokens_auto BOOLEAN NOT NULL DEFAULT TRUE" in create_system_config
    assert add_output_mode.endswith(
        "llm_max_tokens_auto BOOLEAN NOT NULL DEFAULT FALSE"
    )


def test_postgres_upsert_persists_output_mode_with_matching_parameters():
    statements: list[tuple[str, Any]] = []
    repo = _repo_with_fake_connection(statements)
    repo.get_system_config = lambda: {  # type: ignore[method-assign]
        "llm_max_tokens": 2345,
        "llm_max_tokens_auto": False,
    }

    result = repo.upsert_system_config(
        {
            "api_base_url": "http://127.0.0.1:18000",
            "grafana_url": "http://127.0.0.1:3000",
            "llm_max_tokens": 2345,
            "llm_max_tokens_auto": False,
        }
    )

    insert_sql, params = next(
        (statement, values)
        for statement, values in statements
        if "INSERT INTO system_config" in statement
    )
    assert "llm_max_tokens_auto" in insert_sql
    assert insert_sql.count("%s") == 27
    assert len(params) == 27
    assert params[12] is False
    assert result == {"llm_max_tokens": 2345, "llm_max_tokens_auto": False}
