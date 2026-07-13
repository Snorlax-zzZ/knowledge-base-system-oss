#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="$ROOT_DIR/data/.local_api.pid"
source "$ROOT_DIR/scripts/kb-ports.sh"

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

  # 每次 signal 前重新核验完整 command，缩小陈旧 PID/PID 复用误杀窗口。
  is_owned_api_process "$pid" || return 1
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..15}; do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  if kill -0 "$pid" >/dev/null 2>&1 && is_owned_api_process "$pid"; then
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
  return 0
}

PORT="${KB_PORT_API:-18000}"

stopped=0

if [[ -f "$PID_FILE" ]]; then
  pid="$(tr -d '[:space:]' < "$PID_FILE" || true)"
  if stop_owned_pid "$pid"; then
    stopped=1
  fi
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if stop_owned_pid "$pid"; then
      stopped=1
    fi
  done < <(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
fi

if command -v pgrep >/dev/null 2>&1; then
  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if stop_owned_pid "$pid"; then
      stopped=1
    fi
  done < <(pgrep -f "$ROOT_DIR/bin/kb-api" 2>/dev/null || true)

  while IFS= read -r pid; do
    [[ -z "$pid" ]] && continue
    if stop_owned_pid "$pid"; then
      stopped=1
    fi
  done < <(pgrep -f "uvicorn app.main:app" 2>/dev/null || true)
fi

if [[ "$stopped" -eq 1 ]]; then
  echo "Knowledge base stopped (port=$PORT)"
  exit 0
fi

if [[ -f "$ROOT_DIR/docker-compose.yml" || -f "$ROOT_DIR/docker-compose.yaml" ]]; then
  if command -v docker >/dev/null 2>&1; then
    docker compose stop api postgres qdrant prometheus grafana >/dev/null
    echo "Knowledge base docker services stopped"
    exit 0
  fi
fi

echo "Knowledge base not running (port=$PORT)"
exit 0
