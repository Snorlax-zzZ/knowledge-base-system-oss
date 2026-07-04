"""BackupService.import_overwrite 测试：happy path / 回滚 / sha256 / schema / Qdrant 顺序。"""
from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def populated_repo_and_pkg(tmp_path):
    """构造一个 repo（含 3 条 item）+ 一个备份包（含 1 条 item）。"""
    from app.repository_sqlite import SqliteKnowledgeRepo
    from app.services.backup_service import BackupService

    db = tmp_path / "data" / "knowledge.db"
    qdrant = tmp_path / "data" / "qdrant_local"
    qdrant.mkdir(parents=True)
    (qdrant / "v.bin").write_bytes(b"local-vec")
    repo = SqliteKnowledgeRepo(sqlite_path=str(db), vector_index=None)
    for i in range(3):
        repo.upsert_item({
            "title": f"local-{i}",
            "domain": "work",
            "project": "p",
            "type": "fact",
            "content_markdown": f"local content {i}",
            "summary": "",
            "author": "test-user",
            "change_note": "",
        })

    backup_db = tmp_path / "backup_src" / "knowledge.db"
    backup_qdrant = tmp_path / "backup_src" / "qdrant_local"
    backup_qdrant.mkdir(parents=True)
    (backup_qdrant / "vb.bin").write_bytes(b"backup-vec")
    backup_repo = SqliteKnowledgeRepo(sqlite_path=str(backup_db), vector_index=None)
    backup_repo.upsert_item({
        "title": "backup-only",
        "domain": "work",
        "project": "p",
        "type": "fact",
        "content_markdown": "from backup",
        "summary": "",
        "author": "test-user",
        "change_note": "",
    })
    backup_svc = BackupService(
        repo=backup_repo,
        sqlite_path=str(backup_db),
        qdrant_local_path=str(backup_qdrant),
        on_qdrant_close=lambda: None,
        on_qdrant_reinit=lambda: None,
    )
    pkg_path = tmp_path / "backup.tar.gz"
    backup_svc.export_to(str(pkg_path))

    return repo, db, qdrant, pkg_path


def _make_service(repo, db, qdrant, order=None):
    from app.services.backup_service import BackupService

    def _close():
        if order is not None:
            order.append("close")

    def _reinit():
        if order is not None:
            order.append("reinit")

    return BackupService(
        repo=repo,
        sqlite_path=str(db),
        qdrant_local_path=str(qdrant),
        on_qdrant_close=_close,
        on_qdrant_reinit=_reinit,
    )


def _auto(db, qdrant, bak_root):
    from app.services.backup_service import AutoBackupService
    return AutoBackupService(
        sqlite_path=str(db),
        qdrant_local_path=str(qdrant),
        auto_backup_root=str(bak_root),
    )


# ---------------------------------------------------------------------------
# Task 3.2: happy path
# ---------------------------------------------------------------------------


def test_overwrite_replaces_all_items(tmp_path, populated_repo_and_pkg):
    repo, db, qdrant, pkg = populated_repo_and_pkg
    bak_root = tmp_path / "auto-backup"

    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, bak_root)
    result = svc.import_overwrite(package_path=str(pkg), auto_backup_service=auto_svc)

    # 注意：因为 SqliteKnowledgeRepo 使用 lru_cache 单例 + sqlite_path，
    # 我们这里通过新 repo 直接读 db 文件验证替换结果。
    from app.repository_sqlite import SqliteKnowledgeRepo
    fresh = SqliteKnowledgeRepo(sqlite_path=str(db), vector_index=None)
    with fresh._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item WHERE status='active'"
        ).fetchone()[0]
        titles = [r[0] for r in conn.execute(
            "SELECT title FROM knowledge_item WHERE status='active'"
        ).fetchall()]
    assert count == 1
    assert titles == ["backup-only"]

    assert result["ok"] is True
    assert result["mode"] == "overwrite"
    assert result["items_after"] == 1
    assert "auto_backup_path" in result
    assert Path(result["auto_backup_path"]).exists()


# ---------------------------------------------------------------------------
# Task 3.9: 审计日志
# ---------------------------------------------------------------------------


