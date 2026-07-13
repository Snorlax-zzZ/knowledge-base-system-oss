"""POST /v1/system/backup/import 路由测试：二次确认 / 端到端 / 错误分支。"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("QDRANT_LOCAL_PATH", str(tmp_path / "qdrant_local"))
    monkeypatch.setenv("VECTOR_ENABLED", "0")
    # 把 auto-backup 写到 tmp，不污染用户目录
    monkeypatch.setenv("KB_AUTO_BACKUP_ROOT", str(tmp_path / "auto-backup"))

    (tmp_path / "qdrant_local").mkdir()
    (tmp_path / "qdrant_local" / "stub.bin").write_bytes(b"x")

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[server]\nport = 18000\n", encoding="utf-8")
    monkeypatch.setenv("KB_CONFIG_TOML_PATH", str(cfg_path))

    from app.main import _repo_singleton_sqlite, _repo_singleton_postgres
    _repo_singleton_sqlite.cache_clear()
    _repo_singleton_postgres.cache_clear()

    from app.main import app
    from app.services.maintenance import get_maintenance_flag
    get_maintenance_flag().clear()  # 清理前测试可能遗留的 flag

    return TestClient(app)


def _build_pkg_bytes() -> bytes:
    """构造一个 sha256 自洽的最小合法备份包。"""
    db_content = b"sqlite-db-stub"
    sha = hashlib.sha256(db_content).hexdigest()
    manifest = {
        "schema_version": 1,
        "created_at": "2026-05-19T00:00:00Z",
        "backend": "sqlite",
        "host": "h",
        "knowledge_db_sha256": sha,
        "embedding": {"model": "", "dim": 0, "base_url": ""},
        "stats": {"items": 0, "versions": 0, "chunks": 0, "vectors": 0},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        m_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(m_bytes)
        tar.addfile(info, io.BytesIO(m_bytes))
        info = tarfile.TarInfo(name="data/knowledge.db")
        info.size = len(db_content)
        tar.addfile(info, io.BytesIO(db_content))
    return buf.getvalue()


def test_import_rejects_missing_confirm(client):
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "overwrite"},
        files={"file": ("x.tar.gz", b"dummy", "application/gzip")},
    )
    assert resp.status_code == 400
    assert "confirm" in resp.text.lower()


def test_import_rejects_weak_confirm(client):
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "overwrite", "confirm": "true"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert resp.status_code == 400


def test_import_rejects_mode_token_mismatch(client):
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "merge", "confirm": "I-CONFIRM-OVERWRITE"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert resp.status_code == 400


def test_import_rejects_unknown_mode(client):
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "delete-everything", "confirm": "I-CONFIRM-OVERWRITE"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert resp.status_code == 400


def test_import_merge_returns_501_in_p0(client):
    """merge 模式在 P0 范围内尚未实现，路由必须明确 501，不能默默成功。"""
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "merge", "confirm": "I-CONFIRM-MERGE"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert resp.status_code == 501


def test_import_rejects_postgres_backend(client, monkeypatch):
    monkeypatch.setenv("KB_BACKEND", "postgres")
    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
        files={"file": ("x.tar.gz", b"x", "application/gzip")},
    )
    assert resp.status_code == 501


def test_import_overwrite_happy_path(client):
    """合法包 + 合法 confirm → 200 + 返回 ok。"""
    # 先生成一个本地数据 + 真备份包，避免 sha 不匹配
    r = client.post("/v1/knowledge/items/upsert", json={
        "title": "src",
        "domain": "work",
        "project": "p",
        "type": "fact",
        "content_markdown": "c",
        "author": "wzt",
        "change_note": "init",
    })
    assert r.status_code == 200

    # export 一份当作 import 源
    export_resp = client.post("/v1/system/backup/export")
    assert export_resp.status_code == 200
    pkg_bytes = export_resp.content

    # 再 import
    import_resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
        files={"file": ("backup.tar.gz", pkg_bytes, "application/gzip")},
    )
    assert import_resp.status_code == 200, import_resp.text
    body = import_resp.json()
    assert body["ok"] is True
    assert body["mode"] == "overwrite"
    assert "auto_backup_path" in body


def test_import_sha256_mismatch_returns_400(client):
    """sha 不一致 → 400，且 maintenance flag 不留尾巴。"""
    from app.services.maintenance import get_maintenance_flag

    pkg = _build_pkg_bytes()
    # 改一字节让 sha 与 manifest 失配
    buf = io.BytesIO(pkg)
    with tarfile.open(fileobj=buf, mode="r:gz") as tar:
        members = tar.getmembers()

    # 简单构造：在原 pkg 末尾追加垃圾，破坏 db 内容
    bad_pkg = b""
    src = io.BytesIO(pkg)
    out = io.BytesIO()
    with tarfile.open(fileobj=src, mode="r:gz") as tar_in:
        with tarfile.open(fileobj=out, mode="w:gz") as tar_out:
            for m in tar_in.getmembers():
                data = tar_in.extractfile(m).read() if m.isfile() else b""
                if m.name == "data/knowledge.db":
                    data = data + b"TAMPER"
                    m.size = len(data)
                tar_out.addfile(m, io.BytesIO(data))
    bad_pkg = out.getvalue()

    resp = client.post(
        "/v1/system/backup/import",
        data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
        files={"file": ("bad.tar.gz", bad_pkg, "application/gzip")},
    )
    assert resp.status_code == 400, resp.text
    assert "sha256" in resp.text.lower()

    # 路由结束后 maintenance flag 应自动清除
    assert not get_maintenance_flag().is_active()


def test_import_blocked_when_maintenance_already_active(client):
    """另一 import 正在跑（flag 已置位）时新 import 应被 maintenance 中间件 503。"""
    from app.services.maintenance import MaintenanceReason, get_maintenance_flag
    flag = get_maintenance_flag()
    flag.clear()
    flag.set(MaintenanceReason.BACKUP_IMPORT, detail="simulated")
    try:
        resp = client.post(
            "/v1/system/backup/import",
            data={"mode": "overwrite", "confirm": "I-CONFIRM-OVERWRITE"},
            files={"file": ("x.tar.gz", b"x", "application/gzip")},
        )
        assert resp.status_code == 503
    finally:
        flag.clear()
