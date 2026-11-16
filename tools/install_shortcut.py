"""重建桌面快捷方式：指向 I:\\CodexSwitcher\\CodexSwitcher.exe，并套用新图标。

用 ctypes 直接调 IShellLink + IPersistFile，不需要 WScript.Shell / powershell。
覆盖前会把旧 .lnk 备份到 build/backup/。
"""

from __future__ import annotations

import shutil
import sys
import time
import winreg
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / 'src'))

import shell_link  # noqa: E402

EXE = PKG / 'CodexSwitcher.exe'
ICON = PKG / 'assets' / 'icon.ico'
LNK_NAME = 'Codex 切换器.lnk'
DESCRIPTION = 'Codex 切换器 - 切换官方账号 / 端砚 API 密钥 / DeepSeek'


def desktop_dir() -> Path:
    """按 Windows 标准顺序解析真实桌面位置，找不到就明确报错而不是造目录。"""
    # 1) 注册表 User Shell Folders（未展开的 %USERPROFILE% 需展开）
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Explorer'
                             r'\User Shell Folders')
        value, _ = winreg.QueryValueEx(key, 'Desktop')
        value = winreg.ExpandEnvironmentStrings(value) if '%' in value else value
        p = Path(value)
        if p.is_dir():
            return p
    except Exception:
        pass
    # 2) 常见位置（OneDrive 重定向桌面前于普通桌面）
    candidates = [
        Path.home() / 'OneDrive' / 'Desktop',
        Path.home() / 'Desktop',
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    # 绝不返回硬编码 D 盘路径：shell_link.save() 会 mkdir，等于在没有 D 盘的
    # 机器上凭空造目录让用户以为桌面快捷方式建好了。宁可显式失败。
    raise SystemExit('无法确定桌面目录，请检查 OneDrive / 桌面重定向设置。')


def refresh_shell():
    """通知资源管理器刷新图标缓存，避免仍然显示旧图标。"""
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
        return True
    except Exception:
        return False


def main():
    desk = desktop_dir()
    lnk = desk / LNK_NAME
    print('桌面目录 :', desk)

    if not EXE.exists():
        raise SystemExit('找不到主程序：%s' % EXE)
    if not ICON.exists():
        raise SystemExit('找不到图标：%s' % ICON)

    if lnk.exists():
        backup = PKG / 'build' / 'backup'
        backup.mkdir(parents=True, exist_ok=True)
        dst = backup / ('Codex 切换器.backup-%s.lnk' % time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(lnk, dst)
        print('已备份旧快捷方式 ->', dst)

    shell_link.create(lnk_path=lnk, target=str(EXE), arguments='',
                      working_dir=str(PKG), icon=str(ICON), description=DESCRIPTION)
    print('已重建快捷方式 ->', lnk)
    print('  目标     :', EXE)
    print('  图标     :', ICON)
    print('  图标缓存刷新 :', 'ok' if refresh_shell() else 'skipped')


if __name__ == '__main__':
    main()
