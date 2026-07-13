"""直装包发布版本与运行时依赖契约。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("requirements_name", ["requirements.txt", "requirements-local.txt"])
def test_portalocker_is_a_pinned_direct_dependency(requirements_name: str):
    requirements = (ROOT / requirements_name).read_text(encoding="utf-8-sig")
    declared = {
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "portalocker==3.2.0" in declared


def test_direct_install_build_defaults_stay_at_1_3_13():
    mac_build = (ROOT / "scripts" / "build_mac_direct_install_dmg.sh").read_text(
        encoding="utf-8"
    )
    windows_build = (ROOT / "scripts" / "build_direct_install.ps1").read_text(
        encoding="utf-8-sig"
    )
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8-sig")

    assert re.search(r'^VERSION="1\.3\.13"$', mac_build, re.MULTILINE)
    assert re.search(
        r'^\s*\[string\]\$Version\s*=\s*"1\.3\.13"$',
        windows_build,
        re.MULTILINE,
    )
    assert re.search(
        r'^\s*#define AppVersion "1\.3\.13"$', installer, re.MULTILINE
    )


def test_version_file_matches_build_defaults():
    """VERSION 四处一致兜底: VERSION 源码文件 == ps1 -Version default == iss AppVersion
    == dmg.sh VERSION 四处一致, 防 dev/build/installer 版本漂移。
    VERSION 加入 git 追踪后,任何 bump 都要同步四处;不一致 CI 立即拦。"""
    version = (ROOT / "VERSION").read_bytes()
    # 校验 VERSION 文件无 BOM (0xEF 0xBB 0xBF)
    assert not version.startswith(b"\xef\xbb\xbf"), (
        "VERSION 文件不允许 UTF-8 BOM,否则 APP_VERSION 首字符含 U+FEFF"
    )
    version_str = version.decode("ascii").strip()
    assert version_str, "VERSION 文件不允许空"

    mac_build = (ROOT / "scripts" / "build_mac_direct_install_dmg.sh").read_text(
        encoding="utf-8"
    )
    windows_build = (ROOT / "scripts" / "build_direct_install.ps1").read_text(
        encoding="utf-8-sig"
    )
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8-sig")

    # dmg.sh:  VERSION="1.3.13"
    ver_re = re.escape(version_str)
    assert re.search(rf'^VERSION="{ver_re}"$', mac_build, re.MULTILINE), (
        f"build_mac_direct_install_dmg.sh VERSION 未同步到 {version_str}"
    )
    # ps1 -Version default
    assert re.search(
        rf'^\s*\[string\]\$Version\s*=\s*"{ver_re}"$',
        windows_build,
        re.MULTILINE,
    ), f"build_direct_install.ps1 -Version default 未同步到 {version_str}"
    # installer.iss #define AppVersion
    assert re.search(
        rf'^\s*#define AppVersion "{ver_re}"$', installer, re.MULTILINE
    ), f"installer.iss AppVersion 未同步到 {version_str}"


def test_readme_mac_packaging_example_uses_the_default_release_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    mac_section = readme.split("## Mac 直装打包 / macOS Direct Installer", 1)[1].split(
        "\n---", 1
    )[0]

    assert "默认版本 1.3.13" in mac_section
    assert "./scripts/build_mac_direct_install_dmg.sh --build-api 1.3.13" in mac_section
    assert "1.0.0" not in mac_section


def test_windows_installer_checks_the_python_command_used_by_runtime():
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8-sig")

    assert 'python -c "import sys;' in installer
    assert "sys.version_info[:2] == (3, 13)" in installer
    assert "RegQueryStringValue" not in installer
    assert "py -3.13 --version" not in installer


def test_windows_uninstaller_uses_install_scoped_stop_script():
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8-sig")

    assert 'Source: "{#RootDir}\\scripts\\local-stop.ps1"' in installer
    assert 'local-stop.ps1' in installer.split("[UninstallRun]", 1)[1]
    assert "-IncludeTray" in installer.split("[UninstallRun]", 1)[1]
    assert "/IM kb-api.exe" not in installer
    assert "/IM kb-tray.exe" not in installer


def test_windows_build_fails_when_installer_cannot_be_produced():
    windows_build = (ROOT / "scripts" / "build_direct_install.ps1").read_text(
        encoding="utf-8-sig"
    )

    missing_inno = windows_build.split('if (-not (Test-Path $InnoSetup))', 1)[1].split(
        "} else {", 1
    )[0]
    assert "Write-Error" in missing_inno
    assert "exit 1" in missing_inno
    assert "KnowledgeBase-Setup-$Version.exe" in windows_build
    assert "LastWriteTime" in windows_build


def test_windows_build_bundles_openssl_runtime_for_python_ssl_extensions():
    """Anaconda 的 ``_ssl.pyd`` / ``_hashlib.pyd`` 依赖 Library/bin 下的 DLL。

    PyInstaller 在混合 ``.venv`` + Anaconda 基础解释器环境里可能无法自动解析这条
    PE 依赖链；发布脚本必须显式将两份运行时 DLL 打进 API 与 tray onedir。
    """
    windows_build = (ROOT / "scripts" / "build_direct_install.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert windows_build.count(
        '--add-binary "$AnacondaDll\\libcrypto-3-x64.dll;."'
    ) == 2
    assert windows_build.count(
        '--add-binary "$AnacondaDll\\libssl-3-x64.dll;."'
    ) == 2
    assert "Test-OpenSslRuntimeBundle" in windows_build


def test_installer_whitelists_agent_integration_files_without_python_cache():
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8-sig")

    assert 'Source: "{#RootDir}\\agent-integration\\*"' not in installer
    for filename in ("kb-mcp-proxy.py", "SKILL.md", "安装说明.md"):
        assert f'Source: "{{#RootDir}}\\agent-integration\\{filename}"' in installer


def test_installer_removes_legacy_agent_integration_python_cache_on_upgrade():
    """旧版通配打包留下的 pyc 不在新 [Files] 中，Inno 升级时不会自动删除。"""
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8-sig")
    install_delete = installer.split("[InstallDelete]", 1)[1].split("[Dirs]", 1)[0]

    assert (
        'Type: filesandordirs; Name: "{app}\\agent-integration\\__pycache__"'
        in install_delete
    )
