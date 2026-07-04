"""目录增量导入 in-process 实现（跨平台，绕开 shell 脚本）。

设计动机：旧实现 ``mcp_tools.import_incremental_knowledge`` 调
``scripts/kb-import-incremental.sh``，里面跑 osascript（macOS 原生对话框）+
``python scripts/import_incremental.py --api-url ...`` HTTP 调用。Windows 没
bash 也没 osascript → ``subprocess.run`` 抛 OSError → endpoint 500。

按 design.md §3.3 "app/ 内零平台分支" 原则，导入逻辑应该是 in-process Python，
不依赖任何 shell 或外部进程。本模块即此实现，复用 ``app/main.py:/v1/knowledge/
import-file`` 端点的核心三步：``parse_document()`` → ``UpsertRequest()`` →
``KnowledgeService(repo).upsert()``。

关于增量（hash state）：v1 实现先做全量遍历（每次跑都重新 upsert 所有文件），
依赖 ``KnowledgeService.upsert`` 的 upsert 语义（同 source_uri 已存在则 update
+ bump version，不重复创建）。后续如有性能需求再补 hash state（``.kb-import-
state.json``）做真增量跳过未变文件。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas import UpsertRequest
from app.service import KnowledgeService
from app.services.import_document import (
    EmptyDocumentError,
    ParseDependencyError,
    UnsupportedFileTypeError,
    parse_document,
)


logger = logging.getLogger(__name__)


# 支持的文件类型（跟 /v1/knowledge/import-file endpoint 白名单一致）
_SUPPORTED_PATTERNS = ("*.md", "*.markdown", "*.txt", "*.pdf", "*.docx")

# 扫描时跳过的目录名（典型 dev / build 残留）
_SKIP_DIR_PARTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".idea", ".vscode", "dist", "build", "bin", ".cache",
})


def _collect_files(root: Path) -> list[Path]:
    """递归扫 root 下匹配 ``_SUPPORTED_PATTERNS`` 的文件,排除 ``_SKIP_DIR_PARTS``。"""
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in _SUPPORTED_PATTERNS:
        for p in root.rglob(pattern):
            if not p.is_file():
                continue
            if any(part in _SKIP_DIR_PARTS for part in p.parts):
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(p)
    files.sort()
    return files


def incremental_import_directory(
    *,
    service: KnowledgeService,
    directory: str,
    project: str,
    domain: str,
    knowledge_type: str,
    actor: str = "incremental-import",
) -> dict[str, Any]:
    """目录批量增量导入；in-process,跨平台。

    返回结构跟旧 ``_run_script`` ``kb-import-incremental.sh`` 输出对齐
    （``status / root / total_candidates / imported / failed / successes /
    failures``),方便 mcp_tools 调用方零适配。
    """
    root = Path(directory)
    if not root.exists():
        return {
            "status": "error",
            "root": str(root),
            "error": f"directory does not exist: {root}",
        }
    if not root.is_dir():
        return {
            "status": "error",
            "root": str(root),
            "error": f"not a directory: {root}",
        }

    candidates = _collect_files(root)
    if not candidates:
        return {
            "status": "ok",
            "root": str(root),
            "total_candidates": 0,
            "imported": 0,
            "failed": 0,
            "successes": [],
            "failures": [],
        }

    successes: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for fpath in candidates:
        rel_path = str(fpath.relative_to(root)) if fpath.is_relative_to(root) else str(fpath)
        try:
            payload = parse_document(
                fpath,
                project=project,
                domain=domain,
                knowledge_type=knowledge_type,
                actor=actor,
                source_uri=fpath.resolve().as_uri(),
            )
            req = UpsertRequest(**payload)
            result = service.upsert(req)
            successes.append({
                "file": rel_path,
                "knowledge_item_id": result.get("knowledge_item_id"),
                "version": result.get("version"),
            })
        except UnsupportedFileTypeError as exc:
            failures.append({"file": rel_path, "error": f"unsupported: {exc}"})
        except EmptyDocumentError as exc:
            failures.append({"file": rel_path, "error": f"empty: {exc}"})
        except ParseDependencyError as exc:
            failures.append({"file": rel_path, "error": f"parse_dep: {exc}"})
        except ValidationError as exc:
            failures.append({"file": rel_path, "error": f"schema: {exc.errors()}"})
        except ValueError as exc:
            # KnowledgeService.upsert 内部业务校验抛 ValueError
            failures.append({"file": rel_path, "error": f"upsert: {exc}"})
        except Exception as exc:  # noqa: BLE001
            logger.exception("incremental_import: unexpected failure on %s", fpath)
            failures.append({"file": rel_path, "error": f"unexpected: {type(exc).__name__}: {exc}"})

    status = "ok" if not failures else ("partial" if successes else "error")
    return {
        "status": status,
        "root": str(root),
        "total_candidates": len(candidates),
        "imported": len(successes),
        "failed": len(failures),
        "successes": successes,
        "failures": failures,
    }
