#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/kb-ports.sh"

# restart 必须与普通 start 共用同一把锁，并在锁内完成 stop → wait/clean →
# start。FD9 随 exec 传给 kb-start.sh；显式标记让它复用已持有的锁。
mkdir -p "$ROOT_DIR/runtime" "$ROOT_DIR/data" "$ROOT_DIR/logs"
LIFECYCLE_LOCK="$ROOT_DIR/runtime/kb-api-lifecycle.lock"
exec 9>"$LIFECYCLE_LOCK"
if ! /usr/bin/lockf -s -t 120 9; then
  echo "timed out waiting for kb-api lifecycle lock: $LIFECYCLE_LOCK" >&2
  exit 1
fi
export KB_API_LIFECYCLE_LOCK_HELD=1

# kb-stop.sh 已包含 PID file、监听端口和进程路径三层清理，并等待 TERM 后
# 必要时 KILL。锁仍由本 shell 的 FD9 持有，期间不会有另一个 kb-start 抢跑。
"$ROOT_DIR/scripts/kb-stop.sh"

# 启动统一委托 kb-start.sh：PID、日志、stale cleanup 和健康等待只保留一套。
# exec 会保留 FD9；kb-start 在 spawn 长生命周期 child 时主动 9>&-。
exec "$ROOT_DIR/scripts/kb-start.sh"
