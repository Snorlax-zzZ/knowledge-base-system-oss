"""备份导出与导入服务。

本模块当前负责：
- BackupService.export_to(out_path)：把 SQLite + Qdrant + system_config 打成 tar.gz

后续 chunk 会加 import_overwrite / import_merge 与 AutoBackupService。

Qdrant 文件锁（审计 #2）：cp qdrant_local 之前必须 close client，操作完成后 reinit。
通过构造时注入 on_qdrant_close / on_qdrant_reinit 回调解耦，让上层（main.py）决定
具体如何 close 与重新装配 Qdrant client。reinit 写在 finally，确保异常路径也恢复。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.services.disk_space import require_disk_space
from app.services.manifest import (
    CURRENT_SCHEMA_VERSION,
    ManifestParseError,
    parse_manifest,
)


logger = logging.getLogger(__name__)


class BackupImportError(RuntimeError):
    """import 失败的统一异常基类。

    `kind` 用于路由层映射 HTTP 状态码（避免依赖错误消息字符串关键词，审计 #11）：

    - ``"client"``：客户端可纠正（包损坏 / sha256 不匹配 / schema_version 不识别 /
      manifest 缺字段 / backend 不匹配 / mode 不支持）→ HTTP 400
    - ``"rolled_back"``：服务端在 restore 阶段失败，已用 .pre-restore 副本回滚 →
      HTTP 500（业务可重试或换包）
    - ``"rollback_partial"``：服务端失败 + 回滚自身也失败（数据可能处于损坏态，
      用户应走外层 auto-backup 兜底）→ HTTP 500，告警
    - ``"server"``：其他服务端错误 → HTTP 500
    """

    def __init__(self, message: str, *, kind: str = "server") -> None:
        super().__init__(message)
        self.kind = kind


_REDACTED_FIELDS = (
    "llm_api_key",
    "embedding_api_key",
    "rerank_api_key",
)


def _redact_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """复制并把敏感字段替换为 ***REDACTED***。空值保持空。"""
    out = dict(cfg)
    for k in _REDACTED_FIELDS:
        if out.get(k):
            out[k] = "***REDACTED***"
    return out


def _sha256_of_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extractall_fallback(tar: tarfile.TarFile, dest: str | Path) -> None:
    """tarfile 老版本（无 filter='data'）的安全 extractall fallback（审计 #2）。

    逐成员校验：
    - 拒绝绝对路径与 ``..`` 越权
    - 拒绝符号链接（symlink / hardlink）—— 备份包内不应出现链接
    - realpath 解析后必须仍在 dest 子树内

    满足条件后再调 ``tar.extract(member, dest)`` 单独抽取。
    """
    dest_path = Path(dest).resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise BackupImportError(
                f"refuse to extract link member: {member.name}",
                kind="client",
            )
        name = member.name
        if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
            raise BackupImportError(
                f"refuse to extract member with unsafe path: {name}",
                kind="client",
            )
        target = (dest_path / name).resolve()
        try:
            target.relative_to(dest_path)
        except ValueError as e:
            raise BackupImportError(
                f"refuse to extract member outside destination: {name}",
                kind="client",
            ) from e
    # 二次循环再抽取，保证全员校验通过才落盘
    for member in tar.getmembers():
        tar.extract(member, dest_path)


def _dir_size(path: str | Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


class BackupService:
    """SQLite + 本地 Qdrant backend 的备份导出（导入在后续 chunk）。"""

    def __init__(
        self,
        repo: Any,
        sqlite_path: str,
        qdrant_local_path: str,
        on_qdrant_close: Callable[[], None],
        on_qdrant_reinit: Callable[[], None],
    ) -> None:
        self.repo = repo
        self.sqlite_path = sqlite_path
        self.qdrant_local_path = qdrant_local_path
        self.on_qdrant_close = on_qdrant_close
        self.on_qdrant_reinit = on_qdrant_reinit

    def export_to(self, out_path: str) -> dict[str, Any]:
        """导出全量备份到 tar.gz。严格按 plan 顺序：

          1. 磁盘空间预校验
          2. on_qdrant_close()
          3. tmp 目录：先 cp knowledge.db → 算 sha256
          4. 收集 stats / redacted config
          5. 写 manifest.json
          6. tarfile 打包写出
          7. finally: on_qdrant_reinit()

        sha256 算在 tmp 副本上而非 live db，避免 export 期间写入污染（审计 #15）。
        """
        out_dir = os.path.dirname(out_path) or "."
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        live_db_size = (
            os.path.getsize(self.sqlite_path)
            if os.path.exists(self.sqlite_path)
            else 0
        )
        qdrant_size = (
            _dir_size(self.qdrant_local_path)
            if os.path.isdir(self.qdrant_local_path)
            else 0
        )
        data_size = live_db_size + qdrant_size
        require_disk_space(
            target_dir=out_dir,
            data_size_bytes=data_size,
            safety_factor=2.5,
        )

        self.on_qdrant_close()
        manifest: dict[str, Any] = {}
        try:
            with tempfile.TemporaryDirectory(prefix="kb-export-") as tmpd:
                tmp_db = Path(tmpd) / "knowledge.db"
                shutil.copy2(self.sqlite_path, tmp_db)
                db_sha256 = _sha256_of_file(tmp_db)

                cfg = self.repo.get_system_config() or {}
                redacted = _redact_config(cfg)
                emb_model = str(cfg.get("embedding_model") or "")
                emb_dim = int(cfg.get("embedding_dim") or 0)
                emb_base = str(cfg.get("embedding_base_url") or "")

                stats = self._collect_stats()

                manifest = {
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "backend": "sqlite",
                    "host": socket.gethostname(),
                    "knowledge_db_sha256": db_sha256,
                    "embedding": {
                        "model": emb_model,
                        "dim": emb_dim,
                        "base_url": emb_base,
                    },
                    "stats": stats,
                }

                manifest_path = Path(tmpd) / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                redacted_path = Path(tmpd) / "system_config_redacted.json"
                redacted_path.write_text(
                    json.dumps(redacted, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                with tarfile.open(out_path, mode="w:gz", compresslevel=6) as tar:
                    tar.add(manifest_path, arcname="manifest.json")
                    tar.add(tmp_db, arcname="data/knowledge.db")
                    if os.path.isdir(self.qdrant_local_path):
                        tar.add(self.qdrant_local_path, arcname="data/qdrant_local")
                    tar.add(redacted_path, arcname="meta/system_config_redacted.json")
        finally:
            self.on_qdrant_reinit()

        logger.info(
            "op=backup_export result=ok out=%s items=%d chunks=%d sha=%s",
            out_path,
            manifest.get("stats", {}).get("items", 0),
            manifest.get("stats", {}).get("chunks", 0),
            manifest.get("knowledge_db_sha256", "")[:8],
        )
        return {"out_path": out_path, "manifest": manifest}

    def _collect_stats(self) -> dict[str, int]:
        with self.repo._connect() as conn:
            items = conn.execute(
                "SELECT COUNT(*) FROM knowledge_item WHERE status='active'"
            ).fetchone()[0]
            versions = conn.execute(
                "SELECT COUNT(*) FROM knowledge_version"
            ).fetchone()[0]
            chunks = conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunk"
            ).fetchone()[0]
            vectors = conn.execute(
                "SELECT COUNT(*) FROM knowledge_chunk "
                "WHERE vector_id IS NOT NULL AND vector_id != ''"
            ).fetchone()[0]
        return {
            "items": int(items),
            "versions": int(versions),
            "chunks": int(chunks),
            "vectors": int(vectors),
        }

    # ------------------------------------------------------------------
    # import / overwrite
    # ------------------------------------------------------------------

    def import_overwrite(
        self,
        package_path: str,
        auto_backup_service: "AutoBackupService",
    ) -> dict[str, Any]:
        """overwrite 模式：清空当前库 + 还原备份包。

        严格顺序（双层防护，审计 #5 / #11）：
          1. 解压包到临时目录
          2. parse_manifest（schema_version + 字段完整性）
          3. sha256 校验：解压后 db sha == manifest.knowledge_db_sha256
          ※ 前置校验失败抛 BackupImportError，不生成 auto-backup，零副作用
          4. 外层 auto-backup snapshot（永久保留）
          5. on_qdrant_close() 释放 Qdrant 文件锁
          6. 内层 .pre-restore.bak + .pre-restore-qdrant（cp 一份原 data）
          7. clear_all_active_data + cp 备份内 db / qdrant 还原，并迁移旧 DB schema
          8. 用迁移后的 system_config 覆盖当前 config（含真凭证）
          9. 删除内层 .pre-restore.* 副本（成功路径）
         10. finally: on_qdrant_reinit()
        步骤 6-9 任意失败 → 用 .pre-restore.* 回滚 → BackupImportError
        """
        import sqlite3

        sqlite_path = Path(self.sqlite_path)
        qdrant_path = Path(self.qdrant_local_path)
        data_dir = sqlite_path.parent
        pre_db = data_dir / ".pre-restore.bak"
        pre_qdrant = data_dir / ".pre-restore-qdrant"

        with tempfile.TemporaryDirectory(prefix="kb-import-") as tmpd:
            # 步骤 1: 解压
            #  Python 3.12+ 用 filter='data' 拒绝越权 / 符号链接（zip slip）；
            #  老版本走 _safe_extractall_fallback 逐成员做 realpath 边界与 link
            #  类型校验，杜绝审计 #2 报告的 fallback 风险。
            try:
                with tarfile.open(package_path, "r:gz") as tar:
                    try:
                        tar.extractall(tmpd, filter="data")
                    except TypeError:
                        _safe_extractall_fallback(tar, tmpd)
            except (tarfile.TarError, OSError) as e:
                raise BackupImportError(
                    f"failed to extract backup package: {e}",
                    kind="client",
                ) from e

            extracted = Path(tmpd)
            manifest_path = extracted / "manifest.json"
            db_path_in_pkg = extracted / "data" / "knowledge.db"
            qdrant_in_pkg = extracted / "data" / "qdrant_local"

            if not manifest_path.exists() or not db_path_in_pkg.exists():
                raise BackupImportError(
                    "package missing required entries: manifest.json or data/knowledge.db",
                    kind="client",
                )

            # 步骤 2: manifest 校验
            try:
                manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
            except ManifestParseError as e:
                raise BackupImportError(f"invalid manifest: {e}", kind="client") from e

            # backend 兼容性校验（审计 #13）：sqlite 服务只能 import sqlite manifest
            if manifest.backend != "sqlite":
                raise BackupImportError(
                    f"backup backend='{manifest.backend}' incompatible with "
                    f"current service (sqlite-only); "
                    f"postgres backups see backup-restore-docker-mode proposal",
                    kind="client",
                )

            # 步骤 3: sha256 校验
            actual_sha = _sha256_of_file(db_path_in_pkg)
            if actual_sha != manifest.knowledge_db_sha256:
                raise BackupImportError(
                    f"knowledge.db sha256 mismatch: "
                    f"expected={manifest.knowledge_db_sha256[:12]}... "
                    f"actual={actual_sha[:12]}...",
                    kind="client",
                )

            # 步骤 4: 外层 auto-backup（仅在前置校验通过后）
            auto_path = auto_backup_service.snapshot_current_data(
                trigger="import_before",
                extra_meta={
                    "mode": "overwrite",
                    "package_sha256": manifest.knowledge_db_sha256,
                },
            )

            # 步骤 5: Qdrant close
            self.on_qdrant_close()
            rolled_back = False
            try:
                # 步骤 6: 内层 .pre-restore
                if sqlite_path.exists():
                    shutil.copy2(sqlite_path, pre_db)
                if qdrant_path.exists():
                    if pre_qdrant.exists():
                        shutil.rmtree(pre_qdrant)
                    shutil.copytree(qdrant_path, pre_qdrant)

                try:
                    # 步骤 7: 清表 + 还原
                    self.repo.clear_all_active_data()
                    shutil.copy2(db_path_in_pkg, sqlite_path)
                    # 文件级覆盖可能把当前 DB 降回旧 schema；必须先运行仓储迁移，
                    # 再用当前版本的 upsert 回填配置。否则旧备份缺少新增列时会
                    # 触发 no column 并整次回滚。
                    self.repo.reset_schema()
                    if qdrant_path.exists():
                        shutil.rmtree(qdrant_path)
                    if qdrant_in_pkg.exists():
                        shutil.copytree(qdrant_in_pkg, qdrant_path)

                    # 步骤 8: 用包内 db 的 system_config 覆盖当前 config（含真凭证）
                    self._restore_system_config_from(sqlite_path)

                    # 步骤 9: 成功路径，删内层副本
                    if pre_db.exists():
                        pre_db.unlink()
                    if pre_qdrant.exists():
                        shutil.rmtree(pre_qdrant)
                except Exception as exc:
                    # 回滚：用 .pre-restore.* 覆盖
                    logger.error(
                        "import_overwrite failed in restore phase, rolling back: %s",
                        exc,
                        exc_info=True,
                    )
                    rollback_errs = self._rollback_from_pre_restore(
                        sqlite_path=sqlite_path,
                        qdrant_path=qdrant_path,
                        pre_db=pre_db,
                        pre_qdrant=pre_qdrant,
                    )
                    rolled_back = True
                    if rollback_errs:
                        # 回滚自身失败（审计 #9）：用户必须走外层 auto-backup
                        raise BackupImportError(
                            f"import_overwrite failed AND rollback partially failed "
                            f"({len(rollback_errs)} step(s)); "
                            f"data may be inconsistent — restore from auto-backup at "
                            f"{auto_path}. original error: {exc}; "
                            f"rollback errors: {rollback_errs}",
                            kind="rollback_partial",
                        ) from exc
                    raise BackupImportError(
                        f"import_overwrite failed, data rolled back to pre-restore: {exc}",
                        kind="rolled_back",
                    ) from exc
            finally:
                # 步骤 10: reinit Qdrant，无论成功 / 回滚 / 二次失败
                self.on_qdrant_reinit()

        # 统计：成功路径
        with self.repo._connect() as conn:
            items_after = conn.execute(
                "SELECT COUNT(*) FROM knowledge_item WHERE status='active'"
            ).fetchone()[0]

        logger.info(
            "op=backup_import mode=overwrite result=ok auto_backup_path=%s items_after=%d sha=%s",
            auto_path,
            items_after,
            manifest.knowledge_db_sha256[:8],
        )
        return {
            "ok": True,
            "mode": "overwrite",
            "items_after": int(items_after),
            "auto_backup_path": auto_path,
            "rolled_back": rolled_back,
        }

    def _restore_system_config_from(self, db_after_restore: Path) -> None:
        """用刚还原的 sqlite 文件中的 system_config 行回填到 repo（让 repo 反映备份配置）。

        失败时直接抛异常给上层（审计 #10），由 import_overwrite 触发回滚契约。
        """
        import sqlite3

        with sqlite3.connect(str(db_after_restore)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM system_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return
        payload = {k: row[k] for k in row.keys() if k not in ("id", "updated_at")}
        # 旧版本备份没有模式列；它的 llm_max_tokens 当时始终是显式限制，恢复时
        # 必须保持手动语义，不能套用“新安装默认 auto”的 repository fallback。
        payload.setdefault("llm_max_tokens_auto", False)
        for bool_field in (
            "llm_enabled",
            "llm_max_tokens_auto",
            "embedding_enabled",
            "rerank_enabled",
            "enrichment_enabled",
        ):
            if bool_field in payload:
                payload[bool_field] = bool(payload[bool_field])
        # 不要 try/except 吞——失败必须冒泡触发回滚
        self.repo.upsert_system_config(payload)

    def _rollback_from_pre_restore(
        self,
        sqlite_path: Path,
        qdrant_path: Path,
        pre_db: Path,
        pre_qdrant: Path,
    ) -> list[str]:
        """从 .pre-restore.* 覆盖回原 data。

        返回回滚过程中发生的错误描述列表（审计 #9）：
        - 空列表 = 回滚完整成功
        - 非空 = 回滚部分失败，调用方应区分"已回滚 / 部分回滚失败"语义
        """
        errors: list[str] = []
        try:
            if pre_db.exists():
                shutil.copy2(pre_db, sqlite_path)
                pre_db.unlink()
        except Exception as e:
            logger.error(
                "rollback: failed to restore sqlite from .pre-restore.bak",
                exc_info=True,
            )
            errors.append(f"sqlite rollback failed: {e}")
        try:
            if pre_qdrant.exists():
                if qdrant_path.exists():
                    shutil.rmtree(qdrant_path)
                shutil.move(str(pre_qdrant), str(qdrant_path))
        except Exception as e:
            logger.error(
                "rollback: failed to restore qdrant from .pre-restore-qdrant",
                exc_info=True,
            )
            errors.append(f"qdrant rollback failed: {e}")
        return errors


class AutoBackupService:
    """快速 cp 当前 data/ 到 auto-backup/{ts}/。

    用于：
    - import_overwrite / import_merge 前的外层防护快照
    - Install.command 升级前（脚本会调用相同结构，但脚本侧自己写 manifest）
    """

    def __init__(
        self,
        sqlite_path: str,
        qdrant_local_path: str,
        auto_backup_root: str,
    ) -> None:
        self.sqlite_path = sqlite_path
        self.qdrant_local_path = qdrant_local_path
        self.auto_backup_root = auto_backup_root

    def snapshot_current_data(
        self,
        trigger: str,
        extra_meta: Optional[dict[str, Any]] = None,
    ) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_dir = Path(self.auto_backup_root) / ts
        bak_dir.mkdir(parents=True, exist_ok=True)

        data_dir = bak_dir / "data"
        data_dir.mkdir(exist_ok=True)
        if os.path.exists(self.sqlite_path):
            shutil.copy2(self.sqlite_path, data_dir / "knowledge.db")
        if os.path.isdir(self.qdrant_local_path):
            shutil.copytree(
                self.qdrant_local_path,
                data_dir / "qdrant_local",
                dirs_exist_ok=False,
            )

        meta_dir = bak_dir / "meta"
        meta_dir.mkdir(exist_ok=True)
        manifest = {
            "trigger": trigger,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "host": socket.gethostname(),
        }
        if extra_meta:
            manifest.update(extra_meta)
        (meta_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "op=auto_backup_snapshot trigger=%s path=%s",
            trigger,
            str(bak_dir),
        )
        return str(bak_dir)
