"""Codex 模式切换核心逻辑。

职责：
  * 安全改写 ~/.codex/config.toml（只动托管键，保留 plugins / projects 等其余内容）
  * 三种模式互切：官方账号 / 端砚 API 密钥 / DeepSeek
  * 端砚本地网关的启停、健康检查、密钥状态查询
  * 重启 Codex 桌面端

本模块不弹任何 UI，方便单独跑脚本或写测试。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

CODEX_HOME = Path(os.environ.get('CODEX_HOME') or (Path.home() / '.codex'))
CONFIG_PATH = CODEX_HOME / 'config.toml'
SWITCHER_DIR = CODEX_HOME / 'switcher'
BACKUP_DIR = CODEX_HOME / 'backups'
STATE_PATH = SWITCHER_DIR / 'state.json'

GATEWAY_DIR = CODEX_HOME / 'intern-gateway'
GATEWAY_SCRIPT = GATEWAY_DIR / 'gateway.py'
GATEWAY_AUTH = GATEWAY_DIR / 'auth.ps1'
GATEWAY_STORE = GATEWAY_DIR / 'secrets.dpapi'
GATEWAY_STATE = GATEWAY_DIR / 'state.json'
GATEWAY_LOG = GATEWAY_DIR / 'gateway.log'
GATEWAY_ERR = GATEWAY_DIR / 'gateway.err.log'
GATEWAY_PORT = 18791
GATEWAY_URL = f'http://127.0.0.1:{GATEWAY_PORT}'

CATALOG_INTERN = SWITCHER_DIR / 'models-intern.json'
CATALOG_INTERN_META = SWITCHER_DIR / 'models-intern-meta.json'
CATALOG_DEEPSEEK = CODEX_HOME / 'models.json'

# Codex 桌面端启动入口（开始菜单快捷方式）
CODEX_LAUNCHER = (Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' /
                  'Start Menu' / 'Programs' / 'Codex.lnk')

INTERN_PROVIDER_ID = 'intern-rotate'
DEEPSEEK_PROVIDER_ID = 'deepseek'

# 端砚上游
INTERN_BASE_URL = 'https://discovery-api.intern-ai.org.cn/v1'

NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW
DETACHED = 0x00000008           # DETACHED_PROCESS
NEW_GROUP = 0x00000200          # CREATE_NEW_PROCESS_GROUP
BREAKAWAY = 0x01000000          # CREATE_BREAKAWAY_FROM_JOB

MAX_CONTEXT_CAP = 262144        # 单次发给模型的上下文上限（保守）
COMPACT_RATIO = 0.80            # 自动压缩触发点


# --------------------------------------------------------------------------
# 定位「能跑 gateway.py 的 Python」
#
# 打包成 EXE 后 sys.executable 就是本程序自己，不能再拿它当解释器去跑
# 网关脚本，否则会递归启动 N 个切换器。所以这里按
#   环境变量 → config/runtime.json → 原解释器 → PATH → 常见安装目录
# 的顺序找一个真实可用的 Python。
# --------------------------------------------------------------------------

_PY_CACHE: list = []


def _app_dirs() -> list:
    """程序自身所在目录（EXE 目录 / 源码目录及其上级），用于找 runtime.json。"""
    dirs = []
    if getattr(sys, 'frozen', False):
        dirs.append(Path(sys.executable).resolve().parent)
    here = Path(__file__).resolve().parent
    dirs.append(here)
    dirs.append(here.parent)
    dirs.append(here.parent.parent)
    dirs.append(SWITCHER_DIR)
    return dirs


def runtime_config() -> dict:
    env = os.environ.get('CODEX_SWITCHER_CONFIG')
    cands = [Path(env)] if env else []
    for d in _app_dirs():
        cands.append(d / 'config' / 'runtime.json')
        cands.append(d / 'runtime.json')
    for c in cands:
        try:
            if c.is_file():
                return json.loads(c.read_text(encoding='utf-8'))
        except Exception:
            continue
    return {}


def _candidates() -> list:
    if _PY_CACHE:
        return _PY_CACHE
    out = []
    for key in ('PYTHON', 'PYTHONW'):
        val = os.environ.get('CODEX_SWITCHER_' + key)
        if val:
            out.append(Path(val))
    cfg = runtime_config()
    for key in ('python', 'pythonw'):
        val = cfg.get(key)
        if isinstance(val, str) and val:
            out.append(Path(val))
    if not getattr(sys, 'frozen', False):
        out.append(Path(sys.executable))
    for name in ('pythonw.exe', 'python.exe'):
        try:
            found = shutil.which(name)
        except Exception:
            found = None
        if found:
            out.append(Path(found))
    roots = [Path('C:/'), Path('C:/Program Files'), Path('C:/Program Files (x86)'),
             Path(os.environ.get('LOCALAPPDATA', '') or 'C:/') / 'Programs' / 'Python',
             Path(os.environ.get('PROGRAMFILES', '') or 'C:/') / 'Python']
    pats = ('Python3*/pythonw.exe', 'Python3*/python.exe',
            'Python3*.*/pythonw.exe', 'Python3*.*/python.exe',
            'python3*/pythonw.exe', 'python3*/python.exe')
    def _py_version_key(p: Path):
        # 目录名 Python312 / Python39 按字符串排会把 3.9 排在 3.12 前，
        # 必须解析成数字元组再排。
        m = re.search(r'Python(\d)(\d+)', p.name, re.I)
        return tuple(int(x) for x in m.groups()) if m else (0, 0)

    for r in roots:
        for pat in pats:
            try:
                out.extend(sorted(r.glob(pat), key=_py_version_key, reverse=True))
            except Exception:
                continue
    _PY_CACHE[:] = out
    return out


def _pick_python(windowed: bool = True) -> str:
    for cand in _candidates():
        try:
            p = Path(cand)
            if not p.is_file():
                continue
            if windowed:
                w = p.with_name('pythonw.exe')
                if w.is_file():
                    p = w
            return str(p)
        except Exception:
            continue
    # 打包后 sys.executable 是本 EXE 自己，绝不能当解释器去跑 gateway.py，
    # 否则会递归启动 N 个切换器窗口。找不到就返回空串，交给调用方干净失败。
    return '' if getattr(sys, 'frozen', False) else sys.executable


def _pythonw() -> str:
    """无窗口解释器，用来后台拉网关。"""
    return _pick_python(windowed=True)


def _python() -> str:
    """需要读取 stdout 时用带控制台的解释器（pythonw 没有输出）。"""
    picked = _pick_python(windowed=False)
    if not picked:
        return ''
    p = Path(picked)
    c = p.with_name('python.exe')
    return str(c if c.is_file() else p)


# --------------------------------------------------------------------------
# 极简 TOML 行编辑（只做「顶层键」和「整段表」两种操作，其余原样保留）
# --------------------------------------------------------------------------

_HEADER = re.compile(r'^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$')


def split_toml(text: str):
    """切成 (顶层行列表, [{'name':..,'array':bool,'lines':[..]}...])。"""
    pre, sections, cur = [], [], None
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            # [[x]] 与 [x] 在 TOML 里语义完全不同（数组表 vs 普通表），
            # 必须记录，避免 drop_section 把用户的数组表整段删掉。
            cur = {'name': m.group(1).strip(),
                   'array': line.lstrip().startswith('[['),
                   'lines': [line]}
            sections.append(cur)
        elif cur is None:
            pre.append(line)
        else:
            cur['lines'].append(line)
    return pre, sections


def join_toml(pre, sections) -> str:
    out = list(pre)
    for s in sections:
        out.extend(s['lines'])
    return '\n'.join(out).rstrip('\n') + '\n'


def set_top_level(pre, key: str, literal):
    """literal 为 None 表示删除该键。只匹配顶层（第一个 [section] 之前）。

    两处加固：
    1) 跳过三引号字符串内部的同名行——原本会把
       prompt = \"\"\"...\\nmodel = \"fake\"\\n...\"\"\" 里的 model 行当成真键改掉，
       破坏 prompt 甚至产生非法 TOML。
    2) 兼容带引号的键 \"model\" = ... / 'model' = ...，否则会再追加一个
       裸键造成重复定义、TOML 解析失败。
    """
    pat = re.compile(r'^\s*(?:"' + re.escape(key) + r'"|\'' + re.escape(key) +
                     r'\'|' + re.escape(key) + r')\s*=')
    in_multi = None      # 当前所在三引号类型：'"""' / "'''"
    idx = []
    for i, l in enumerate(pre):
        stripped = l.lstrip()
        if in_multi:
            if in_multi in l:
                in_multi = None
            continue
        # 三引号可能出现在 `key = """...` 中，找行内首个三引号；
        # 若该行只有一个三引号（未闭合）就进入多行状态。
        positions = [(stripped.find(q), q) for q in ('"""', "'''")]
        positions = [(p, q) for p, q in positions if p >= 0]
        if positions:
            p, q = min(positions)
            if stripped.count(q) - stripped[:p + 3].count(q) == 0:
                in_multi = q
        if in_multi or pat.match(l) is None:
            continue
        if pat.match(l):
            idx.append(i)
    if literal is None:
        for i in reversed(idx):
            del pre[i]
        return pre
    line = f'{key} = {literal}'
    if idx:
        pre[idx[0]] = line
        for i in reversed(idx[1:]):
            del pre[i]
    else:
        while pre and not pre[-1].strip():
            pre.pop()
        pre.append(line)
    return pre


