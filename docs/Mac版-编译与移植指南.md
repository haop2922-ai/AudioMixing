# AudioMixing · Mac 版编译与移植指南

> 基于当前仓库（PC v2.38 / 手机 2.26）的真实代码写成，不是通用教程。
> 所有文件路径都对应本仓库；带行号的说明会随代码变动，以文件内容为准。
> 本文只讲"怎么把现有东西搬到 macOS"，不改 Windows / Android 线的任何代码。

---

## 0. 先看结论

- **协议层和业务层基本能原样搬**：UDP 收发、时钟同步、发现、路由、Web 面板、混音引擎的算法部分都是纯 Python。
- **真正的工作量在音频这一层**：`audiomixing/audio/backends/wasapi.py` + `wasapi_com.py`（约 57 KB 的 COM/WASAPI 代码）在 Mac 上完全没有对应物，要换成 CoreAudio；而且 **WASAPI 的"系统声音回环采集"在 macOS 上不是一个 API，得靠 ScreenCaptureKit 或虚拟声卡**，这是整个移植里最硬的一块。
- **托盘、单实例、开机自启、防火墙、蓝牙识别**是 Windows 专有实现，需要各写一份 Mac 版，但都属于"外围活"。
- 量级预估：一个有音视频经验的开发者，**音频层 2–3 周，外围 + 打包签名 1 周左右**；不是"改几行就能跑"。

---

## 1. 前置条件

### 1.1 机器

| 项 | 建议 | 原因 |
|---|---|---|
| 芯片 | **Apple Silicon（M1 及以上）** | 新机器都是 arm64；Nuitka/CoreAudio 在 arm64 上路径最短。Intel Mac 也能开发，但产物是 x86_64，后续要 universal 就得双架构构建 |
| macOS | **13 Ventura 及以上** | 系统声音采集如果走 ScreenCaptureKit，需要 13+；12 及以下只能靠虚拟声卡 |
| 磁盘 | 预留 10 GB | Xcode CLT ~2 GB、Python 依赖、Nuitka 编译缓存 |
| Xcode | 只装 **Command Line Tools** 即可 | `xcode-select --install`。完整 Xcode 只有做公证/打包 .app 图标时才更方便，不是必须 |

### 1.2 Python

- 用 **Python 3.13**（与 Windows 线一致；Nuitka 4.3 支持）。
- 建议 `brew install python@3.13`，不要用系统自带的 `/usr/bin/python3`（Apple 的 Python 被系统保护，装包容易出问题）。
- 虚拟环境一律放仓库外或 `.venv/`（本仓库的 `.gitignore` 已忽略 `.venv`）。

### 1.3 签名：要不要 Apple 开发者账号

| 场景 | 做法 | 代价 |
|---|---|---|
| 只在自己的 Mac 上跑 | **ad-hoc 签名**：`codesign --force --deep --sign - AudioMixing.app`，再 `xattr -dr com.apple.quarantine AudioMixing.app` | 免费；每台新机器第一次要手动去掉隔离属性 |
| 发给别人（少量） | 同上，对方右键→打开，或终端执行 `xattr -d com.apple.quarantine` | 免费；对方会被 Gatekeeper 拦一次 |
| 正式分发 | **Apple Developer Program（$99/年）** → Developer ID Application 证书 → `codesign` → `notarytool` 公证 → `stapler` 装订 | 要付费、要审核等待（通常几分钟到几小时）；之后双击即可打开 |

> 关键点：**没签名/没公证的 .app 从网上下载后会被加 `com.apple.quarantine` 扩展属性**，双击提示"已损坏，无法打开"。`xattr -dr com.apple.quarantine` 就是去掉这个标记，不是绕过安全机制，只是告诉系统"这个文件是我自己信任的"。

---

## 2. 逐模块：能搬 / 要重写 / 要改配置

### 2.1 能原样搬（纯 Python，无平台依赖）

