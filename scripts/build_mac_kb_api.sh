#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "missing venv python: $VENV_PY" >&2
  exit 1
fi

if ! "$VENV_PY" -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller not available in .venv. run: .venv/bin/pip install pyinstaller" >&2
  exit 1
fi

mkdir -p "$ROOT_DIR/bin" "$ROOT_DIR/build"

# 2026-07-01 从 --onefile 切 onedir：
# onefile 把 datas 解压到 macOS $TMPDIR（/var/folders/.../T/_MEIxxxxxx/），
# launchd com.apple.launchd.peruser.cleanup 3+ 天后清 tmp 里未 fd 持有的文件，
# HTML/JS/CSS 静态资源被清导致 /console 返 404（进程仍活着 /health 仍 200）。
# onedir 把 datas 放在 exe 同目录 bin/kb-api/_internal/，装到 /Applications/ 下
# 不受 tmp 清理影响，从根本解决"跑 3 天前端 404"问题。
"$VENV_PY" -m PyInstaller \
  --noconfirm \
  --name kb-api \
  --distpath "$ROOT_DIR/bin" \
  --workpath "$ROOT_DIR/build/api-mac" \
  --specpath "$ROOT_DIR/build" \
  --collect-all app \
  --collect-all qdrant_client \
  --collect-all grpc \
  --hidden-import uvicorn.lifespan.on \
  --hidden-import uvicorn.protocols.http.h11_impl \
  --hidden-import uvicorn.protocols.websockets.websockets_impl \
  --hidden-import uvicorn.logging \
  --hidden-import h11 \
  --hidden-import anyio \
  --hidden-import starlette \
  --add-data "$ROOT_DIR/app/static:app/static" \
  --add-data "$ROOT_DIR/scripts:scripts" \
  "$ROOT_DIR/app/server_entry.py"

# onedir 产物：bin/kb-api/ 目录（含 kb-api 可执行 + _internal/ 静态资源）
if [[ ! -x "$ROOT_DIR/bin/kb-api/kb-api" ]]; then
  echo "build failed: bin/kb-api/kb-api not generated (onedir layout)" >&2
  exit 1
fi

echo "Built (onedir): $ROOT_DIR/bin/kb-api/kb-api"
