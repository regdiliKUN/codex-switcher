# Codex 切换器（独立 EXE 版）

把原来「桌面快捷方式 → pythonw.exe 跑 .py 脚本」的启动方式，改成一个**双击即用**的
独立可执行程序。桌面上只留一个快捷方式，右键 → 打开文件所在位置会直接落到
`I:\CodexSwitcher\CodexSwitcher.exe`。

---

## 快速上手

1. 双击桌面「Codex 切换器」。
2. 选服务商（官方账号 / 端砚 API 密钥 / DeepSeek）→ 选模型 → 「应用并重启 Codex」。

配置写入的仍然是 `C:\Users\<你>\.codex\config.toml`，备份仍在
`C:\Users\<你>\.codex\backups\`，所以**原来切换器的所有行为、状态、密钥都没变**。

---

## 目录结构

```
I:\CodexSwitcher\
├── CodexSwitcher.exe          主程序（单文件，12 MB，内含 Python 运行时 + tkinter）
├── assets\
│   ├── icon.ico               多尺寸图标（16/24/32/48/64/128/256）
│   ├── icon-16.png ... icon-512.png
│   ├── preview-sizes.png      尺寸总览（深/浅两版）
│   ├── preview-desktop.png    桌面真实尺寸模拟
│   └── evolution.png          20 轮迭代演进图
├── config\
│   └── runtime.json           运行环境配置（网关用的 Python 路径）
├── src\                       源码（可改，改完重新打包）
│   ├── codex_switcher.py      界面
│   ├── codex_switch_core.py   切换逻辑 / 配置改写 / 网关控制
│   └── shell_link.py          ctypes 版 IShellLink（生成 .lnk）
├── tools\
│   ├── make_icon.py           图标渲染（20 轮参数全部在里面）
│   └── install_shortcut.py    重建桌面快捷方式
├── docs\
│   └── 图标迭代记录.md
└── build\
    ├── CodexSwitcher.spec     PyInstaller 配置
    ├── version_info.txt       EXE 版本信息
    └── backup\                旧快捷方式备份
```

---

## 关于「依赖」的说明（重要）

| 部分 | 是否依赖本机 Python |
|---|---|
| 主界面（选服务商 / 选模型 / 写配置 / 重启 Codex） | **不依赖**，Python 运行时已打进 EXE |
| 端砚 API 密钥模式的后台网关（`gateway.py`） | **需要**本机有一个 Python 3 |

原因：端砚模式的本地网关 `C:\Users\<你>\.codex\intern-gateway\gateway.py` 是一个
常驻的独立 HTTP 进程，原架构就是「主程序 + 网关脚本」两个进程。它不是源码脚本依赖，
而是这个功能本身的组成部件。

程序按以下顺序找这个 Python，找到即可，**不需要你手动配置**：

1. 环境变量 `CODEX_SWITCHER_PYTHON`
2. `config\runtime.json` 里的 `python` 字段（当前已填好本机 Python 3.12）
3. 原解释器 / PATH 里的 `pythonw.exe`
4. `C:\`、`C:\Program Files`、`%LOCALAPPDATA%\Programs\Python` 下的 `Python3*`

如果全部找不到，只有端砚模式会提示「网关启动失败」，官方账号与 DeepSeek 模式照常可用。

---

## 常见问题

**Q：双击后要等一两秒才出窗口？**
A：单文件 EXE 每次启动会把内置运行时解包到 `%TEMP%`（约 25 MB），这是 onefile
模式的正常代价。若想秒开，可改用目录模式重新打包（见下）。

**Q：杀毒软件报警 / 拦截？**
A：PyInstaller 打包的未签名 EXE 常被启发式误判。加入信任即可，程序不联网上传任何
数据（只有端砚模式会连 `discovery-api.intern-ai.org.cn`）。

**Q：桌面图标还是旧的？**
A：Windows 图标缓存。执行一次
`python tools/install_shortcut.py`（会重建快捷方式并刷新缓存），
或在任务管理器里重启「Windows 资源管理器」。

**Q：换了 Python 安装位置？**
A：改 `config\runtime.json` 的 `python` 字段，或删掉该字段让它自动搜索。

---

## 重新打包

```bash
# 1) 准备一个带 tkinter 的 Python 3.12 venv
python -m venv venv
venv\Scripts\pip install pyinstaller pillow

# 2) 打包
venv\Scripts\pyinstaller --noconfirm --onefile --windowed ^
  --name CodexSwitcher ^
  --icon "I:\CodexSwitcher\assets\icon.ico" ^
  --add-data "I:\CodexSwitcher\assets\icon.ico;assets" ^
  --version-file "I:\CodexSwitcher\build\version_info.txt" ^
  --distpath "I:\CodexSwitcher" ^
  --workpath "I:\CodexSwitcher\build\work" ^
  --specpath "I:\CodexSwitcher\build" ^
  "I:\CodexSwitcher\src\codex_switcher.py"

# 3) 重建快捷方式
venv\Scripts\python tools\install_shortcut.py

# 4) 重做图标（可选）
venv\Scripts\python tools\make_icon.py assets
```

> 想秒开可把 `--onefile` 换成 `--onedir`，产物会变成 `CodexSwitcher\` 文件夹
> （EXE 在文件夹根目录），把整个文件夹放到 I 盘同样位置即可。

---

## 相对原版的代码改动

只有三处，都是为了适应「打包成 EXE」：

1. `codex_switcher.py`
   - `HERE` 改为由 `sys.executable` 推算（打包后 `__file__` 指向临时解包目录）
   - 新增 `resource_path()`：图标按 EXE 同目录 → `assets\` → 内置副本 查找
   - `main()` 里设置 `AppUserModelID`，让任务栏用本程序的图标而不是 Python 的
2. `codex_switch_core.py`
   - `_pythonw()` 重写为 `_pick_python()` 多路查找（否则打包后 `sys.executable`
     就是本 EXE，会递归启动自己）
   - `reset_gateway_state()` 里 `sys.executable` → `_python()`
3. 新增 `config\runtime.json`

业务逻辑（模式定义、config.toml 改写、网关健康检查、重启 Codex）一行没动。