def test_overwrite_logs_op_with_required_fields(tmp_path, populated_repo_and_pkg, caplog):
    import logging

    repo, db, qdrant, pkg = populated_repo_and_pkg
    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, tmp_path / "auto")
    with caplog.at_level(logging.INFO, logger="app.services.backup_service"):
        svc.import_overwrite(str(pkg), auto_svc)

    relevant = [r for r in caplog.records if "op=backup_import" in r.getMessage()]
    assert relevant, f"expected backup_import log: {[r.getMessage() for r in caplog.records]}"
    msg = relevant[0].getMessage()
    assert "mode=overwrite" in msg
    assert "result=ok" in msg
    assert "auto_backup_path=" in msg


# ---------------------------------------------------------------------------
# Task 3.6: Qdrant close/reinit 顺序
# ---------------------------------------------------------------------------


def test_overwrite_calls_qdrant_close_and_reinit(tmp_path, populated_repo_and_pkg):
    repo, db, qdrant, pkg = populated_repo_and_pkg
    order: list[str] = []
    svc = _make_service(repo, db, qdrant, order=order)
    auto_svc = _auto(db, qdrant, tmp_path / "auto")
    svc.import_overwrite(str(pkg), auto_svc)
    assert order == ["close", "reinit"]


# ---------------------------------------------------------------------------
# Task 3.4: sha256 不匹配拒绝（不进双层防护）
# ---------------------------------------------------------------------------


def test_overwrite_rejects_sha256_mismatch(tmp_path, populated_repo_and_pkg):
    from app.services.backup_service import BackupImportError

    repo, db, qdrant, pkg = populated_repo_and_pkg

    # 解压后改 db 1 字节，再重新打包，使 sha256 与 manifest 不一致
    work = tmp_path / "tamper"
    work.mkdir()
    with tarfile.open(pkg, "r:gz") as tar:
        tar.extractall(work)
    tampered_db = work / "data" / "knowledge.db"
    raw = tampered_db.read_bytes()
    tampered_db.write_bytes(raw + b"\x00")

    tampered_pkg = tmp_path / "tampered.tar.gz"
    with tarfile.open(tampered_pkg, "w:gz") as tar:
        for item in sorted(work.rglob("*")):
            if item.is_file():
                tar.add(item, arcname=str(item.relative_to(work)))

    bak_root = tmp_path / "auto-backup-tamper"
    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, bak_root)

    with pytest.raises(BackupImportError, match="sha256 mismatch"):
        svc.import_overwrite(str(tampered_pkg), auto_svc)

    # 数据零变更
    from app.repository_sqlite import SqliteKnowledgeRepo
    fresh = SqliteKnowledgeRepo(sqlite_path=str(db), vector_index=None)
    with fresh._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item WHERE status='active'"
        ).fetchone()[0]
    assert count == 3

    # auto-backup 未生成（前置校验失败，不进双层防护）
    assert not bak_root.exists() or not any(bak_root.iterdir())


# ---------------------------------------------------------------------------
# Task 3.3: 回滚
# ---------------------------------------------------------------------------


def test_overwrite_rollback_on_restore_config_failure(tmp_path, populated_repo_and_pkg, monkeypatch):
    """步骤 8 (_restore_system_config_from) 失败时必须触发回滚（审计 #10）。"""
    from app.services.backup_service import BackupImportError, BackupService

    repo, db, qdrant, pkg = populated_repo_and_pkg
    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, tmp_path / "auto-backup")

    def boom(*a, **kw):
        raise RuntimeError("simulated system_config restore failure")

    monkeypatch.setattr(
        BackupService,
        "_restore_system_config_from",
        boom,
    )

    with pytest.raises(BackupImportError) as exc:
        svc.import_overwrite(str(pkg), auto_svc)
    # 必须 rolled_back，不能 server，也不能 client
    assert exc.value.kind in ("rolled_back", "rollback_partial")

    # 数据应回到初始 3 条
    from app.repository_sqlite import SqliteKnowledgeRepo
    fresh = SqliteKnowledgeRepo(sqlite_path=str(db), vector_index=None)
    with fresh._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item WHERE status='active'"
        ).fetchone()[0]
    assert count == 3


