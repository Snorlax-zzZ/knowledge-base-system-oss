"""BackupService.export 测试：tar 结构 / manifest / sha256 / Qdrant close-reinit 顺序。"""
from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest


@pytest.fixture
def repo_with_data(tmp_path):
    from app.repository_sqlite import SqliteKnowledgeRepo

    db_path = tmp_path / "kb.db"
    qdrant_dir = tmp_path / "qdrant_local"
    qdrant_dir.mkdir()
    (qdrant_dir / "dummy.bin").write_bytes(b"vector data")
    (qdrant_dir / "sub").mkdir()
    (qdrant_dir / "sub" / "more.bin").write_bytes(b"more")

    repo = SqliteKnowledgeRepo(sqlite_path=str(db_path), vector_index=None)
    repo.upsert_item({
        "title": "t",
        "domain": "work",
        "project": "proj-a",
        "type": "fact",
        "content_markdown": "a paragraph",
        "summary": "",
        "author": "wzt",
        "change_note": "",
    })
    return repo, db_path, qdrant_dir


def _make_service(repo, db_path, qdrant_dir, order=None):
    from app.services.backup_service import BackupService

    def _close():
        if order is not None:
            order.append("close")

    def _reinit():
        if order is not None:
            order.append("reinit")

    return BackupService(
        repo=repo,
        sqlite_path=str(db_path),
        qdrant_local_path=str(qdrant_dir),
        on_qdrant_close=_close,
        on_qdrant_reinit=_reinit,
    )


def test_export_writes_manifest_and_data(tmp_path, repo_with_data):
    repo, db_path, qdrant_dir = repo_with_data
    out_path = tmp_path / "out.tar.gz"
    svc = _make_service(repo, db_path, qdrant_dir)
    svc.export_to(str(out_path))

    assert out_path.exists()
    with tarfile.open(out_path, "r:gz") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert "data/knowledge.db" in names
        assert any(n.startswith("data/qdrant_local/") for n in names), (
            f"qdrant_local should be archived: {names}"
        )

        manifest_raw = tar.extractfile("manifest.json").read()
        manifest = json.loads(manifest_raw)
        assert manifest["schema_version"] == 1
        assert manifest["backend"] == "sqlite"
        assert manifest["stats"]["items"] >= 1
        # sha256 必须等于 tar 内 db 的 sha256
        db_bytes = tar.extractfile("data/knowledge.db").read()
        assert manifest["knowledge_db_sha256"] == hashlib.sha256(db_bytes).hexdigest()
        assert len(manifest["knowledge_db_sha256"]) == 64


def test_export_redacted_config(tmp_path, repo_with_data):
    repo, db_path, qdrant_dir = repo_with_data
    repo.upsert_system_config({
        "api_base_url": "http://127.0.0.1:18000",
        "service_port": 18000,
        "grafana_url": "http://127.0.0.1:3000",
        "ui_theme": "neo",
        "llm_enabled": True,
        "llm_api_key": "SECRET-LLM-KEY",
        "llm_base_url": "https://x",
        "llm_model": "m",
        "llm_timeout_sec": 30,
        "llm_temperature": 0.2,
        "llm_max_tokens": 1024,
        "embedding_enabled": True,
        "embedding_api_key": "SECRET-EMB-KEY",
        "embedding_base_url": "",
        "embedding_model": "M",
        "embedding_dim": 384,
        "embedding_timeout_sec": 20,
        "rerank_enabled": False,
        "rerank_api_key": "",
        "rerank_base_url": "",
        "rerank_model": "",
        "rerank_path": "/rerank",
        "rerank_timeout_sec": 20,
        "enrichment_enabled": False,
    })

    out_path = tmp_path / "out.tar.gz"
    svc = _make_service(repo, db_path, qdrant_dir)
    svc.export_to(str(out_path))

    with tarfile.open(out_path, "r:gz") as tar:
        assert "meta/system_config_redacted.json" in tar.getnames()
        raw = tar.extractfile("meta/system_config_redacted.json").read()
        cfg = json.loads(raw)
        assert cfg["llm_api_key"] == "***REDACTED***"
        assert cfg["embedding_api_key"] == "***REDACTED***"
        # 空 key 保持空（无需脱敏 / 也可脱敏，两种都接受）
        assert cfg["rerank_api_key"] in ("", "***REDACTED***")


def test_export_calls_qdrant_close_before_reinit(tmp_path, repo_with_data):
    repo, db_path, qdrant_dir = repo_with_data
    order: list[str] = []
    svc = _make_service(repo, db_path, qdrant_dir, order=order)
    svc.export_to(str(tmp_path / "out.tar.gz"))
    assert order == ["close", "reinit"]