def drop_section(sections, name: str, array: bool | None = None):
    """删除指定段。array=None 时两种都删；给定 True/False 时只删对应类型。

    切换器自己写入的是普通表，传 array=False 可避免误删用户手写的同名数组表。
    """
    sections[:] = [s for s in sections
                   if not (s['name'] == name and (array is None or s.get('array') == array))]
    return sections


def append_section(sections, name: str, body):
    # 与 split_toml 产出的段结构保持一致：写入 array=False。
    # 本程序追加的都是普通表，缺这个键会让直接访问 s['array'] 的代码 KeyError。
    sections.append({'name': name, 'array': False, 'lines': [f'[{name}]'] + list(body)})
    return sections


def toml_str(value: str) -> str:
    """转义成 TOML 基本字符串。控制字符必须转成 \\n/\\t/\\uXXXX，
    否则模型名或路径里一旦混入换行/制表符就会写出非法 TOML，让 Codex 启动失败。"""
    s = str(value)
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif o < 0x20 or o == 0x7f:
            out.append('\\u%04X' % o)
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def set_feature(sections, key: str, enabled):
    """开关 [features] 里的一个 feature。enabled=None 表示删掉这个键、交还给 Codex 默认值。

    注意：Codex 的「压缩历史」默认走 ChatGPT 后端（remote_compaction_v2），
    只认 OpenAI 自家模型。用第三方模型时必须关掉，否则一压缩就报
    「model is not supported when using Codex with a ChatGPT account」。
    """
    target = None
    for s in sections:
        if s['name'] == 'features':
            target = s
            break
    if target is None:
        # enabled is None 表示「交还 Codex 默认值」，段不存在时本来就不用写；
        # 但显式 True/False（尤其 False——第三方模式要关掉远端压缩）必须落盘，
        # 否则干净配置下这个关键开关会被静默跳过。
        if enabled is None:
            return sections
        append_section(sections, 'features', [])
        target = sections[-1]

    pat = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
    body = target['lines'][1:]
    idx = [i for i, l in enumerate(body) if pat.match(l)]
    if enabled is None:
        for i in reversed(idx):
            del body[i]
    elif enabled:
        if idx:
            body[idx[0]] = f'{key} = true'
            for i in reversed(idx[1:]):
                del body[i]
        else:
            body.append(f'{key} = true')
    else:
        if idx:
            body[idx[0]] = f'{key} = false'
            for i in reversed(idx[1:]):
                del body[i]
        else:
            body.append(f'{key} = false')
    target['lines'] = [target['lines'][0]] + body
    return sections


