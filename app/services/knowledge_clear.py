"""Legacy 知识库清空 lane 的 in-process 实现（严格 mirror shell 版 kb-clear.sh）。

跟 `KnowledgePackageService` 同源，服务 `/v1/knowledge/clear` 端点，替代
`subprocess.run(['kb-clear.sh', ...])` 在 Windows 撞 WinError 193 的老路径。

**跟 BackupService.import_overwrite / repo.clear_all_active_data() 的关键差异**：

- shell 版 `kb-clear.sh` 是 **factory reset 语义**：``rm data/knowledge.db`` +
  ``rm -rf data/qdrant_local`` + 重建空目录 → **连 system_config 一起清**
  （因为 system_config 表也在 knowledge.db 里）
- ``repo.clear_all_active_data()`` 只清业务表，**保留 system_config**（跟 shell 版语义不一致）
- 因此本 service **不能** 直接调 ``repo.clear_all_active_data()``，必须走文件级 rm
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from app.services.knowledge_package import (
    KnowledgePackageError,
    KnowledgePackageService,
    _detect_backend,
)


logger = logging.getLogger(__name__)


class KnowledgeClearService:
    """Legacy 知识库清空 lane 的 in-process 实现。

    严格 mirror `scripts/kb-clear.sh` 行为：
      1. 强制 confirm（endpoint 层校验，本 service 不再判 --yes）
      2. 调 `package_service.create_backup(backup_dir)` 生成备份（等价 shell 版
         line 22-26 的 backup_create.sh 调用）
      3. detect_backend
      4. sqlite backend：rm data/knowledge.db + import_state.json + qdrant_local + 重建空
         qdrant_local；stdout ``"Knowledge base cleared (backend=sqlite)"``
      5. postgres backend：rm 3 目录 + 重建；stdout ``"Knowledge base cleared (backend=postgres)"``

    构造参数：跟 KnowledgePackageService 同源，还接受一个 ``package_service`` 引用用来
    调 create_backup（避免 KnowledgeClearService 自己再实现一遍 backup 逻辑）。
    """

    def __init__(
        self,
        install_root: Path,
        sqlite_path: str,
        qdrant_local_path: str,
        on_qdrant_close: Callable[[], None],
        on_qdrant_reinit: Callable[[], None],
        on_sqlite_reinit: Callable[[], None],
        package_service: KnowledgePackageService,
    ) -> None:
        self.install_root = Path(install_root)
        self.sqlite_path = Path(sqlite_path)
        self.qdrant_local_path = Path(qdrant_local_path)
        self.on_qdrant_close = on_qdrant_close
        self.on_qdrant_reinit = on_qdrant_reinit
        # 2026-07-03 P0 修复：clear 走文件级 rm knowledge.db 后 sqlite3.connect(path)
        # 会自动重建空 db 但缺 schema → 后续所有请求撞 no such table 500。必须显式
        # 触发 CREATE TABLE IF NOT EXISTS 重建 schema。跟 on_qdrant_reinit 对称。
        # 未修前实测：clear POST 200 但 kb-api 半死，后续 status/search/import 全 500。
        self.on_sqlite_reinit = on_sqlite_reinit
        self.package_service = package_service

    def clear(self, backup_dir: str | None = None) -> dict[str, Any]:
        """严格 mirror ``scripts/kb-clear.sh --yes [backup_dir]``。

        - 先调 package_service.create_backup（相当于 shell 版 line 22-26 backup_create.sh）
        - 再走文件级 rm + mkdir 重建
        - 返回结构跟 KnowledgeMcpTools._run_script 保持一致（``ok / exit_code / script /
          args / output / error``）
        """
        # 触发 backup（等价 shell 版 backup_create.sh 调用），忽略返回路径
        self.package_service.create_backup(backup_dir)

        backend = _detect_backend(self.install_root, os.getenv("KB_BACKEND"))
        (self.install_root / "data").mkdir(parents=True, exist_ok=True)

        # cp/rm qdrant 前必须释放文件锁
        self.on_qdrant_close()
        try:
            if backend == "sqlite":
                # shell 版：rm -f data/knowledge.db data/import_state.json
                #          rm -rf data/qdrant_local
                #          mkdir -p data/qdrant_local
                if self.sqlite_path.exists():
                    self.sqlite_path.unlink()
                import_state = self.install_root / "data" / "import_state.json"
                if import_state.exists():
                    import_state.unlink()
                if self.qdrant_local_path.exists():
                    shutil.rmtree(self.qdrant_local_path)
                self.qdrant_local_path.mkdir(parents=True, exist_ok=True)
                output = "Knowledge base cleared (backend=sqlite)"
            else:
                # postgres backend（严格 mirror shell 版 line 51-53）
                for name in ("postgres", "qdrant", "minio"):
                    target = self.install_root / "data" / name
                    if target.exists():
                        shutil.rmtree(target)
                    target.mkdir(parents=True, exist_ok=True)
                output = "Knowledge base cleared (backend=postgres)"
        finally:
            self.on_qdrant_reinit()
            # 2026-07-03 P0：重建 sqlite schema（sqlite backend 才有意义，postgres
            # backend 的 KnowledgeRepo 是不同实现，reset_schema 是 no-op 也无副作用）。
            # 放在 finally 保证 rmtree/rm 出异常时也能兜底建表，避免 kb-api 半死态。
            if backend == "sqlite":
                self.on_sqlite_reinit()

        logger.info(
            "op=knowledge_clear result=ok backend=%s backup_dir=%s",
            backend,
            backup_dir or "<default>",
        )
        return {
            "ok": True,
            "exit_code": 0,
            "script": "kb-clear.sh",
            "args": ["--yes"] + ([backup_dir] if backup_dir else []),
            "output": output,
            "error": "",
        }
