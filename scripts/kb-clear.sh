#!/usr/bin/env bash
# NOTE (2026-07-02 X2.5 refactor):
# 此脚本作为 CLI + Mac MenuBar 便利工具保留。
# API endpoint /v1/knowledge/clear 契约已迁移到 app/services/knowledge_clear.py。
# shell 版和 Python 版严格等语义(factory reset: rm data/knowledge.db +
# import_state.json + qdrant_local 后重建空 qdrant_local, system_config 也一起清)。
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<USAGE
Usage: $0 --yes [backup_dir]

Dangerous operation: clear primary store and vector store.
USAGE
  exit 0
fi

if [[ "${1:-}" != "--yes" ]]; then
  echo "refuse to clear without --yes" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BACKUP_DIR_ARG="${2:-}"
if [[ -n "$BACKUP_DIR_ARG" ]]; then
  "$ROOT_DIR/scripts/backup_create.sh" "$BACKUP_DIR_ARG" >/dev/null
else
  "$ROOT_DIR/scripts/backup_create.sh" >/dev/null
fi

detect_backend() {
  if [[ -n "${KB_BACKEND:-}" ]]; then
    echo "$KB_BACKEND"
    return
  fi
  if [[ -f "$ROOT_DIR/data/knowledge.db" || -d "$ROOT_DIR/data/qdrant_local" ]]; then
    echo "sqlite"
    return
  fi
  echo "postgres"
}

BACKEND="$(detect_backend)"
mkdir -p "$ROOT_DIR/data"

if [[ "$BACKEND" == "sqlite" ]]; then
  rm -f "$ROOT_DIR/data/knowledge.db" "$ROOT_DIR/data/import_state.json"
  rm -rf "$ROOT_DIR/data/qdrant_local"
  mkdir -p "$ROOT_DIR/data/qdrant_local"
  echo "Knowledge base cleared (backend=sqlite)"
  exit 0
fi

rm -rf "$ROOT_DIR/data/postgres" "$ROOT_DIR/data/qdrant" "$ROOT_DIR/data/minio"
mkdir -p "$ROOT_DIR/data/postgres" "$ROOT_DIR/data/qdrant" "$ROOT_DIR/data/minio"
echo "Knowledge base cleared (backend=postgres)"
