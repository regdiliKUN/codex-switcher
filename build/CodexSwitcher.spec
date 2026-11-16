# -*- mode: python ; coding: utf-8 -*-
# 所有路径基于 spec 自身所在目录解析，换盘符 / 换机器无需改动。
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), '..'))
SRC = os.path.join(ROOT, 'src', 'codex_switcher.py')
ICON = os.path.join(ROOT, 'assets', 'icon.ico')
VERFILE = os.path.join(ROOT, 'build', 'version_info.txt')


a = Analysis(
    [SRC],
    pathex=[],
    binaries=[],
    datas=[(ICON, 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CodexSwitcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERFILE,
    icon=[ICON],
)
