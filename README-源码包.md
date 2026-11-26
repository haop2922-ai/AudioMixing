# Audio Mixing 源码包

- 电脑端版本：**2.46.11**
- 手机端版本：**2.31.7（versionCode 55）**
- 打包时间：2026-11-26
- 来源：当前工作区 `e:\AI\Audio\AudioMixing`

## 目录

| 目录 | 内容 |
|---|---|
| `电脑端/` | Python 源码（`audiomixing/`）、测试（`tests/`）、构建/验收工具（`tools/`）、启动脚本、根文档、版本索引 |
| `手机端/` | Android 工程源码：`android/app/src`、`android/testapp/src`、`android/tools`、Gradle 配置 |
| `文档/` | 现有 Markdown 文档（不含截图、自检输出） |

## AI 接口（MCP）

- 文件：`电脑端/tools/audiomixing_mcp.py`（仅标准库，本地 stdio，无第三方依赖）。
- 配置：`python tools/audiomixing_mcp.py --print-config`，把输出的 `mcpServers` 粘到 AI 客户端。
- 自检：`python tools/audiomixing_mcp.py --selftest`（需要 Audio Mixing 正在运行）。
- 说明：`电脑端/tools/audiomixing_mcp.README.md`。
- 只访问 `127.0.0.1:17800`；批量改动先 `plan`、再 `apply(confirm=True)`，支持 `undo`。
- 审计日志：`%APPDATA%\AudioMixing\mcp-audit.log`。

## 电脑端构建

1. Python 3.13 x64。
2. 安装依赖：`pip install nuitka numpy pyogg opuslib_next qrcode pillow`。
3. 源码运行：`python audiomixing_launcher.py`，或双击 `start-from-source.bat`。
4. 打包单文件：`python tools/build_nuitka.py`，产物在 `dist_nuitka/AudioMixing.exe`。
5. Nuitka 首次会自动下载 MinGW/zig 工具链；构建脚本已固定 `-mcpu=baseline`，避免 AVX-512 非法指令。

## 手机端构建

1. JDK 17、Android SDK、Gradle 8.7（本包不含 Gradle Wrapper，可用本机 Gradle 或 Android Studio 打开 `手机端/android`）。
2. 在 `手机端/android/` 下新建 `local.properties`，写 `sdk.dir=你的 Android SDK 路径`（本包刻意不带机器路径）。
3. 构建：`gradle -p android assembleRelease`。
4. 唯一第三方依赖：`io.github.jaredmdobson:concentus:1.0.2`（Maven Central，纯 Java Opus 解码）。
5. 本包不含 keystore；签名请用你自己的调试/发布证书。

## 本包对应的近期改动

- **2.46.11 蓝牙卡顿**：`/api/devices` 原来被面板每 800ms 强制重枚举全部 WASAPI 端点
  （含蓝牙 A2DP、还会扫注册表），反复打开端点会让蓝牙音频一卡一卡。改为 4 秒缓存；
  多耳机页「刷新设备」显式 `?refresh=1` 才强制重扫；`/api/multiout` 轮询复用同一份缓存。
- **2.46.10 端口与安卓广播**：音频端口 65534 不再算出 65535 的广播端口（400 拒绝）；
  手动添加设备的非数字端口返回 400；安卓定向广播改用网卡真实广播地址（/16、/23 正确）；
  安卓手动电脑 IP 覆盖全部发现端口。
- **2.46.9 自主审查连续修复**：USB 自动模式只认真插 USB（跳过无线调试/模拟器）；
  `lan_report()` 不在请求线程跑 PowerShell；`gain=1` 真快速路径；修复抖动缓冲设置失效；
  修复 `/api/monitor/disconnect` GET 的 NameError。
- **2.46.8 代码审查**：USB 自动桥只在新插上执行一次；`find_adb` 兼容 onefile；
  网卡类型探测移到后台；`usb_auto` 字符串归一；安卓发现自检用「尝试轮数」；
  多耳机矩阵保留离线对端行、全部停止连远端路由一起停。
- **2.46.7 多耳机矩阵**：简洁＝输出设备卡片墙；详细＝「音源 × 输出设备」矩阵，
  行/列点表头整行整列、最后一行/列折叠、左上全开右下全关、列头 🎚 调音量/延迟。
- **手机 2.31.7**：真实前缀定向广播 + 手动 IP 覆盖全部发现端口（对应电脑端 2.46.10 起）。
- **手机 2.31.6**：发现自检用尝试轮数；手动电脑 IP 改用文本/URI 键盘。
- **手机 2.31.5**：发现端口兜底、手动电脑 IP、空列表发现自检、NEARBY_WIFI_DEVICES。

## 刻意排除

`release/`、`dist_nuitka/`、`版本备份/`、`Mac版交接/`、`promo/`、`dev/`、`.tmp/`、`.trae/`、
`docs/screenshots`、`docs/selfcheck`、`docs/参赛材料-截图`、`tools/installer_payload`、
`tools/testmedia`、`tools/adb-logs`、`build/`、`.gradle/`、`out/`、`__pycache__/`、
`*.apk`、`*.pyc`、`android/local.properties`。

## 统计

- 合计：**170 个文件，约 4.39 MB**（未压缩）；压缩包见同目录 `AudioMixing-源码-2.46.11-2.31.7.zip`。