# --------------------------------------------------------------------------
# 模式定义
# --------------------------------------------------------------------------

OFFICIAL_MODELS = ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                   'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5']
DEEPSEEK_MODELS = ['deepseek-flash', 'deepseek-v4-pro']
FALLBACK_INTERN_MODELS = ['Atria-Dawn-Preview', 'deepseek-v4-flash-0731',
                          'deepseek-v4-pro-0813', 'glm-5.3', 'kimi-k2.6',
                          'qwen3.8-27b', 'minimax-m3', 'intern-s2', 'Agents-A1',
                          'deepseek-v4-flash-vision']

MODES = {
    'openai': {
        'label': '官方账号',
        'provider': 'openai',
        'provider_name': 'OpenAI',
        'models': OFFICIAL_MODELS,
        'default_model': 'gpt-6.1-sol',
        'catalog': None,
        'reasoning_effort': 'high',
        'reasoning_options': ['low', 'medium', 'high', 'xhigh', 'ultra', 'max'],
        'web_search': 'live',
        'service_tier': 'priority',
        'features': {},
        'needs_gateway': False,
        'note': '使用 Codex 桌面端已登录的官方账号，不经过任何本地代理。',
    },
    'intern': {
        'label': '端砚 API 密钥',
        'provider': INTERN_PROVIDER_ID,
        'provider_name': 'Intern - sequential keys',
        'models': FALLBACK_INTERN_MODELS,
        'default_model': 'Atria-Dawn-Preview',
        'catalog': CATALOG_INTERN,
        'reasoning_effort': 'low',
        'reasoning_options': ['low', 'high', 'max'],
        'web_search': 'disabled',
        'service_tier': 'default',
        'features': {'remote_compaction_v2': False},
        'needs_gateway': True,
        'note': '经由本机 127.0.0.1:18791 代理，7 把密钥顺序轮换，用光一把自动换下一把。',
    },
    'deepseek': {
        'label': 'DeepSeek 官方',
        'provider': DEEPSEEK_PROVIDER_ID,
        'provider_name': 'deepseek',
        'models': DEEPSEEK_MODELS,
        'default_model': 'deepseek-flash',
        'catalog': CATALOG_DEEPSEEK,
        'reasoning_effort': 'high',
        'reasoning_options': ['low', 'high', 'max'],
        'web_search': 'disabled',
        'service_tier': 'default',
        'features': {'remote_compaction_v2': False},
        'needs_gateway': False,
        'note': '直连 api.deepseek.com，使用 config.toml 里已有的 deepseek 密钥。',
    },
}

MODE_ORDER = ['openai', 'intern', 'deepseek']

# 这些 feature 在切到第三方服务商时必须关掉，切回官方时交还给 Codex 默认值。
# remote_compaction_v2：压缩历史默认走 ChatGPT 后端，只认 OpenAI 自家模型。
MANAGED_FEATURES = ['remote_compaction_v2']


# --------------------------------------------------------------------------
# 状态文件
# --------------------------------------------------------------------------

def load_state() -> dict:
    data = {'mode': None, 'models': {}, 'context_window': {}, 'reasoning': {}}
    if STATE_PATH.exists():
        try:
            raw = json.loads(STATE_PATH.read_text(encoding='utf-8'))
            # 只接受 dict；旧实现直接 update，一旦文件是 []/字符串就会 TypeError
            # 被 except 吞掉，或把 {"models": null} 写进内存导致后续崩溃。
            if isinstance(raw, dict):
                data.update(raw)
        except Exception:
            pass
    # 逐字段收敛类型：手工编辑或旧版本可能写入 null/字符串/list，
    # 不能让脏数据把内存结构搞坏（apply_mode 会对 models 做下标赋值）。
    for k in ('models', 'context_window', 'reasoning'):
        if not isinstance(data.get(k), dict):
            data[k] = {}
    if not isinstance(data.get('mode'), (str, type(None))):
        data['mode'] = None
    return data


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, STATE_PATH)


# --------------------------------------------------------------------------
# 模型清单
# --------------------------------------------------------------------------

def intern_models() -> list:
    """可用的端砚模型。不支持 Responses 协议的会被排除（Codex 用不了）。"""
    if CATALOG_INTERN.exists():
        try:
            data = json.loads(CATALOG_INTERN.read_text(encoding='utf-8'))
            slugs = [m['slug'] for m in data.get('models', []) if m.get('slug')]
            if slugs:
                return slugs
        except Exception:
            pass
    meta = context_windows()
    if meta:
        usable = [k for k, v in meta.items() if v.get('responses') is not False]
        if usable:
            return usable
    return list(FALLBACK_INTERN_MODELS)


def intern_unsupported() -> list:
    """已知不支持 Responses 协议、因而在 Codex 里选不得的模型。"""
    return [k for k, v in context_windows().items() if v.get('responses') is False]