| 文件 | 说明 |
|---|---|
| `audiomixing/proto.py` | 28 字节包头、编解码、时钟同步。纯 `struct`，零平台相关 |
| `audiomixing/net/rtpaudio.py`（66 KB） | RTP 风格收发、抖动缓冲、SSRC。只用 `socket` / `threading` |
| `audiomixing/net/discovery.py`（55 KB） | UDP 广播发现、信标。只用 `socket`；信标里带的 `platform.system()` 会自动写 `Darwin`，不影响互通 |
| `audiomixing/ports.py` | 端口占用探测。只有一处 Windows 专用：`SO_EXCLUSIVEADDRUSE`（L48），Mac 上 `hasattr` 为 False 会自动跳过 |
| `audiomixing/audio/format.py` | 重采样、声道映射、限幅、测量。纯 numpy |
| `audiomixing/audio/capture.py` / `local.py` / `tap.py` | 采集分发、本机播放循环、旁路。逻辑与平台无关，只通过 `base.py` 的后端接口取设备 |
| `audiomixing/audio/backends/file.py` / `null.py` / `netmic.py` | 文件、空设备、网络麦克风，都是纯 Python |
| `audiomixing/ui/*.html` | 面板前端，浏览器里跑，与系统无关 |
| `audiomixing/webui.py`（273 KB） | HTTP 面板与 API 主体。里面有不少 `sys.platform == "win32"` 分支（自启动、防火墙、Chrome 查找），Mac 上会走 else 分支，**能起来**，但这些功能要另补（见 2.3） |
| `audiomixing/engine.py` 的大部分 | 混音、路由、增益、限幅、统计。只有两处要动：`_boost_current_thread()`（L108–140，Windows MMCSS）和默认后端选择 |

### 2.2 必须重写（Windows 专有 → macOS 对应物）

| 现有文件 | 为什么不行 | Mac 上换成什么 |
|---|---|---|
| `audio/backends/wasapi.py`（32 KB） | `ctypes.wintypes`、`IMMDevice`、`IAudioClient`、WAVEFORMATEXTENSIBLE，全是 COM/WASAPI | 新建 `audio/backends/coreaudio.py`：用 `pyobjc`（`AVFoundation` / `CoreAudio`）或 `sounddevice`（PortAudio 封装）。需要实现 `BackendBase` 的 `available/list_devices/open_source/open_sink` 四个接口 |
| `audio/backends/wasapi_com.py`（25 KB） | 手写 COM 虚表、`CoInitialize`、IID 定义 | 整文件不需要；换成 `pyobjc` 的 Objective-C 桥或 `ctypes` 调 `AudioObjectGetPropertyData` |
| **系统声音回环采集**（wasapi 的 `loopback` 类别） | WASAPI 有 `AUDCLNT_STREAMFLAGS_LOOPBACK`；**CoreAudio 没有等价 API** | 两条路：① macOS 13+ 用 **ScreenCaptureKit**（`SCStream` + `SCStreamConfiguration.capturesAudio`，需要"屏幕录制"权限）；② 装虚拟声卡 **BlackHole**，把它当输入设备（稳定、兼容性好，但要用户装驱动）。建议先做 ②，再做 ① |
| 设备枚举与默认设备切换（wasapi.py 里 `_endpoint_devices` / `_find_endpoint` / `_mix_format_of`） | 依赖 IMMDeviceEnumerator、PKEY 属性 | CoreAudio：`AudioObjectGetPropertyData(kAudioHardwarePropertyDevices)` 枚举；默认设备用 `kAudioHardwarePropertyDefaultOutputDevice`；切换默认设备需要 `AudioObjectSetPropertyData`（部分属性只读，可能要用 `SwitchAudioSource` 这类命令行工具兜底） |
| `audio/multiout.py`（7.6 KB） | `_sink_spec()` 把非文件设备**写死成 `wasapi`**（L34–43）；多路输出靠每路各开一个 WASAPI 渲染端点 | 逻辑可留，把后端名改成 `coreaudio`。更优：macOS 原生支持**聚合设备（Aggregate Device）**，可以在 Audio MIDI Setup 里把多个输出合成一个，程序只开一路——能省掉不少同步问题 |
| 蓝牙端点识别（wasapi.py `_a2dp_registry_endpoint_id` 读注册表；`bluetooth.py` 全量） | `bluetooth.py` 用 PowerShell + Windows 蓝牙栈（`_windows_bluetooth_available` L147 直接要求 `win32`）；`winreg` 在 Mac 不存在 | 设备侧：CoreAudio 的 `kAudioDevicePropertyTransportType` 判断 `Bluetooth`；系统侧：`system_profiler SPBluetoothDataType -json` 解析已配对设备。`bluetooth.py` 需要新建 `bluetooth_mac.py`，接口保持不变 |
| `pycaw` / `comtypes` 类依赖 | 仓库里目前是**手写 ctypes COM**，没有直接 import pycaw，但思路相同，Mac 一律不可用 | 不需要 1:1 替代；用 `pyobjc-framework-CoreAudio` / `sounddevice` 覆盖 |
| `audiomixing/tray.py`（22 KB） | `NOTIFYICONDATA`、`WNDCLASSW`、`Shell_NotifyIcon`、找 Edge 的 `LOCALAPPDATA` 路径 | 换 `rumps`（macOS 菜单栏，最贴合）或 `pystray`；打开面板用 `open http://127.0.0.1:17800`（`webbrowser.open` 会自动用默认浏览器）。Windows 的 Edge `--app=` 窗口模式在 Mac 上可用 Chrome/Safari 代替，或直接默认浏览器 |
| `audiomixing/audio/opus.py` | 只找 `opus*.dll`（`_candidate_dirs` 里判断 `.dll`） | 改找 `libopus*.dylib`：`brew install opus` 后从 `/opt/homebrew/lib/libopus.dylib` 加载，打包时把 dylib 放进 `.app/Contents/Frameworks` 并 `install_name_tool -change`。`opuslib_next` 在 macOS 上通常自带 dylib，先试它，找不到再回退 Homebrew |
| `netcheck.py` 的诊断分支 | 大量 `netsh` / `Get-NetAdapter` / 防火墙规则 | 新建 Darwin 分支：`ifconfig`、`networksetup -getinfo Wi-Fi`、`scutil --dns`；防火墙用 `socketfilterfw`（只读检查即可） |

