#!/usr/bin/env bash
# NOTE (2026-07-02 X2.5 refactor):
# 此脚本作为 CLI + Mac MenuBar 便利工具保留。
# API endpoint /v1/knowledge/cleanup-expired 契约已迁移到
# app/services/knowledge_package.py + app/services/knowledge_clear.py。
# shell 版和 Python 版严格等语义(tarball 结构、stdout 输出、行为契约一致),
# 详见 docs/_review_inputs/2026-07-02-tarball-format-audit.md。
set -euo pipefail

MODE="archive"
AS_OF=""
BACKUP_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --as-of)
      AS_OF="${2:-}"
      shift 2
      ;;
    --backup-dir)
      BACKUP_DIR="${2:-}"
      shift 2
      ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--mode archive|delete] [--as-of YYYY-MM-DD] [--backup-dir path]

archive mode: create a backup/export package, do not delete data.
delete mode : backup first, then clear knowledge base.
USAGE
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ "$MODE" != "archive" && "$MODE" != "delete" ]]; then
  echo "mode must be archive or delete" >&2
  exit 1
fi

if [[ "$MODE" == "archive" ]]; then
  if [[ -n "$BACKUP_DIR" ]]; then
    "$ROOT_DIR/scripts/backup_create.sh" "$BACKUP_DIR"
  else
    "$ROOT_DIR/scripts/backup_create.sh"
  fi
  echo "Expired knowledge archived (mode=archive, as_of=${AS_OF:-N/A})"
  exit 0
fi

if [[ -n "$BACKUP_DIR" ]]; then
  "$ROOT_DIR/scripts/kb-clear.sh" --yes "$BACKUP_DIR"
else
  "$ROOT_DIR/scripts/kb-clear.sh" --yes
fi
echo "Expired knowledge cleaned (mode=delete, as_of=${AS_OF:-N/A})"