def models_for(mode: str) -> list:
    if mode == 'intern':
        return intern_models()
    return list(MODES[mode]['models'])


def context_windows() -> dict:
    """端砚各模型的上下文长度（token）。

    返回值形如 {model_id: {'context': int, 'responses': bool|None}}。
    手工编辑或旧版本可能写入 null/字符串值，这里统一收敛，保证下游
    对每项都能安全调用 .get()，不会 AttributeError。
    """
    if CATALOG_INTERN_META.exists():
        try:
            raw = json.loads(CATALOG_INTERN_META.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                return {k: (v if isinstance(v, dict) else {})
                        for k, v in raw.items() if isinstance(k, str)}
        except Exception:
            pass
    return {}


def default_context_window(mode: str, model: str) -> int:
    """默认上下文上限。1M 的模型不默认全开——发那么多 token 又慢又费。"""
    if mode == 'intern':
        reported = int(context_windows().get(model, {}).get('context', 131072))
        return min(reported, MAX_CONTEXT_CAP)
    return {'openai': 400000, 'deepseek': 1048576}.get(mode, 131072)


# --------------------------------------------------------------------------
# 读取 / 写入配置
# --------------------------------------------------------------------------

def read_config() -> str:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f'找不到 Codex 配置：{CONFIG_PATH}')
    return CONFIG_PATH.read_text(encoding='utf-8')


def _top_level(text: str) -> str:
    """只取第一个 [section] 之前的顶层文本，避免子段/多行字符串里的同名键干扰。"""
    return '\n'.join(split_toml(text)[0])


def _unquote_toml(value: str) -> str:
    """解一个 TOML 基本/字面字符串：处理反斜杠转义，原样保留异类引号。

    旧代码用 [^"']+ 取内容，模型名里出现另一种引号（gpt's-turbo）或转义
    双引号（say\\"hi\\"）都会被截断，导致界面显示的当前配置是错的。
    """
    if len(value) < 2:
        return value
    q = value[0]
    if value[-1] != q:
        return value
    body = value[1:-1]
    if q == "'":
        return body                      # 字面字符串不做转义
    out, i = [], 0
    simple = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
              '"': '"', '\\': '\\'}
    while i < len(body):
        ch = body[i]
        if ch != '\\' or i + 1 >= len(body):
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt in ('u', 'U'):
            width = 4 if nxt == 'u' else 8
            hexpart = body[i + 2:i + 2 + width]
            try:
                out.append(chr(int(hexpart, 16)))
                i += 2 + width
            except (ValueError, OverflowError):
                out.append(nxt)
                i += 2
        else:
            out.append(nxt)
            i += 2
    return ''.join(out)


def _top_str(text: str, key: str) -> str:
    # 用引号配对 + 转义感知的扫描，而不是 ["\']([^"\']+)["\']：
    # 后者遇到值里的另一种引号就提前收尾。
    pat = re.compile(r'(?m)^\s*' + re.escape(key) +
                     r'\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\']*)\')')
    m = pat.search(_top_level(text))
    return _unquote_toml(m.group(1)) if m else ''


def detect_mode(text: str) -> str:
    provider = _top_str(text, 'model_provider') or 'openai'
    for key, spec in MODES.items():
        if spec['provider'] == provider:
            return key
    # 未识别的 provider（azure / ollama / 用户自定义）不再伪装成官方账号，
    # 否则 UI 会显示「官方账号」，一点「应用」就把对方的 model_provider 覆盖掉。
    return 'unknown'


def detect_model(text: str) -> str:
    return _top_str(text, 'model')


_BACKUP_NAME = re.compile(r'^config-\d{8}-\d{6}(?:-\d+)?\.toml$')


