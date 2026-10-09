# 平台说明：每台设备上「系统声音采集」到底能做到什么

这一页把各平台的现实限制写清楚，避免在不可能的地方浪费时间。

## Windows 10/11 —— 已实现并实测 ✅

* **WASAPI Loopback**：对默认渲染端点（扬声器/耳机）请求一个捕获客户端，就能拿到这台机器
  *正在播放* 的所有声音（含系统提示音、浏览器、会议软件）。
  不需要虚拟声卡，不需要管理员权限，不需要安装驱动。
* 实现方式：`wavelink/audio/backends/wasapi_com.py` 用 ctypes 手写 COM 调用
  （`IMMDeviceEnumerator` / `IAudioClient` / `IAudioCaptureClient` / `IAudioRenderClient`），
  零第三方依赖。共享模式格式（通常是 48 kHz float32）在边界处转成本项目统一的
  48 kHz float32 交错格式，其余代码完全平台无关。
* **单进程采集**（只抓某个 App，比如只要微信不要网易云）：Windows 10 2004+ 提供
  Process Loopback（`ActivateAudioInterfaceAsync` + `AUDIOCLIENT_ACTIVATION_PARAMS`
  指定进程树）。接口位置已在 `wasapi.py` 的 `loopback` 分支预留，未实现。
  现阶段替代方案：把该 App 的输出设备设成虚拟声卡（VB-CABLE），WaveLink 采集那块虚拟声卡。
* 采样率不是 48 kHz 的设备：用 `audio/format.py` 的窗函数 sinc 重采样器处理，
  已测 44.1k↔48k 往返相关性 0.9999。

## Linux —— 接口就绪，后端待接

* PulseAudio：`pactl list short sources` 里形如 `*monitor` 的就是「系统声音」源，直接能读；
  PipeWire 时代用 `pw-record --target=<monitor>` 或 `parec`。
* 计划实现：`wavelink/audio/backends/pipewire.py`，用子进程（`parec`/`pw-record`）采集、
  `pacat`/`pw-play` 播放，统一转成本项目的 float32@48k 格式。不需要 ctypes，实现成本低。
* 如果只想快速验证，也可以先 `arecord | python -m wavelink send <host>` 走标准输入。

## macOS —— 系统限制，需虚拟声卡

* macOS 不允许普通 App 捕获其它 App 的输出。要采集系统声音，业界通用做法是安装
  虚拟音频设备（**BlackHole** 免费、Loopback 商业），把系统输出路由到虚拟设备，
  WaveLink 再采集该设备。
* macOS 13+ 的 ScreenCaptureKit 提供系统音频捕获（需用户授权“屏幕录制”权限），
  可以做成无虚拟声卡的方案，但需要 ObjC/Swift 桥接。
* 播放侧（CoreAudio / AudioQueue）没有限制。

## Android —— 必须用户授权（系统的硬性规定）

* **AudioPlaybackCapture**（Android 10+）：可以捕获**其它 App** 的音频，但必须
  ① 用户通过 MediaProjection 弹窗授权，② 目标 App 的 `allowAudioPlaybackCapture` 允许，
  ③ 受 DRM 保护的内容永远抓不到。
* **MediaProjection + AudioRecord**：授权一次可以持续采集整机可捕获音频，是主流做法
  （Scrcpy、AudioRelay 等都走这条路）。
* 因此手机端的产品形态是：打开 App → 点「开始投射」→ 系统弹窗授权 → 之后的音频走 WaveLink
  协议发到目标节点。**无法做到“装完就静默后台采集”**，这是 Android 隐私策略，不是实现问题。
* 实现路线建议（工作量从大到小）：
  1. 用 Kotlin 写一个最小 App：MediaProjection → AudioRecord(48k, stereo, PCM16) →
     UDP 按 `proto.py` 的格式发包。协议是公开且已经稳定的（28 字节头）。
  2. 先用 **Termux + scrcpy 的音频转发** 做验证，把 PCM 通过管道喂给
     `python -m wavelink send <host>`（需要 Termux 里能跑 Python）。
  3. 先不做手机端，用蓝牙/音频线把手机声音接进一台电脑（见 `VS-PHONELINK.md` 的混合方案）。

## iOS —— 限制最严

* 只能通过 **Broadcast Upload Extension**（ReplayKit）采集，需要用户从控制中心手动开始，
  且扩展进程内存上限约 50 MB；受 DRM 保护的内容禁止录制。
* 结论：iOS 端适合做「我主动开播」的形态，不适合「长期后台静默采集」。

## 移动端采集接入后需要的东西（协议已经就绪）

手机端只需要实现三件事：

1. **发现**：监听/发送 `net/discovery.py` 的 JSON 信标（UDP 47830，组播 239.255.77.120 +
   广播），或者手动填目标 IP；
2. **发送**：48 kHz、双声道、PCM16 或 Opus，按 `proto.py` 打包，UDP 发到目标节点的音频端口
   （默认 47831），包头字段 `ssrc/seq/samples/ts_ns` 必须填对（`ts_ns` 用 Unix epoch 纳秒）；
3. **播放**（可选）：AudioTrack 播放收到的流，就成了一个「音响设备」。

协议是自描述的、没有握手要求，所以一个 200 行的原生客户端就能成为合法的 WaveLink 节点。