def test_overwrite_rollback_partial_kind_when_rollback_fails(tmp_path, populated_repo_and_pkg, monkeypatch):
    """步骤 7 失败 + 回滚自身也失败 → kind=rollback_partial（审计 #9）。"""
    from app.services.backup_service import BackupImportError, BackupService

    repo, db, qdrant, pkg = populated_repo_and_pkg
    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, tmp_path / "auto-backup")

    # 触发步骤 7 失败：让 clear_all_active_data 抛错
    original_clear = repo.__class__.clear_all_active_data
    def boom_clear(self):
        raise RuntimeError("simulated clear failure")
    monkeypatch.setattr(repo.__class__, "clear_all_active_data", boom_clear)

    # 同时让回滚自身的 shutil.copy2 抛错
    real_copy = shutil.copy2
    def boom_copy(src, dst, *a, **kw):
        if ".pre-restore.bak" in str(src):
            raise OSError("simulated rollback copy failure")
        return real_copy(src, dst, *a, **kw)
    monkeypatch.setattr(
        "app.services.backup_service.shutil.copy2",
        boom_copy,
    )

    with pytest.raises(BackupImportError) as exc:
        svc.import_overwrite(str(pkg), auto_svc)
    assert exc.value.kind == "rollback_partial"
    assert "auto-backup" in str(exc.value)  # 提示用户走外层兜底


def test_overwrite_rejects_incompatible_backend(tmp_path, populated_repo_and_pkg):
    """manifest.backend 不是 sqlite 时拒绝（审计 #13）。"""
    import json
    import io

    from app.services.backup_service import BackupImportError

    repo, db, qdrant, pkg = populated_repo_and_pkg

    # 解压 → 改 manifest.backend → 重打包（保留 sha 自洽：sha 算 db 内容，
    # backend 是元数据不进 sha）
    work = tmp_path / "tamper-backend"
    work.mkdir()
    with tarfile.open(pkg, "r:gz") as tar:
        tar.extractall(work)
    manifest_path = work / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["backend"] = "postgres"
    manifest_path.write_text(json.dumps(manifest))

    tampered = tmp_path / "tampered-backend.tar.gz"
    with tarfile.open(tampered, "w:gz") as tar:
        for item in sorted(work.rglob("*")):
            if item.is_file():
                tar.add(item, arcname=str(item.relative_to(work)))

    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, tmp_path / "auto-backup-incompat")
    with pytest.raises(BackupImportError) as exc:
        svc.import_overwrite(str(tampered), auto_svc)
    assert exc.value.kind == "client"
    assert "postgres" in str(exc.value)


def test_overwrite_rollback_on_copy_failure(tmp_path, populated_repo_and_pkg, monkeypatch):
    """步骤 7 还原 qdrant 时抛错 → 回滚到原数据。"""
    from app.services.backup_service import BackupImportError

    repo, db, qdrant, pkg = populated_repo_and_pkg
    bak_root = tmp_path / "auto-backup"

    svc = _make_service(repo, db, qdrant)
    auto_svc = _auto(db, qdrant, bak_root)

    real_copytree = shutil.copytree
    call_count = {"n": 0}

    def failing_copytree(src, dst, *a, **kw):
        call_count["n"] += 1
        # 第 1 次：AutoBackup snapshot 内的 qdrant cp → 放行
        # 第 2 次：内层 .pre-restore-qdrant cp → 放行
        # 第 3 次：还原 qdrant 时 → 抛错
        if call_count["n"] >= 3 and "qdrant_local" in str(dst) and ".pre-restore" not in str(dst):
            raise OSError("simulated disk failure")
        return real_copytree(src, dst, *a, **kw)

    monkeypatch.setattr(
        "app.services.backup_service.shutil.copytree",
        failing_copytree,
    )

    with pytest.raises(BackupImportError, match="rolled back"):
        svc.import_overwrite(str(pkg), auto_svc)

    # 数据应回滚到初始 3 条
    from app.repository_sqlite import SqliteKnowledgeRepo
    fresh = SqliteKnowledgeRepo(sqlite_path=str(db), vector_index=None)
    with fresh._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_item WHERE status='active'"
        ).fetchone()[0]
        titles = sorted(
            r[0] for r in conn.execute(
                "SELECT title FROM knowledge_item WHERE status='active'"
            ).fetchall()
        )
    assert count == 3
    assert titles == ["local-0", "local-1", "local-2"]

    # 内层 .pre-restore 已被消耗
    assert not (db.parent / ".pre-restore.bak").exists()
    assert not (db.parent / ".pre-restore-qdrant").exists()