def backup_config() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = BACKUP_DIR / f'config-{stamp}.toml'
    # 同一秒内多次切换不能互相覆盖：撞名就加序号
    seq = 1
    while dest.exists():
        dest = BACKUP_DIR / f'config-{stamp}-{seq}.toml'
        seq += 1
    shutil.copy2(CONFIG_PATH, dest)
    # 只清理本程序生成的、严格符合命名格式的备份；按 mtime 而非文件名排序，
    # 避免用户手动放入的文件被误删、或系统时钟调整导致删错。
    olds = [p for p in BACKUP_DIR.glob('config-*.toml') if _BACKUP_NAME.match(p.name)]
    olds.sort(key=lambda p: p.stat().st_mtime)
    for old in olds[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def build_provider_sections(sections, mode: str):
    """写入/更新该模式需要的 [model_providers.*] 段落。"""
    spec = MODES[mode]
    pid = spec['provider']
    if mode == 'openai':
        return sections

    if mode == 'deepseek':
        if not any(s['name'] == f'model_providers.{DEEPSEEK_PROVIDER_ID}' for s in sections):
            append_section(sections, f'model_providers.{DEEPSEEK_PROVIDER_ID}', [
                'name = "deepseek"',
                'base_url = "https://api.deepseek.com/"',
                'wire_api = "responses"',
            ])
        return sections

    # intern：本地轮换网关。只删普通表，保留用户可能手写的同名数组表。
    drop_section(sections, f'model_providers.{pid}', array=False)
    drop_section(sections, f'model_providers.{pid}.auth', array=False)
    append_section(sections, f'model_providers.{pid}', [
        f'name = {toml_str(spec["provider_name"])}',
        f'base_url = {toml_str(GATEWAY_URL + "/v1")}',
        'wire_api = "responses"',
        'supports_websockets = false',
        'requires_openai_auth = false',
    ])
    append_section(sections, f'model_providers.{pid}.auth', [
        'command = "powershell.exe"',
        'args = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", '
        f'{toml_str(str(GATEWAY_AUTH))}, "-Store", {toml_str(str(GATEWAY_STORE))}, "-Mode", "Token"]',
        'timeout_ms = 15000',
        'refresh_interval_ms = 300000',
    ])
    return sections


def apply_mode(mode: str, model: str | None = None, context_window: int | None = None,
               reasoning_effort: str | None = None, gateway_timeout: float = 12.0) -> dict:
    """切换模式并写回 config.toml。返回一份结果说明。"""
    if mode not in MODES:
        raise ValueError(f'未知模式：{mode}')
    spec = MODES[mode]
    state = load_state()
    model = model or state['models'].get(mode) or spec['default_model']
    effort = reasoning_effort or state.get('reasoning', {}).get(mode) or spec['reasoning_effort']
    if effort not in spec['reasoning_options']:
        effort = spec['reasoning_effort']

    result = {'mode': mode, 'model': model, 'effort': effort, 'gateway': None, 'backup': None}

    if spec['needs_gateway']:
        ok = ensure_gateway(timeout=gateway_timeout)
        result['gateway'] = 'running' if ok else 'failed'
        if not ok:
            raise RuntimeError('端砚网关启动失败，已放弃切换。请查看 '
                               f'{GATEWAY_ERR}')

    text = read_config()
    pre, sections = split_toml(text)

    set_top_level(pre, 'model', toml_str(model))
    set_top_level(pre, 'model_provider', toml_str(spec['provider']))
    set_top_level(pre, 'model_reasoning_effort', toml_str(effort))
    set_top_level(pre, 'web_search', toml_str(spec['web_search']))
    set_top_level(pre, 'service_tier', toml_str(spec['service_tier']))

    if spec['catalog']:
        set_top_level(pre, 'model_catalog_json', toml_str(str(spec['catalog'])))
    else:
        set_top_level(pre, 'model_catalog_json', None)

    ctx = context_window or state['context_window'].get(mode)
    if spec['needs_gateway']:
        ctx = int(ctx or default_context_window(mode, model))
        set_top_level(pre, 'model_context_window', str(ctx))
        # 把自动压缩触发点拉到模型真实上限以上（1M），让它在实际使用中基本不会被触发。
        # 原因：Codex 的「压缩历史」在第三方模型上会走 ChatGPT 后端，
        # 服务端会拒绝。即使把 [features] remote_compaction_v2 设为 false，
        # 桌面端在旧会话/某些路径下还是会触发并报错。
        # 想恢复自动压缩，把 model_auto_compact_token_limit 改小即可（比如 200000）。
        model_real_max = int(context_windows().get(model, {}).get('context', 1048576))
        auto_compact = max(model_real_max, 1048576)
        set_top_level(pre, 'model_auto_compact_token_limit', str(auto_compact))
    else:
        set_top_level(pre, 'model_context_window', None)
        set_top_level(pre, 'model_auto_compact_token_limit', None)

    build_provider_sections(sections, mode)

    for key in MANAGED_FEATURES:
        set_feature(sections, key, spec.get('features', {}).get(key))

    result['backup'] = str(backup_config())
    tmp = CONFIG_PATH.with_suffix('.toml.tmp')
    tmp.write_text(join_toml(pre, sections), encoding='utf-8', newline='\r\n')
    os.replace(tmp, CONFIG_PATH)

    state['mode'] = mode
    state['models'][mode] = model
    state.setdefault('reasoning', {})[mode] = effort
    if ctx:
        state['context_window'][mode] = int(ctx)
    save_state(state)

    result['context_window'] = ctx
    return result


def detect_effort(text: str) -> str:
    # 与 detect_model/detect_mode 保持一致：只认顶层键。
    # 旧实现直接对全文 re.search，子段里若恰好有同名键且位置更靠前，
    # 界面会把子段的值当成当前推理强度显示，甚至据此做判断。
    return _top_str(text, 'model_reasoning_effort')


def config_summary() -> dict:
    text = read_config()
    mode = detect_mode(text)
    if mode in MODES:
        label = MODES[mode]['label']
    else:
        provider = _top_str(text, 'model_provider') or '（未设置）'
        label = '未识别的配置（%s）' % provider
    return {
        'mode': mode,
        'mode_label': label,
        'model': detect_model(text),
        'effort': detect_effort(text),
        'path': str(CONFIG_PATH),
    }


# --------------------------------------------------------------------------
# 网关控制
# --------------------------------------------------------------------------

def _ps(args, timeout=60):
    return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', *args],
                          capture_output=True, timeout=timeout,
                          creationflags=NO_WINDOW if os.name == 'nt' else 0)


_TOKEN_CACHE = {'key': None, 'value': None}


def gateway_token() -> str:
    # 凭证文件不变时缓存 token。健康检查每 6 秒一次、启动轮询 0.4 秒一次，
    # 每次都新起一个 PowerShell 会在切换器里造成进程风暴。
    try:
        key = (GATEWAY_AUTH.stat().st_mtime_ns, GATEWAY_STORE.stat().st_mtime_ns)
    except OSError:
        key = None
    if key is not None and _TOKEN_CACHE['key'] == key and _TOKEN_CACHE['value'] is not None:
        return _TOKEN_CACHE['value']
    r = _ps(['-File', str(GATEWAY_AUTH), '-Store', str(GATEWAY_STORE), '-Mode', 'Token'], timeout=15)
    if r.returncode != 0:
        raise RuntimeError('读取本地网关凭证失败：' +
                           r.stderr.decode('utf-8', 'replace')[:200])
    value = r.stdout.decode('utf-8', 'replace').strip()
    _TOKEN_CACHE.update(key=key, value=value)
    return value


