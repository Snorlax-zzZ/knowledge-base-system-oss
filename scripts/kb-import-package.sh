#!/usr/bin/env bash
# NOTE (2026-07-02 X2.5 refactor):
# 此脚本作为 CLI + Mac MenuBar 便利工具保留(含 osascript 弹选择框逻辑)。
# API endpoint /v1/knowledge/import-package 契约已迁移到
# app/services/knowledge_package.py:KnowledgePackageService.import_package
# (只 mirror 本脚本的 tar.gz 分支; 非 tar.gz 单文件上传走 /v1/knowledge/import-file)。
# Python 版和本脚本产的 tarball 双向互操作 (legacy lane 严格等语义)。
# 详见 docs/_review_inputs/2026-07-02-tarball-format-audit.md。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$ROOT_DIR/logs/import-menu.log"
mkdir -p "$ROOT_DIR/logs"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] kb-import-package start args=$*"
} >>"$LOG_FILE"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<USAGE
Usage: $0 <kb-export-*.tar.gz>
USAGE
  exit 0
fi

pick_package_interactive() {
  if ! command -v osascript >/dev/null 2>&1; then
    return 1
  fi
  osascript <<'APPLESCRIPT'
try
  set pickedFile to choose file with prompt "选择知识库导入包（.tar.gz 或 .tgz）"
  POSIX path of pickedFile
on error number -128
  return ""
end try
APPLESCRIPT
}

PKG_PATH="${1:-}"
if [[ -z "$PKG_PATH" ]]; then
  PKG_PATH="$(pick_package_interactive | tr -d '\r')"
fi
if [[ -z "$PKG_PATH" ]]; then
  echo "Import cancelled by user."
  exit 0
fi
if [[ ! -f "$PKG_PATH" ]]; then
  echo "package not found: $PKG_PATH" >&2
  exit 1
fi

cd "$ROOT_DIR"

if [[ "$PKG_PATH" != *.tar.gz && "$PKG_PATH" != *.tgz ]]; then
  prompt_meta() {
    if ! command -v osascript >/dev/null 2>&1; then
      return 1
    fi
    osascript <<'APPLESCRIPT'
try
  set presetChoice to choose from list {"个人默认（project=personal, domain=personal, type=fact）", "工作默认（project=work, domain=work, type=fact）", "个人-经验（project=personal, domain=personal, type=lesson）", "工作-手册（project=work, domain=work, type=runbook）", "自定义..."} with prompt "该文件不是知识包，将按单文件导入。请选择参数" default items {"个人默认（project=personal, domain=personal, type=fact）"}
  if presetChoice is false then return ""
  set presetVal to item 1 of presetChoice
on error number -128
  return ""
end try

if presetVal starts with "个人默认" then
  set projectVal to "personal"
  set domainVal to "personal"
  set typeVal to "fact"
else if presetVal starts with "工作默认" then
  set projectVal to "work"
  set domainVal to "work"
  set typeVal to "fact"
else if presetVal starts with "个人-经验" then
  set projectVal to "personal"
  set domainVal to "personal"
  set typeVal to "lesson"
else if presetVal starts with "工作-手册" then
  set projectVal to "work"
  set domainVal to "work"
  set typeVal to "runbook"
else
  try
    set projectVal to text returned of (display dialog "输入 project（必填）" default answer "personal")
    if projectVal is "" then return ""
    set domainChoice to choose from list {"personal", "work"} with prompt "选择 domain" default items {"personal"}
    if domainChoice is false then return ""
    set domainVal to item 1 of domainChoice
    set typeChoice to choose from list {"fact", "runbook", "decision", "lesson"} with prompt "选择 type" default items {"fact"}
    if typeChoice is false then return ""
    set typeVal to item 1 of typeChoice
  on error number -128
    return ""
  end try
end if

return projectVal & tab & domainVal & tab & typeVal
APPLESCRIPT
  }

  meta_line="$(prompt_meta | tr -d '\r' || true)"
  if [[ -z "$meta_line" ]]; then
    echo "Import cancelled by user."
    exit 0
  fi
  IFS=$'\t' read -r PROJECT DOMAIN KNOWLEDGE_TYPE <<<"$meta_line"

  if [[ -f "$ROOT_DIR/scripts/kb-ports.sh" ]]; then
    # shellcheck disable=SC1090
    source "$ROOT_DIR/scripts/kb-ports.sh"
  fi
  API_PORT="${KB_PORT_API:-18000}"
  API_URL="http://127.0.0.1:${API_PORT}"

  # 直接走 kb-api 的 in-process 解析端点，避免依赖用户系统 Python + httpx 等包。
  # curl 是 macOS 自带，零外部依赖。
  HTTP_CODE="$(curl -sS -o "$LOG_FILE.body" -w '%{http_code}' \
    -X POST "$API_URL/v1/knowledge/import-file" \
    -F "file=@${PKG_PATH}" \
    -F "project=${PROJECT}" \
    -F "domain=${DOMAIN}" \
    -F "knowledge_type=${KNOWLEDGE_TYPE}" \
    -F "actor=manual" 2>>"$LOG_FILE")"

  {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] import-file http_code=$HTTP_CODE"
    cat "$LOG_FILE.body" 2>/dev/null || true
    echo ""
  } >>"$LOG_FILE"
  rm -f "$LOG_FILE.body"

  if [[ "$HTTP_CODE" =~ ^2 ]]; then
    echo "Import file done: $PKG_PATH"
    exit 0
  else
    # 从 JSON body 里提取 detail 字段；FastAPI 标准错误结构 {"detail": "..."}
    # 用纯 shell（sed）解析避免对 jq 的依赖
    DETAIL=""
    if [[ -f "$LOG_FILE" ]]; then
      # 取最后一段 import-file 的 body：从最后一行 http_code= 之后的内容
      DETAIL="$(tail -n 5 "$LOG_FILE" \
        | sed -n 's/.*"detail":[[:space:]]*"\(.*\)".*/\1/p' \
        | head -n 1)"
    fi
    if [[ -z "$DETAIL" ]]; then
      DETAIL="HTTP ${HTTP_CODE}（详见 ${LOG_FILE}）"
    fi
    # 变量必须用 ${} 显式包，否则中文括号会被 bash 当成变量名一部分（set -u 触发 unbound）
    echo "导入失败（HTTP ${HTTP_CODE}）：${DETAIL}" >&2
    exit 1
  fi
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/kb-import.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

tar -xzf "$PKG_PATH" -C "$TMP_DIR"

RESTORE_SRC=""
if [[ $(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') -eq 1 ]]; then
  RESTORE_SRC="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n1)"
else
  RESTORE_SRC="$TMP_DIR"
fi

"$ROOT_DIR/scripts/backup_restore.sh" "$RESTORE_SRC" >>"$LOG_FILE" 2>&1
echo "Import package done: $PKG_PATH"
