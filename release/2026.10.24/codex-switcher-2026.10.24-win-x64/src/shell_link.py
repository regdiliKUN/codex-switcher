"""通过 Windows 官方 IShellLink + IPersistFile 创建 .lnk。

用 ctypes 直接调 COM，不依赖 pywin32，也不需要 WScript.Shell。
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_void_p, c_wchar_p
from pathlib import Path

ole32 = ctypes.WinDLL('ole32', use_last_error=True)

HRESULT = ctypes.c_long
BOOL = ctypes.c_int


class GUID(ctypes.Structure):
    _fields_ = [('Data1', ctypes.c_ulong),
                ('Data2', ctypes.c_ushort),
                ('Data3', ctypes.c_ushort),
                ('Data4', ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    g = GUID()
    hr = ole32.CLSIDFromString(c_wchar_p(text), byref(g))
    if hr < 0:
        raise OSError(f'CLSIDFromString 失败: {text}')
    return g


CLSID_ShellLink = _guid('{00021401-0000-0000-C000-000000000046}')
IID_IShellLinkW = _guid('{000214F9-0000-0000-C000-000000000046}')
IID_IPersistFile = _guid('{0000010b-0000-0000-C000-000000000046}')

CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2
SW_SHOWNORMAL = 1

_FN_W = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p)
_FN_LONG = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_long)
_FN_SAVE = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, BOOL)


def _method(ptr, index, proto):
    vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    return proto(vtable[index])


class ShellLink:
    def __init__(self):
        self._link = c_void_p()
        self._persist = c_void_p()
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        hr = ole32.CoCreateInstance(byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
                                    byref(IID_IShellLinkW), byref(self._link))
        if hr < 0:
            raise OSError(f'CoCreateInstance(ShellLink) 失败: 0x{hr & 0xFFFFFFFF:08X}')
        hr = _method(self._link, 0, ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.POINTER(GUID),
                                                       ctypes.POINTER(c_void_p)))(
            self._link, byref(IID_IPersistFile), byref(self._persist))
        if hr < 0:
            raise OSError(f'QueryInterface(IPersistFile) 失败: 0x{hr & 0xFFFFFFFF:08X}')

    def set_path(self, path):
        self._check(_method(self._link, 20, _FN_W)(self._link, str(path)), 'SetPath')

    def set_arguments(self, args):
        self._check(_method(self._link, 11, _FN_W)(self._link, str(args)), 'SetArguments')

    def set_working_dir(self, path):
        self._check(_method(self._link, 9, _FN_W)(self._link, str(path)), 'SetWorkingDirectory')

    def set_description(self, text):
        self._check(_method(self._link, 7, _FN_W)(self._link, str(text)), 'SetDescription')

    def set_icon(self, icon_path, index=0):
        self._check(_method(self._link, 17, ctypes.WINFUNCTYPE(
            HRESULT, c_void_p, c_wchar_p, ctypes.c_int))(
                self._link, str(icon_path), index), 'SetIconLocation')

    def set_show_cmd(self, cmd=SW_SHOWNORMAL):
        self._check(_method(self._link, 15, _FN_LONG)(self._link, cmd), 'SetShowCmd')

    def save(self, lnk_path):
        lnk_path = Path(lnk_path)
        lnk_path.parent.mkdir(parents=True, exist_ok=True)
        if lnk_path.exists():
            lnk_path.unlink()
        self._check(_method(self._persist, 6, _FN_SAVE)(self._persist, str(lnk_path), True),
                    'IPersistFile::Save')
        return lnk_path

    @staticmethod
    def _check(hr, what):
        if hr < 0:
            raise OSError(f'{what} 失败: 0x{hr & 0xFFFFFFFF:08X}')


def create(lnk_path, target, arguments='', working_dir='', icon='', description=''):
    link = ShellLink()
    link.set_path(target)
    if arguments:
        link.set_arguments(arguments)
    if working_dir:
        link.set_working_dir(working_dir)
    if description:
        link.set_description(description)
    if icon:
        link.set_icon(icon)
    link.set_show_cmd(SW_SHOWNORMAL)
    return link.save(lnk_path)


if __name__ == '__main__':
    import sys
    p = create(r'C:\Users\xukunhang\WorkBuddy AI\2026-10-09-21-06-43\testout\probe.lnk',
               r'C:\Python314\pythonw.exe', '"-c"', r'C:\Python314', '', '测试')
    print('OK', p, p.stat().st_size, 'bytes')
