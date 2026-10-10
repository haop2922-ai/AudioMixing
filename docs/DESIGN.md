# WaveLink 设计文档（局域网声音投射）

## 1. 要解决的问题

* 办公室：3 台手机 + 2 台电脑，只有 1 副耳机。多台设备的提示音、通话、音乐都要进到**同一副耳机**，且**不能外放**（公司不能吵）。
* 家庭：我的电脑声音要能投射到伴侣的耳机；她的电脑声音我也能听到；**只在我们指定的音响设备播放**，不影响其他家庭成员（隐私/不打扰）。
* 更一般地：**任意源设备 → 任意一个或多个目标音响设备**，可多对多，可混音。

### 1.1 范围边界（重要）

本方案**只在同一个局域网内工作**，并刻意不提供以下任何能力：

| 不做 | 原因 |
| --- | --- |
| 云服务器中转 / 账号体系 | 你的声音不该经过第三方；家里/公司也不需要多一个依赖 |
| NAT 穿透 / 打洞 / 中继 | 那是"跨网络语音软件"（OOPZ 那一类）的形态，不是本项目的目标 |
| 接受公网地址作为源或目标 | `is_lan_address()` 会拒绝，`send --allow-wan` 才可显式绕过（默认关闭） |
| 互联网发现服务 | 发现只靠局域网组播/广播 + 手动填同网段 IP |

判定标准：目标地址必须是私有地址、链路本地地址或 `.local` 名字。用 `wavelink net` 查看本机
可被哪些局域网地址访问。这样做的额外好处是攻击面极小——数据包根本出不去这个子网。

结论：需要一个「源（Source）— 路由（Route）— 目标（Sink）」模型，并把
**采集**、**编码**、**传输**、**混音/同步**、**播放** 五件事做好。

```
 手机A ─┐                                    ┌─→ 我的耳机      (仅我听到)
 手机B ─┤  采集+编码 ──UDP/LAN──→ 目标节点   ├─→ 伴侣的耳机    (她听到我的 + 我听到她的)
 电脑C ─┤      (多对多路由表)                 └─→ 客厅音箱      (全家共享, 可选)
 伴侣电脑┘
```

## 2. 角色与拓扑

一台设备上跑一个 **WaveLink 节点进程**（node）。节点同时具备两种能力：

| 能力 | 说明 | 对应代码 |
| --- | --- | --- |
| Source（采集端） | 抓本机系统声音 / 麦克风 / 指定进程，编码后经 UDP 发出 | `wavelink/audio/backends/*` 的 `open_source()` |
| Sink（播放端） | 收包、抖动缓冲、解复用、混音、重采样，输出到指定输出设备 | `open_sink()` |
| Control（控制面） | 发现、路由下发、音量/静音、状态上报；Web UI | `wavelink/net/*`, `wavelink/webui.py` |

**默认是去中心化的**：没有服务器也能用。任一台设备都可以打开 Web UI 当“总控台”，
它只是向各节点下发同一份路由表（幂等、可重复下发），节点之间仍然直连传输音频。
（可选 `--hub` 模式：所有音频经一台设备转发，适合跨网段/AP 隔离的场景。）

## 3. 数据面协议（port 47831/udp，单播）

为了低延迟和最小依赖，数据面是自研的极简 RTP：12 字节固定头 + 负载。

```
偏移  字段        类型     说明
0     magic       u16      0x57 0x4C ("WL")，用于快速丢弃噪声
2     version     u8       1
3     type        u8       1=AUDIO 2=HELLO 3=HELLO_ACK 4=PING 5=PONG
                          6=BYE 7=STATS 8=ROUTE 9=KEEPALIVE
4     flags       u8       bit0=discontinuity(丢包后首包) bit1=last(结束帧)
5     codec       u8       1=PCM_F32 2=PCM_S16 3=OPUS
6     seq         u16      每流递增，用于丢包统计
8     ssrc        u32      流 ID（一个 source→sink 会话内唯一）
12    samples     u32      该包第一个采样在本流中的位置（48kHz 域）
16    ts_ns       u64      采集时刻的 Unix epoch 纳秒（用于端到端同步）
24    gain        i16      源端建议增益 Q14（10000 = 1.0），便于源端做音量
26    payload_len u16
28    payload    bytes     编码后的音频
```

* 采样率**统一为 48 kHz / float32 或 s16 交错**。重采样只在设备边界做一次。
* PCM 是本项目的默认编码：局域网内带宽充裕（48k 立体声 s16 = 1.54 Mbit/s），
  零算法延迟、零额外依赖。`codec=3` 走 Opus（约 64 kbit/s），若本机有 `opuslib`
  或 `libopus` 动态库则自动可用，否则回落到 PCM（并在 UI 上标注）。
* 时间戳用 epoch 纳秒，配合 `PING/PONG` 四时间戳法估算 **时钟偏移 + RTT**
  （NTP 风格），Sink 才能把不同机器的采样对齐到同一个播放时间轴。

## 4. 发现（discovery）

1. **自定义信标（默认，跨平台、无依赖）**：每 2 秒向
   `239.255.77.120:47830` 组播 + `255.255.255.255:47830` 广播一份 JSON 信标，
   内含 `node_id / name / platform / 输入设备列表 / 输出设备列表 / 正在监听的端口`。
   收到即加入“在线设备表”，15 秒没收到标记离线。同时向子网扫描一次 `/24`，兼容
   屏蔽组播的路由器。
