"""Legacy 知识包 lane 的 in-process 实现（严格 mirror shell 版）。

本模块是 `mcp_tools.py` 4 个 endpoint（export-package / import-package / cleanup-expired /
clear 中的 backup 环节）的 Python 化替代，用来消除 Windows 上 subprocess exec `.sh` 抛
`WinError 193` 的坑（`_run_script` 直接 exec shebang 文件在 Windows 无法启动）。

设计契约（跟 shell 版严格等语义，见 docs/_review_inputs/2026-07-02-tarball-format-audit.md）：

- Tarball 结构严格 mirror `kb-export-package.sh` + `backup_create.sh` 产出：
    ``kb-export-{ts}.tar.gz`` 顶层含 ``{ts}/`` 目录，内含 ``config/`` + ``data/`` + ``meta/``
- 备份目录结构 mirror `backup_create.sh`：
    ``{backup_dir}/config/config.toml``
    ``{backup_dir}/data/{knowledge.db,qdrant_local,import_state.json}``（sqlite backend）
    ``{backup_dir}/data/{postgres,qdrant,minio}``（postgres backend）
    ``{backup_dir}/meta/manifest.json``（3 字段：created_at / backend / host）
- `_restore_from_backup` mirror `backup_restore.sh:20-45` 的分支逻辑
- stdout 输出严格 mirror shell 版（见每个方法的 docstring）
- 保留 postgres/qdrant/minio 后端分支（虽然 OSS 直装版几乎不用，但契约边界不能破）

**不与 BackupService 混淆**：BackupService 服务 `/v1/system/backup/*` 的强校验 lane（含 sha256 /
schema_version / auto-backup / pre-restore rollback），本 module 服务 `/v1/knowledge/*` 的
shell 兼容 legacy lane，两条 lane 独立契约，见 22-win-cc-反向接力 文档。
"""
from __future__ import annotations

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


logger = logging.getLogger(__name__)


class KnowledgePackageError(RuntimeError):
    """knowledge package export/import 失败的统一异常。

    路由层可根据 kind 映射 HTTP 状态码：
    - ``"client"``：包路径不存在 / 包结构不合法 → HTTP 400
    - ``"server"``：其他服务端错误 → HTTP 500
    """

    def __init__(self, message: str, *, kind: str = "server") -> None:
        super().__init__(message)
        self.kind = kind


def _timestamp() -> str:
    """mirror shell 版 ``TS="$(date '+%Y%m%d_%H%M%S')"``。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso_utc_z() -> str:
    """mirror shell 版 ``date -u '+%Y-%m-%dT%H:%M:%SZ'``。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detect_backend(install_root: Path, env_backend: str | None = None) -> str:
    """mirror shell 版 ``detect_backend()``：优先 KB_BACKEND env，否则按 data 目录内容识别。

    shell 原文（backup_create.sh:10-20）：
        if [[ -n "${KB_BACKEND:-}" ]]; then echo "$KB_BACKEND"; return; fi
        if [[ -f "$ROOT_DIR/data/knowledge.db" || -d "$ROOT_DIR/data/qdrant_local" ]]; then
            echo "sqlite"; return
        fi
        echo "postgres"
    """
    if env_backend:
        return env_backend
    if (install_root / "data" / "knowledge.db").is_file():
        return "sqlite"
    if (install_root / "data" / "qdrant_local").is_dir():
        return "sqlite"
    return "postgres"


def _copy_if_exists(src: Path, dst: Path) -> None:
    """mirror shell 版 ``copy_if_exists``：源存在才 cp -R，dst 父目录自动建。"""
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


