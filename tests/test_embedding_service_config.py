"""验证 system_config 表新增 embedding_service_* 5 字段的读写与默认值。

覆盖：默认值（老用户无感升级 mode=disabled）/ 往返写读 / 非法值回落 /
managed 布尔转换。对应 openspec embedded-embedding-service v1.2 配置变更。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.repository_sqlite import SqliteKnowledgeRepo


@pytest.fixture()
def repo(tmp_path) -> SqliteKnowledgeRepo:
    return SqliteKnowledgeRepo(str(tmp_path / "kb.db"))


def _base_payload() -> dict:
    # upsert 要求 api_base_url / grafana_url 非空。
    return {
        "api_base_url": "http://127.0.0.1:18000",
        "grafana_url": "http://127.0.0.1:3000",
    }


class TestDefaults:
    def test_fresh_db_defaults(self, repo: SqliteKnowledgeRepo) -> None:
        cfg = repo.get_system_config()
        assert cfg["embedding_service_mode"] == "disabled"
        assert cfg["embedding_service_managed"] is False
        assert cfg["embedding_service_model_id"] == ""
        assert cfg["embedding_service_port"] == 0
        assert cfg["embedding_service_device"] == "cpu"


class TestRoundTrip:
    def test_write_read_local_mode(self, repo: SqliteKnowledgeRepo) -> None:
        payload = _base_payload()
        payload.update({
            "embedding_service_mode": "local",
            "embedding_service_managed": True,
            "embedding_service_model_id": "bge-m3",
            "embedding_service_port": 7687,
            "embedding_service_device": "cpu",
        })
        repo.upsert_system_config(payload)
        cfg = repo.get_system_config()
        assert cfg["embedding_service_mode"] == "local"
        assert cfg["embedding_service_managed"] is True
        assert cfg["embedding_service_model_id"] == "bge-m3"
        assert cfg["embedding_service_port"] == 7687
        assert cfg["embedding_service_device"] == "cpu"

    def test_managed_bool_conversion(self, repo: SqliteKnowledgeRepo) -> None:
        payload = _base_payload()
        payload["embedding_service_managed"] = False
        repo.upsert_system_config(payload)
        assert repo.get_system_config()["embedding_service_managed"] is False

    @pytest.mark.parametrize(
        ("mode", "submitted_managed", "expected_managed"),
        [
            ("local", False, True),
            ("external", True, False),
            ("disabled", True, False),
        ],
    )
    def test_upsert_enforces_managed_from_mode(
        self,
        repo: SqliteKnowledgeRepo,
        mode: str,
        submitted_managed: bool,
        expected_managed: bool,
    ) -> None:
        """直接调用 repository 也不能持久化矛盾的 mode/managed 组合。"""
        payload = _base_payload()
        payload.update({
            "embedding_service_mode": mode,
            "embedding_service_managed": submitted_managed,
        })

        cfg = repo.upsert_system_config(payload)

        assert cfg["embedding_service_mode"] == mode
        assert cfg["embedding_service_managed"] is expected_managed


class TestManagedModeMigration:
    @pytest.mark.parametrize(
        ("mode", "historical_managed", "expected_managed"),
        [
            ("local", 0, 1),
            ("external", 1, 0),
            ("disabled", 1, 0),
        ],
    )
    def test_reopen_repairs_historical_mismatch_bidirectionally_and_idempotently(
        self,
        tmp_path,
        mode: str,
        historical_managed: int,
        expected_managed: int,
    ) -> None:
        db_path = tmp_path / f"historical-{mode}.db"
        repo = SqliteKnowledgeRepo(str(db_path))
        repo.upsert_system_config(_base_payload())

        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE managed_migration_audit (updates INTEGER NOT NULL);
                INSERT INTO managed_migration_audit (updates) VALUES (0);
                """
            )
            conn.execute(
                "UPDATE system_config "
                "SET embedding_service_mode = ?, embedding_service_managed = ? "
                "WHERE id = 1",
                (mode, historical_managed),
            )
            conn.executescript(
                """
                CREATE TRIGGER count_managed_migration
                AFTER UPDATE OF embedding_service_managed ON system_config
                BEGIN
                    UPDATE managed_migration_audit SET updates = updates + 1;
                END;
                """
            )

        SqliteKnowledgeRepo(str(db_path))
        with sqlite3.connect(db_path) as conn:
            first_value = conn.execute(
                "SELECT embedding_service_managed FROM system_config WHERE id = 1"
            ).fetchone()[0]
            first_updates = conn.execute(
                "SELECT updates FROM managed_migration_audit"
            ).fetchone()[0]

        SqliteKnowledgeRepo(str(db_path))
        with sqlite3.connect(db_path) as conn:
            second_value = conn.execute(
                "SELECT embedding_service_managed FROM system_config WHERE id = 1"
            ).fetchone()[0]
            second_updates = conn.execute(
                "SELECT updates FROM managed_migration_audit"
            ).fetchone()[0]

        assert first_value == expected_managed
        assert second_value == expected_managed
        assert first_updates == 1
        assert second_updates == first_updates


class TestInvalidFallback:
    def test_invalid_mode_falls_back_disabled(self, repo: SqliteKnowledgeRepo) -> None:
        payload = _base_payload()
        payload["embedding_service_mode"] = "bogus"
        repo.upsert_system_config(payload)
        assert repo.get_system_config()["embedding_service_mode"] == "disabled"

    def test_invalid_device_falls_back_cpu(self, repo: SqliteKnowledgeRepo) -> None:
        payload = _base_payload()
        payload["embedding_service_device"] = "tpu"
        repo.upsert_system_config(payload)
        assert repo.get_system_config()["embedding_service_device"] == "cpu"

    def test_existing_fields_unaffected(self, repo: SqliteKnowledgeRepo) -> None:
        # 加 5 字段不能破坏既有 embedding_* / rerank_* 字段往返。
        payload = _base_payload()
        payload.update({
            "embedding_enabled": True,
            "embedding_model": "text-embedding-3-small",
            "embedding_dim": 1536,
        })
        repo.upsert_system_config(payload)
        cfg = repo.get_system_config()
        assert cfg["embedding_enabled"] is True
        assert cfg["embedding_model"] == "text-embedding-3-small"
        assert cfg["embedding_dim"] == 1536
