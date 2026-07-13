#!/usr/bin/env bash
# NOTE (2026-07-02 X2.5 refactor):
# 此脚本作为 CLI + Mac MenuBar 便利工具保留。
# API endpoint /v1/knowledge/export-package 契约已迁移到
# app/services/knowledge_package.py:KnowledgePackageService.export_package。
# Python 版产的 tarball 顶层含 {ts}/ 目录, 结构跟本脚本严格一致 (legacy lane),
# 可跟本脚本产的 tarball 双向互操作。
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<USAGE
Usage: $0 [export_dir]

Create a full cross-machine export package (.tar.gz), including:
- PostgreSQL dump
- Qdrant storage
- MinIO storage
USAGE
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

TS="$(date '+%Y%m%d_%H%M%S')"
EXPORT_DIR="${1:-$ROOT_DIR/exports}"
mkdir -p "$EXPORT_DIR"

BACKUP_DIR="$ROOT_DIR/backups/$TS"
"$ROOT_DIR/scripts/backup_create.sh" "$BACKUP_DIR"

OUT="$EXPORT_DIR/kb-export-$TS.tar.gz"
tar -czf "$OUT" -C "$ROOT_DIR/backups" "$TS"

echo "Export package created: $OUT"
