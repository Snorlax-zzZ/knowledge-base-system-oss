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

# 串行化完整 API 生命周期窗口（health check → stale cleanup → spawn →
# wait healthy）。macOS 使用原生 lockf，Linux 使用 flock，均锁 shell 已打开
# 的 FD9。restart.sh 会带同一个 FD9 exec 本脚本，held marker 只有在 FD9
# 确实存在时才可信。
mkdir -p "$ROOT_DIR/runtime" "$ROOT_DIR/data" "$ROOT_DIR/logs"
LIFECYCLE_LOCK="$ROOT_DIR/runtime/kb-api-lifecycle.lock"

fd_is_open() {
  local fd="$1"
  [[ "$fd" == "9" ]] || return 1
  { true >&9; } 2>/dev/null
}

if [[ "${KB_API_LIFECYCLE_LOCK_HELD:-0}" != "1" ]] || ! fd_is_open 9; then
  unset KB_API_LIFECYCLE_LOCK_HELD
  exec 9>"$LIFECYCLE_LOCK"

  if [[ "$(uname -s)" == "Darwin" ]] && [[ -x /usr/bin/lockf ]]; then
    if ! /usr/bin/lockf -s -t 120 9; then
      echo "timed out waiting for kb-api lifecycle lock: $LIFECYCLE_LOCK" >&2
      exit 1
    fi
  elif command -v flock >/dev/null 2>&1; then
    if ! flock -w 120 9; then
      echo "timed out waiting for kb-api lifecycle lock: $LIFECYCLE_LOCK" >&2
      exit 1
    fi
  else
    echo "no supported file-lock utility (macOS lockf or Linux flock); refusing unlocked kb-api start" >&2
    exit 1
  fi
fi

# marker 只用于 restart → exec kb-start 的一次锁交接；不能传给长期 API
# child，否则 child 后续拉起脚本时可能错误绕过加锁。
unset KB_API_LIFECYCLE_LOCK_HELD

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

is_owned_api_process() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1

  local cmd
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  cmd="${cmd#"${cmd%%[![:space:]]*}"}"
  [[ -n "$cmd" ]] || return 1

  local frozen_onedir="$ROOT_DIR/bin/kb-api/kb-api"
  local frozen_onefile="$ROOT_DIR/bin/kb-api"
  case "$cmd" in
    "$frozen_onedir"|"$frozen_onedir "*|"$frozen_onefile"|"$frozen_onefile "*)
      return 0
      ;;
  esac

  local venv_python="$ROOT_DIR/.venv/bin/python"
  case "$cmd" in
    "$venv_python"|"$venv_python "*)
      case " $cmd " in
        *" -m uvicorn app.main:app "*) return 0 ;;
      esac
      ;;
  esac
  return 1
}

stop_owned_pid() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1 || return 1

  # 每次 signal 前重新读取完整 command，避免陈旧 PID file 或 PID 复用误杀。
  is_owned_api_process "$pid" || return 1
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..10}; do
    kill -0 "$pid" >/dev/null 2>&1 || return 0
    sleep 0.2
  done
  if kill -0 "$pid" >/dev/null 2>&1 && is_owned_api_process "$pid"; then
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
  return 0
}

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
    if stop_owned_pid "$pid"; then
      killed=1
    fi
    # 无论 PID 是否仍存活/属于本安装根，陈旧 PID file 都必须清掉。
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
        if stop_owned_pid "$pid"; then
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
  # 长生命周期 child 必须关闭 FD9；否则启动脚本退出后锁仍被 child 持有。
  nohup "$KB_API_BIN" >"$LOG_OUT" 2>"$LOG_ERR" 9>&- &
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
    >"$LOG_OUT" 2>"$LOG_ERR" 9>&- &
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