### 2.3 要改配置和路径（逻辑保留，实现换掉）

| 位置 | 现状 | Mac 上怎么改 |
|---|---|---|
| `config.py` L46–55 | **已经写好 Darwin 分支**：`~/Library/Application Support/AudioMixing` | 不用改；确认 `AUDIOMIXING_HOME` 覆盖逻辑仍优先（L43） |
| `webui.py` L233 / L2380 / L2467 等 | `os.environ.get("APPDATA")` | 统一走 `config.config_dir()`，不要再用 `APPDATA`；Mac 上该变量不存在，`or` 后面的兜底会指到 `~/AudioMixing`，与 `config.py` 不一致 |
| 单实例锁 | `webui.py` 会查端口 + 进程（`sys.platform != "win32"` 时跳过 PID 反查） | 用 `fcntl.flock` 锁 `~/Library/Application Support/AudioMixing/.lock`，或继续用"端口被占即视为已有实例"（更简单，已能工作） |
| 开机自启动 | `webui.py` 写 `HKCU\...\Run` 或 `shell:startup` 的 .lnk | 写 **LaunchAgent**：`~/Library/LaunchAgents/com.audiomixing.agent.plist`（模板见下）。`launchctl bootstrap gui/$UID <plist>` 加载，`bootout` 卸载 |
| 托盘 | `tray.py` Win32 | `rumps` 菜单栏 App；`serve` 模式下在后台线程跑 `rumps.App.run()` |
| 日志 | `%APPDATA%\AudioMixing\launcher.log` | `~/Library/Logs/AudioMixing/launcher.log`（macOS 惯例），或沿用 Application Support；`_autostart_log` 等函数里的兜底路径要一起改 |
| 打开面板 | `webui.py` / `tray.py` 找 Edge 的 `msedge.exe --app=` | `subprocess.run(["open", "-a", "Google Chrome", "--args", "--app=" + url])`，失败退回 `webbrowser.open(url)` |
| 防火墙放行 | `webui.py` 调 `netsh advfirewall` | Mac 默认不拦出站；入站首次监听会弹"是否允许接受传入连接"，点允许即可。可在设置页给一句说明，不必写代码 |

**LaunchAgent 模板**（`~/Library/LaunchAgents/com.audiomixing.agent.plist`）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.audiomixing.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/AudioMixing.app/Contents/MacOS/AudioMixing</string>
    <string>serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>/Users/你的用户名/Library/Logs/AudioMixing/out.log</string>
  <key>StandardErrorPath</key><string>/Users/你的用户名/Library/Logs/AudioMixing/err.log</string>