def gateway_health(timeout: float = 3.0):
    """返回 health dict，网关没跑返回 None。"""
    if not GATEWAY_AUTH.exists() or not GATEWAY_STORE.exists():
        return None
    try:
        token = gateway_token()
    except Exception:
        return None
    req = urllib.request.Request(GATEWAY_URL + '/health',
                                 headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """进程是否仍存活（Windows tasklist / POSIX kill(0)）。"""
    if os.name == 'nt':
        image = _image_name(pid)
        return image is not None and image.startswith('python')
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def gateway_pid():
    """监听 18791 的进程 PID。优先用 gateway.pid（记录的是真实启动者），
    校验存活后返回；否则回退到 netstat 按端口反查。"""
    pid_file = GATEWAY_DIR / 'gateway.pid'
    try:
        recorded = int(pid_file.read_text(encoding='utf-8').strip())
        if recorded > 0 and _pid_alive(recorded):
            return recorded
    except (OSError, ValueError):
        pass
    try:
        r = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                           timeout=30, creationflags=NO_WINDOW if os.name == 'nt' else 0)
    except Exception:
        return None
    # 网关只绑定 127.0.0.1。旧代码用 endswith(':18791') 会把 [::]:18791、
    # 127.0.0.2:18791 等无关监听也算进来，进而可能 stop 掉别人的进程。
    want_addr = '127.0.0.1:' + str(GATEWAY_PORT)
    for line in r.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == 'TCP' \
                and parts[1].lower() == want_addr and parts[3].upper() == 'LISTENING':
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def _image_name(pid: int):
    try:
        r = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                           capture_output=True, timeout=30,
                           creationflags=NO_WINDOW if os.name == 'nt' else 0)
    except Exception:
        return None
    txt = r.stdout.decode('utf-8', 'replace').strip()
    if not txt or 'INFO:' in txt:
        return None
    return txt.split(',')[0].strip('"').lower()


_GATEWAY_LOCK = threading.RLock()


def ensure_gateway(timeout: float = 12.0) -> bool:
    """网关没跑就拉起来。进程内串行化，避免自愈线程/应用线程/手动启动并发拉多个。"""
    with _GATEWAY_LOCK:
        return _ensure_gateway_locked(timeout)


def _ensure_gateway_locked(timeout: float) -> bool:
    if gateway_health(timeout=2.0):
        return True
    if not GATEWAY_SCRIPT.exists():
        return False
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    interpreter = _pythonw()
    if not interpreter:
        # frozen 且本机找不到可用的 Python：如实失败，绝不拿 EXE 自己当解释器
        try:
            with open(GATEWAY_ERR, 'ab') as f:
                f.write(('未找到可用的 Python 解释器，无法启动网关；'
                         '请安装 Python 3 或在 config/runtime.json 里配置其路径。\n')
                        .encode('utf-8'))
        except OSError:
            pass
        return False
    cmd = [interpreter, '-u', str(GATEWAY_SCRIPT),
           '--store', str(GATEWAY_STORE), '--state', str(GATEWAY_STATE)]
    base = DETACHED | NEW_GROUP | NO_WINDOW
    proc = None
    # 子进程拿到的是 dup 后的句柄；父进程这边无论成功失败都要 close，
    # 否则反复启动失败时每次泄漏 2 个 fd，最终耗尽文件描述符。
    with contextlib.ExitStack() as stack:
        log = stack.enter_context(open(GATEWAY_LOG, 'ab'))
        err = stack.enter_context(open(GATEWAY_ERR, 'ab'))
        for flags in (base | BREAKAWAY, base):
            try:
                proc = subprocess.Popen(cmd, stdout=log, stderr=err, stdin=subprocess.DEVNULL,
                                        cwd=str(GATEWAY_DIR), creationflags=flags)
                break
            except OSError:
                continue
    if proc is None:
        return False
    try:
        (GATEWAY_DIR / 'gateway.pid').write_text(str(proc.pid), encoding='utf-8')
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        if gateway_health(timeout=2.0):
            return True
    return False


def stop_gateway() -> str:
    if not _require_windows('停止网关'):
        return '当前系统不支持网关控制（仅 Windows）。'
    pid = gateway_pid()
    if pid is None:
        return '网关本来就没在运行。'
    image = _image_name(pid)
    # 解释器可能是 python3.12.exe / pypy3.exe / venv 里的自定义名字，
    # 用前缀集合覆盖，既不误杀也不误拒。
    ok_prefixes = ('python', 'pypy')
    if image is not None and not image.startswith(ok_prefixes):
        return f'端口 {GATEWAY_PORT} 被 {image} 占用，不是网关，未做任何操作。'
    # 判定不了进程身份时（tasklist 失败/被拦截）不能盲目 taskkill：
    # 端口可能被系统或其他程序占用，误杀会造成外部破坏。
    if image is None:
        return f'无法确认 PID {pid} 的进程身份，为安全起见未停止。请手动检查端口 {GATEWAY_PORT}。'
    try:
        r = subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, timeout=30,
                           creationflags=NO_WINDOW if os.name == 'nt' else 0)
    except OSError as exc:
        return f'停止失败：无法执行 taskkill（{exc}）。'
    # 用轮询代替固定 sleep(0.6)：慢机器上进程退出可能更久，一次性等待会误报失败。
    for _ in range(10):
        time.sleep(0.2)
        if gateway_pid() is None:
            try:
                (GATEWAY_DIR / 'gateway.pid').unlink(missing_ok=True)
            except OSError:
                pass
            return f'已停止网关（PID {pid}）。'
    return '停止失败：' + r.stderr.decode('utf-8', 'replace')[:200]