class KnowledgePackageService:
    """Legacy 知识包 lane 的 in-process 实现。

    严格 mirror shell 版：
      - `kb-export-package.sh` → :meth:`export_package`
      - `kb-import-package.sh`（tar.gz 分支）→ :meth:`import_package`
      - `backup_create.sh` → :meth:`create_backup`
      - `backup_restore.sh` → :meth:`_restore_from_backup`

    构造参数：
      - ``install_root``：项目根（装机版 = ``D:\\KnowledgeBase`` 或 ``/Applications/KnowledgeBase``）
      - ``sqlite_path`` / ``qdrant_local_path``：从 env 拿到并 validate 过的绝对路径
      - ``on_qdrant_close`` / ``on_qdrant_reinit``：跟 BackupService 同款回调，
        cp/rm qdrant_local 前必须 close 释放 portalocker 文件锁
    """

    def __init__(
        self,
        install_root: Path,
        sqlite_path: str,
        qdrant_local_path: str,
        on_qdrant_close: Callable[[], None],
        on_qdrant_reinit: Callable[[], None],
        on_sqlite_reinit: Callable[[], None],
    ) -> None:
        self.install_root = Path(install_root)
        self.sqlite_path = Path(sqlite_path)
        self.qdrant_local_path = Path(qdrant_local_path)
        self.on_qdrant_close = on_qdrant_close
        self.on_qdrant_reinit = on_qdrant_reinit
        # 2026-07-03 P0：import-package cp 完备份的 knowledge.db 后，kb-api 端
        # SqliteKnowledgeRepo 单例的连接语义是每次 sqlite3.connect(path)，文件被
        # unlink+cp 后新 fd 打开的是备份文件本身 schema 完整，通常自愈；但如果
        # kb-api 处于半死态（比如 clear 先跑没 reinit schema）就需要显式调这个
        # 回调兜底建表。跟 on_qdrant_reinit 对称，clear/import 都要调。
        self.on_sqlite_reinit = on_sqlite_reinit

    # ------------------------------------------------------------------
    # backup（对应 shell 版 backup_create.sh）
    # ------------------------------------------------------------------

    def create_backup(self, backup_dir: str | None = None) -> str:
        """严格 mirror ``scripts/backup_create.sh`` 行为。

        - ``backup_dir=None`` → 默认 ``{install_root}/backups/{ts}/``
        - 建 ``config/`` + ``data/`` + ``meta/`` 三个子目录
        - copy_if_exists ``config/config.toml`` + 后端数据文件
        - 写 ``meta/manifest.json``（3 字段：created_at + backend + host）
        - 返回 backup_dir 绝对路径（跟 shell ``echo "Backup created: $BACKUP_DIR"`` 的信息等价）

        Qdrant 文件锁：cp qdrant_local 前必须 :func:`on_qdrant_close`，操作结束后
        :func:`on_qdrant_reinit`；即使 exception 也走 finally 释放。
        """
        if not backup_dir:
            ts = _timestamp()
            bak_path = self.install_root / "backups" / ts
        else:
            bak_path = Path(backup_dir)
        bak_path.mkdir(parents=True, exist_ok=True)
        (bak_path / "config").mkdir(exist_ok=True)
        (bak_path / "data").mkdir(exist_ok=True)
        (bak_path / "meta").mkdir(exist_ok=True)

        backend = _detect_backend(self.install_root, os.getenv("KB_BACKEND"))

        _copy_if_exists(
            self.install_root / "config" / "config.toml",
            bak_path / "config" / "config.toml",
        )

        self.on_qdrant_close()
        try:
            if backend == "sqlite":
                _copy_if_exists(self.sqlite_path, bak_path / "data" / "knowledge.db")
                _copy_if_exists(
                    self.qdrant_local_path, bak_path / "data" / "qdrant_local"
                )
                _copy_if_exists(
                    self.install_root / "data" / "import_state.json",
                    bak_path / "data" / "import_state.json",
                )
            else:
                # postgres backend：mirror shell 版 postgres/qdrant/minio 三份 cp -R
                _copy_if_exists(
                    self.install_root / "data" / "postgres",
                    bak_path / "data" / "postgres",
                )
                _copy_if_exists(
                    self.install_root / "data" / "qdrant",
                    bak_path / "data" / "qdrant",
                )
                _copy_if_exists(
                    self.install_root / "data" / "minio",
                    bak_path / "data" / "minio",
                )
        finally:
            self.on_qdrant_reinit()

        manifest = {
            "created_at": _iso_utc_z(),
            "backend": backend,
            "host": socket.gethostname(),
        }
        (bak_path / "meta" / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        logger.info(
            "op=knowledge_package_backup result=ok path=%s backend=%s",
            str(bak_path),
            backend,
        )
        return str(bak_path)

    # ------------------------------------------------------------------
    # export（对应 shell 版 kb-export-package.sh）
    # ------------------------------------------------------------------

    def export_package(self, export_dir: str | None = None) -> dict[str, Any]:
        """严格 mirror ``scripts/kb-export-package.sh`` 行为。

        流程：
          1. ``TS="$(date '+%Y%m%d_%H%M%S')"``
          2. 决定 ``EXPORT_DIR``（arg 或 ``{install_root}/exports``）
          3. ``BACKUP_DIR="{install_root}/backups/{ts}"``
          4. 调 :meth:`create_backup` 生成 shell 兼容的备份目录
          5. ``tar -czf {EXPORT_DIR}/kb-export-{ts}.tar.gz -C {install_root}/backups {ts}``
             → tarball 顶层含 ``{ts}/``（**这是 legacy lane 的核心契约**）
          6. 返回 ``{ok, exit_code, script, args, output, error}`` 结构，
             ``output`` = ``"Export package created: {out_path}"``

        **跟 BackupService.export_to 的关键差异**：
          - 无 sha256 校验（shell 版没有）
          - 无 stats（shell 版 manifest 只 3 字段）
          - 无 REDACTED system_config（shell 版 dump 原始 config.toml）
          - Tarball 顶层含 ``{ts}/``（BackupService 没有）
        """
        ts = _timestamp()
        if export_dir:
            out_dir = Path(export_dir)
        else:
            out_dir = self.install_root / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)

        # backup_dir 固定放 {install_root}/backups/{ts}，跟 shell 版 kb-export-package.sh:23 一致
        bak_path = Path(self.create_backup())
        # 上一步 create_backup 用默认 backup_dir = {install_root}/backups/{ts}
        # 但 create_backup 内部自己算的 ts 跟这里的 ts 可能有毫秒差
        # 为了严格 mirror shell 版（shell 版 kb-export-package.sh 里的 TS 就是同一个），
        # 用 create_backup 返回的实际路径当基准
        actual_ts = bak_path.name

        out_path = out_dir / f"kb-export-{actual_ts}.tar.gz"

        # tar -czf 顶层含 {ts}/ 目录：等价 shell 版 `tar -czf $OUT -C $ROOT_DIR/backups $TS`
        with tarfile.open(str(out_path), mode="w:gz", compresslevel=6) as tar:
            tar.add(str(bak_path), arcname=actual_ts)

        output = f"Export package created: {out_path}"
        logger.info("op=knowledge_package_export result=ok path=%s", str(out_path))
        return {
            "ok": True,
            "exit_code": 0,
            "script": "kb-export-package.sh",
            "args": [str(export_dir)] if export_dir else [],
            "output": output,
            "error": "",
            "out_path": str(out_path),
        }

    # ------------------------------------------------------------------
    # import（对应 shell 版 kb-import-package.sh 的 tar.gz 分支）
    # ------------------------------------------------------------------

    def import_package(self, package_path: str) -> dict[str, Any]:
        """严格 mirror ``scripts/kb-import-package.sh`` 的 tar.gz 分支（line 149-162）。

        流程：
          1. 校验 ``package_path`` 存在 + 后缀 ``.tar.gz`` / ``.tgz``
          2. 创建 tmp dir + trap rm（Python 用 ``TemporaryDirectory`` context manager）
          3. ``tar -xzf $PKG_PATH -C $TMP_DIR``
          4. 找 tmp 里唯一顶层子目录当 restore_src；如果没有唯一子目录 → 用 tmp 本身
             （mirror shell 版 kb-import-package.sh:154-159 的 find 逻辑）
          5. 调 :meth:`_restore_from_backup`（mirror ``backup_restore.sh``）
          6. 返回 output = ``"Import package done: {package_path}"``

        **不做的事**（保持 legacy lane 语义）：
          - 无 manifest schema_version 校验
          - 无 sha256 校验
          - 无 auto-backup snapshot
          - 无 pre-restore rollback

          BackupService 那些强校验都放在 ``/v1/system/backup/import`` lane。用户想要强校验就走
          BackupService lane。本 lane 严格 mirror shell 版行为。

        非 tar.gz 分支（kb-import-package.sh:47-147 的 osascript + curl HTTP 逻辑）**不 mirror**：
          - osascript 是 macOS UI 交互，Python 化后 API 层不该有 UI 交互
          - curl HTTP 自调用是"API 进程 fork 子进程 curl 调自己"多余且危险，
            调用方（Mac tray / MCP proxy / Web UI）想上传单文件直接调
            ``/v1/knowledge/import-file`` endpoint（``app/main.py:612``）
        """
        pkg = Path(package_path)
        if not pkg.is_file():
            raise KnowledgePackageError(
                f"package not found: {package_path}", kind="client"
            )
        name_lower = pkg.name.lower()
        if not (name_lower.endswith(".tar.gz") or name_lower.endswith(".tgz")):
            raise KnowledgePackageError(
                f"only .tar.gz / .tgz packages are supported by import-package "
                f"lane; single-file uploads should use /v1/knowledge/import-file. "
                f"got: {package_path}",
                kind="client",
            )

        with tempfile.TemporaryDirectory(prefix="kb-import.") as tmpd:
            tmp_path = Path(tmpd)

            # tar -xzf：Python 3.12+ 用 filter='data' 拒绝 zip slip；老版本走
            # safe extract fallback。跟 BackupService.import_overwrite:296-306 一致。
            try:
                with tarfile.open(str(pkg), "r:gz") as tar:
                    try:
                        tar.extractall(str(tmp_path), filter="data")
                    except TypeError:
                        self._safe_extractall_fallback(tar, tmp_path)
            except (tarfile.TarError, OSError) as e:
                raise KnowledgePackageError(
                    f"failed to extract package: {e}", kind="client"
                ) from e

            # mirror shell 版 kb-import-package.sh:154-159：
            #   find $TMP_DIR -mindepth 1 -maxdepth 1 -type d | wc -l == 1
            #     → RESTORE_SRC=<唯一子目录>
            #   else → RESTORE_SRC=$TMP_DIR
            children = [p for p in tmp_path.iterdir() if p.is_dir()]
            restore_src = children[0] if len(children) == 1 else tmp_path

            self._restore_from_backup(restore_src)

        output = f"Import package done: {package_path}"
        logger.info("op=knowledge_package_import result=ok pkg=%s", package_path)
        return {
            "ok": True,
            "exit_code": 0,
            "script": "kb-import-package.sh",
            "args": [package_path],
            "output": output,
            "error": "",
        }

    # ------------------------------------------------------------------
    # restore（对应 shell 版 backup_restore.sh）
    # ------------------------------------------------------------------

    def _restore_from_backup(self, backup_dir: Path) -> None:
        """严格 mirror ``scripts/backup_restore.sh:18-45`` 行为。

        - ``config/config.toml`` 存在 → cp 覆盖 ``{install_root}/config/config.toml``
        - sqlite 布局（``data/knowledge.db`` 存在）：
            rm 老 knowledge.db + qdrant_local → cp 备份的 knowledge.db + qdrant_local +
            可选 import_state.json；输出 ``"Restore done (backend=sqlite): {backup_dir}"``
        - postgres 布局（``data/postgres`` 或 ``data/qdrant`` 或 ``data/minio`` 目录存在）：
            rm 老 3 目录 → cp -R 备份的对应目录；输出 ``"Restore done (backend=postgres): {backup_dir}"``
        - 都不匹配 → raise KnowledgePackageError（对应 shell 版 exit 1）
        """
        if not backup_dir.is_dir():
            raise KnowledgePackageError(
                f"backup dir not found: {backup_dir}", kind="client"
            )

        (self.install_root / "data").mkdir(parents=True, exist_ok=True)
        (self.install_root / "config").mkdir(parents=True, exist_ok=True)

        cfg_src = backup_dir / "config" / "config.toml"
        if cfg_src.is_file():
            shutil.copy2(cfg_src, self.install_root / "config" / "config.toml")

        db_src = backup_dir / "data" / "knowledge.db"
        qdrant_local_src = backup_dir / "data" / "qdrant_local"
        import_state_src = backup_dir / "data" / "import_state.json"

        pg_src = backup_dir / "data" / "postgres"
        qdrant_pg_src = backup_dir / "data" / "qdrant"
        minio_src = backup_dir / "data" / "minio"

        if db_src.is_file():
            # sqlite 分支：cp 前必须释放 qdrant 文件锁
            self.on_qdrant_close()
            try:
                if self.sqlite_path.exists():
                    self.sqlite_path.unlink()
                if self.qdrant_local_path.exists():
                    shutil.rmtree(self.qdrant_local_path)
                shutil.copy2(db_src, self.sqlite_path)
                if qdrant_local_src.is_dir():
                    shutil.copytree(qdrant_local_src, self.qdrant_local_path)
                if import_state_src.is_file():
                    shutil.copy2(
                        import_state_src,
                        self.install_root / "data" / "import_state.json",
                    )
            finally:
                self.on_qdrant_reinit()
                # 2026-07-03 P0：cp 完备份的 knowledge.db 后显式调 sqlite reinit，
                # 保险起见跑 CREATE TABLE IF NOT EXISTS（备份 db 已有完整 schema
                # 时是 no-op；kb-api 若因前置 clear 处于半死态则触发建表自愈）。
                self.on_sqlite_reinit()
            logger.info(
                "op=knowledge_package_restore backend=sqlite src=%s", str(backup_dir)
            )
            return

        if pg_src.is_dir() or qdrant_pg_src.is_dir() or minio_src.is_dir():
            # postgres 分支（严格 mirror shell 版 backup_restore.sh:38-45）
            for name in ("postgres", "qdrant", "minio"):
                target = self.install_root / "data" / name
                if target.exists():
                    shutil.rmtree(target)
            _copy_if_exists(pg_src, self.install_root / "data" / "postgres")
            _copy_if_exists(qdrant_pg_src, self.install_root / "data" / "qdrant")
            _copy_if_exists(minio_src, self.install_root / "data" / "minio")
            logger.info(
                "op=knowledge_package_restore backend=postgres src=%s", str(backup_dir)
            )
            return

        raise KnowledgePackageError(
            f"restore failed: unsupported backup layout in {backup_dir}",
            kind="client",
        )

    # ------------------------------------------------------------------
    # safe extract fallback
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_extractall_fallback(tar: tarfile.TarFile, dest: Path) -> None:
        """tarfile 老版本（无 ``filter='data'``）的安全 extractall fallback。

        同款逻辑复制自 ``BackupService._safe_extractall_fallback``（本模块不 import
        避免耦合两条 lane）：
        - 拒绝绝对路径 / ``..`` 越权
        - 拒绝 symlink / hardlink
        - realpath 边界校验
        """
        dest_path = dest.resolve()
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise KnowledgePackageError(
                    f"refuse to extract link member: {member.name}", kind="client"
                )
            name = member.name
            if name.startswith("/") or name.startswith("\\") or ".." in Path(name).parts:
                raise KnowledgePackageError(
                    f"refuse to extract member with unsafe path: {name}", kind="client"
                )
            target = (dest_path / name).resolve()
            try:
                target.relative_to(dest_path)
            except ValueError as e:
                raise KnowledgePackageError(
                    f"refuse to extract member outside destination: {name}",
                    kind="client",
                ) from e
        for member in tar.getmembers():
            tar.extract(member, str(dest_path))
