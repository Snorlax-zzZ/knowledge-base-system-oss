#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/mac-app/KnowledgeBaseMenuBar.app"
BIN_PATH="$APP_DIR/Contents/MacOS/KnowledgeBaseMenuBar"

# 目录或 binary 缺失都要重新 build. 只 check 目录会 open 半成品 App
# (前次 build_menubar_app.sh 失败留下的骨架 + plist 但没 binary 场景).
if [[ ! -d "$APP_DIR" ]] || [[ ! -x "$BIN_PATH" ]]; then
  "$ROOT_DIR/scripts/build_menubar_app.sh"
fi

open "$APP_DIR"

echo "Opened: $APP_DIR"