</dict>
</plist>
```

> 注意：LaunchAgent 里**不能写 `%TEMP%/onefile_*`**，也不能依赖相对路径；`ProgramArguments` 必须是 `.app` 里真正的可执行文件。若用 Nuitka `--onefile`，启动时会解包，首启会慢 1–3 秒，但 `ProgramArguments` 仍指向 app 内的启动器，不需要改。

---

## 3. 打包

### 3.1 Nuitka（首选，与 Windows 线一致）

Windows 线现在用的是：

```
--onefile / --standalone
--zig --target-arch=x86_64
--enable-plugin=numpy
--include-data-dir=audiomixing/ui=audiomixing/ui
--output-dir=dist_nuitka
```

macOS 上把架构换成 arm64，并生成 .app：

```bash
python -m nuitka audiomixing_launcher.py \
  --standalone \
  --macos-create-app-bundle \
  --macos-app-icon=tools/audiomixing.icns \
  --enable-plugin=numpy \
  --include-data-dir=audiomixing/ui=audiomixing/ui \
  --include-data-files=/opt/homebrew/lib/libopus.dylib=libopus.dylib \
  --macos-target-arch=arm64 \
  --output-dir=dist_nuitka
```

要点：

- **不要再用 `--zig`**：macOS 直接用系统 clang（`xcode-select --install` 提供）最稳；zig 交叉编译在 Mac 上对 CoreAudio 框架链接容易出问题。
- **不要 `--onefile`**：.app 已经是目录形态；`--standalone` + app bundle 启动更快，也方便签名（onefile 每次解包会破坏签名校验）。
- **universal2**：如果确实要同时支持 Intel 和 Apple Silicon，最稳的是分别构建 arm64 和 x86_64，再用 `lipo -create` 合并可执行文件，或使用 Nuitka 的 `--macos-target-arch=universal2`（构建机需同时具备两套 SDK 架构，失败就先退回双构建）。
- `.app` 里需要把 `libopus.dylib` 放到 `Contents/Frameworks/`，并用 `install_name_tool -change` 把引用改成 `@executable_path/../Frameworks/libopus.dylib`。

### 3.2 py2app（备选）

只在 Nuitka 遇到无法绕过的打包问题时才用。`setup.py` 里 `setup(app=[...], options={"py2app": {...}})`，同样要把 `audiomixing/ui` 作为 `resources` 打进去。缺点是 numpy/opus 的 hook 不如 Nuitka 成熟，产物体积也更大。

### 3.3 签名与公证命令

```bash
# A. 自己用（ad-hoc）
codesign --force --deep --sign - dist_nuitka/AudioMixing.app
xattr -dr com.apple.quarantine dist_nuitka/AudioMixing.app

# B. 分发（Developer ID）
codesign --force --deep --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  dist_nuitka/AudioMixing.app

ditto -c -k --keepParent dist_nuitka/AudioMixing.app AudioMixing.zip
xcrun notarytool submit AudioMixing.zip \
  --apple-id "你的 Apple ID" --team-id TEAMID \
  --password "app-specific-password" --wait
