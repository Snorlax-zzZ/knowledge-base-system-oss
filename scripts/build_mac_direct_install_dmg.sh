#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<USAGE
Usage: $0 [--build-api] [version]

Build a macOS direct-install dmg package.
Preconditions:
1) bin/kb-api exists (or pass --build-api)
2) mac-app/KnowledgeBaseMenuBar.app exists
USAGE
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

BUILD_API=0
VERSION="1.0.0"

if [[ "${1:-}" == "--build-api" ]]; then
  BUILD_API=1
  shift
fi
if [[ $# -ge 1 ]]; then
  VERSION="$1"
fi
TS="$(date '+%Y%m%d_%H%M%S')"
DIST_DIR="$ROOT_DIR/dist"
WORK_DIR="$DIST_DIR/mac-direct-install-$TS"
PAYLOAD_DIR="$WORK_DIR/KnowledgeBase"
DMG_NAME="KnowledgeBase-mac-direct-${VERSION}.dmg"
DMG_PATH="$DIST_DIR/$DMG_NAME"

if [[ "$BUILD_API" -eq 1 ]]; then
  "$ROOT_DIR/scripts/build_mac_kb_api.sh"
fi

# 2026-07-01 起 build_mac_kb_api.sh 产 onedir 布局：bin/kb-api/kb-api（目录 + 内部可执行）
if [[ ! -x "$ROOT_DIR/bin/kb-api/kb-api" ]]; then
  echo "missing bin/kb-api/kb-api (onedir). run scripts/build_mac_kb_api.sh first." >&2
  exit 1
fi

# 菜单栏 App binary 必须每次重编（mac-app/MenuBarApp/*.swift 有任何改动都得反映
# 到 dmg payload；曾经踩坑：Phase 3b 改了 Swift 源码但 build 流程没编，连续 4
# 个 dmg 装机后菜单栏 App 不动）。swiftc 编译很快（<5s），无脑跑。
"$ROOT_DIR/scripts/build_menubar_swift.sh"

if ! command -v hdiutil >/dev/null 2>&1; then
  echo "hdiutil not found (macOS only)" >&2
  exit 1
fi

rm -rf "$WORK_DIR"
mkdir -p "$PAYLOAD_DIR/bin" "$PAYLOAD_DIR/config" "$PAYLOAD_DIR/data" "$PAYLOAD_DIR/logs" "$PAYLOAD_DIR/scripts" "$PAYLOAD_DIR/agent-integration"

# onedir 布局：cp 整个 bin/kb-api/ 目录（含可执行 + _internal/ 静态资源）
cp -R "$ROOT_DIR/bin/kb-api" "$PAYLOAD_DIR/bin/"
chmod +x "$PAYLOAD_DIR/bin/kb-api/kb-api"

"$ROOT_DIR/scripts/build_menubar_app.sh" \
  --output "$PAYLOAD_DIR/KnowledgeBaseMenuBar.app" \
  --project-root "/Applications/KnowledgeBase"

cp "$ROOT_DIR/config/config.toml" "$PAYLOAD_DIR/config/config.toml"
cp "$ROOT_DIR/mac-app/restart.sh" "$PAYLOAD_DIR/scripts/restart.sh"
chmod +x "$PAYLOAD_DIR/scripts/restart.sh"

for f in kb-start.sh kb-stop.sh kb-status.sh kb-ports.sh kb-export-package.sh kb-import-package.sh kb-clear.sh kb-clean-expired.sh kb-import-incremental.sh backup_create.sh backup_restore.sh; do
  cp "$ROOT_DIR/scripts/$f" "$PAYLOAD_DIR/scripts/$f"
  chmod +x "$PAYLOAD_DIR/scripts/$f"
done

# Import flows depend on these helper Python scripts.
for f in import_incremental.py import_document.py import_markdown.py; do
  cp "$ROOT_DIR/scripts/$f" "$PAYLOAD_DIR/scripts/$f"
done
for f in enrichment.py ocr_extract.py; do
  if [[ -f "$ROOT_DIR/scripts/$f" ]]; then
    cp "$ROOT_DIR/scripts/$f" "$PAYLOAD_DIR/scripts/$f"
  fi
done

# Agent integration: MCP server 实现 + Skill 主干 + AI 自助接入指南，
# 整目录拷贝，跟 Windows installer 的 agent-integration\* 递归打包行为一致。
# 排除 __pycache__ / .pyc 等运行时产物。
rsync -a \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$ROOT_DIR/agent-integration/" "$PAYLOAD_DIR/agent-integration/"

# 开发者源码直连调试入口（非用户路径，仅给排障用）
cp "$ROOT_DIR/scripts/install_claude_integration.py" "$PAYLOAD_DIR/scripts/install_claude_integration.py"
cp "$ROOT_DIR/scripts/install_codex_integration.py" "$PAYLOAD_DIR/scripts/install_codex_integration.py"

# Stamp VERSION 文件，供 Install.command 写 auto-backup manifest 用
echo "$VERSION" > "$PAYLOAD_DIR/VERSION"

# 使用说明 —— 双平台共用一份，跟 Windows 安装包对齐。
cp "$ROOT_DIR/使用说明.md" "$PAYLOAD_DIR/使用说明.md"

cat > "$WORK_DIR/Install.command" <<'EOF'
#!/usr/bin/env bash
# Install.command —— 直装版安装/升级器，自带数据保护
#
# 严格流程（任何一步失败立即 exit，不动旧安装；审计 #3 / #4）：
#  1. 进程检测：若 kb-api 在跑，提示退出 App 后再装
#  2. auto-backup：cp 当前 data/ + models/ + embedding-service/ 到
#     ~/Library/.../auto-backup/{ts}/ —— data/ 失败立即 abort（核心数据
#     不容丢失）；models/ + embedding-service/ 失败仅警告（可重下重装，但
#     成功时下次升级跳过下载 4GB 模型 + pip 重装 infinity-emb 的痛点）
#  3. 把新版解到 staging 目录 $DST_DIR.new
#  4. 把 auto-backup 里的 data/ + models/ + embedding-service/ 注入 staging
#  5. 原子切换：mv 旧 DST → .old，mv staging → DST；
#     成功后再删 .old；失败则把 .old 还原回 DST 并退出
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)/KnowledgeBase"
DST_DIR="/Applications/KnowledgeBase"
STAGING_DIR="${DST_DIR}.new"
OLD_DIR="${DST_DIR}.old"
SAFE_DIR="$HOME/Library/Application Support/KnowledgeBase/auto-backup"

# --- helpers ---
abort() {
  echo ""
  echo "============================================================"
  echo "  安装失败：$1"
  echo "  旧安装未被改动，可以重试安装或手动排查"
  echo "============================================================"
  # 清理任何中间残留
  rm -rf "$STAGING_DIR" 2>/dev/null || true
  exit 1
}

# APFS clone 加速复制：cp -c 用 clonefile(2)，同卷内瞬时完成、写时复制零空间开销。
# 失败（跨卷 / 非 APFS / 旧 macOS）退化到普通 cp -R 真拷贝。
# 用于 backup/inject models 与 embedding-service（4.3GB 模型权重 + venv），
# 否则 cp -R 真复制 ~8GB 会让 Install.command 慢到几分钟。
clone_or_copy() {
  cp -cR "$1" "$2" 2>/dev/null && return 0
  cp -R "$1" "$2" 2>/dev/null
}

# 1. 进程检测
if pgrep -f "$DST_DIR/bin/kb-api" >/dev/null 2>&1; then
  echo ""
  echo "============================================================"
  echo "  检测到 KnowledgeBase 服务正在运行"
  echo "  请先在菜单栏退出 App（点击托盘图标 → Quit / 退出），再继续安装"
  echo "  （为防止 SQLite / Qdrant 拿到不一致 snapshot）"
  echo "============================================================"
  exit 1
fi

# 0. 清理上次失败可能留下的中间残留（不影响生效安装）
rm -rf "$STAGING_DIR" 2>/dev/null || true
rm -rf "$OLD_DIR" 2>/dev/null || true

TS="$(date '+%Y%m%d_%H%M%S')"
BAK=""

# 2. auto-backup —— data/ 失败立即 abort，models/ + embedding-service/ 失败仅警告
if [ -d "$DST_DIR/data" ]; then
  BAK="$SAFE_DIR/$TS"
  mkdir -p "$BAK/meta" || abort "无法创建备份目录 $BAK"

  # data/：核心数据，备份失败立即 abort（不允许在没有数据备份的情况下进入"删旧装新"阶段）
  if ! cp -R "$DST_DIR/data" "$BAK/data"; then
    abort "备份 $DST_DIR/data 到 $BAK/data 失败（磁盘满 / 权限？）"
  fi

  # models/：大模型权重（bge-m3 单个 ~4GB）。clone_or_copy 走 APFS clonefile，
  # 同卷瞬时；跨卷退化为真复制，可能慢但不影响数据安全。
  # 备份成功 → 升级新版本时直接复用，不重下；失败 → 警告但不 abort（新版本走 /setup 下载）。
  if [ -d "$DST_DIR/models" ]; then
    if clone_or_copy "$DST_DIR/models" "$BAK/models"; then
      echo "auto-backup: $BAK/models（含权重，升级时复用）"
    else
      rm -rf "$BAK/models" 2>/dev/null || true
      echo "⚠️ 备份 models/ 失败，升级后需要重新下载（约 4GB）；data/ 已备份不受影响"
    fi
  fi

  # embedding-service/：venv（infinity-emb + torch + huggingface_hub 等，几百 MB）。
  # 备份成功 → 跳过 pip install 几分钟；失败 → 警告，新版本走 /setup 重装。
  if [ -d "$DST_DIR/embedding-service" ]; then
    if clone_or_copy "$DST_DIR/embedding-service" "$BAK/embedding-service"; then
      echo "auto-backup: $BAK/embedding-service（含 venv，升级时复用）"
    else
      rm -rf "$BAK/embedding-service" 2>/dev/null || true
      echo "⚠️ 备份 embedding-service/ 失败，升级后需要重新安装依赖；data/ 已备份不受影响"
    fi
  fi

  APP_VER_BEFORE="unknown"
  if [ -f "$DST_DIR/VERSION" ]; then
    APP_VER_BEFORE="$(cat "$DST_DIR/VERSION" 2>/dev/null || echo unknown)"
  fi
  APP_VER_AFTER="unknown"
  if [ -f "$SRC_DIR/VERSION" ]; then
    APP_VER_AFTER="$(cat "$SRC_DIR/VERSION" 2>/dev/null || echo unknown)"
  fi

  cat > "$BAK/meta/manifest.json" <<MANIFEST
{
  "trigger": "install",
  "created_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "app_version_before": "$APP_VER_BEFORE",
  "app_version_after": "$APP_VER_AFTER",
  "host": "$(hostname)",
  "has_models_backup": $([ -d "$BAK/models" ] && echo true || echo false),
  "has_embedding_service_backup": $([ -d "$BAK/embedding-service" ] && echo true || echo false)
}
MANIFEST

  echo "auto-backup: $BAK"
fi

# 3. 准备 staging（不动旧 DST）
mkdir -p "$(dirname "$DST_DIR")"
if ! cp -R "$SRC_DIR" "$STAGING_DIR"; then
  abort "拷贝新版到 staging 失败"
fi

# 4. 把 auto-backup 里的 data/ + models/ + embedding-service/ 注入 staging
if [ -n "${BAK:-}" ] && [ -d "$BAK/data" ]; then
  rm -rf "$STAGING_DIR/data"
  if ! cp -R "$BAK/data" "$STAGING_DIR/data"; then
    abort "把备份数据注入 staging 失败"
  fi
  echo "数据已注入新版 staging"
fi

# models/：优先用 backup；备份未命中时（旧版本 Install.command 不备份模型）回退到
# 直接从旧 DST 复用——此时 DST 还在原位（step 5 才原子切换），直接 cp 是安全的。
# 这条 fallback 解决 1.3.7→1.3.8 升级时旧 Install.command 没备份 models 的过渡问题。
MODELS_SRC=""
if [ -n "${BAK:-}" ] && [ -d "$BAK/models" ]; then
  MODELS_SRC="$BAK/models"
elif [ -d "$DST_DIR/models" ]; then
  MODELS_SRC="$DST_DIR/models"
fi
if [ -n "$MODELS_SRC" ]; then
  rm -rf "$STAGING_DIR/models" 2>/dev/null || true
  if clone_or_copy "$MODELS_SRC" "$STAGING_DIR/models"; then
    echo "模型权重已注入新版 staging（跳过 4GB 下载，来源：${MODELS_SRC}）"
  else
    echo "⚠️ 注入 models/ 失败，新版本启动后会重新下载"
  fi
fi

# embedding-service/：同款 fallback 逻辑
VENV_SRC=""
if [ -n "${BAK:-}" ] && [ -d "$BAK/embedding-service" ]; then
  VENV_SRC="$BAK/embedding-service"
elif [ -d "$DST_DIR/embedding-service" ]; then
  VENV_SRC="$DST_DIR/embedding-service"
fi
if [ -n "$VENV_SRC" ]; then
  rm -rf "$STAGING_DIR/embedding-service" 2>/dev/null || true
  if clone_or_copy "$VENV_SRC" "$STAGING_DIR/embedding-service"; then
    echo "embedding-service venv 已注入新版 staging（跳过 pip install，来源：${VENV_SRC}）"
  else
    echo "⚠️ 注入 embedding-service/ 失败，新版本启动后会重装依赖"
  fi
fi

# 标记 project_root（在 staging 上做，原子切换后立即生效）
RES_DIR="$STAGING_DIR/KnowledgeBaseMenuBar.app/Contents/Resources"
if [ -d "$RES_DIR" ]; then
  echo "/Applications/KnowledgeBase" > "$RES_DIR/project_root.txt"
fi

# 5. 原子切换：先把旧 DST 移到 .old，再把 staging 移到 DST
if [ -d "$DST_DIR" ]; then
  if ! mv "$DST_DIR" "$OLD_DIR"; then
    abort "无法把旧安装移到 $OLD_DIR"
  fi
fi
if ! mv "$STAGING_DIR" "$DST_DIR"; then
  # 切换失败：尽量还原旧 DST，再 abort
  if [ -d "$OLD_DIR" ]; then
    mv "$OLD_DIR" "$DST_DIR" || true
  fi
  abort "切换 staging → $DST_DIR 失败"
fi

# 6. 清理 .old
rm -rf "$OLD_DIR" 2>/dev/null || true

echo "Installed to: $DST_DIR"
open "$DST_DIR/KnowledgeBaseMenuBar.app"

# 给用户一个看得见的"装好了"反馈：macOS Terminal 默认偏好是 shell 退出后保留窗口
# （Terminal → 设置 → 描述文件 → Shell → "如果 shell 正常退出" 默认 "从不"），
# read -t 8 超时退出脚本后窗口还是不关，所以不再承诺"自动关闭"，请用户手动关。
INSTALLED_VER="$(cat "$DST_DIR/VERSION" 2>/dev/null || echo unknown)"
echo ""
echo "============================================================"
echo "  ✅ 安装完成"
echo "  版本：$INSTALLED_VER"
echo "  菜单栏 App 已自动启动，请看屏幕顶部状态栏图标"
echo ""
echo "  可手动关闭本窗口（⌘W 或直接关）"
echo "============================================================"
EOF
chmod +x "$WORK_DIR/Install.command"

# 卸载器：源在 mac-app/Uninstall.command，跟 dmg 一起发，
# 用户从挂载卷直接双击即可；不依赖 KnowledgeBase 安装根存在。
cp "$ROOT_DIR/mac-app/Uninstall.command" "$WORK_DIR/Uninstall.command"
chmod +x "$WORK_DIR/Uninstall.command"

mkdir -p "$DIST_DIR"
rm -f "$DMG_PATH"
hdiutil create -volname "KnowledgeBase Installer" -srcfolder "$WORK_DIR" -ov -format UDZO "$DMG_PATH" >/dev/null

echo "DMG created: $DMG_PATH"
