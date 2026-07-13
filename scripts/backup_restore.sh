#!/usr/bin/env bash
# NOTE (2026-07-02 X2.5 refactor):
# 此脚本作为 CLI + Mac MenuBar (via kb-import-package.sh) 便利工具保留。
# 对应 in-process Python 实现:
# app/services/knowledge_package.py:KnowledgePackageService._restore_from_backup。
# 支持的备份布局 (sqlite / postgres) 跟本脚本严格一致, 双向互操作。
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backup_dir>" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR="$1"
if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "backup dir not found: $BACKUP_DIR" >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/config"

if [[ -f "$BACKUP_DIR/config/config.toml" ]]; then
  cp "$BACKUP_DIR/config/config.toml" "$ROOT_DIR/config/config.toml"
fi

if [[ -f "$BACKUP_DIR/data/knowledge.db" ]]; then
  rm -f "$ROOT_DIR/data/knowledge.db"
  rm -rf "$ROOT_DIR/data/qdrant_local"
  cp "$BACKUP_DIR/data/knowledge.db" "$ROOT_DIR/data/knowledge.db"
  if [[ -d "$BACKUP_DIR/data/qdrant_local" ]]; then
    cp -R "$BACKUP_DIR/data/qdrant_local" "$ROOT_DIR/data/qdrant_local"
  fi
  if [[ -f "$BACKUP_DIR/data/import_state.json" ]]; then
    cp "$BACKUP_DIR/data/import_state.json" "$ROOT_DIR/data/import_state.json"
  fi
  echo "Restore done (backend=sqlite): $BACKUP_DIR"
  exit 0
fi

if [[ -d "$BACKUP_DIR/data/postgres" || -d "$BACKUP_DIR/data/qdrant" || -d "$BACKUP_DIR/data/minio" ]]; then
  rm -rf "$ROOT_DIR/data/postgres" "$ROOT_DIR/data/qdrant" "$ROOT_DIR/data/minio"
  [[ -d "$BACKUP_DIR/data/postgres" ]] && cp -R "$BACKUP_DIR/data/postgres" "$ROOT_DIR/data/postgres"
  [[ -d "$BACKUP_DIR/data/qdrant" ]] && cp -R "$BACKUP_DIR/data/qdrant" "$ROOT_DIR/data/qdrant"
  [[ -d "$BACKUP_DIR/data/minio" ]] && cp -R "$BACKUP_DIR/data/minio" "$ROOT_DIR/data/minio"
  echo "Restore done (backend=postgres): $BACKUP_DIR"
  exit 0
fi

echo "restore failed: unsupported backup layout in $BACKUP_DIR" >&2
exit 1
