# Windows 直装版 / Windows Direct Install

## 1. 概述

直装版是面向非开发者用户的 Windows 安装包，无需 Docker / Python 环境，双击安装程序即可使用。

**交付物**：
- `KnowledgeBase-Setup-x.x.x.exe` — 标准 Windows 安装向导

**安装后结构**：
```
%LocalAppData%\KnowledgeBase\
  bin\
    kb-api.exe              # FastAPI 服务进程（SQLite + Qdrant 嵌入模式）
    kb-tray.exe             # 系统托盘管理程序
  config\
    config.toml             # 引导配置（端口、数据路径）
  scripts\
    local-restart-direct.ps1   # /v1/system/restart 调用的直装版重启脚本
  agent-integration\        # Claude / Codex 接入工具包
    kb-mcp-proxy.py           # MCP server 实现（stdio ↔ kb-api HTTP）
    SKILL.md                  # Skill 行为规则主干
    安装说明.md               # 给 AI 读的安装指南（自助接入）
  data\                     # 运行时自动生成（knowledge.db、qdrant_local\），卸载保留
  logs\                     # 运行时日志，卸载清理
  app.ico                   # 桌面/开始菜单快捷方式图标
  使用说明.md               # 用户手册
```

---

## 2. 源文件

| 文件 | 说明 |
|------|------|
| `app/server_entry.py` | kb-api.exe 入口，包含 PyInstaller noconsole 兼容修复 |
| `windows-app/tray_app_local.py` | kb-tray.exe 主程序，管理本地进程 |
| `windows-app/win32_menu_icons.py` | Win32 菜单图标注入（monkey-patch pystray） |
| `windows-app/assets/` | 托盘图标 PNG（64×64）+ app.ico |
| `scripts/build_direct_install.ps1` | 一键构建脚本 |
| `scripts/installer.iss` | Inno Setup 安装包脚本 |

---

## 3. 构建环境要求

| 工具 | 路径 | 说明 |
|------|------|------|
| Python 虚拟环境 | `F:\knowledge-base-system\.venv` | 安装 requirements-local.txt |
| Anaconda DLL | `E:\anaconda\Library\bin` | ffi.dll、libexpat.dll 等 |
| Inno Setup 6 | `E:\Inno Setup 6\ISCC.exe` | 打包安装程序 |

---

## 4. 构建步骤

```powershell
# 在项目根目录执行
powershell -ExecutionPolicy Bypass -File scripts\build_direct_install.ps1
```

构建流程：
1. PyInstaller 打包 `kb-api.exe` → `bin\`
2. PyInstaller 打包 `kb-tray.exe` → `bin\`
3. Inno Setup 编译 `installer.iss` → `dist\KnowledgeBase-Setup-x.x.x.exe`

---

## 5. 托盘菜单功能

| 菜单项 | 说明 |
|--------|------|
| 状态（不可点击） | 显示当前运行状态：运行中 / 未运行 / 检查中 |
| 启动知识库 | 启动 kb-api.exe，等待健康检查通过（最多 20 秒） |
| 停止知识库 | 终止 kb-api.exe 进程 |
| 打开控制台 | 浏览器打开 `http://127.0.0.1:{port}/console` |
| 打开系统配置 | 浏览器打开 `http://127.0.0.1:{port}/settings` |
| 打开 API 文档 | 浏览器打开 `http://127.0.0.1:{port}/docs` |
| 诊断状态 | 弹窗显示 health 检查结果、端口、PID |
| 退出 | 停止托盘程序（不停止 kb-api.exe） |

状态图标：
- 绿色 = 运行中
- 红色 = 未运行
- 蓝色 = 操作进行中（启动/停止）

---

## 6. 配置

`config\config.toml`（安装后可修改，升级时保留）：

```toml
[server]
host = "127.0.0.1"
port = 18000        # 如需修改端口，改这里后重启知识库

[data]
sqlite_path = "data/knowledge.db"
qdrant_local_path = "data/qdrant_local"
vector_enabled = true
```

修改端口后需重启知识库（停止 → 启动）才生效。

---

## 7. 已知 PyInstaller 兼容性修复

| 问题 | 根因 | 修复位置 |
|------|------|---------|
| `'NoneType' has no attribute 'isatty'` | `--noconsole` 模式下 `sys.stdout/stderr = None`，uvicorn DefaultFormatter 崩溃 | `app/server_entry.py` 重定向到 devnull |
| `ImportError: DLL load failed` (_ctypes 等) | Anaconda 虚拟环境缺少系统 DLL | `build_direct_install.ps1` 显式 `--add-binary` |
| 诊断弹窗闪退 | `--noconsole` 下 subprocess 会显示 CMD 窗口 | `CREATE_NO_WINDOW` + `DEVNULL stdin` |
| `icon.notify()` 死锁 | pystray 消息泵线程不能调自身 | 独立 daemon 线程 + `MessageBoxW` fallback |
| 菜单图标全相同 | 图标文件绑定方式问题 | `win32_menu_icons.py` 程序绘制 + `MIIM_BITMAP` 注入 |

---

## 8. 数据备份

数据全部在 `%LocalAppData%\KnowledgeBase\data\`，直接复制整个 `data\` 目录即可备份。