xcrun stapler staple dist_nuitka/AudioMixing.app
spctl --assess --type execute -vv dist_nuitka/AudioMixing.app   # 应输出 accepted
```

> `--deep` 只用于本地临时签名；正式分发建议对 `Contents/Frameworks/*.dylib` 逐个签名，再签主 bundle。

---

## 4. 分阶段（别一次吃成胖子）

| 阶段 | 目标 | 怎么验证 | 卡住先查 |
|---|---|---|---|
| **① 能出声（本机）** | 新建 `coreaudio.py`，实现"列设备 + 打开默认输出 + 播放一个文件/正弦" | `python -m audiomixing.cli serve`，面板里选 file 音源 → 默认设备能听到；用 `null` 后端跑通全链路 | 采样率：`format.py` 固定 48 kHz，若设备是 44.1 kHz，确认 `Resampler` 被调用；权限：系统设置→隐私→麦克风 |
| **② 两台 Mac UDP 通** | 发现、点对点、路由表 | 两台 Mac 同一 WiFi，互相能看到；`python tools/selftest_twonode.py`（如依赖 Windows 的部分跳过） | 组播/广播被 AP 隔离；防火墙入站弹窗；端口是否被别的程序占（`ports.py` 会自动换端口） |
| **③ Mac ↔ Windows 互通** | 协议不变，直接对连 | Windows 端 v2.38 与 Mac 端互发；看 `/api/status` 的 peer 列表与延迟 | 两端 `SAMPLE_RATE`/`CHANNELS` 必须一致（48000/2）；编解码若走 opus，确认 dylib 版本 |
| **④ Mac ↔ Android** | 手机连 Mac | 手机扫码/手填 Mac IP，能收听/送声 | Android 端按 IP 直连，不依赖 mDNS；确认 Mac 监听 0.0.0.0 且未被 macOS 防火墙拦 |
| **⑤ 系统声音采集** | 虚拟声卡或 ScreenCaptureKit | 播放音乐，面板"收"有电平 | 权限（屏幕录制）、BlackHole 是否设为多输出设备 |
| **⑥ 托盘 + 自启动 + 打包签名** | 完整体验 | 重启后菜单栏有图标、LaunchAgent 生效、双击 .app 能开 | LaunchAgent 路径、quarantine、签名 |

---

## 5. 第一天在 Mac 上要敲的命令

```bash
# 1) 基础工具
xcode-select --install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2) Python 与音频库
brew install python@3.13 opus

# 3) 取代码（换成你的仓库地址/拷贝方式）
cd ~/code
git clone <你的仓库> AudioMixing
cd AudioMixing

# 4) 虚拟环境与依赖（仓库目前没有 requirements.txt，按实际 import 装）
python3.13 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install numpy opuslib-next pyogg qrcode
# 后面做 CoreAudio 时再加：
# pip install pyobjc-framework-CoreAudio pyobjc-framework-AVFoundation sounddevice rumps

# 5) 先不碰声卡，用空后端确认程序能起
AUDIOMIXING_HOME="$HOME/Library/Application Support/AudioMixing-dev" \
  python audiomixing_launcher.py serve --host 127.0.0.1 --web-port 17800

# 6) 打开面板
open http://127.0.0.1:17800
```

> 预期：第 5 步能起服务、面板能打开；音频设备列表里暂时只有 `file` / `null` / `netmic`，没有真实声卡——这是正常的，CoreAudio 后端还没写。

---

## 6. 明确的坑

1. **系统声音采集没有现成 API**：WASAPI loopback 在 macOS 上不存在。要么 ScreenCaptureKit（13+，要屏幕录制权限），要么虚拟声卡（BlackHole）。这是移植里唯一"必须做架构选择"的地方。
2. **采样率**：`format.py` 固定 `SAMPLE_RATE = 48000`、`CHANNELS = 2`。Mac 内置扬声器常见 44.1 kHz，蓝牙耳机可能是 16 kHz（通话模式）。`Resampler` 已经存在，但要在 CoreAudio 后端里对每个设备读实际格式并转换，不能假定 48 kHz。
3. **麦克风/屏幕录制权限**：TCC 弹窗只在**打包成 .app 且 Info.plist 带 `NSMicrophoneUsageDescription`** 时正常出现；终端里跑 python 会继承终端自己的权限，容易误判"代码没问题"。调试期建议先给 Terminal/iTerm 授权。
4. **`opus.py` 只认 `.dll`**：`_candidate_dirs()` 里的 `endswith(".dll")` 在 Mac 上永远匹配不到，必须加 `.dylib`，否则表现是"能起服务但一发声音就报找不到 opus"。
5. **`multiout.py` 写死 `wasapi`**（L34–43）：不改的话，Mac 上多路输出会直接报 unknown backend。
6. **`webui.py` 的 `APPDATA` 兜底**：Mac 上没有 `APPDATA`，会落到 `~/AudioMixing`，和 `config.py` 的 `~/Library/Application Support/AudioMixing` 不一致，出现"设置读不到/两份配置"。
7. **Nuitka 的 `--zig --target-arch=x86_64` 不要照搬**：那是为了 Windows 上避开 AVX-512；Mac 上用 clang + `--macos-target-arch=arm64`。
8. **签名与 onefile 冲突**：onefile 每次启动解包到临时目录，签名校验会失败/变慢；Mac 上优先 `--standalone` + .app。
9. **托盘不是窗口**：macOS 菜单栏 App 需要 `LSUIElement`/`NSApplicationActivationPolicyAccessory`，否则会多出一个 Dock 图标。用 `rumps` 时设 `app = rumps.App(..., quit_button=None)` 并处理 Dock 图标。
10. **`selftest_twonode.py` 与部分单测带 Windows 假设**：跑之前先看它有没有直接 import `wasapi`；必要时先跑 `python -m unittest discover -s tests -k "proto or format or discovery"` 这类平台无关子集。

---

## 7. 一句实话

**工作量最大的是 CoreAudio 后端 + 系统声音采集，约占整个移植的 60–70%**；其余（托盘、自启动、路径、打包签名）是零碎但可预期的一周活。

如果只想"先在自己的 Mac 上听到声音"，做到阶段 ① 就够，大概几天；要做到"和 Windows/Android 对等可用"，请按 2–3 周规划，并且把"系统声音采集方案"（虚拟声卡 vs ScreenCaptureKit）当成第一个要拍板的技术决策，而不是最后才碰的细节。