2. **mDNS/DNS-SD（兼容层）**：注册 `_wavelink._udp.local`（PTR/SRV/TXT），
   方便将来的原生客户端和 `dns-sd -B` 调试。（本仓库先实现信标；mDNS 解析器可选实现。）
3. **手动添加**：`audiomixing peer add 192.168.1.23`，用于 AP 隔离 / 跨网段场景。

## 5. 同步与抖动

* 每个 Sink 每个流一个 **抖动缓冲**（目标深度 = 40 ms 起步，自适应 20–120 ms）。
* 播放时刻 = `ts_ns + offset`（offset 由 PING/PONG 估算）。到点前不出声，到点后若数据
  未到，插一帧静音并标记 `discontinuity`，避免其它流被拖住。
* 慢速时钟漂移用**比例控制器**修正：若缓冲长期偏大就丢一个采样，长期偏小就补一个采样，
  周期 ≥ 1 s，听感上不可察觉（< 1 sample/秒）。
* 混音用饱和加法（`clip(a+b)`），多路同时到达也不爆音。

## 6. 路由模型

一份路由表就是一组规则，格式（JSON）：

```json
{
  "sinks": { "<node_id>": { "name": "我的耳机", "device": "耳机 (Realtek)", "master": 0.9 } },
  "routes": [
    { "id": "r1", "from": ["phone-a", "laptop-c"], "to": ["my-pc"], "gain": 1.0, "mute": false }
  ],
  "privacy": { "accept_unknown_sources": false, "invite": "8f3a-c21b" }
}
```

* `from` = 源节点集合（* 表示任意），`to` = 目标节点集合。
* 只有匹配的 `(source, sink)` 组合才会建立发送会话 —— 这就是“**只在我们指定的音响
  设备播放**”：别人不在 `to` 列表里，物理上收不到你的音频。
* `privacy.accept_unknown_sources=false` 时，未握手/未邀请的源一律丢弃（公司场景防打扰）。

## 7. 平台矩阵（采集 / 播放）

| 平台 | 系统声音采集 | 单进程采集 | 播放 | 实现 |
| --- | --- | --- | --- | --- |
| Windows 10/11 | ✅ WASAPI Loopback | ✅ (Process Loopback, 19041+) | ✅ WASAPI Shared | 本仓库 `wavelink/audio/backends/wasapi.py`（纯 ctypes，零依赖） |
| Windows (备用) | ✅ 虚拟声卡 (VB-CABLE) | ✅ | ✅ | 同上 + `--capture device:` |
| Linux | ✅ PulseAudio `*.monitor` | ✅ `pactl` 模块/pipewire | ✅ | `wavelink/audio/backends/pipewire.py`（`pactl`/`pw-record` 子进程） |
| macOS | ⚠️ 需 BlackHole/Loopback 虚拟声卡（系统限制） | ✅ ScreenCaptureKit(13+) | ✅ CoreAudio | 计划中，先走虚拟声卡方案 |
| Android | ⚠️ 需 MediaProjection 授权（系统限制） | ✅ per-app(10+) | ✅ AudioTrack | 见 `docs/PLATFORM-NOTES.md` 的 Android 端计划 |
| 无音频设备（测试） | ✅ WAV 文件 / 合成音 | – | ✅ WAV 文件 / null | `wavelink/audio/backends/file.py` |

**关键平台事实**（避免踩坑）：
* Windows 采集系统声音**不需要**装虚拟声卡，WASAPI Loopback 直接抓默认渲染端点。
* 但要把声音「喂给」别的程序（比如把它当成麦克风），Windows 上必须有一个虚拟
  输入设备（VB-CABLE 之类），这是系统层面的限制，任何 App 都一样。
* Android/iOS 出于隐私和 DRM，系统级录音必须由用户显式授权（MediaProjection /
  Broadcast Upload Extension），无法静默后台采集；因此手机端要么每次授权，
  要么用 `AudioPlaybackCapture` 的 per-app 白名单。

## 8. 时延预算（办公室/家庭局域网）

| 环节 | Windows WASAPI 共享模式 |
| --- | --- |
| 采集缓冲 | 10–20 ms |
| 编码 | 0 ms (PCM) / 20 ms (Opus 帧) |
| 网络（Wi-Fi 5/6） | 2–10 ms |
| 抖动缓冲 | 20–40 ms |
| 播放缓冲 | 10–20 ms |
| **合计** | **约 45–90 ms**（PCM 路线；Opus 约 +20 ms） |

< 100 ms 足够“提示音不会错过、通话自然”，做不到音乐级同步（那需要 < 10 ms 的专有方案）。

## 9. 目录结构

```
wavelink/
  wavelink/
    proto.py          报文编解码
    rtpaudio.py       发送/接收会话、抖动缓冲、时钟同步
    net/discovery.py  信标发现 + 手动 peer
    net/control.py    控制面消息（ROUTE/STATS）
    audio/base.py     后端接口：list_devices/open_source/open_sink
    audio/format.py   采样格式、重采样、混音
    audio/backends/   wasapi.py / pipewire.py / file.py / null.py
    engine.py         节点引擎：把配置变成线程与流
    config.py         ~/.wavelink/config.json
    webui.py          控制台 HTTP 服务 + 单页 UI
    cli.py            audiomixing devices|serve|send|play|route|doctor
  tools/              真机验证脚本
  tests/              单元测试 + 端到端测试
```
