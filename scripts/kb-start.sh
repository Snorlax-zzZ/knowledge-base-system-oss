#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/.local_api.pid"
LOG_OUT="$ROOT_DIR/logs/api.log"
LOG_ERR="$ROOT_DIR/logs/api.err.log"
source "$ROOT_DIR/scripts/kb-ports.sh"

# PyInstaller frozen binary 下 __file__ 指向 _MEIxxx 临时目录，读不到 VERSION 文件
# 显式给后端指定安装根目录，让 app/main.py 的 _load_app_version 能命中第一个候选路径
export KB_APP_ROOT="$ROOT_DIR"

wait_healthy() {
  local port="$1"
  local pid="${2:-}"
  local attempts="${3:-120}"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
      return 0
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" >/dev/null 2>&1; then
      return 1
    fi
    sleep 0.5
  done
  return 1
}

PORT="${KB_PORT_API:-18000}"

if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "Knowledge base already running (port=$PORT)"
  exit 0
fi

# health 不通但可能有僵尸 kb-api 进程（health 卡死 / 异常退出未清理 PID file / 退出 App 时被系统强杀）
# 启动前强杀残留，避免新进程 bind 端口冲突
cleanup_stale() {
  local killed=0

  # 1. PID file 里的进程（如果还活着）
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(tr -d '[:space:]' < "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      for _ in {1..10}; do
        kill -0 "$pid" >/dev/null 2>&1 || break
        sleep 0.2
      done
      kill -9 "$pid" >/dev/null 2>&1 || true
      killed=1
    fi
    rm -f "$PID_FILE"
  fi

  # 2. 端口占用的 kb-api 进程兜底（PID file 丢失场景）
  if command -v lsof >/dev/null 2>&1; then
    local port_pids
    port_pids="$(lsof -ti tcp:"$PORT" 2>/dev/null || true)"
    if [[ -n "$port_pids" ]]; then
      while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        local cmd
        cmd="$(ps -o comm= -p "$pid" 2>/dev/null || true)"
        if [[ "$cmd" == *"kb-api"* ]] || [[ "$cmd" == *"uvicorn"* ]] || [[ "$cmd" == *"python"* ]]; then
          kill -9 "$pid" >/dev/null 2>&1 || true
          killed=1
        fi
      done <<< "$port_pids"
    fi
  fi

  if [[ "$killed" -eq 1 ]]; then
    echo "cleaned up stale kb-api processes before start (port=$PORT)"
    sleep 0.5
  fi
}
cleanup_stale

mkdir -p "$ROOT_DIR/data" "$ROOT_DIR/logs"

# 2026-07-01 onedir 布局：bin/kb-api/kb-api（新）；兼容旧 onefile 布局：bin/kb-api（老 dmg build）
# 注意：`-x <dir>` 对目录返回 true（+x = 可进入），必须先判目录再挑真二进制，
# 否则 nohup 一个目录会得到 "Permission denied"。
KB_API_BIN=""
if [[ -x "$ROOT_DIR/bin/kb-api/kb-api" ]]; then
  KB_API_BIN="$ROOT_DIR/bin/kb-api/kb-api"
elif [[ -x "$ROOT_DIR/bin/kb-api" ]] && [[ ! -d "$ROOT_DIR/bin/kb-api" ]]; then
  KB_API_BIN="$ROOT_DIR/bin/kb-api"
fi

if [[ -n "$KB_API_BIN" ]]; then
  nohup "$KB_API_BIN" >"$LOG_OUT" 2>"$LOG_ERR" &
  pid=$!
  echo "$pid" > "$PID_FILE"

  if wait_healthy "$PORT" "$pid"; then
    echo "Knowledge base started via kb-api (port=$PORT)"
    exit 0
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "Knowledge base process started (pid=$pid), health endpoint not ready yet (port=$PORT)"
    exit 0
  fi

  rm -f "$PID_FILE"
  echo "kb-api start failed (port=$PORT), see logs:" >&2
  echo "  $LOG_OUT" >&2
  echo "  $LOG_ERR" >&2
fi

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  export KB_BACKEND=sqlite
  export SQLITE_PATH="$ROOT_DIR/data/knowledge.db"
  export VECTOR_ENABLED=1
  export QDRANT_MODE=local
  export QDRANT_LOCAL_PATH="$ROOT_DIR/data/qdrant_local"
  export UVICORN_WORKERS=1
  nohup "$ROOT_DIR/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --workers 1 \
    >"$LOG_OUT" 2>"$LOG_ERR" &
  pid=$!
  echo "$pid" > "$PID_FILE"

  if wait_healthy "$PORT" "$pid"; then
    echo "Knowledge base started via uvicorn (port=$PORT)"
    exit 0
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "Knowledge base process started (pid=$pid), health endpoint not ready yet (port=$PORT)"
    exit 0
  fi

  rm -f "$PID_FILE"
  echo "uvicorn start failed (port=$PORT), see logs:" >&2
  echo "  $LOG_OUT" >&2
  echo "  $LOG_ERR" >&2
fi

echo "start failed: no runnable entry found or process not healthy" >&2
exit 1
