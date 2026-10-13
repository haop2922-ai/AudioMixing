# 协议字段：`samples` ＝ 本包帧数（不是累计帧偏移）

2026-10-14，用户真机诊断报告抓到一个**跨端字段语义错误**，是"手机完全听不到电脑的声音"的根因。
这份文档把它钉死，避免下一个人再踩。

---

## 一、字段定义（28 字节包头，偏移 8，uint32）

```
偏移  长度  字段          含义
 0     2    magic        0x574C ("WL")
 2     1    version      1
 3     1    ptype        1=AUDIO 4=PING 5=PONG …
 4     1    flags
 5     1    codec        1=pcm_f32 2=pcm_s16 3=opus
 6     2    seq          包序号（uint16 回绕）
 8     4    ssrc         流标识
12★    4    samples      ★ 本包帧数（每帧 2 声道）★  ← 就是这里
16     8    ts_ns        发送端时钟（纳秒）
24     2    gain_q14     增益（×10000）
26     2    payload_len  负载字节数
```

- `samples` **只描述这一包**：PCM16 立体声、一包 10ms 就是 `480`。
- 时间轴信息由 `ts_ns` 承担；**累计帧偏移没有任何接收方需要**。
- ★ 包头布局、字段顺序、`VERSION` 都是跨语言对拍过的，**不许改** ★

## 二、错误是怎么发生的、为什么一直没暴露

```python
# audiomixing/net/rtpaudio.py（修复前）
samples=frame_offset & 0xFFFFFFFF,     # frame_offset = 480 × seq，一直在涨 ✗
```

| 端 | 怎么用 samples | 后果 |
|---|---|---|
| 电脑端接收（`rtpaudio.Receiver`） | **完全不读**它（按解码后的负载长度算帧数） | 电脑↔电脑**没有任何症状** ✗ |
| 手机端接收（`AudioRecv`） | 当**准入判据**：`samples > 每包帧数` 即判坏包 | 电脑发出的**每一个**音频包都被拒收 ✗ |

用户报告里的现场数字：`seq=10260`、`samples=4924800`（= 480 × 10260）、判成"帧长不是 480"共 10266 个。
也就是说：**电脑一直在正常发，手机一个包都没收下**。

## 三、修复与证据

```python
# audiomixing/net/rtpaudio.py（现在）
frames_here = len(chunk) // CHANNELS      # 本包帧数（完整包 480；收尾短包按实际）
samples=frames_here & 0xFFFFFFFF,
```

包头前 16 字节（同一台机器上抓的）：

```
修复前：4c 57 01 01 00 02 14 28 57 9f c8 54 80 25 4b 00   ← seq=10260, samples=0x004b2580（累计）
修复后：4c 57 01 01 00 02 00 00 9a d2 b1 0b e0 01 00 00   ← seq=0,     samples=0x000001e0 = 480 ✓
        4c 57 01 01 00 02 01 00 9a d2 b1 0b e0 01 00 00   ← seq=1,     samples=480 ✓
        4c 57 01 01 00 02 02 00 9a d2 b1 0b e0 01 00 00   ← seq=2,     samples=480 ✓
```

## 四、两边同时改、互相兼容

手机端这一轮把准入判据改成**以负载长度为唯一主判据**（`samples` 只做参考），
所以即使遇到还没升级的电脑端（发累计值）也能放声音；电脑端改好之后，
新老手机端都能正常收。**两边各自独立成立**。

## 五、回归测试（谁改回去就会红）

```
tests/test_audiomixing.py::TestAudioPacketSamplesField
  · test_the_header_layout_is_untouched           28 字节布局 / 字段顺序 / 版本号不许变
  · test_every_packet_carries_its_own_frame_count 连发多包：每包 samples == 本包帧数，且不随包号变大
  · test_a_short_tail_packet_reports_its_own_frames 收尾短包按自己的帧数填（300 帧就是 300）
  · test_the_python_receiver_never_reads_samples  AST 级护栏：接收端不许开始依赖 samples
```

手动复现（看包头）：

```powershell
python -c "import socket,sys; sys.path.insert(0,'.'); from audiomixing import proto; from audiomixing.net.rtpaudio import SenderSession; ..."
```

或者直接跑测试：

```powershell
python -m unittest tests.test_audiomixing.TestAudioPacketSamplesField -v
```
