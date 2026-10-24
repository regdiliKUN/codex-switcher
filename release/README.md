# 发行包 · Codex 切换器

本目录存放 **Codex 切换器** 的 Windows x64 可运行包体（由桌面快捷方式实际指向的安装/发布目录整理而来）。

| 项目 | 内容 |
| --- | --- |
| 版本 | `2026.10.24` |
| 平台 | Windows x64 |
| 包体路径 | `release/2026.10.24/codex-switcher-2026.10.24-win-x64/` |
| 文件数 | 21 |
| 体积 | 约 12.2 MB |

## 运行方式

双击 `CodexSwitcher.exe`

## 校验完整性

```bash
cd release
sha256sum -c SHA256SUMS.txt
```

Windows PowerShell：

```powershell
Get-Content release\SHA256SUMS.txt | ForEach-Object {
  $h, $p = $_ -split '  ', 2
  if ((Get-FileHash $p -Algorithm SHA256).Hash -ne $h.ToUpper()) { "MISMATCH $p" }
}
```

## 关于 Codex 切换器

一键切换 Codex 官方账号 / 端砚 API 密钥 / DeepSeek 配置。

## 未纳入的内容

PyInstaller 中间产物 `build/` 未纳入。

以上目录属于开发环境、运行期缓存或大体积模型权重，不影响程序本体运行。
