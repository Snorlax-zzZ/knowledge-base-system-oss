#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_PATH="${KB_CONFIG_TOML_PATH:-$ROOT_DIR/config/config.toml}"
DEFAULT_PORT=18000

read_port() {
  local cfg="$1"
  if [[ ! -f "$cfg" ]]; then
    echo "$DEFAULT_PORT"
    return
  fi
  awk '
    BEGIN { in_server=0 }
    /^\s*\[server\]\s*$/ { in_server=1; next }
    /^\s*\[/ && in_server==1 { exit }
    in_server==1 && /^\s*port\s*=\s*[0-9]+\s*$/ {
      gsub(/.*=/, "", $0)
      gsub(/[[:space:]]/, "", $0)
      print $0
      exit
    }
  ' "$cfg" | head -n1
}

PORT="$(read_port "$CONFIG_PATH")"
if [[ -z "$PORT" ]]; then
  PORT="$DEFAULT_PORT"
fi

# stop phase: try pid file first, fallback to process pattern
if [[ -f "$ROOT_DIR/data/.local_api.pid" ]]; then
  PID="$(cat "$ROOT_DIR/data/.local_api.pid" 2>/dev/null || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" || true
    sleep 1
  fi
  rm -f "$ROOT_DIR/data/.local_api.pid"
fi
pkill -f "uvicorn app.main:app" >/dev/null 2>&1 || true
pkill -f "kb-api" >/dev/null 2>&1 || true

mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs"

# 2026-07-01 加 KB_APP_ROOT export：让 kb-api server_entry._install_root() 拿到
# 明确根路径，避免推断漂移（onefile → onedir 迁移期特别重要，配置/token/version
# 文件路径必须锁定在 ROOT_DIR，不能被 sys.executable 反推）。
export KB_APP_ROOT="$ROOT_DIR"

# 2026-07-01 onedir 布局：bin/kb-api/kb-api（新）；兼容旧 onefile 布局：bin/kb-api（老 dmg build）
KB_API_BIN=""
if [[ -x "$ROOT_DIR/bin/kb-api/kb-api" ]]; then
  KB_API_BIN="$ROOT_DIR/bin/kb-api/kb-api"
elif [[ -x "$ROOT_DIR/bin/kb-api" ]] && [[ ! -d "$ROOT_DIR/bin/kb-api" ]]; then
  # 老 onefile 布局：bin/kb-api 是文件不是目录
  KB_API_BIN="$ROOT_DIR/bin/kb-api"
fi

if [[ -n "$KB_API_BIN" ]]; then
  nohup "$KB_API_BIN" >/dev/null 2>&1 &
  echo "Local knowledge base restarted via kb-api (port=$PORT, bin=$KB_API_BIN)"
  exit 0
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  export KB_BACKEND=sqlite
  export SQLITE_PATH="$ROOT_DIR/data/knowledge.db"
  export VECTOR_ENABLED=1
  export QDRANT_MODE=local
  export QDRANT_LOCAL_PATH="$ROOT_DIR/data/qdrant_local"
  export UVICORN_WORKERS=1
  nohup "$ROOT_DIR/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1 \
    > "$ROOT_DIR/logs/api.log" 2> "$ROOT_DIR/logs/api.err.log" &
  echo $! > "$ROOT_DIR/data/.local_api.pid"
  echo "Local knowledge base restarted via uvicorn (port=$PORT)"
  exit 0
fi

echo "restart failed: no runnable entry found (.venv/bin/python or bin/kb-api)" >&2
exit 1
