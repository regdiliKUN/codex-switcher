"""Codex 切换器 —— 桌面小面板。

双击桌面图标打开，选服务商 + 选模型 → 一键应用并重启 Codex。
"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import codex_switch_core as core

APP_TITLE = 'Codex 切换器'

# 打包成 EXE 后 __file__ 指向 PyInstaller 的临时解包目录，
# 所以「程序所在目录」必须用 sys.executable 推算。
FROZEN = bool(getattr(sys, 'frozen', False))
HERE = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent)
BUNDLE = Path(getattr(sys, '_MEIPASS', HERE))


def resource_path(name: str) -> Path:
    """资源查找：EXE 同目录 → 同目录 assets/ → 上级 assets/ → 打包内置副本。"""
    for cand in (HERE / name, HERE / 'assets' / name, HERE.parent / 'assets' / name,
                 BUNDLE / 'assets' / name, BUNDLE / name):
        if cand.exists():
            return cand
    return HERE / name


def set_app_id():
    """让任务栏归组到本程序自己的图标，而不是 python 的。"""
    if os.name != 'nt':
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'CodexSwitcher.Desktop.1')
    except Exception:
        pass

CTX_PRESETS = [
    ('模型最大值', None),
    ('512K', 524288),
    ('256K', 262144),
    ('128K', 131072),
    ('64K', 65536),
    ('32K', 32768),
]


def system_dark() -> bool:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize')
        value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
        return value == 0
    except Exception:
        return True


def apply_window_chrome(win: tk.Misc, dark: bool, icon: Path | None = None):
    """标题栏跟随深色模式，并设置窗口图标。"""
    if icon and icon.exists():
        try:
            win.iconbitmap(default=str(icon))
        except Exception:
            pass
    if not dark:
        return
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        flag = ctypes.c_int(1)
        for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE（版本不同编号不同）
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag))
    except Exception:
        pass


DARK = {
    'bg': '#1c1d21', 'card': '#26282d', 'line': '#3a3d43',
    'fg': '#eceff3', 'sub': '#9aa3ae', 'accent': '#4f8cff',
    'ok': '#3fbf6f', 'warn': '#e0a33e', 'err': '#f0605d',
    'btn': '#33363c', 'btn_active': '#3f434a', 'entry': '#2c2f35',
}
LIGHT = {
    'bg': '#f2f3f5', 'card': '#ffffff', 'line': '#dcdfe4',
    'fg': '#1c1e21', 'sub': '#5f6672', 'accent': '#2f6feb',
    'ok': '#1f9d55', 'warn': '#b8770f', 'err': '#c9382f',
    'btn': '#e8eaed', 'btn_active': '#dadee3', 'entry': '#ffffff',
}


class Switcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.p = DARK if system_dark() else LIGHT
        self.title(APP_TITLE)
        self.resizable(False, False)
        self.configure(bg=self.p['bg'])
        self.protocol('WM_DELETE_WINDOW', self.destroy)

        self.state = core.load_state()
        self.mode_var = tk.StringVar(value='openai')
        self.model_var = tk.StringVar()
        self.effort_var = tk.StringVar()
        self.ctx_var = tk.StringVar(value=CTX_PRESETS[0][0])
        self.busy = False
        self.events = queue.Queue()
        self.autostart_ok = True      # 手动停过网关后置 False，不再自动拉起

        self._build()
        self._load_current()
        self._center()
        apply_window_chrome(self, self.p is DARK, resource_path('icon.ico'))
        self.after(120, self._pump)
        self.after(400, self._tick)

    # ---------------- 界面 ----------------
    def _card(self, parent):
        return tk.Frame(parent, bg=self.p['card'], highlightbackground=self.p['line'],
                        highlightthickness=1)

    def _style_ttk(self):
        p = self.p
        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TCombobox', fieldbackground=p['entry'], background=p['btn'],
                        foreground=p['fg'], arrowcolor=p['fg'], bordercolor=p['line'],
                        lightcolor=p['line'], darkcolor=p['line'], selectbackground=p['entry'],
                        selectforeground=p['fg'], padding=4)
        style.map('TCombobox',
                  fieldbackground=[('readonly', p['entry']), ('disabled', p['card'])],
                  foreground=[('disabled', p['sub'])],
                  selectbackground=[('readonly', p['entry'])],
                  selectforeground=[('readonly', p['fg'])])
        self.option_add('*TCombobox*Listbox.background', p['entry'])
        self.option_add('*TCombobox*Listbox.foreground', p['fg'])
        self.option_add('*TCombobox*Listbox.selectBackground', p['accent'])
        self.option_add('*TCombobox*Listbox.selectForeground', '#ffffff')

    def _build(self):
        p = self.p
        pad = dict(padx=18)
        self._style_ttk()
        tk.Label(self, text=APP_TITLE, bg=p['bg'], fg=p['fg'],
                 font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w', pady=(16, 2), **pad)
        self.subtitle = tk.Label(self, text='', bg=p['bg'], fg=p['sub'],
                                 font=('Microsoft YaHei UI', 9))
        self.subtitle.pack(anchor='w', pady=(0, 12), **pad)

        # --- 当前状态 ---
        cur = self._card(self)
        cur.pack(fill='x', **pad)
        inner = tk.Frame(cur, bg=p['card'])
        inner.pack(fill='x', padx=14, pady=12)
        self.cur_mode = tk.Label(inner, text='', bg=p['card'], fg=p['fg'],
                                 font=('Microsoft YaHei UI', 11, 'bold'), anchor='w')
        self.cur_mode.pack(fill='x')
        self.cur_detail = tk.Label(inner, text='', bg=p['card'], fg=p['sub'],
                                   font=('Consolas', 9), anchor='w', justify='left')
        self.cur_detail.pack(fill='x', pady=(4, 0))

        # --- 选择 ---
        pick = self._card(self)
        pick.pack(fill='x', pady=(12, 0), **pad)
        box = tk.Frame(pick, bg=p['card'])
        box.pack(fill='x', padx=14, pady=12)

        tk.Label(box, text='服务商', bg=p['card'], fg=p['sub'],
                 font=('Microsoft YaHei UI', 9)).grid(row=0, column=0, sticky='w')
        self.mode_frame = tk.Frame(box, bg=p['card'])
        self.mode_frame.grid(row=1, column=0, columnspan=3, sticky='w', pady=(6, 0))
        self.mode_buttons = {}
        for key in core.MODE_ORDER:
            rb = tk.Radiobutton(
                self.mode_frame, text=core.MODES[key]['label'], value=key,
                variable=self.mode_var, command=self._on_mode_change,
                bg=p['card'], fg=p['fg'], selectcolor=p['card'], activebackground=p['card'],
                activeforeground=p['accent'], font=('Microsoft YaHei UI', 10),
                highlightthickness=0, bd=0, cursor='hand2', padx=2)
            rb.pack(side='left', padx=(0, 16))
            self.mode_buttons[key] = rb

        tk.Label(box, text='模型', bg=p['card'], fg=p['sub'],
                 font=('Microsoft YaHei UI', 9)).grid(row=2, column=0, sticky='w', pady=(14, 0))
        tk.Label(box, text='推理强度', bg=p['card'], fg=p['sub'],
                 font=('Microsoft YaHei UI', 9)).grid(row=2, column=1, sticky='w',
                                                      padx=(14, 0), pady=(14, 0))
        tk.Label(box, text='上下文上限', bg=p['card'], fg=p['sub'],
                 font=('Microsoft YaHei UI', 9)).grid(row=2, column=2, sticky='w',
                                                      padx=(14, 0), pady=(14, 0))

        self.model_box = ttk.Combobox(box, textvariable=self.model_var, width=30,
                                      font=('Consolas', 10), state='normal')
        self.model_box.grid(row=3, column=0, sticky='we', pady=(6, 0))
        self.model_box.bind('<<ComboboxSelected>>', lambda e: self._on_model_change())

        self.effort_box = ttk.Combobox(box, textvariable=self.effort_var, width=10,
                                       font=('Consolas', 10), state='readonly')
        self.effort_box.grid(row=3, column=1, sticky='w', padx=(14, 0), pady=(6, 0))

        self.ctx_box = ttk.Combobox(box, textvariable=self.ctx_var, width=11,
                                    font=('Microsoft YaHei UI', 9), state='readonly',
                                    values=[name for name, _ in CTX_PRESETS])
        self.ctx_box.grid(row=3, column=2, sticky='w', padx=(14, 0), pady=(6, 0))
        box.columnconfigure(0, weight=1)

        self.hint = tk.Label(pick, text='', bg=p['card'], fg=p['sub'],
                             font=('Microsoft YaHei UI', 9), anchor='w', justify='left',
                             wraplength=490)
        self.hint.pack(fill='x', padx=14, pady=(0, 12))

        # --- 网关状态 ---
        gw = self._card(self)
        gw.pack(fill='x', pady=(12, 0), **pad)
        row = tk.Frame(gw, bg=p['card'])
        row.pack(fill='x', padx=14, pady=10)
        self.gw_dot = tk.Label(row, text='●', bg=p['card'], fg=p['sub'],
                               font=('Microsoft YaHei UI', 11))
        self.gw_dot.pack(side='left')
        self.gw_text = tk.Label(row, text='网关状态检查中…', bg=p['card'], fg=p['sub'],
                                font=('Microsoft YaHei UI', 9), anchor='w')
        self.gw_text.pack(side='left', padx=(6, 0))
        self.btn_reset = self._small_button(row, '重置密钥状态', self._reset_gateway)
        self.btn_reset.pack(side='right')
        self.btn_stop = self._small_button(row, '停止网关', self._stop_gateway)
        self.btn_stop.pack(side='right', padx=(0, 6))
        self.btn_start = self._small_button(row, '启动网关', self._start_gateway)
        self.btn_start.pack(side='right', padx=(0, 6))

        # --- 主按钮 ---
        actions = tk.Frame(self, bg=p['bg'])
        actions.pack(fill='x', pady=(14, 0), **pad)
        self.btn_apply = tk.Button(
            actions, text='应用并重启 Codex', command=self._apply_and_restart,
            bg=p['accent'], fg='#ffffff', activebackground=p['accent'],
            activeforeground='#ffffff', font=('Microsoft YaHei UI', 10, 'bold'),
            relief='flat', bd=0, cursor='hand2', padx=16, pady=9)
        self.btn_apply.pack(side='left')
        self.btn_save = self._small_button(actions, '只写配置', self._apply_only)
        self.btn_save.pack(side='left', padx=(8, 0), pady=2)
        self.btn_refresh = self._small_button(actions, '刷新端砚模型目录', self._refresh_catalog)
        self.btn_refresh.pack(side='left', padx=(8, 0), pady=2)

        # --- 日志 ---
        self.log = tk.Text(self, height=6, bg=p['card'], fg=p['sub'], bd=0,
                           font=('Consolas', 9), wrap='word', relief='flat',
                           highlightthickness=1, highlightbackground=p['line'],
                           insertbackground=p['fg'])
        self.log.pack(fill='both', expand=True, padx=18, pady=(12, 16))
        self.log.configure(state='disabled')

    def _small_button(self, parent, text, command):
        p = self.p
        return tk.Button(parent, text=text, command=command, bg=p['btn'], fg=p['fg'],
                         activebackground=p['btn_active'], activeforeground=p['fg'],
                         font=('Microsoft YaHei UI', 9), relief='flat', bd=0,
                         cursor='hand2', padx=10, pady=5)

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 3
        self.geometry(f'+{x}+{y}')

    # ---------------- 数据 ----------------
    def _load_current(self):
        try:
            info = core.config_summary()
        except Exception as exc:                       # noqa: BLE001
            self._say('读取配置失败：%s' % exc, 'err')
            info = {'mode': 'openai', 'mode_label': '官方账号', 'model': ''}
        self.mode_var.set(info['mode'])
        self.model_var.set(info['model'] or core.MODES[info['mode']]['default_model'])
        self._sync_models(keep=True)
        options = core.MODES[info['mode']]['reasoning_options']
        if info.get('effort') in options:
            self.effort_var.set(info['effort'])
        self.cur_mode.configure(text='当前：%s' % info['mode_label'])
        self.cur_detail.configure(text='模型 %s · 推理 %s\n配置 %s'
                                       % (info['model'] or '—', info.get('effort') or '—',
                                          info['path']))
        self._say('已读取当前配置。', 'sub')

    def _sync_models(self, keep=True):
        mode = self.mode_var.get()
        names = core.models_for(mode)
        self.model_box.configure(values=names)
        remembered = self.state['models'].get(mode)
        current = self.model_var.get()
        if keep and current and (current in names or mode == 'openai'):
            pass
        elif remembered and remembered in names:
            self.model_var.set(remembered)
        elif names:
            self.model_var.set(core.MODES[mode]['default_model']
                               if core.MODES[mode]['default_model'] in names else names[0])

        spec = core.MODES[mode]
        note = spec['note']
        if mode == 'intern':
            dropped = core.intern_unsupported()
            if dropped:
                note += '\n注：%s 不支持 Responses 协议，在 Codex 里用不了，已从列表移除。' \
                        % '、'.join(dropped)
        self.hint.configure(text=note)
        self.effort_box.configure(values=spec['reasoning_options'])
        remembered = self.state.get('reasoning', {}).get(mode)
        self.effort_var.set(remembered if remembered in spec['reasoning_options']
                            else spec['reasoning_effort'])
        self.ctx_box.configure(state='readonly' if spec['needs_gateway'] else 'disabled')
        if spec['needs_gateway']:
            ctx = core.default_context_window(mode, self.model_var.get())
            self.ctx_var.set(self._ctx_label(ctx))
        else:
            self.ctx_var.set('—')

    def _ctx_label(self, value):
        for name, val in CTX_PRESETS:
            if val is None:
                continue
            if val == value:
                return name
        return '模型最大值'

    def _on_mode_change(self):
        self.model_var.set('')
        self._sync_models(keep=False)

    def _on_model_change(self):
        mode = self.mode_var.get()
        if core.MODES[mode]['needs_gateway']:
            self.ctx_var.set(self._ctx_label(
                core.default_context_window(mode, self.model_var.get())))

    def _ctx_value(self):
        name = self.ctx_var.get()
        for label, val in CTX_PRESETS:
            if label == name:
                if val is None:
                    return None
                return val
        return None

    # ---------------- 动作 ----------------
    def _guard(self):
        if self.busy:
            self._say('上一个操作还在进行，稍等。', 'warn')
            return False
        return True

    def _run(self, fn, done=None):
        self.busy = True
        self._set_buttons('disabled')

        def worker():
            try:
                self.events.put(('log', fn()))
            except Exception as exc:                   # noqa: BLE001
                self.events.put(('log', ('✗ ' + str(exc), 'err')))
            finally:
                self.events.put(('done', done))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_and_restart(self):
        if not self._guard():
            return
        self._apply(restart=True)

    def _apply_only(self):
        if not self._guard():
            return
        self._apply(restart=False)

    def _apply(self, restart: bool):
        mode = self.mode_var.get()
        model = self.model_var.get().strip()
        ctx = self._ctx_value()
        effort = self.effort_var.get() or None
        if not model:
            messagebox.showwarning(APP_TITLE, '请先选择或填写模型 ID。')
            return
        self.autostart_ok = True

        def work():
            out = []
            res = core.apply_mode(mode, model, ctx, effort)
            out.append(('✓ 已切到「%s」，模型 %s，推理 %s'
                        % (core.MODES[mode]['label'], res['model'], res['effort']), 'ok'))
            if res.get('context_window'):
                out.append(('  上下文上限 %s，自动压缩点 %s'
                            % (f"{res['context_window']:,}",
                               f"{int(res['context_window'] * core.COMPACT_RATIO):,}"), 'sub'))
            if res.get('gateway') == 'running':
                out.append(('  端砚网关已就绪', 'ok'))
            out.append(('  原配置已备份到 %s' % res['backup'], 'sub'))
            if restart:
                out.append((core.restart_codex(), 'ok'))
            return out

        self._run(work, done=lambda: (self._load_current(), self._refresh_gw()))

    def _start_gateway(self):
        if not self._guard():
            return
        self.autostart_ok = True

        def work():
            ok = core.ensure_gateway(timeout=15)
            return [('✓ 网关已启动。' if ok else '✗ 网关启动失败，看 gateway.err.log。',
                     'ok' if ok else 'err')]

        self._run(work, done=self._refresh_gw)

    def _stop_gateway(self):
        if not self._guard():
            return
        self.autostart_ok = False
        self._run(lambda: [(core.stop_gateway() + '（本次不再自动拉起）', 'sub')],
                  done=self._refresh_gw)

    def _reset_gateway(self):
        if not self._guard():
            return
        if not messagebox.askyesno(APP_TITLE, '把 7 把密钥的「额度耗尽 / 失效」标记全部清掉？\n'
                                              '余额没恢复的话，下一轮请求会再次被标记。'):
            return
        self._run(lambda: [(core.reset_gateway_state(), 'sub')], done=self._refresh_gw)

    def _refresh_catalog(self):
        if not self._guard():
            return

        def work():
            msg = core.refresh_intern_catalog()
            return [('✓ ' + msg, 'ok')]

        self._run(work, done=lambda: (self._sync_models(), self._refresh_gw()))

    # ---------------- 事件泵 ----------------
    def _pump(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'log':
                    items = payload if isinstance(payload, list) else [payload]
                    for item in items:
                        if isinstance(item, tuple):
                            self._say(item[0], item[1])
                        else:
                            self._say(item)
                elif kind == 'gw':
                    self._apply_gw(payload)
                elif kind == 'done':
                    self.busy = False
                    self._set_buttons('normal')
                    if callable(payload):
                        payload()
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _set_buttons(self, state):
        for b in (self.btn_apply, self.btn_save, self.btn_start, self.btn_stop,
                  self.btn_reset, self.btn_refresh):
            try:
                b.configure(state=state)
            except tk.TclError:
                pass
        self.btn_apply.configure(bg=self.p['accent'] if state == 'normal' else self.p['line'])

    def _say(self, text, kind='sub'):
        color = self.p.get(kind, self.p['sub'])
        self.log.configure(state='normal')
        start = self.log.index('end-1c')
        self.log.insert('end', str(text).rstrip() + '\n')
        end = self.log.index('end-1c')
        self.log.tag_add(kind, start, end)
        self.log.tag_configure(kind, foreground=color)
        self.log.see('end')
        self.log.configure(state='disabled')

    # ---------------- 网关状态轮询 ----------------
    def _tick(self):
        if not self.busy:
            self._refresh_gw()
        self.after(6000, self._tick)

    def _refresh_gw(self):
        def work():
            self.events.put(('gw', core.gateway_health(timeout=2.0)))
        threading.Thread(target=work, daemon=True).start()

    def _apply_gw(self, health):
        p = self.p
        if health:
            keys = health.get('keys', [])
            active = health.get('active_key_number')
            bad = [k['number'] for k in keys if k.get('state') != 'available']
            text = '网关运行中 · 当前第 %s 把 · 共 %s 把可用' % (active, len(keys) - len(bad))
            if bad:
                text += ' · 已标记 %s' % ','.join('#%d' % n for n in bad)
            self.gw_dot.configure(fg=p['ok'])
            self.gw_text.configure(text=text, fg=p['fg'])
            return
        self.gw_dot.configure(fg=p['sub'])
        self.gw_text.configure(text='网关未运行（端砚模式会自动拉起）', fg=p['sub'])
        # 自愈：当前配置就是端砚模式、用户也没手动停过，就悄悄拉起来
        if self.autostart_ok and not self.busy and self.mode_var.get() == 'intern' \
                and not getattr(self, '_healing', False):
            self._healing = True
            self._say('检测到端砚模式下网关没在跑，正在自动拉起…', 'warn')

            def heal():
                ok = core.ensure_gateway(timeout=15)
                self._healing = False
                self.events.put(('log', [('  网关已自动恢复。' if ok else '  自动拉起失败，请点「启动网关」。',
                                          'ok' if ok else 'err')]))

            threading.Thread(target=heal, daemon=True).start()


def main():
    set_app_id()
    try:
        app = Switcher()
    except Exception as exc:                            # noqa: BLE001
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_TITLE, '启动失败：\n%s' % exc)
        except Exception:
            pass
        raise
    app.mainloop()


if __name__ == '__main__':
    main()
