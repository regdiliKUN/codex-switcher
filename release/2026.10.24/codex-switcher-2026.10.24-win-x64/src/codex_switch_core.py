"""Codex 模式切换核心逻辑。

职责：
  * 安全改写 ~/.codex/config.toml（只动托管键，保留 plugins / projects 等其余内容）
  * 三种模式互切：官方账号 / 端砚 API 密钥 / DeepSeek
  * 端砚本地网关的启停、健康检查、密钥状态查询
  * 重启 Codex 桌面端

本模块不弹任何 UI，方便单独跑脚本或写测试。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
    for r in roots:
        for pat in pats:
            try:
                out.extend(sorted(r.glob(pat), reverse=True))
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
    return sys.executable


def _pythonw() -> str:
    """无窗口解释器，用来后台拉网关。"""
    return _pick_python(windowed=True)


def _python() -> str:
    """需要读取 stdout 时用带控制台的解释器（pythonw 没有输出）。"""
    p = Path(_pick_python(windowed=False))
    c = p.with_name('python.exe')
    return str(c if c.is_file() else p)


# --------------------------------------------------------------------------
# 极简 TOML 行编辑（只做「顶层键」和「整段表」两种操作，其余原样保留）
# --------------------------------------------------------------------------

_HEADER = re.compile(r'^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$')


def split_toml(text: str):
    """切成 (顶层行列表, [{'name':..,'lines':[..]}...])。"""
    pre, sections, cur = [], [], None
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            cur = {'name': m.group(1).strip(), 'lines': [line]}
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
    """literal 为 None 表示删除该键。只匹配顶层（第一个 [section] 之前）。"""
    pat = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
    idx = [i for i, l in enumerate(pre) if pat.match(l)]
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


def drop_section(sections, name: str):
    sections[:] = [s for s in sections if s['name'] != name]
    return sections


def append_section(sections, name: str, body):
    sections.append({'name': name, 'lines': [f'[{name}]'] + list(body)})
    return sections


def toml_str(value: str) -> str:
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


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
        if enabled is not True:
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
            data.update(json.loads(STATE_PATH.read_text(encoding='utf-8')))
        except Exception:
            pass
    data.setdefault('models', {})
    data.setdefault('context_window', {})
    data.setdefault('reasoning', {})
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
    """端砚各模型的上下文长度（token）。"""
    if CATALOG_INTERN_META.exists():
        try:
            return json.loads(CATALOG_INTERN_META.read_text(encoding='utf-8'))
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


def detect_mode(text: str) -> str:
    m = re.search(r'(?m)^\s*model_provider\s*=\s*["\']?([\w.\-]+)', text)
    provider = m.group(1) if m else 'openai'
    for key, spec in MODES.items():
        if spec['provider'] == provider:
            return key
    return 'openai' if provider in ('openai', 'chatgpt') else 'openai'


def detect_model(text: str) -> str:
    m = re.search(r'(?m)^\s*model\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else ''


def backup_config() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = BACKUP_DIR / f'config-{stamp}.toml'
    shutil.copy2(CONFIG_PATH, dest)
    olds = sorted(BACKUP_DIR.glob('config-*.toml'))
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

    # intern：本地轮换网关
    drop_section(sections, f'model_providers.{pid}')
    drop_section(sections, f'model_providers.{pid}.auth')
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
    m = re.search(r'(?m)^\s*model_reasoning_effort\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1) if m else ''


def config_summary() -> dict:
    text = read_config()
    mode = detect_mode(text)
    return {
        'mode': mode,
        'mode_label': MODES[mode]['label'],
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


def gateway_token() -> str:
    r = _ps(['-File', str(GATEWAY_AUTH), '-Store', str(GATEWAY_STORE), '-Mode', 'Token'])
    if r.returncode != 0:
        raise RuntimeError('读取本地网关凭证失败：' +
                           r.stderr.decode('utf-8', 'replace')[:200])
    return r.stdout.decode('utf-8', 'replace').strip()


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


def gateway_pid():
    """监听 18791 的进程 PID。"""
    try:
        r = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True,
                           timeout=30, creationflags=NO_WINDOW if os.name == 'nt' else 0)
    except Exception:
        return None
    for line in r.stdout.decode('utf-8', 'replace').splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == 'TCP' \
                and parts[1].endswith(':' + str(GATEWAY_PORT)) and parts[3].upper() == 'LISTENING':
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


def ensure_gateway(timeout: float = 12.0) -> bool:
    """网关没跑就拉起来。"""
    if gateway_health(timeout=2.0):
        return True
    if not GATEWAY_SCRIPT.exists():
        return False
    GATEWAY_DIR.mkdir(parents=True, exist_ok=True)
    log = open(GATEWAY_LOG, 'ab')
    err = open(GATEWAY_ERR, 'ab')
    cmd = [_pythonw(), '-u', str(GATEWAY_SCRIPT),
           '--store', str(GATEWAY_STORE), '--state', str(GATEWAY_STATE)]
    base = DETACHED | NEW_GROUP | NO_WINDOW
    proc = None
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
    pid = gateway_pid()
    if pid is None:
        return '网关本来就没在运行。'
    image = _image_name(pid)
    if image not in ('python.exe', 'pythonw.exe'):
        return f'端口 {GATEWAY_PORT} 被 {image} 占用，不是网关，未做任何操作。'
    r = subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, timeout=30,
                       creationflags=NO_WINDOW if os.name == 'nt' else 0)
    time.sleep(0.6)
    if gateway_pid() is None:
        return f'已停止网关（PID {pid}）。'
    return '停止失败：' + r.stderr.decode('utf-8', 'replace')[:200]


def reset_gateway_state() -> str:
    """清空「已耗尽/失效」标记。"""
    was_running = gateway_health(timeout=2.0) is not None
    if was_running:
        stop_gateway()
    r = subprocess.run([_python(), str(GATEWAY_SCRIPT), '--store', str(GATEWAY_STORE),
                        '--state', str(GATEWAY_STATE), '--reset-exhausted'],
                       capture_output=True, timeout=120,
                       creationflags=NO_WINDOW if os.name == 'nt' else 0)
    if r.returncode != 0:
        return '重置失败：' + r.stderr.decode('utf-8', 'replace')[:300]
    if was_running:
        ensure_gateway()
    return '已清空全部「额度耗尽 / 密钥失效」标记。'


# --------------------------------------------------------------------------
# 重启 Codex
# --------------------------------------------------------------------------

def codex_pids() -> list:
    ps = ("Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | "
          "Where-Object { try { $_.Path -like '*OpenAI.Codex*' } catch { $false } } | "
          "Select-Object -ExpandProperty Id")
    try:
        r = _ps(['-Command', ps], timeout=60)
    except Exception:
        return []
    return [int(x) for x in r.stdout.decode('utf-8', 'replace').split() if x.strip().isdigit()]


def restart_codex(wait: bool = True) -> str:
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

def fetch_intern_models(timeout: float = 60.0):
    """向端砚拉取模型清单，返回 (models, context_map)。"""
    # 取全部上游密钥，逐把试到成功为止
    r = _ps(['-File', str(GATEWAY_AUTH), '-Store', str(GATEWAY_STORE), '-Mode', 'All'], timeout=60)
    secrets = json.loads(r.stdout.decode('utf-8'))
    keys = secrets.get('keys') or []
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
        models, ctx = [], {}
        for m in data.get('data', []):
            slug = m.get('id')
            if not slug:
                continue
            models.append(m)
            window = None
            for im in m.get('input_modalities', []):
                if im.get('type') == 'text':
                    window = (im.get('supported_inputs', {})
                              .get('max_context_length', {}).get('value'))
            ctx[slug] = {'context': int(window) if window else 131072,
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
    r = _ps(['-File', str(GATEWAY_AUTH), '-Store', str(GATEWAY_STORE), '-Mode', 'All'], timeout=60)
    keys = json.loads(r.stdout.decode('utf-8')).get('keys') or []
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
    skipped = [m['id'] for m in models if m not in usable]
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
