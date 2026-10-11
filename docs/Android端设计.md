# Android 端设计（手机独立节点）

> **铁律：手机之间不经过电脑。** 电脑只是局域网里的另一个可选节点，有它没它都要能用。
> 所以 Android 端直接讲我们原生的 UDP 协议，不借电脑转发任何东西。

## 一、为什么必须做 APK（而不是网页）

```
✗ 浏览器不允许网页发 UDP 广播 → 网页版做不到"自主发现"
✗ 浏览器不允许网页监听 UDP  → 网页版也做不到"手机直连"
✗ 网页版想做直连只能借电脑转发协商信息 → 违反"手机必须独立"这条铁律
✓ 原生 App 两样都能做：直接发广播、直接收 UDP 音频
```

## 二、发现协议（复用电脑端，逐字节一致）

组播地址 `239.255.77.120`，端口 **47830**；同时往子网广播地址发一份（有些 Wi-Fi 挡组播）。

每 **2 秒**发一次自己的信标；**15 秒**收不到某台的信标就从列表里移除。

信标是 JSON 文本，**不超过 1200 字节**：

```json
{"wl":1,"id":"<uuid>","name":"小红的手机","platform":"Android-14",
 "version":"1.0.0","audio_port":47831,"web_port":0,
 "sources":[{"id":"mic","name":"麦克风","detail":"mic"}],
 "sinks":[{"id":"speaker","name":"本机扬声器","detail":"render"}],
 "busy":false}
```

- `wl` 协议版本（固定 1）、`id` 节点 UUID、`name` 给人看的名字
- `audio_port` 音频端口（手机固定 47831，被占则换一个并在信标里如实写）
- `web_port` 手机没有网页面板，写 0
- `sources` / `sinks` 本机可提供的音源与播放端（手机只要 mic / speaker 两项即可）

**收到信标就更新列表，不需要任何握手、不需要配对、不需要电脑。**

## 三、音频协议（同一个 28 字节包头，小端）

```
偏移  类型      字段          说明
 0    uint16   magic         0x574C（'W','L'）
 2    uint8    version       1
 3    uint8    ptype         1=AUDIO 4=PING 5=PONG 6=BYE
 4    uint8    flags         0x01 不连续 / 0x02 最后一块 / 0x04 静音
 5    uint8    codec         2=PCM16（手机用这个）3=Opus（电脑之间用）
 6    uint16   seq           包序号，每包 +1（uint16 回绕）
 8    uint32   ssrc          本流的随机标识（一条发送会话一个）
12    uint32   samples       本包有多少帧（每帧 2 声道）
16    uint64   ts_ns         发送时刻的墙钟纳秒
24    int16    gain_q14      音量，Q14 定点（1.0 == 16384）
26    uint16   payload_len   负载字节数
= 28 字节头 + 负载
```

**负载**：`codec=2`（PCM16）时是**交错立体声 16 位小端**，48000 Hz。
手机端用 PCM16 就够了（Wi-Fi 上 1536 KB/s，几台设备完全吃得下），
**这样不需要 NDK、不需要 libopus**，把 Android 端的复杂度压到最低。

**发送**：每 10 毫秒一包 = 480 帧 = 1920 字节负载。
**接收**：按 `ssrc` 分流到各自的抖动缓冲，再喂给 `AudioTrack`。

**PING/PONG**：接收方定期发 `ptype=4`，发送方回 `ptype=5`，用来算两台手机的时钟偏差
（`offset = 对端时钟 - 本机时钟`），播放时刻 = `包时间戳 - offset + 抖动目标`。

## 四、要实现的模块（Kotlin）

| 文件 | 职责 | 关键点 |
|---|---|---|
| `Proto.kt` | 28 字节包头编解码 | 全小端；`ByteBuffer.order(LITTLE_ENDIAN)`；单元可测 |
| `Discovery.kt` | 发/收信标，维护设备列表 | `MulticastSocket` 加入 `239.255.77.120`；同时发子网广播；要有 `WifiManager.MulticastLock`（不加锁安卓会丢组播收发） |
| `AudioSender.kt` | 采集 → 打包 → UDP 发 | `AudioRecord`，48000/立体声/PCM16，10ms 一块；麦克风用 `MediaRecorder.AudioSource.VOICE_COMMUNICATION` |
| `AudioReceiver.kt` | UDP 收 → 抖动缓冲 → 播放 | `AudioTrack` 流模式；缓冲 100~200ms；丢包就用静音补 |
| `Engine.kt` | 串起来 + 状态 | 一个前台服务持有它，退出 App 不断流 |
| `MainActivity.kt` | 界面 | 设备列表（自动出现，不用输号）+ 每台一个「收听 / 送声音」开关 + 电平条 + 状态 |
| `Net.kt` | 列本机 IP / 广播地址 | 和电脑端 `netcheck` 同一套判定（私有地址优先，避开 VPN 叠加网段） |