def test_export_calls_reinit_even_on_failure(tmp_path, repo_with_data, monkeypatch):
    """tar 写入异常时仍必须 reinit qdrant，否则后续服务永久不可用。"""
    repo, db_path, qdrant_dir = repo_with_data
    order: list[str] = []
    svc = _make_service(repo, db_path, qdrant_dir, order=order)

    import tarfile as tf

    real_open = tf.open

    def _boom(*a, **kw):
        # 让 tar 阶段炸（在 close 之后才会进入此分支）
        order.append("tar_open")
        raise OSError("simulated tar failure")

    monkeypatch.setattr("app.services.backup_service.tarfile.open", _boom)
    with pytest.raises(OSError, match="simulated tar failure"):
        svc.export_to(str(tmp_path / "out.tar.gz"))
    assert "close" in order
    assert "reinit" in order, "异常路径仍必须 reinit qdrant"
    assert order.index("reinit") > order.index("close")


def test_export_sha256_computed_on_copied_file_not_live_db(tmp_path, repo_with_data, monkeypatch):
    """sha256 必须基于 cp 后的临时文件，不是 live db。"""
    repo, db_path, qdrant_dir = repo_with_data
    out_path = tmp_path / "out.tar.gz"
    svc = _make_service(repo, db_path, qdrant_dir)

    import shutil as _shutil
    real_copy = _shutil.copy2

    def tampering_copy(src, dst, *a, **kw):
        result = real_copy(src, dst, *a, **kw)
        # 模拟 cp 后 live db 被改（不应影响备份内 db）
        if str(src) == str(db_path):
            with open(src, "ab") as f:
                f.write(b"TAMPER")
        return result

    monkeypatch.setattr("app.services.backup_service.shutil.copy2", tampering_copy)

    svc.export_to(str(out_path))

    with tarfile.open(out_path, "r:gz") as tar:
        db_bytes = tar.extractfile("data/knowledge.db").read()
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert hashlib.sha256(db_bytes).hexdigest() == manifest["knowledge_db_sha256"]
        assert b"TAMPER" not in db_bytes, "备份内的 db 不应包含 cp 后 live 写入的字节"


def test_export_logs_op_and_redacts(tmp_path, repo_with_data, caplog):
    """审计日志含 op / result / stats，且不带凭证。"""
    import logging

    repo, db_path, qdrant_dir = repo_with_data
    repo.upsert_system_config({
        "api_base_url": "http://127.0.0.1:18000",
        "service_port": 18000,
        "grafana_url": "http://127.0.0.1:3000",
        "ui_theme": "neo",
        "llm_enabled": True,
        "llm_api_key": "SECRET-LLM-KEY",
        "llm_base_url": "",
        "llm_model": "",
        "llm_timeout_sec": 30,
        "llm_temperature": 0.2,
        "llm_max_tokens": 1024,
        "embedding_enabled": False,
        "embedding_api_key": "",
        "embedding_base_url": "",
        "embedding_model": "",
        "embedding_dim": 384,
        "embedding_timeout_sec": 20,
        "rerank_enabled": False,
        "rerank_api_key": "",
        "rerank_base_url": "",
        "rerank_model": "",
        "rerank_path": "/rerank",
        "rerank_timeout_sec": 20,
        "enrichment_enabled": False,
    })

    svc = _make_service(repo, db_path, qdrant_dir)
    out = tmp_path / "out.tar.gz"
    with caplog.at_level(logging.INFO, logger="app.services.backup_service"):
        svc.export_to(str(out))

    relevant = [r for r in caplog.records if "op=backup_export" in r.getMessage()]
    assert relevant, f"expected backup_export log line, got: {[r.getMessage() for r in caplog.records]}"
    msg = relevant[0].getMessage()
    assert "SECRET-LLM-KEY" not in msg
    assert "items=" in msg
    assert "result=ok" in msg


def test_export_includes_qdrant_subdirectories(tmp_path, repo_with_data):
    """子目录文件也要包含进 tar。"""
    repo, db_path, qdrant_dir = repo_with_data
    out_path = tmp_path / "out.tar.gz"
    svc = _make_service(repo, db_path, qdrant_dir)
    svc.export_to(str(out_path))

    with tarfile.open(out_path, "r:gz") as tar:
        names = tar.getnames()
        assert any("sub/more.bin" in n for n in names), names
