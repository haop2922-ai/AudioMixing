# 真实渲染自测截图（2026-11-06 / 11-07）

这些截图由 `tools\capture_ui_screenshots.py` 启动真实浏览器（Chrome/Edge headless + CDP）
访问真实服务后截取，不是手画的、也不是单测拼出来的 DOM。

| 目录 | 是什么 | 怎么用 |
|---|---|---|
| `round1\` | 修复前（v2.30 源码） | 反面证据：01 横幅错误地写了 `http://172.23.128.1:...`（WSL/Hyper-V 虚拟网卡），手机连不上 |
| `round2-source\` | 第一轮修复后的源码服务 | 证明横幅已变成 `192.168.0.105`、本机卡片已从「局域网里的设备」消失 |
| `round3-exe\` | 第一次成品 exe 截图（**沙箱默认配置**，无 `launch_at_login`） | 只作过程证据。它的 04 图里「自启动」勾选框是空的 —— 这是沙箱配置没带该字段，不是产品问题；验收请看 `round5-desktop-exe` |
| `round4-realconfig\` | 成品 exe + **真实 config.json 副本**（临时目录） | 证明真实配置下勾选框是勾上的、端口/节点名与用户机器一致 |
| `round5-desktop-exe\` | **最终验收**：桌面 `Audio Mixing v2.31.exe` 本体 + 真实配置副本 | 用户实际会看到的那一份；04 图的注册表提示指向桌面 v2.31 |

> 为什么 round3 会出现「未勾选 + 已在注册表登记」：勾选框读的是 `config.launch_at_login`，
> 下面那行读的是**真实注册表**。round3 的临时 HOME 是程序新建的默认配置（该项为 false），
> 注册表却是上一轮用户勾过留下的（true），两者本来就可以不同 —— 页面把两个事实都写出来，
> 是设计如此。round4 / round5 用真实配置副本复核后，勾选框与注册表提示一致。

## round5-desktop-exe 逐张图看什么（验收图）

- `01-status.png` —— 顶端横幅：局域网地址必须是 `192.168.0.105` 一类真实网卡，不能是 `172.x`；
  版本号应为 v2.31；「本机状态」各字段不得出现测试名或“还没测到”占位。
- `02-devices.png` —— 「局域网里的设备」：**不应出现任何标着「本机」的卡片**；
  列表为空时应是「还没有发现别的设备…」而不是空白/假设备；
  下方「蓝牙（手机的声音）」卡片应是真实配对设备名与 MAC，不能有 `probe/selftest/test` 这类名字。
- `03-relation.png` / `03b-relation-card.png` —— 设备关系图全貌与单独裁切：
  本机节点只出现一次、IP 是 `192.168.0.105`；蓝牙设备名与真实配对一致；图例是中文且能解释连线。
- `04-settings.png` —— 设置页：节点名 `2024TSJ`；三个端口；「登录 Windows 后自动运行」应为已勾选，
  且显示的命令指向桌面 `Audio Mixing v2.31.exe`（不能出现“指向临时目录/和当前程序对不上”的红字）。
- `05-monitor.png` —— 监控页：输出电平、运行时长、刷新说明；
  「网络自检」未跑时写「还没有检查」并给出「检查」按钮（这是明确的待操作提示，不是假状态）。
- `06-diagnose.png` —— 诊断全文：版本必须写 `Audio Mixing v2.31 · exe`；
  地址 `192.168.0.105 / 以太网`；开机自启动「已登记」；
  不得出现 `自启动复验`、`probe`、`selftest`、`还没测到` 等残留或红字错误。
- `07-cast.png` —— 广播页：频道音量、三个勾选项文案；横幅地址同样必须是真实网卡地址。

## 复现命令

```powershell
# 1) 临时 HOME + 复制真实配置（绝不写用户的 config.json）
$sb = Join-Path $env:TEMP 'am-exe-selfcheck'
Remove-Item $sb -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $sb | Out-Null
Copy-Item "$env:APPDATA\AudioMixing\config.json" (Join-Path $sb 'config.json')
$env:AUDIOMIXING_HOME = $sb

# 2) 用桌面成品 exe 启动（用真实端口；不要用 dist_nuitka 副本，
#    因为“登录自启动”自愈会把注册表改写成当前 exe 的路径）
Start-Process 'C:\Users\75341\Desktop\Audio Mixing v2.31.exe' -ArgumentList 'serve' -PassThru

# 3) 真实浏览器截图
python tools\capture_ui_screenshots.py --url http://127.0.0.1:17800 --out docs\selfcheck\round5-desktop-exe

# 4) 收尾：停止实例；确认注册表仍指向桌面 v2.31
Get-Process 'Audio Mixing v2.31' | Stop-Process -Force
(Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run').AudioMixing

# 5) UI 残留门禁（对真实配置）
python tools\check_ui_residue.py
```