## 五、界面（对齐已有的手机网页版视觉）

```
顶部：Audio Mixing + 本机名 + 状态胶囊
列表：局域网里的设备（自动出现）
       每行：名字 · 平台 · [收听] [送声音] 两个开关 + 实时电平
底部：本机音量 / 灵敏度滑杆
      折叠「详情」：本机 IP / 端口 / 已连接几台 / 丢包统计
常驻通知：「Audio Mixing 正在运行 · 2 台设备已连接」（点一下回到界面）
```

**要点**：设备一出现就能点，**没有配对、没有房间号、没有二维码**。

## 六、验收标准

```
□ Proto.kt 的编解码与 Python 端逐字节一致（用同一组样例数据对齐测试）
□ 发现：两部手机在同一 Wi-Fi 下，打开 App 就能看到对方（不用电脑）
□ 音频：手机 A 点「送声音」，手机 B 点「收听 A」，B 出声
□ 关掉电脑：以上两条照常工作
□ 断网/切后台 30 秒再回来，能自动恢复
□ 通知常驻，一眼能看出"正在传输"
```

## 七、构建

```
SDK: E:\Android\sdk（命令行工具 + platform-tools + platforms;android-34 + build-tools;34.0.0）
JDK: 17（本机已有 C:\Program Files\Java\jdk-17）
Gradle: E:\Android\gradle\gradle-8.7
产出: android/app/build/outputs/apk/debug/app-debug.apk（debug 签名，装上即用）
```

工程放在仓库的 `android/` 目录；`E:\Android\sdk` 不进仓库。

### 实际可用的构建命令（2026-10-12 验证过）

```powershell
cd "E:\AI\deep blue\AudioMixing\android"
$env:JAVA_HOME = "C:\Program Files\Java\jdk-17"      # 必须是 JDK 而不是 JRE，gradle 要 javac
$env:ANDROID_HOME = "E:\Android\sdk"
$env:ANDROID_SDK_ROOT = "E:\Android\sdk"
E:\Android\gradle\gradle-8.7\bin\gradle.bat --no-daemon --console=plain assembleDebug
```

### 踩过的坑（别重复踩）

```
✗ appcompat 会拖进旧版 kotlin-stdlib，和 AGP 自带的撞出十几个 Duplicate class
  → 所以这个工程【零第三方依赖】，主题用 @android:style/Theme.Material.NoActionBar，UI 全用代码写
✗ JAVA_HOME 指到 C:\Program Files\Common Files\Oracle\Java（那是 javapath 启动器）会报 invalid directory
  → 要用 C:\Program Files\Java\jdk-17
✗ PowerShell 会把 -Dfile.encoding=UTF-8 这种带点带等号的参数拆开
  → 必须加引号："-Dfile.encoding=UTF-8"
```

### 协议一致性怎么验（最重要的一步）

手机和电脑各写一遍二进制格式，差一个字节就永远连不上。所以不靠读代码，靠对字节：

```bash
# 1. Java 端自检（纯 JVM，不需要安卓）
cd android
"C:\Program Files\Java\jdk-17\bin\javac.exe" -encoding UTF-8 -d out `
  app\src\main\java\com\audiomixing\app\Proto.java `
  app\src\test\java\com\audiomixing\app\ProtoTest.java
"C:\Program Files\Java\jdk-17\bin\java.exe" -cp out com.audiomixing.app.ProtoTest

# 2. Python 端编同样的样例
python tools/check_android_proto.py

# 3. 两边的 hex 必须逐字符相同
#    2026-10-12 实测：5 个样例（156/28/30/35/28 字节）整包逐字节一致 ✓
python tools/check_android_proto.py <把Java那边的hex粘进来>   # 也可以反向解一段 hex
```

---

HWHHYN 用 DEEPSEEK 制作