def reset_gateway_state() -> str:
    """清空「已耗尽/失效」标记。"""
    was_running = gateway_health(timeout=2.0) is not None
    if was_running:
        stopped = stop_gateway()
        # stop_gateway 返回「停止失败：...」说明端口仍被占用，此时跑重置脚本
        # 会与仍在运行的网关抢 state.json，结果不可信——直接中止。
        if stopped.startswith('停止失败'):
            return '重置中止：' + stopped + ' 请先手动停止网关。'
    interpreter = _python()
    if not interpreter:
        return '重置失败：本机找不到可用的 Python 解释器。'
    try:
        r = subprocess.run([interpreter, str(GATEWAY_SCRIPT), '--store', str(GATEWAY_STORE),
                            '--state', str(GATEWAY_STATE), '--reset-exhausted'],
                           capture_output=True, timeout=120,
                           creationflags=NO_WINDOW if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return '重置失败：执行重置脚本出错（%s）。' % exc
    if r.returncode != 0:
        return '重置失败：' + r.stderr.decode('utf-8', 'replace')[:300]
    if was_running:
        if ensure_gateway():
            return '已清空标记，网关已恢复运行。'
        return '已清空标记，但网关未能重新启动，请点「启动网关」。'
    return '已清空全部「额度耗尽 / 密钥失效」标记。'


# --------------------------------------------------------------------------
# 重启 Codex
# --------------------------------------------------------------------------

def _require_windows(action: str = '') -> bool:
    """网关控制/重启依赖 Windows 专有命令；非 Windows 上明确报错而不是抛底层异常。

    action 用于拼出可读的提示（如「停止网关仅支持 Windows」），
    旧实现收下参数却不用，调用方只能自己写死提示文案。
    """
    if os.name != 'nt':
        if action:
            try:
                with open(GATEWAY_ERR, 'ab') as f:
                    f.write((f'{action}仅支持 Windows，当前操作已跳过。\n').encode('utf-8'))
            except OSError:
                pass
        return False
    return True


def codex_pids() -> list:
    if os.name != 'nt':
        return []
    ps = ("Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | "
          "Where-Object { try { $_.Path -like '*OpenAI.Codex*' } catch { $false } } | "
          "Select-Object -ExpandProperty Id")
    try:
        r = _ps(['-Command', ps], timeout=60)
    except Exception:
        return []
    return [int(x) for x in r.stdout.decode('utf-8', 'replace').split() if x.strip().isdigit()]


def restart_codex(wait: bool = True) -> str:
    if not _require_windows('重启 Codex'):
        return '当前系统不支持自动重启 Codex（仅 Windows）。配置已写入，请手动打开。'
    pids = codex_pids()
    if pids:
        id_list = ','.join(str(p) for p in pids)
        _ps(['-Command', f'Get-Process -Id {id_list} -ErrorAction SilentlyContinue | '
                         'ForEach-Object { $_.CloseMainWindow() | Out-Null }'], timeout=60)
        time.sleep(3)
        still = codex_pids()
        if still:
            id_list = ','.join(str(p) for p in still)
            _ps(['-Command', f'Stop-Process -Id {id_list} -Force -ErrorAction SilentlyContinue'],
                timeout=60)
            time.sleep(2)
    if CODEX_LAUNCHER.exists():
        os.startfile(str(CODEX_LAUNCHER))  # noqa: S606
        if wait:
            time.sleep(2)
        return 'Codex 已重启。'
    return '配置已写入，但没找到 Codex 启动入口，请手动打开 Codex。'


# --------------------------------------------------------------------------
# 刷新端砚模型目录（联网）
# --------------------------------------------------------------------------

def _all_keys() -> list:
    """读取全部上游密钥，统一检查子进程返回码与 JSON 合法性。"""
    r = _ps(['-File', str(GATEWAY_AUTH), '-Store', str(GATEWAY_STORE), '-Mode', 'All'], timeout=60)
    if r.returncode != 0:
        raise RuntimeError('读取密钥库失败：' + r.stderr.decode('utf-8', 'replace')[:200])
    try:
        secrets = json.loads(r.stdout.decode('utf-8', 'replace'))
    except Exception as exc:                          # noqa: BLE001
        raise RuntimeError('密钥库返回内容不是合法 JSON：%s' % exc)
    return secrets.get('keys') or []


def _safe_int(value, default: int = 131072) -> int:
    """上游可能返回 '1M' / '131072' / None，统一收敛成整数且在合理区间。"""
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        n = int(value)
    else:
        raw = str(value)
        m = re.match(r'\s*(\d+(?:\.\d+)?)', raw)
        if not m:
            return default
        n = float(m.group(1))
        # 单位要允许 M/MB/MiB、K/KB/KiB 这些常见写法。
        # 旧代码用 [mM]\b：'1MB' 里 M 后紧跟 B（同为单词字符）没有词边界，
        # 于是匹配失败、退化成默认的 131072，把 1M 上下文的模型当成 128K。
        tail = raw[m.end():].lstrip().lower()
        if re.match(r'm(?:i?b)?\b', tail) or tail.startswith('m'):
            n *= 1024 * 1024
        elif re.match(r'k(?:i?b)?\b', tail) or tail.startswith('k'):
            n *= 1024
        n = int(n)
    return n if 1024 <= n <= 10 * 1024 * 1024 else default


def fetch_intern_models(timeout: float = 60.0):
    """向端砚拉取模型清单，返回 (models, context_map)。"""
    # 取全部上游密钥，逐把试到成功为止
    keys = _all_keys()
    if not keys:
        raise RuntimeError('密钥库是空的，无法刷新模型目录。')
    last_error = None
    for key in keys:
        req = urllib.request.Request(INTERN_BASE_URL + '/models',
                                     headers={'Authorization': 'Bearer ' + key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as exc:                      # noqa: BLE001
            last_error = exc
            continue
        # 上游偶发会返回 data=null / data={} / 元素缺字段，这里逐层收敛，
        # 避免一个畸形响应就把整次刷新打成未捕获异常。
        raw_models = data.get('data') if isinstance(data, dict) else None
        if not isinstance(raw_models, list):
            last_error = RuntimeError('模型列表格式不正确（data 不是数组）')
            continue
        models, ctx = [], {}
        for m in raw_models:
            if not isinstance(m, dict):
                continue
            slug = m.get('id')
            if not slug:
                continue
            models.append(m)
            window = None
            modalities = m.get('input_modalities')
            if not isinstance(modalities, list):
                modalities = []
            for im in modalities:
                if not isinstance(im, dict) or im.get('type') != 'text':
                    continue
                supported = im.get('supported_inputs')
                if not isinstance(supported, dict):
                    continue
                length = supported.get('max_context_length')
                if isinstance(length, dict):
                    window = length.get('value')
            ctx[slug] = {'context': _safe_int(window),
                         'display': m.get('name') or slug}
        if models:
            return models, ctx
    raise RuntimeError(f'端砚模型列表获取失败：{last_error}')


def probe_responses(model: str, key: str, timeout: float = 90.0):
    """探一下这个模型认不认 Responses 协议（直连上游，不经过网关）。

    返回 True / False / None（None 表示网络抖动，判断不了，按可用处理）。
    """
    body = json.dumps({
        'model': model,
        'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}],
        'stream': False,
    }).encode('utf-8')
    req = urllib.request.Request(INTERN_BASE_URL + '/responses', data=body,
                                 headers={'Authorization': 'Bearer ' + key,
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            json.loads(resp.read().decode('utf-8'))
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace').lower()
        if 'not support' in detail and 'protocol' in detail:
            return False
        if exc.code in (400, 404, 422):
            return False
        return None
    except Exception:                                   # noqa: BLE001
        return None


def _first_working_key() -> str:
    keys = _all_keys()
    if not keys:
        raise RuntimeError('密钥库是空的')
    return keys[0]


def refresh_intern_catalog(probe: bool = True) -> str:
    models, ctx = fetch_intern_models()

    # 继承上一次的协议支持结论，避免一次网络抖动把模型误判成可用
    previous = context_windows()
    for mid, info in ctx.items():
        if mid in previous and 'responses' in previous[mid]:
            info['responses'] = previous[mid]['responses']

    if probe:
        try:
            key = _first_working_key()
            import concurrent.futures as _futures

            def one(m):
                mid = m['id']
                verdict = probe_responses(mid, key)
                if verdict is None:                     # 可能只是被限流，退一步再试
                    time.sleep(1.5)
                    verdict = probe_responses(mid, key, timeout=120)
                if verdict is None:                     # 还判断不了就保留上次结论
                    verdict = ctx.get(mid, {}).get('responses')
                return verdict

            with _futures.ThreadPoolExecutor(max_workers=2) as pool:
                verdicts = list(pool.map(one, models))
            for m, verdict in zip(models, verdicts):
                ctx.setdefault(m['id'], {})['responses'] = verdict
        except Exception:                               # noqa: BLE001
            for m in models:
                ctx.setdefault(m['id'], {}).setdefault('responses', None)
    else:
        for m in models:
            ctx.setdefault(m['id'], {}).setdefault('responses', None)

    usable = [m for m in models if ctx.get(m['id'], {}).get('responses') is not False]
    usable_ids = {m['id'] for m in usable}
    skipped = [m['id'] for m in models if m['id'] not in usable_ids]
    _build_intern_catalog(usable, ctx)

    note = f'已刷新端砚模型目录：{len(usable)} 个可用'
    if skipped:
        note += '；%d 个不支持 Responses 协议已排除（%s）' % (len(skipped), '、'.join(skipped))
    return note


def _build_intern_catalog(models, ctx) -> Path:
    """基于 models.json 的 deepseek 模板，生成端砚模型目录。"""
    template = None
    if CATALOG_DEEPSEEK.exists():
        try:
            cat = json.loads(CATALOG_DEEPSEEK.read_text(encoding='utf-8'))
            entries = cat.get('models', [])
            template = entries[0] if entries else None
        except Exception:
            template = None

    out = []
    for m in models:
        slug = m['id']
        entry = json.loads(json.dumps(template)) if template else {'slug': slug}
        entry['slug'] = slug
        entry['display_name'] = m.get('name') or slug
        window = int(ctx.get(slug, {}).get('context', 131072))
        entry['context_window'] = window
        entry['max_context_window'] = window
        entry['auto_compact_token_limit'] = int(window * COMPACT_RATIO)
        entry['visibility'] = 'list'
        entry['supported_in_api'] = True
        modalities = []
        for im in m.get('input_modalities', []):
            kind = im.get('type')
            if kind == 'text':
                modalities.append('text')
            elif kind == 'image':
                modalities.append('image')
        if 'text' not in modalities:
            modalities.append('text')
        entry['input_modalities'] = modalities
        desc = m.get('description')
        if desc:
            entry['description'] = desc
        out.append(entry)

    CATALOG_INTERN.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_INTERN.write_text(
        json.dumps({'models': out}, ensure_ascii=False, indent=2), encoding='utf-8')
    CATALOG_INTERN_META.write_text(
        json.dumps(ctx, ensure_ascii=False, indent=2), encoding='utf-8')
    return CATALOG_INTERN
