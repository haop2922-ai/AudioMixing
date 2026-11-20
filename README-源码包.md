# Audio Mixing 源码包

- 电脑端版本：**2.44.2**
- 手机端版本：**2.31.1（versionCode 49）**
- 打包时间：2026-11-21
- 来源：当前工作区 `e:\AI\Audio\AudioMixing`

## 目录

| 目录 | 内容 |
|---|---|
| `电脑端/` | Python 源码（`audiomixing/`）、测试（`tests/`）、构建/验收工具（`tools/`）、启动脚本、根文档 |
| `手机端/` | Android 工程源码：`android/app/src`、`android/tools`、`android/testapp/src`、Gradle 配置 |
| `文档/` | 现有 Markdown 文档（不含截图、自检输出） |

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

## 本包对应的当前改动

- **2.44.2 蓝牙保活**：常开 helper 在 `State` 持续非 `Opened`（约 3.5 秒）时重建连接；`Opened` 满 20 分钟主动回收一次，修复长时间后 A2DP 假死。
- **2.31.1 Opus 解码**：手机端用 Concentus 解 PC 发来的 Opus；PC 只在手机信标带 `opus:1` 时才发 Opus，老手机仍强制 PCM16。
- **路由 epoch + 自环丢弃**（2.44.1）：按作者拒绝旧路由推送，丢弃本机↔本机自环。
- **诊断**：PC 转发手机 `sys_q14 / sys_mute`。

## 刻意排除

`release/`、`dist_nuitka/`、`版本备份/`、`回收站/`、`.tmp/`、`promo/`、`docs/screenshots`、
`build/`、`.gradle/`、`__pycache__/`、`*.apk`、`*.pyc`、`android/local.properties`。

## 统计

- 电脑端：77 个文件
- 手机端：45 个文件
- 文档：39 个文件
- 合计：161 个文件，4.02 MB（不含本说明；加本说明后 162 个文件）