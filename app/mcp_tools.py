"""MCP tool 层：暴露给 FastAPI endpoint (`/v1/knowledge/*`) + MCP stdio 协议
(`app/mcp_server.py`) 的调用面。

2026-07-02 X2.5 refactor：4 个"知识包/清理/清空"端点从 `subprocess.run(['xxx.sh', ...])`
改成 in-process 调用 :class:`KnowledgePackageService` / :class:`KnowledgeClearService`，
消除 Windows 上 `WinError 193`（subprocess 无法 exec `.sh` shebang 文件）。

**产品线边界**（对齐 docs/_review_inputs/2026-07-02-tarball-format-audit.md）：

- **Legacy lane**（本模块）：``/v1/knowledge/{cleanup-expired,clear,export-package,
  import-package}``，Python 版严格 mirror shell 版行为（tarball 顶层 ``{ts}/`` +
  简版 manifest + 无 sha256）
- **BackupService lane**（``app/services/backup_service.py``）：``/v1/system/backup/*``，
  新格式（顶层 ``manifest.json`` + ``knowledge_db_sha256`` + auto-backup + rollback）

两条 lane **不互相导入**，是设计契约不是 bug。用户想要强校验走 BackupService lane，
想要跟历史 shell tarball 兼容走 Legacy lane。

保留的 shell 脚本（`scripts/kb-{clear,clean-expired,export-package,import-package,
backup_create,backup_restore}.sh`）供 CLI + Mac MenuBar 手动使用，跟 Python 版严格
等语义（tarball 结构、stdout 输出）。
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app.schemas import SearchRequest, UpsertRequest
from app.service import KnowledgeService
from app.services.knowledge_clear import KnowledgeClearService
from app.services.knowledge_package import (
    KnowledgePackageError,
    KnowledgePackageService,
)


logger = logging.getLogger(__name__)


class KnowledgeMcpTools:
    def __init__(self, repo: Any) -> None:
        self.repo = repo
        self.service = KnowledgeService(repo)
        # project_root：subprocess.run 的 cwd（仅供保留但已废弃的 _run_script 用）
        self.project_root = Path(__file__).resolve().parents[1]
        self.require_dangerous_confirm = self._read_bool_env(
            "KB_MCP_REQUIRE_DANGEROUS_CONFIRM", default=False
        )
        # 惰性构造缓存
        self._package_service: Optional[KnowledgePackageService] = None
        self._clear_service: Optional[KnowledgeClearService] = None

    # ------------------------------------------------------------------
    # 只读 / 增删改查（跟本次 refactor 无关）
    # ------------------------------------------------------------------

    def search_knowledge(
        self,
        query: str,
        domain: str,
        project: str | None = None,
        module: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        source_uri: str | None = None,
        as_of: datetime | None = None,
        top_k: int = 8,
        actor: str = "codex-local",
    ) -> dict[str, Any]:
        req = SearchRequest(
            query=query,
            domain=domain,
            project=project,
            module=module,
            feature=feature,
            tags=tags or [],
            source_uri=source_uri,
            as_of=as_of,
            top_k=top_k,
            actor=actor,
        )
        return self.service.search(req)

    def get_knowledge_item(self, item_id: str, actor: str = "codex-local") -> dict[str, Any]:
        row = self.service.get_item(item_id, actor=actor)
        if row is None:
            raise ValueError("knowledge item not found")
        return row

    def upsert_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = UpsertRequest(**payload)
        return self.service.upsert(req)

    def import_incremental_knowledge(
        self,
        directory: str,
        project: str,
        domain: str = "work",
        knowledge_type: str = "fact",
    ) -> dict[str, Any]:
        # in-process Python 实现,绕开 shell 脚本(Mac 走 osascript + bash,Win 没 sh
        # → subprocess 抛 OSError → endpoint 500)。详见 incremental_import 模块注释。
        from app.services.incremental_import import incremental_import_directory
        return incremental_import_directory(
            service=self.service,
            directory=directory,
            project=project,
            domain=domain,
            knowledge_type=knowledge_type,
        )

    # ------------------------------------------------------------------
    # Legacy lane：知识包 / 清理 / 清空（2026-07-02 X2.5 refactor）
    # ------------------------------------------------------------------

    def export_knowledge_package(self, export_dir: str | None = None) -> dict[str, Any]:
        """POST /v1/knowledge/export-package —— 严格 mirror `kb-export-package.sh`。

        产 shell 兼容 tarball（顶层 ``{ts}/``），保持 legacy lane 契约。
        """
        try:
            return self._get_package_service().export_package(export_dir=export_dir)
        except KnowledgePackageError as e:
            return self._error_result(
                script="kb-export-package.sh",
                args=[export_dir] if export_dir else [],
                error=str(e),
            )

    def import_knowledge_package(
        self, package_path: str, confirm: bool = False
    ) -> dict[str, Any]:
        """POST /v1/knowledge/import-package —— 严格 mirror `kb-import-package.sh`
        的 tar.gz 分支。

        非 tar.gz 单文件上传不 mirror（原 shell 版走 osascript + curl HTTP 自调用，
        Python 化后调用方直接打 ``/v1/knowledge/import-file`` 更干净）。
        """
        if self.require_dangerous_confirm and not confirm:
            raise ValueError(
                "dangerous operation: set confirm=true to import and restore knowledge package "
                "(KB_MCP_REQUIRE_DANGEROUS_CONFIRM=1)"
            )
        try:
            return self._get_package_service().import_package(
                package_path=package_path
            )
        except KnowledgePackageError as e:
            return self._error_result(
                script="kb-import-package.sh",
                args=[package_path],
                error=str(e),
            )

    def clear_knowledge_base(
        self, confirm: bool = False, backup_dir: str | None = None
    ) -> dict[str, Any]:
        """POST /v1/knowledge/clear —— 严格 mirror `kb-clear.sh --yes [backup_dir]`
        的 factory reset 语义（连 system_config 一起清，跟 shell 版 rm data/knowledge.db
        一致）。
        """
        if self.require_dangerous_confirm and not confirm:
            raise ValueError(
                "dangerous operation: set confirm=true to clear knowledge base "
                "(KB_MCP_REQUIRE_DANGEROUS_CONFIRM=1)"
            )
        try:
            return self._get_clear_service().clear(backup_dir=backup_dir)
        except KnowledgePackageError as e:
            return self._error_result(
                script="kb-clear.sh",
                args=["--yes"] + ([backup_dir] if backup_dir else []),
                error=str(e),
            )

    def cleanup_expired_knowledge(
        self,
        mode: str = "archive",
        as_of: str | None = None,
        backup_dir: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """POST /v1/knowledge/cleanup-expired —— 严格 mirror `kb-clean-expired.sh`。

        archive 模式：跑 `create_backup`，stdout ``"Expired knowledge archived
        (mode=archive, as_of=N/A)"``。

        delete 模式：跑 `create_backup` 再 `clear`，stdout **两行合并**
        ``"Knowledge base cleared (backend=sqlite)\\nExpired knowledge cleaned
        (mode=delete, as_of=N/A)"``。

        **⚠️ shell 版 `--as-of` 参数不参与实际过滤逻辑**（只出现在 stdout 里），
        本次严格 mirror，不引入真过期过滤。语义修正单独开 issue：
        "kb-clean-expired.sh --as-of 参数未参与过滤逻辑, 实为'全量备份'或
        '备份+factory reset'两种粗糙语义; 应加入真过期过滤(按 knowledge_item.effective_to)
        或废弃该参数"
        """
        if mode not in ("archive", "delete"):
            raise ValueError("mode must be archive or delete")
        if mode == "delete" and self.require_dangerous_confirm and not confirm:
            raise ValueError(
                "dangerous operation: mode=delete requires confirm=true "
                "(KB_MCP_REQUIRE_DANGEROUS_CONFIRM=1)"
            )

        # 构造 args 保持跟老 _run_script 调用等价的返回签名
        args = ["--mode", mode]
        if as_of:
            args += ["--as-of", as_of]
        if backup_dir:
            args += ["--backup-dir", backup_dir]

        as_of_display = as_of if as_of else "N/A"

        try:
            if mode == "archive":
                self._get_package_service().create_backup(backup_dir)
                output = f"Expired knowledge archived (mode=archive, as_of={as_of_display})"
            else:
                # delete 模式：先 clear（clear 内部会先 create_backup 再 rm）
                clear_result = self._get_clear_service().clear(backup_dir=backup_dir)
                cleaned_line = f"Expired knowledge cleaned (mode=delete, as_of={as_of_display})"
                # 严格 mirror shell 版两行合并：kb-clear.sh 的 stdout + kb-clean-expired.sh
                # 追加的 echo。见 kb-clean-expired.sh:56-61。
                output = f"{clear_result['output']}\n{cleaned_line}"
        except KnowledgePackageError as e:
            return self._error_result(
                script="kb-clean-expired.sh",
                args=args,
                error=str(e),
            )

        return {
            "ok": True,
            "exit_code": 0,
            "script": "kb-clean-expired.sh",
            "args": args,
            "output": output,
            "error": "",
        }

    # ------------------------------------------------------------------
    # Service lazy builders
    # ------------------------------------------------------------------

    def _make_sqlite_reinit(self) -> Callable[[], None]:
        """2026-07-03 P0：clear/import 后重建 sqlite schema 兜底回调。

        走 repo.reset_schema()（仅 SqliteKnowledgeRepo 才有；postgres backend 走
        RepositoryPostgres 时 getattr 返 None，回调变 no-op —— 语义正确因为 postgres
        没有"文件被删导致缺 schema"的问题）。
        """
        reset = getattr(self.repo, "reset_schema", None)
        if callable(reset):
            return reset
        return lambda: None

    def _get_package_service(self) -> KnowledgePackageService:
        if self._package_service is None:
            install_root, sqlite_path, qdrant_local_path = self._resolve_data_paths()
            close, reinit = self._make_qdrant_callbacks()
            self._package_service = KnowledgePackageService(
                install_root=install_root,
                sqlite_path=sqlite_path,
                qdrant_local_path=qdrant_local_path,
                on_qdrant_close=close,
                on_qdrant_reinit=reinit,
                on_sqlite_reinit=self._make_sqlite_reinit(),
            )
        return self._package_service

    def _get_clear_service(self) -> KnowledgeClearService:
        if self._clear_service is None:
            install_root, sqlite_path, qdrant_local_path = self._resolve_data_paths()
            close, reinit = self._make_qdrant_callbacks()
            self._clear_service = KnowledgeClearService(
                install_root=install_root,
                sqlite_path=sqlite_path,
                qdrant_local_path=qdrant_local_path,
                on_qdrant_close=close,
                on_qdrant_reinit=reinit,
                on_sqlite_reinit=self._make_sqlite_reinit(),
                package_service=self._get_package_service(),
            )
        return self._clear_service

    def _resolve_data_paths(self) -> tuple[Path, str, str]:
        """三级 fallback 拿 install_root + sqlite_path + qdrant_local_path。

        - 装机版：``KB_APP_ROOT`` env var 由 tray 设为 ``D:\\KnowledgeBase`` (Win) 或
          ``/Applications/KnowledgeBase`` (Mac)
        - 装机 kb-api 单跑：``SQLITE_PATH`` env var 由 server_entry.py 设成绝对路径，
          从 parent.parent 反推 install_root
        - 开发/测试：都没有 → 用 ``self.project_root`` 兜底

        跟 ``_build_backup_service`` 保持同款 pattern（``_validate_data_path`` 属于
        FastAPI 层不能在这里调，只做 realpath resolve）。
        """
        sqlite_env = os.environ.get("SQLITE_PATH", "").strip()
        qdrant_env = os.environ.get("QDRANT_LOCAL_PATH", "").strip()
        app_root_env = os.environ.get("KB_APP_ROOT", "").strip()

        if sqlite_env:
            sqlite_path = str(Path(sqlite_env).expanduser().resolve())
        else:
            sqlite_path = str((self.project_root / "data" / "knowledge.db").resolve())

        if qdrant_env:
            qdrant_local_path = str(Path(qdrant_env).expanduser().resolve())
        else:
            qdrant_local_path = str(
                (self.project_root / "data" / "qdrant_local").resolve()
            )

        if app_root_env:
            install_root = Path(app_root_env).expanduser().resolve()
        else:
            # 从 sqlite_path parent.parent 反推
            install_root = Path(sqlite_path).parent.parent

        return install_root, sqlite_path, qdrant_local_path

    def _make_qdrant_callbacks(self):
        """构造 qdrant close/reinit 回调，跟 `_build_backup_service` 同款逻辑。

        cp/rm qdrant_local 前必须 close 释放 portalocker 文件锁,操作后 reinit。
        """
        repo = self.repo

        def _close() -> None:
            idx = getattr(repo, "vector_index", None)
            if idx is None:
                return
            if hasattr(idx, "pause"):
                idx.pause()
            else:
                # 旧路径兼容（跟 _build_backup_service 保持一致）
                client = getattr(idx, "_client", None)
                if client is not None and hasattr(client, "close"):
                    try:
                        client.close()
                    except Exception:
                        logger.warning("qdrant client close failed", exc_info=True)
                try:
                    idx._client = None
                except Exception:
                    pass

        def _reinit() -> None:
            idx = getattr(repo, "vector_index", None)
            if idx is None:
                return
            if hasattr(idx, "resume"):
                idx.resume()
            else:
                try:
                    idx._client = None
                except Exception:
                    pass

        return _close, _reinit

    @staticmethod
    def _error_result(
        *, script: str, args: list[str], error: str
    ) -> dict[str, Any]:
        """构造 error 返回结构，跟老 _run_script fail 路径保持格式一致。"""
        return {
            "ok": False,
            "exit_code": 1,
            "script": script,
            "args": args,
            "output": "",
            "error": error,
        }

    # ------------------------------------------------------------------
    # 已废弃：_run_script（保留仅供文档追溯，不再被业务代码调用）
    # ------------------------------------------------------------------

    def _run_script(self, script_name: str, args: list[str]) -> dict[str, Any]:
        """DEPRECATED（2026-07-02 X2.5 refactor）：subprocess exec `.sh` 在 Windows
        撞 WinError 193。所有业务调用已迁移到 in-process service。此方法保留仅供
        追溯，不再有活跃 caller；将来清理时可删除。
        """
        script_path = self.project_root / "scripts" / script_name
        if not script_path.exists():
            raise FileNotFoundError(f"script not found: {script_path}")

        proc = subprocess.run(
            [str(script_path), *args],
            cwd=str(self.project_root),
            text=True,
            capture_output=True,
            check=False,
        )
        output = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "script": script_name,
            "args": args,
            "output": output,
            "error": err,
        }

    @staticmethod
    def _read_bool_env(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}
