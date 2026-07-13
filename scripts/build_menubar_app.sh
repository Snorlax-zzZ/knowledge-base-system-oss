#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_APP="$ROOT_DIR/mac-app/KnowledgeBaseMenuBar.app"
ASSETS_DIR="$ROOT_DIR/mac-app/assets"

OUTPUT_APP="$TEMPLATE_APP"
PROJECT_ROOT="$ROOT_DIR"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      OUTPUT_APP="${2:-}"
      shift 2
      ;;
    --project-root)
      PROJECT_ROOT="${2:-}"
      shift 2
      ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--output <app_path>] [--project-root <runtime_root>]

Prepare KnowledgeBaseMenuBar.app:
- copy from template bundle
- refresh icon/menu assets
- write project_root.txt for runtime command resolution
USAGE
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

# OSS clone / fresh checkout 场景: template 骨架不存在或 Info.plist 缺失, fallback
# 创建默认 Info.plist + 目录结构. 私仓本地 template 已存在 + Info.plist 齐全, 永远
# 跳过 fallback, 行为不变.
INFO_PLIST="$TEMPLATE_APP/Contents/Info.plist"
if [[ ! -d "$TEMPLATE_APP" ]] || [[ ! -f "$INFO_PLIST" ]]; then
  echo "template skeleton incomplete, creating fresh Info.plist at $INFO_PLIST"
  mkdir -p "$TEMPLATE_APP/Contents/MacOS" "$TEMPLATE_APP/Contents/Resources"
  cat > "$INFO_PLIST" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key>
  <string>com.local.knowledgebase.menubar</string>
  <key>CFBundleName</key>
  <string>KnowledgeBaseMenuBar</string>
  <key>CFBundleDisplayName</key>
  <string>KnowledgeBaseMenuBar</string>
  <key>CFBundleExecutable</key>
  <string>KnowledgeBaseMenuBar</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>CFBundleShortVersionString</key>
  <string>1.3.13</string>
  <key>CFBundleIconFile</key>
  <string>KnowledgeBaseMenuBar.icns</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>LSUIElement</key>
  <true/>
  <key>LSMultipleInstancesProhibited</key>
  <true/>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST
fi

# 独立检查 template binary. 覆盖两种场景 (跟上面 plist fallback 独立判定):
#   1. 上面 fallback 刚创建骨架, binary 一定不存在 → 编译
#   2. 骨架 + plist 已存在但 binary 缺失 (前次编译失败留下的半成品) → 重编
# 私仓 build_mac_direct_install_dmg.sh 已先跑 build_menubar_swift.sh, BIN 已 exist,
# 跳过. 行为完全不变.
TEMPLATE_BIN="$TEMPLATE_APP/Contents/MacOS/KnowledgeBaseMenuBar"
if [[ ! -x "$TEMPLATE_BIN" ]]; then
  echo "template binary missing at $TEMPLATE_BIN, invoking build_menubar_swift.sh"
  "$ROOT_DIR/scripts/build_menubar_swift.sh"
fi

if [[ "$OUTPUT_APP" != "$TEMPLATE_APP" ]]; then
  rm -rf "$OUTPUT_APP"
  mkdir -p "$(dirname "$OUTPUT_APP")"
  cp -R "$TEMPLATE_APP" "$OUTPUT_APP"
fi

RES_DIR="$OUTPUT_APP/Contents/Resources"
BIN_PATH="$OUTPUT_APP/Contents/MacOS/KnowledgeBaseMenuBar"

if [[ ! -x "$BIN_PATH" ]]; then
  echo "menubar executable not found: $BIN_PATH" >&2
  exit 1
fi

mkdir -p "$RES_DIR"

for name in KnowledgeBaseMenuBar.icns menu-running-64.png menu-stopped-64.png menu-busy-64.png; do
  if [[ -f "$ASSETS_DIR/$name" ]]; then
    cp "$ASSETS_DIR/$name" "$RES_DIR/$name"
  fi
done

printf '%s\n' "$PROJECT_ROOT" > "$RES_DIR/project_root.txt"

# swiftc 重编可执行文件、刷新资源后，模板里旧的 _CodeSignature 已失效。
# 没有 Developer ID 时至少做 ad-hoc 重签，确保 bundle 自身完整、不会携带一份
# 明知损坏的签名进入 DMG；正式公证仍需发布环境的 Developer ID 身份。
if ! command -v codesign >/dev/null 2>&1; then
  echo "codesign not found（macOS App 组装需要 codesign）" >&2
  exit 1
fi
codesign --force --deep --sign - "$OUTPUT_APP"
codesign --verify --deep --strict --verbose=4 "$OUTPUT_APP"

echo "Prepared menubar app: $OUTPUT_APP"
echo "Runtime project root: $PROJECT_ROOT"
echo "Ad-hoc signature verified: $OUTPUT_APP"
