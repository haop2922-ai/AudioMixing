# Audio Mixing —— BUG 修复明细（交付必读 / 复刻对照）

> **这份文件是给"基于旧版本复刻的另一个项目"看的。**
> 它不属于 `C:\Users\75341\Desktop\31`，也不要去改那个目录 —— 只读这里。
>
> **硬性规则：每次更新（电脑端 / 手机端，哪怕只改一行）都必须在本文末尾追加一条记录。**
> 一条记录必须同时写清五件事，缺任何一项都算没写完：
>
> 1. **位置**：文件 + 类/函数 + 行号（行号会漂，函数名必须写全）
> 2. **现象**：用户实际看到/听到什么
> 3. **原因**：代码级原因，指出哪一行、哪个判断、哪次调用导致
> 4. **修复**：改成什么（关键判断要贴 3~10 行代码）
> 5. **回归**：哪个测试文件、哪条测试名钉住了它（没有测试就写"缺，待补"）
>
> 复刻时**照抄判断逻辑，不要照抄版本号**。版本号只是定位用的。

---

## 索引

| 编号 | 版本 / 日期 | 一句话 |
|---|---|---|
| BUG-20261101-01 | 手机 2.3 / 2026-11-01 | 「我送它的」滑杆只要不是 1.00 就完全没声音：增益把 UDP 包头一起乘坏了 |
| BUG-20261101-02 | 手机 2.3 + PC v2.13 / 2026-11-01 | 两端音量条不同步：手机统计缺 node id，电脑又只给"正在发送"的对象回统计 |
| BUG-20261101-03 | 手机 2.3 / 2026-11-01 | 手机端未激活的音量条仍可拖：从未按"有没有链路"禁用 |
| BUG-20261101-04 | 手机 2.3 + PC v2.13 / 2026-11-01 | 智连（电脑→手机）不生效：请求被发送方自己的开关挡掉 |
| BUG-20261101-05 | 手机 2.3 + PC v2.13 / 2026-11-01 | 智连（手机→电脑）不生效：手机流没有 node id，电脑认不出人 |
| BUG-20261101-06 | 手机 2.3 / 2026-11-01 | 关闭 App（甚至卸载）后系统仍显示「屏幕共享中」：MediaProjection 一直没 stop |
| BUG-20261101-07 | PC 2.14 + 手机 2.4 / 2026-11-01 | 手机没有端口设置；电脑没有端口冲突检测；同类软件抢同一组端口导致两个一起死 |
| BUG-20261101-08 | PC 2.15 + 手机 2.5 / 2026-11-01 | 对端音量条要 3–5 秒才同步；端口设置仍要填三个，用户要求只统一发现端口 |
| BUG-20261101-09 | PC 2.15 + 手机 2.5 / 2026-11-01 | 电脑调音量手机完全不同步；手机调音量电脑仍要 5 秒；两端默认发现端口改为 17830 |
| BUG-20261101-10 | PC 2.15 + 手机 2.5 / 2026-11-01 | 电脑改端口后要手动重启、看起来像"不能修改"；要求保存后自动重启 |
| BUG-20261101-11 | PC 2.15 + 手机 2.5 / 2026-11-01 | 电脑调音量手机端仍不同步（电脑自己还会弹回旧值）；电脑「智连」不是真实反馈、功能不完整 |
| BUG-20261101-12 | PC 2.16 + 手机 2.6 / 2026-11-02 | 手机没在收听时「信号」永远写「还没收到它的心跳」，即使对方就在线 —— 反馈不真实 |
| BUG-20261102-01 | 开发工具 / 2026-11-02 | 联调用的测试路由（`sync-test`）和测试临时目录留在了用户配置与 `%TEMP%` 里 |
| BUG-20261102-02 | PC 2.17 + 手机 2.7 / 2026-11-02 | 智连自动建的收听路由被当成"别人的路由"跳过 → PC「我听它的」1.60 / 手机「我送它的」0.40 永远不同步 |
| BUG-20261102-03 | 手机 2.7 / 2026-11-02 | 运行中被卸载后系统仍显示「屏幕共享中」：卸载回调来不及执行 |
| BUG-20261102-04 | 手机 2.7 / 2026-11-02 | 手机系统音量键不再改变听到的声音（音量键没指向媒体流） |
| BUG-20261102-05 | PC 2.17 + 手机 2.7 / 2026-11-02 | 同一条共享增益发送端和接收端各乘一次：0.40 实际只有 0.16，滑杆像没效果 |
| BUG-20261102-06 | PC 2.17 + 手机 2.7 / 2026-11-02 | 双端自查修掉的 6 个缺陷（广播误触发自动收听/重挂流串台/音频端口不换口/接收增益不恢复/控制消息无上限/spec 硬编码） |
| BUG-20261102-07 | 手机 2.8 / 2026-11-02 | 2.7 的卸载监视不够硬：真机卸载后仍显示「屏幕共享中」；改为 1 秒同时查包名+APK 文件，并加引擎第二道保险 |
| BUG-20261102-08 | PC 2.19 / 2026-11-02 | 电脑在手机/别的电脑上显示成「本机」：老配置把默认名存成了「本机」 |
| BUG-20261102-09 | 手机 2.9 / 2026-11-02 | 手机音量面板分「手机音量 / App 音量」，本应用不出现在 App 音量里 |
| BUG-20261102-10 | 测试工具 / 2026-11-02 | 单元测试会去请求 127.0.0.1:17800 拉真实路由，用户开着 App 时测试假失败 |
| BUG-20261103-01 | PC 2.20 / 2026-11-03 | 幽灵设备（历史 uuid / 旧手机名）一直留在设备图：发现表只增不减、假 node_id 写进用户配置 |
| BUG-20261103-02 | PC 2.20 / 2026-11-03 | 监控数据自相矛盾：接收速率永远 0 KB/s、状态写死"正在接收"、目标 25ms 但实际缓冲 60ms、"送"条与文字不一致 |
| BUG-20261103-03 | 手机 2.10 + PC 2.20 / 2026-11-03 | 手机媒体/App 音量不影响电脑耳机：手机只显示音量、从不把音量报给电脑 |
| BUG-20261103-04 | 工具 / 2026-11-03 | 审计工具假 PASS：测试身份写在配置里却报"无残留" |
| BUG-20261103-05 | PC 2.20 / 2026-11-03 | 面板「本机」显示 Hyper-V 虚拟网卡 172.26.192.1 |
| BUG-20261103-06 | PC 2.20 / 2026-11-03 | 假身份迁移前没有备份，数据不可回退 |
| BUG-20261103-07 | PC 2.20 / 2026-11-03 | 「迟到」被读成丢音；自适应不看迟到/溢出 |
| BUG-20261103-08 | 工具 / 2026-11-03 | 交付 APK 仍是 2.9，`check_apk` 只查 debug 构建 |
| BUG-20261104-01 | PC 2.20 / 2026-11-04 | 晚到包 597844/597850 ≈ 100% 是时钟键错位（node_id vs IP）+ 目标缓冲被写成「最低」 |
| BUG-20261104-02 | PC 2.20 / 2026-11-04 | 手机音量仍无效：诊断包显示手机跑的是 2.9，`vol` 根本没到电脑 |
| BUG-20261104-03 | PC 2.20 / 2026-11-04 | 走「广播→只听」时手机音量永远无效：通配路由 `from=["*"]` 查不到来源，vol 恒按 1.0 |
| BUG-20261104-04 | PC 2.20 / 2026-11-04 | 未接受的点对点单播能从广播「只听」听到：接收端没有准入，通配路由兜底接住一切 |
| BUG-20261104-05 | PC 2.21 / 2026-11-04 | 审计 A1：认不出身份的发送方在「只有一个播放端」时仍被兜底收下（准入的第二条漏路） |
| BUG-20261104-06 | PC 2.21 / 2026-11-04 | 审计 A2：时钟 offset 只按「历史最小 RTT」更新，对端校时后永久偏移 |
| BUG-20261104-07 | PC 2.21 / 2026-11-04 | 审计 A3：乱序旧包污染丢包计数，重复包被重复写进抖动缓冲 |
| BUG-20261104-08 | PC 2.21 / 2026-11-04 | 审计 A4：一条具名路由写两台来源时，只用第一台的手机音量 |
| BUG-20261104-09 | PC 2.21 / 2026-11-04 | 审计 A9：路由 gain 原样接收 JSON 的 NaN/Infinity，整条输出被污染成 NaN |
| BUG-20261104-10 | PC 2.21 / 2026-11-04 | 审计 B1（第 2 轮）：手动对端的 `manual:IP` 占位身份盖住真实 id，这条流整场挂不上路由（无声） |
| BUG-20261104-11 | PC 2.21 / 2026-11-04 | **回退修复 Q1**：广播+收听被 A1 准入闸门误杀（把残留 `want_listen` 当成点对点） |
| BUG-20261104-12 | PC 2.21 / 2026-11-04 | Q2：手机采集点在音量之后，PC 再乘 `vol` → 手机音量被平方 |
| BUG-20261104-13 | 手机 2.11 / 2026-11-04 | Q3：「送声音」三档 + 显式上报 `send_mode`/`broadcast`（第 2 档静默未实现，见记录） |
| BUG-20261104-14 | PC 2.22 + 手机 2.12 / 2026-11-04 | **智连 = 自动接听**：接收方开智连即自动接、多台全接、静音不挂/断流才挂、手动关跟随、广播时不可用；手机开始处理电脑的 `listen_req` 并回报 ack |
| BUG-20261104-15 | PC 2.22 + 手机 2.12 / 2026-11-04 | **设备身份合并 + 防自环（第一阶段）**：路由主键改 `deviceId`、旧 id 降级 alias、迁移前备份；音频帧 `originId`/`hop` 丢自回声与超跳帧 |
| 历史 v2.2 → v2.12 | 2026-10-21 → 2026-10-30 | 12 个版本的历史修复速查（**v2.10 / v2.11 / v2.12 是基于 v2.9 的项目必须补齐的**） |

---

## BUG-20261101-01 手机发送音量只有 1.00 有声音

**现象**
手机向电脑「送声音」时，调「我送它的」音量滑杆：
- 1.00：声音正常；
- 0.50 / 0.80 / 1.50 等任何其他值：电脑端**完全没声音**。

**位置**
- `android/app/src/main/java/com/audiomixing/app/SendFanout.java`
  - `SendFanout.send(byte[] pkt, int len, float peak)`（整包分发）
  - `SendFanout.scalePcm16(byte[] src, byte[] dst, int len, float g)`（缩放实现，约 L231-L246）

**原因（代码级）**
1. `AudioSend` 里编码后的 `raw` 是**整包**：`28 字节协议头 + 1920 字节 PCM`，
   调用是 `fanout.send(raw, raw.length, p)`。
2. `SendFanout.send` 在 `g != 1f` 时调用 `scalePcm16(pkt, data, len, g)`。
3. 旧实现从 `i = 0` 开始逐字节对，**把包头也当成 PCM 乘了**：
   ```java
   // 错误版本（不要照抄）
   for (int i = 0; i + 1 < len; i += 2) {
       int v = (short) ((src[i] & 0xFF) | ((src[i + 1] & 0xFF) << 8));
       int out = Math.round(v * g);
       ...
   }
   ```
4. 包头第一个字段是魔数 `0x574C`。乘 0.50 后变成 `0x2BA6`，电脑端
   `proto.decode()` 判 `magic != MAGIC_U16`，直接返回 `None`，**整包丢弃**。
5. `g == 1f` 时走的是"不缩放、原包直发"的快路径，所以只有 1.00 没坏。

**修复**
包头原样拷贝，只缩放它后面的负载（`Proto.HEADER_SIZE == 28`）：
```java
private static void scalePcm16(byte[] src, byte[] dst, int len, float g) {
    int header = Math.min(len, Proto.HEADER_SIZE);
    if (header > 0) {
        System.arraycopy(src, 0, dst, 0, header);   // 包头一个字节都不许动
    }
    for (int i = header; i + 1 < len; i += 2) {     // 只乘 PCM 负载
        int v = (short) ((src[i] & 0xFF) | ((src[i + 1] & 0xFF) << 8));
        int out = Math.round(v * g);
        if (out > 32767) out = 32767;
        else if (out < -32768) out = -32768;
        dst[i] = (byte) out;
        dst[i + 1] = (byte) (out >> 8);
    }
}
```

**回归**
- `android/tools/run_fanout_test.sh`
  - 第 8 节：`A 的整包仍能被协议解析（包头没被增益改坏）`
  - 第 9 节：放大 1.5 倍后包头同样没被改坏
  - 合计 36 项断言（PASS）
- 旧探针的坑：`FanoutProbe.flatPcmPacket()` 原来把 28 字节包头**留空**，
  而 `0 × 任何增益 = 0`，所以旧测试根本发现不了这个 bug。
  现已改成用 `Proto.Pkt` + `Proto.encode()` 造**真实包头**。
  **复刻时不要再用"全 0 包头"的测试包。**

**兼容性**
只改手机端发包字节，协议格式不变；电脑端不需要任何改动即可恢复正常缩放。

---

## BUG-20261101-02 两端音量条不同步（电脑 1.00 / 手机 0.59）

**现象**
- 电脑设备卡片：`我听它的 = 1.00`，`我送它的 = —（不可调）`
- 手机同一台设备卡片：`我听它的 = 0.27`，`我送它的 = 0.59`
- 两端都不是同一个值，而且手机那两条还在可拖状态。

**位置**
1. `android/app/src/main/java/com/audiomixing/app/Diagnosis.java`
   - `Diagnosis.json(...)`（完整重载，约 L215-L224）
2. `audiomixing/engine.py`
   - `AudioMixingNode._handle_peer_requests()`（约 L2319-L2328，读 `msg.get("node")`）
   - `AudioMixingNode._node_id_for_host()`（约 L2390-L2403，按 IP 反查）
   - `AudioMixingNode._send_peer_stats()`（约 L2195-L2219，只遍历发送会话）
3. `audiomixing/engine.py`
   - `AudioMixingNode._link_gains()`（约 L2079-L2099，按 node_id 找两条路由）

**原因（代码级）**
这一条有**两个**独立成因，少修一个都还会不同步：

1. **手机统计包没有 `node` 字段。**
   电脑端 `_handle_peer_requests` 优先读 `msg["node"]`；没有就退回
   `_node_id_for_host(host)`，也就是"拿统计包的来源 IP 去发现表里反查"。
   只要手机的信标地址和统计包来源地址不是同一个（换网卡、走 VPN、Wi-Fi 漫游），
   反查就落空 → `node_id` 为空 → 下面的 `gain` / `gain_rx` 全部跳过，两端音量永远对不上。
2. **电脑端只给"我正在发送"的对象回统计。**
   `_send_peer_stats()` 只遍历 `self.sender.sessions`。当电脑**单纯在听手机**（没有发送会话）时，
   它一个统计包都不发，手机永远收不到电脑的 `gain_rx`，手机的「我送它的」只能停在本地值。

**修复**
1. 手机统计包补上 `"node"`（旧电脑 v2.12 本来就读这个键，所以手机单独升级也能生效）：
   ```java
   // Diagnosis.java：新增带 nodeId 的重载
   sb.append("{\"t\":\"").append(TAG_STATS).append("\",\"v\":1");
   if (nodeId != null && nodeId.length() > 0) {
       sb.append(",\"node\":\"").append(escape(nodeId)).append('"');
   }
   ```
   ```java
   // Engine.sendStatsRound：把 discovery.nodeId() 传进去
   String myNodeId = (discovery != null) ? discovery.nodeId() : "";
   byte[] payload = Diagnosis.json(myNodeId, nodeName(), ...);
   ```
2. 电脑端把"我正在收听的对象"也加入统计目标，`sending=false`、`want_listen=false`，
   但 `gain_rx` 照常带出去：
   ```python
   # engine.py：_send_peer_stats()
   for key, info in self._listening_peer_targets().items():
       targets.setdefault(key, info)
   ```
   新增 `_listening_peer_targets()`：遍历 `local_routes()` 里"`它 → 本机`"的路由，
   用 `registry.get/resolve` 找到对端，返回 `{node_id, host, audio_port, sending=False, ...}`。
   频道收听（`from: ["*"]`）没有具体设备，跳过。

**回归**
- `tests/test_audiomixing.py`
  - `TestChannelSourceSafety.test_stats_go_to_a_peer_we_only_listen_to`
    （电脑单纯收听时也要给对方回 `gain_rx`，且 `sending` 必须是 false）
- 既有共享音量测试：
  - `test_shared_gain_from_peer_is_applied...`（`gain=0.5` 落到路由）
  - `test_link_gains_reports_nothing_without_a_route...`（没有路由不许凭空报 1.0）
- 全量：`python -m unittest discover -s tests` → 361 项通过

**兼容性 / 生效条件**
- 手机侧的 `"node"` 改动对旧电脑立即生效。
- 电脑侧的"给收听对象回统计"已随 PC v2.13 发布（本次 EXE 构建 2026-11-01 02:22）。

---

## BUG-20261101-03 手机端未激活的音量条仍可拖

**现象**
电脑端设备卡片上：没有对应路由时滑杆是**灰色、写「—」、不可拖**。
手机端同一张卡片：两条滑杆（我听它的 / 我送它的）**永远是亮色、永远能拖**，
即使根本没有那条链路。

**位置**
- `android/app/src/main/java/com/audiomixing/app/MainActivity.java`
  - `PeerRow.sync()`（约 L1902-L1925，只同步进度，从未设置 enabled）
  - `gainRow(...)`（约 L1483-L1534，创建 SeekBar 时默认就是 enabled）
  - 新增 `styleGainBar(...)`（约 L1536-L1556）

**原因（代码级）**
`PeerRow.sync()` 每 150ms 刷新时只做两件事：
```java
if (recvGainBar != null && !recvGainBar.isPressed()) {
    recvGainBar.setProgress(Math.round(engine.peerRecvGain(host) * 100));
}
```
没有 `setEnabled(false)`、没有 alpha、没有"这条链路存不存在"的判断。
所以即使 `engine.isListening(peer)` / `engine.isSending(peer)` 都是 false，
滑杆照样可拖，拖了也只是改一个**当前没有任何对象**的增益。

**修复**
照抄电脑端的判据（`ui/index.html` 的 `gainTargetFor`："没有路由/目标 → target=null → 滑杆 disabled"）：
```java
// PeerRow.sync()
styleGainBar(recvGainBar, listen, recvGain);            // 只有真的"它在→我听"才可调
styleGainBar(sendGainBar, getsMySound, sendGain);       // getsMySound = send || broadcasting
```
```java
private void styleGainBar(SeekBar bar, boolean active, float value) {
    if (bar == null) return;
    bar.setEnabled(active);
    bar.setAlpha(active ? 1f : 0.35f);
    Object tag = bar.getTag();
    if (tag instanceof TextView) {
        TextView shown = (TextView) tag;
        shown.setAlpha(active ? 1f : 0.35f);
        setTextIfChanged(shown, active
                ? String.format(Locale.US, "%.2f", value) : "—");
    }
}
```
判据说明：
- `我听它的`：只在 `engine.isListening(peer)` 为真时启用；
- `我送它的`：只在 `engine.isSending(peer)` 或正在广播（`broadcasting`）时启用；
- 禁用时**不改引擎里的值**，链路恢复后下一帧自动恢复正常显示。

**回归**
- 手机 UI 无单元测试（缺，待补）。
- 本次用 `gradle assembleDebug` 编译通过 + 真机界面待确认。
- 复刻时至少要保证：`setEnabled(false)`、alpha 0.35、数值写 "—" 三样同时做到。

---

## BUG-20261101-04 智连不生效（电脑 → 手机）

**现象**
用户原话：「PC 端正在向手机发送声音，但是手机不会自动打开收听」。
手机那张卡片的「智连」已经点亮，但手机端「收听」始终不亮、也听不到。

**位置**
1. 电脑端发请求的门槛：
   `audiomixing/engine.py` → `_send_peer_stats()` 约 L2278-L2283
   （`"want_listen": ... and self._smart_wants_listen(peer_id)`）
2. 手机端等请求：
   `android/.../Engine.java` → `handleWantListen()` 约 L1664-L1685
   （只认 `ps.wantListen`）

**原因（代码级）**
- 电脑只有在**自己这张卡片的「智连」开着**时，才把 `want_listen=true` 发出去
  （`_smart_wants_listen` 读的是本机 `config.smart_link`）。
- 手机端又只认 `want_listen`。
- 于是"手机开了智连、电脑没开"这个组合里，请求根本不会发出，手机永远等不到。
  反过来，旧版本电脑还可能因为"无条件发请求"把旧手机自动打开（历史上修过一次），
  所以这里不能简单改回"无条件发"。

**修复（两条，互相兼容）**
1. **手机端不再等对方开口**：只要自己的「智连」开着，对方统计里 `sending=true`
   （说明它确实在往我这儿发，点对点/广播都算），就自动收听；对方停了自动收回：
   ```java
   boolean want = ps.wantListen || (smart && ps.sending);
   int action = LinkPolicy.listenAction(smart, already, want, autoAdded);
   ```
2. **电脑端按能力位发请求**：对端信标里有 `smart:1` 就无条件发 `want_listen`，
   由对方自己决定收不收；旧对端（没有这个键）继续用"本机也开智连才发"的保守口径：
   ```python
   "want_listen": bool(info.get("want_listen")) and (
       self._peer_supports_smart(peer_id) or self._smart_wants_listen(peer_id)
   ),
   ```
   能力位来自：
   - 手机信标：`android/.../Discovery.java` `buildBeacon()` → `o.put("smart", 1)`
   - 电脑信标：`audiomixing/net/discovery.py` `build_beacon()` → `"smart": 1`
   - 电脑解析：`PeerEndpoints.smart` + `DiscoveryRegistry.update()` 里的 `int(payload.get("smart") or 0) == 1`

**语义说明（必须一起复刻）**
- 智连是**接收方自己的开关**，不是发送方的命令。
- 因此手机开了智连后，电脑用「广播」发声音也会自动开始收听。
  （旧规则"广播不触发自动收听"是按发送方视角定的；现在由接收方自己的智连决定，不再矛盾。）
- 手机端只自动开"听"，**绝不自动开"送"**（安全边界，不能丢）。

**回归**
- `tests/test_audiomixing.py` → `TestChannelSourceSafety.test_smart_capable_peer_decides_the_request_not_this_pc`
  - 对端 `smart:1` + 本机没开智连 → 仍必须发 `want_listen=true`
  - 对端 `smart:0`（旧版）+ 本机没开智连 → 不发
- 既有测试：`test_a_broadcast_session_never_asks_the_peer_to_listen`
  （广播那条**电脑端**仍然不报 `want_listen`；手机端是否自动收听由手机自己的智连决定）
- 全量：361 项通过

---

## BUG-20261101-05 智连不生效（手机 → 电脑）

**现象**
用户原话：「手机端反过来也是这样」——手机在送声音给电脑，电脑卡片上的「智连」开着，
但电脑不会自动打开收听。

**位置**
1. `android/.../Diagnosis.java` 统计包缺少 `node`（同 BUG-20261101-02 的第 1 条）
2. `audiomixing/engine.py` → `_queue_auto_listen()`（约 L2707-L2718）
3. `audiomixing/engine.py` → `_attach_stream_routes()`（约 L4616-L4626，流先到时调用它）

**原因（代码级）**
- 手机的音频流只发 `T_AUDIO`，**不发 HELLO**，所以电脑接收端 `StreamInfo.node_id`
  常常是空的。
- `_queue_auto_listen()` 第一行就是 `if not node_id: return`；
- 电脑端又只能靠统计包的 `node` 字段认人；手机没报 `node` 时退回按 IP 反查，
  反查失败就整条自动收听路断掉。
- 结果：电脑的「智连」明明开着，收到流也认不出是谁，什么都不做。

**修复**
1. 手机统计包补 `"node"`（同 BUG-20261101-02）。
2. `_queue_auto_listen()` 在 `node_id` 为空时按来源 IP 反查，名字为空时从注册表补：
   ```python
   if not node_id:
       node_id = self._node_id_for_host(host)
   if not node_id:
       return
   if not name:
       name = self._peer_name_of(node_id)
   ```
   （`_node_id_for_host` 用 `registry.resolve(host)`；`_peer_name_of` 用 `registry.get`）
3. `_attach_stream_routes()` 仍然照旧先查路由；查不到才走 `_queue_auto_listen`，
   由维护线程建 `auto-listen-*` 路由（接收线程不直接改路由表）。

**回归**
- 既有：`TestPeerLivenessAndAutoListen` 系列（自动收听的建/撤/黑名单拆除）
- 新增的 node 字段：`test_stats_go_to_a_peer_we_only_listen_to` 间接覆盖"带 node 的统计包"
- 全量：361 项通过

---

## BUG-20261101-06 关闭 App 后系统仍显示「屏幕共享中」

**现象**
用户截图：手机桌面上方常驻一条系统提示
「屏幕共享中 · “Audio Mixing”正在获取屏…」，右侧有「停止」。
- 退出/关闭 App 后不消失；
- 卸载 App 后仍然存在；
- 只有点系统那条「停止」或重启手机才消失。

**位置**
- `android/.../Engine.java`
  - 字段 `projection`（约 L107-L112，注释写着"刻意在拿到之后一直留着"）
  - `Engine.applySourceState()`（约 L1069-L1104，停止采集时不释放）
  - `Engine.releaseProjection()`（约 L1240-L1270，只有 `Engine.stop()` 会调）
  - `Engine.onProjectionGranted()`（约 L1135-L1180）
- `android/.../MixService.java`
  - `onStartCommand()` 返回 `START_STICKY`（约 L175 / L182）
  - 原先没有 `onTaskRemoved()`；`onDestroy()` 才 `engine.stop()`
- `android/.../MainActivity.java`
  - `onDestroy()`（约 L329-L343）原先注释写明"故意不 stop 引擎"
- `android/app/src/main/AndroidManifest.xml`
  - `<service android:name=".MixService" ...>` 原先没有 `android:stopWithTask`

**原因（代码级）**
1. `MediaProjection` 被**刻意长期持有**（为了少弹几次授权窗），只有 `Engine.stop()` 才会
   走到 `releaseProjection()` → `p.stop()`。
2. 界面关闭 ≠ 引擎停止：`MainActivity.onDestroy()` 故意不 stop；
   `MixService` 又是 `START_STICKY` 的前台服务，从最近任务划掉后系统默认会把它拉回来。
3. 结果：`projection` 对象一直活着，Android 14+ 的系统「屏幕共享中」提示就不会消失 ——
   即使当前根本没在采、没在送。
4. `AudioSend` 里还留着一份 `projection` 引用；`releaseProjection()` 原先只清 Engine 的字段，
   采集端可能继续拿着一个已经 stop 的对象。
5. 卸载旧版本时如果系统还没把上一次会话收干净，那条提示可能作为系统 UI 残留挂到重启为止；
   新版本在关闭/划掉时就会主动 `stop()`，从源头不再留下这个会话。

**修复**
1. 只要不再采「本机声音」，立刻释放授权：
   ```java
   // Engine.applySourceState()
   syncSenderTargets();
   if (!on || sourceKind != AudioSend.SRC_PLAYBACK) {
       releaseProjection();   // 手动停止 / 切到麦克风 → p.stop()
   }
   ```
2. `releaseProjection()` 同时清掉采集端手里的引用：
   ```java
   AudioSend s = sender;
   if (s != null) s.setProjection(null);
   ```
3. 授权弹窗期间用户已经停止采集 → 授权回来也不留：
   ```java
   // Engine.onProjectionGranted()
   if (!sourceEnabled) {
       p.stop();
       projectionError = "授权回来时采集已经停止，已立即关闭投屏授权";
       return;
   }
   ```
4. 从最近任务划掉 App 时，主动收尾（`MixService.onTaskRemoved`）：
   ```java
   if (engine != null) engine.stop();            // 内部会 stop 投屏
   stopForeground(STOP_FOREGROUND_REMOVE);
   removeNotification();
   stopSelf();
   ```
5. 清单声明 `android:stopWithTask="true"`，划掉任务直接停服务。
6. 用户按返回键关闭界面（`isFinishing()`）时停掉前台服务；
   按 Home / 切走不算关闭，后台传输照旧。
7. 通知栏本来就有「停止」按钮（`MixService.build()` 的 `addAction`），继续保留。

**回归**
- 手机端无自动化测试覆盖系统投屏会话（缺，待补）。
- 真机判据（必须逐条看）：
  1. 点「本机声音」开始送 → 系统出现「屏幕共享中」提示；
  2. 点停止「本机声音」→ 提示**立即消失**；
  3. 重新开始会再次弹系统授权（这是预期，不再长期攥着）；
  4. 从最近任务划掉 App → 提示消失、通知消失、再打开不会自动恢复投屏；
  5. 按 Home 切走 → 后台传输继续，提示保留（这是正常的后台采集）；
  6. 按返回键关闭 App → 提示消失、服务停止。

**兼容性 / 生效条件**
- Android 14+ 才会在状态栏显示「屏幕共享中」常驻提示；旧版本表现为通知/投屏图标。
- 必须重新安装手机端 APK 才生效。
- 已经被旧版本卡住的会话：点系统那条「停止」即可；若系统 UI 残留，重启一次手机。
- 电脑端不需要任何改动。

---

## BUG-20261101-07 手机没有端口设置 + 电脑没有端口冲突检测

**现象**
- 手机端只有音频端口的只读显示，没有输入框；电脑在「设置」里改过端口后，
  手机与电脑彻底失联。
- 另一个基于本项目做的软件用了同一组默认端口；两个软件同时运行后
  "两个一起死"。

**位置**
- 电脑端：
  - `audiomixing/net/discovery.py` → `BeaconService.start()`（UDP `SO_REUSEADDR`）
  - `audiomixing/net/rtpaudio.py` → `Receiver.start()`（UDP `SO_REUSEADDR`）
  - `audiomixing_launcher.py` → `run_service()`（原来只处理网页端口顺延）
  - 新增 `audiomixing/ports.py` → `ensure_ports()`
- 手机端：
  - `android/.../Engine.java` → `start()` / `configuredDiscoveryPort()` /
    `configuredAudioPort()` / `savePorts()` / `ensureReceiver()`
  - `android/.../Discovery.java` → `preferredPort` / `setDiscoveryPort()` /
    `beaconTargets()` / `start()` 绑定候选
  - `android/.../MainActivity.java` → `buildPortSettings()`（详细档）

**原因（代码级）**
1. 电脑的发现/音频 UDP socket 都显式设了 `SO_REUSEADDR`。
   Windows 允许两个进程同时绑定同一个 UDP 端口；引擎不会报错，
   两个同类软件同时收/抢同一批包，现场表现为两个一起死。
2. 启动器只对网页端口做了"被占就顺延/报错"的兜底；
   音频端口、发现端口、网页端口+1 都没有独占探测。
3. 手机端发现端口/音频端口写死 47830/47831，没有任何输入入口；
   电脑一改端口，手机既收不到信标，也不知道往哪个音频端口发。
4. 手机 `Discovery.beaconTargets()` 无论实际绑定哪个端口，信标永远发到
   默认 47830 —— 即使用户改了绑定端口，对端也收不到。
5. 手机 `AudioRecv` 在端口被占时会自动换一个，但 `Engine` 没把实际端口
   写回 `Discovery`，信标继续公布旧端口。
6. 启动器的 `_stop_port_owner()` / `_take_over()` 只看"谁占着网页端口"就直接
   `taskkill`，**不校验可执行文件**。同类软件的网页端口会被当成"无界面的旧版
   本 Audio Mixing"直接杀掉；这也是"两个软件一起死"的一部分。

**修复**
1. 新增 `audiomixing/ports.py`：
   ```python
   # TCP：网页端口 + 网页端口+1 必须成对空闲
   # UDP：Windows 用 SO_EXCLUSIVEADDRUSE 探测
   #      （普通 bind 会被别人的 SO_REUSEADDR 骗过去）
   ```
   冲突时向后找空闲端口；网页端口要求一对；返回 `{字段: 新端口}`。
2. `audiomixing_launcher.py` 在引擎启动前调用 `ensure_ports(cfg)`，
   有冲突就写回配置并弹窗列出新端口，提示手机端填相同值。
3. 手机 `Engine` 新增端口读写（SharedPreferences `audiomixing`）：
   `configuredDiscoveryPort()` / `configuredAudioPort()` / `savePorts()`。
4. 手机 `Engine.start()` 用保存的端口创建 Discovery 与接收端；
   `ensureReceiver()` 发现 `AudioRecv` 实际换了端口时，同步
   `Discovery.setAudioPort(actual)`。
5. 手机 `Discovery` 新增 `preferredPort` / `setDiscoveryPort()`；
   绑定顺序：用户端口 → 默认 47830 → 临时端口；
   **信标组播/广播改成发到实际绑定端口**。
6. 手机「详细」档新增「端口设置」：发现 + 音频两个数字输入框、
   保存按钮、重启提示；本机信息新增「发现端口」一行。
7. 启动器新增 `_pid_path()` / `_is_our_path()`：
   · `_stop_port_owner()` 只结束路径像 Audio Mixing 的 exe，其他程序一律跳过；
   · `_take_over()` 先验路径，不是自己人直接拒绝接管；
   · `_existing_node_address()` / `_running_status()` 也验身份：
     拿得到路径就以路径为准；只有路径拿不到时才看 `app` 标记；
   · `/healthz` 与 `/api/status` 增加 `"app": "audiomixing"`。
   这样同类软件即使占了同一个网页端口，也不会被误认、误开窗、误杀。

**回归**
- `tests/test_ports.py`：
  - `test_free_ports_are_kept`
  - `test_occupied_web_port_moves_to_a_free_pair`
  - `test_occupied_audio_port_moves_and_keeps_others`
- 全量：364 项通过。
- 手机端口 UI / Discovery 自定义端口需真机验证（无离线自动化，缺，待补）。
- 真机判据：
  1. 另一个同类软件占默认端口时启动本程序 → 弹窗 + 自动换端口，程序正常起来；
  2. 电脑改端口并重启 → 手机填相同发现/音频端口并重启 → 能发现/收听/送声；
  3. 两边都不改 → 默认 47830/47831 仍然可用。

**兼容性**
- 手机 2.4 / PC 2.14 起支持自定义端口；旧手机仍可用默认端口，
  但电脑若自动换端口，旧手机必须升级到 2.4 才能跟随。
- 电脑端自动换端口会写回配置，下次启动沿用；不再重复探测/弹窗。

---

## BUG-20261101-08 音量条同步 3–5 秒 + 端口设置只保留发现端口

**现象**
- 电脑或手机拖动音量滑杆后，另一端要 3–5 秒才跟着变，甚至看起来"不改变"。
- 端口设置要填面板/音频/发现三个值；用户要求"只要发现端口一致，
  其他端口通过发现端口自己协商统一"。

**位置**
- 电脑面板：`audiomixing/ui/index.html`
  - `setInterval(() => { ... refresh(); }, 4000)`
  - `setCardGain()` 的 `setTimeout(..., 350)`
  - 「设置」页端口卡片 `data-card="ports"`
- 手机端：
  - `android/.../MainActivity.java` → `buildPortSettings()`
  - `android/.../Engine.java` → `configuredAudioPort()` / `savePorts()`
- 对端协议不变：信标本就带 `audio_port` / `web_port`，不需要用户填。

**原因（代码级）**
1. 电脑面板每 **4 秒** 才 `refresh()` 一次；滑杆还有 **350ms** 防抖。
   对端其实几十毫秒就收到新增益，但这一端界面要等下一次 4 秒轮询才重画，
   所以观感是 3–5 秒（甚至以为没同步）。
2. 端口设置把"面板/音频/发现"三个都做成输入框；手机 2.4 也要求填发现+音频。
   实际上信标已经公布 `audio_port` / `web_port`，只有发现端口需要两端一致。
3. 手机 `configuredAudioPort()` 还从 SharedPreferences 读用户手填的音频端口，
   与"自动协商"口径冲突。

**修复**
1. 电脑面板刷新 `4000ms → 1000ms`；滑杆防抖 `350ms → 200ms`。
   手机端原本就是 150ms 一帧；对端统计一到就 `applyRecvGain/applySendGain`，
   两端看到新值的最坏时间约 **1 秒**。
2. 电脑「设置 → 端口设置」只保留 **发现端口** 输入框；
   面板/音频改为只读「自动 · 当前 X」，保存时把当前值一起提交（后端仍校验三个不重复）。
3. 手机「详细 → 端口设置」只保留 **发现端口**；
   `configuredAudioPort()` 改为返回默认端口，由 AudioRecv 自动选空闲口，
   实际值通过 `Discovery.setAudioPort()` 写进信标；电脑按信标发。
4. `savePorts(int disc)` 只保存发现端口，并 `remove("audio_port")` 清掉旧手填值。
5. 只有发现端口需要人工对齐；面板/音频端口由发现层自动公布。

**回归**
- 全量 364 项测试通过；`verify_exe` 13/13、`selftest_twonode` 19/19、
  `audit_leftovers` 0 error、`check_apk` PASS。
- 真机判据：
  1. 电脑拖「我送它的」→ 手机「我听它的」≤1 秒变成同一个值；反向同样；
  2. 手机「详细 → 端口设置」只有发现端口一个输入框；
  3. 只把两端发现端口对齐，设备能发现/收听/送声，音频/面板不用填；
  4. 电脑改自定义发现端口 + 手机填同值并重启 → 仍能正常连接。

**兼容性**
- 手机 2.5 / PC 2.15 起只要求发现端口一致；旧手机 2.4 仍显示音频输入框，
  但把发现端口对齐即可连接，音频值不再需要匹配。
- 协议字段不变；旧电脑/旧手机仍可互操作，只是没有"只填发现端口"的简化。

---

## BUG-20261101-09 电脑调音量手机不同步 + 反向仍要 5 秒 + 默认发现端口 17830

> 这一条仍然是 **PC 2.15 / 手机 2.5**（用户明确要求：这是老 bug，不升版本号）。
> 复刻项目照抄逻辑即可，**不要**因为这条把版本号写成 2.16 / 2.6。

**现象**
1. 电脑端拖「我送它的」滑杆（截图里 1.56），手机端对应的「我听它的」停在 1.00 或「—」。
2. 手机端拖音量，电脑端要 **5 秒左右**才跟着变（不是 BUG-08 里写的"约 1 秒"）。
3. 两端默认发现端口还是 47830；用户要求默认改成 **17830**。
4. **真机自测追加**（USB 调试，2026-11-01）：快速拖动手机滑杆，松手在 0.51，
   电脑端却停在 **1.45**，几秒后手机也被拉回 1.45 —— 松手的最终值丢了。

**位置（函数名为准）**
- 手机 `android/.../Engine.java`
  - `onPeerStats(String host, byte[] payload)`
  - `handleRemoteGain(String host, Diagnosis.PeerStats ps)`（改成三参数）
  - `peerStats(String host)`
  - `configuredDiscoveryPort()`
- 手机 `android/.../Diagnosis.java`
  - `PeerStats` / `parse(byte[] raw, int len)`
- 电脑 `audiomixing/engine.py`
  - `_send_peer_stats(force=False)`
  - 新增 `_route_peer_targets()`
- 电脑 `audiomixing/ui/index.html`
  - 文件末尾的 `setInterval(refreshAll, ...)`
- 默认端口：
  - `audiomixing/proto.py` → `PORT_DISCOVERY`
  - `audiomixing/config.py` → `Config.discovery_port` / `Config.load()`
  - `android/.../Proto.java` → `DEFAULT_DISCOVERY_PORT`
  - `android/.../Engine.java` → `configuredDiscoveryPort()`

**原因（代码级）**
1. **手机按 UDP 来源 IP 存增益，界面按发现层 `peer.host` 读。**
   `onPeerStats` 收到统计包时直接 `ps.host = host`（这是 UDP 包的来源地址），
   然后 `applyRecvGain(host, ...)`、`peerStats.put(host, ps)`。
   而 `MainActivity` 的滑杆读的是 `engine.peerRecvGain(peer.host)` / `peerStats(peer.host)`，
   那个 `peer.host` 是**发现信标里的地址**。
   电脑有双网卡 / VPN / 有线+Wi-Fi 并存时，统计包从 A 网卡发出、信标从 B 网卡报出，
   两个字符串不一样 —— 增益被写进一个界面永远不会读的键。
   手机→电脑那条能成，是因为电脑端 `_handle_peer_requests` 会**按 `node` 反查 node_id**，
   两边不对称，所以只有"电脑调、手机不变"。
2. **电脑的统计包不一定发得出去。**
   `_send_peer_stats` 的收件人只有 `sender.sessions`，并且 `packets_sent <= 0` 直接
   `continue`；用户先调音量、后开声音，或采集刚好空转时，**唯一携带 `gain/gain_rx`
   的消息一个都不发**。手机没有别的通道能知道电脑调了音量。
3. **电脑主面板轮询其实还是 2500ms。**
   BUG-08 里写的"4000ms → 1000ms"改在了**监控页**（`index.html` 里另一处
   `setInterval(..., 1000)`），主面板文件末尾一直是 `setInterval(refreshAll, 2500)`。
   再加上滑杆 200ms 防抖、手机统计最长 2 秒一轮，量出来就是 3–5 秒。
4. **默认发现端口写死 47830**，升级后老配置也不会动；两端都要用户手填一致。
5. **快速拖动时最终值被中间值顶掉。**
   · 手机 `setPeerRecvGain / setPeerSendGain` 每收到一帧 `onProgressChanged` 就
     `pokeStats()` —— 一次拖动连发几十个统计包；
   · 电脑 `_apply_shared_gain` 采纳**第一个到达的远端值**后，还写了
     `_gain_local_at[rid] = time.monotonic()`。这条 1.2 秒 hold 的本意是
     "别抢本机用户正在拖的滑杆"，但被写在了**远端采纳**路径上 ——
     接下来 1.2 秒内的远端更新全被挡掉；
   · 结果：1.56 → 1.45 先到，0.51 后到但被 hold 挡在门外；
     电脑回推 1.45，手机 1.2 秒本地 hold 一过也采纳 1.45，两端停在中间值。

**修复**
1. 手机统计包按 `node` 认设备（电脑端一直带 `"node": self.node_id`）：
   ```java
   // Diagnosis.PeerStats
   public String node = "";
   // Diagnosis.parse
   ps.node = nz(str(s, "node"));

   // Engine.onPeerStats
   String key = resolveHost(ps.node, host);   // node → 发现层 peer.host；查不到才退回来源 IP
   ps.host = key;
   onHeartbeat(key);
   handleWantListen(key, ps);
   handleRemoteGain(key, ps, host);           // 第三参数 = 音频包真正的来源地址
   peerStats.put(key, ps);
   ```
   `resolveHost` 用 `peerByNode(ps.node)` 遍历 `peers()` 找同一个 `nodeId`，
   命中就返回它的 `host`。`handleRemoteGain` 在来源地址和发现地址不同时，
   **两个键都设一次接收增益**（界面读前者、AudioRecv 按后者出声）。
   `peerStats(host)` 增加兜底：按 host 找不到时，按这台设备的 nodeId 再对一次。
2. 电脑统计包补上"有路由但还没出声 / 采集空转"的对端：
   ```python
   for key, info in self._listening_peer_targets().items():
       targets.setdefault(key, info)
   # 2026-11-01：路由存在就说明链路存在，不能等 packets_sent > 0
   for key, info in self._route_peer_targets().items():
       targets.setdefault(key, info)
   ```
   `_route_peer_targets()` 遍历 `local_routes()`，只认 `from` 里含本机、`to` 是
   **具体一台**的规则（`to:["*"]` 广播跳过）；`sending=False, want_listen=False`，
   只报 `gain / gain_rx`，绝不谎报"正在送"。
3. 电脑主面板轮询 `2500ms → 800ms`（保留滑杆 200ms 防抖）：
   ```js
   setInterval(refreshAll, 800);
   ```
   手机端本来就是 150ms 一帧，统计包一到就落增益 → "拖完 → 对面看到" ≤1 秒。
4. 默认发现端口改 17830，并**只迁移旧默认**：
   ```python
   PORT_DISCOVERY = 17830
   LEGACY_PORT_DISCOVERY = 47830
   # Config.load()
   if int(cfg.discovery_port or 0) == LEGACY_PORT_DISCOVERY:
       cfg.discovery_port = PORT_DISCOVERY
       if create:
           cfg.save(p)
   ```
   ```java
   public static final int DEFAULT_DISCOVERY_PORT = 17830;
   public static final int LEGACY_DISCOVERY_PORT = 47830;
   // Engine.configuredDiscoveryPort()
   if (v == Proto.LEGACY_DISCOVERY_PORT) { v = 17830; 写回 SharedPreferences; }
   ```
   用户自己填的 17930 之类**不动**（电脑端和手机端都按这个边界）。
5. 快速拖动的**最终值**必须最后一个落地：
   - 电脑 `_apply_shared_gain()` 删除 `_gain_local_at[rid] = time.monotonic()`：
     这条 hold 只由**本机 UI 真的拖动**（`note_local_gain_change()`）写入；
     远端连续更新按顺序全部落地，最后一个就是松手值。
   - 手机 `Engine.pokeGainStats()`：拖动的每一帧仍立即改本地音频增益，
     但叫醒统计线程按 **120ms 节流**；
     `MainActivity.onStopTrackingTouch()` 松手时再补一次 `engine.pokeStats()`，
     保证最终值最后发出。旧实现一帧一个包，既是流量也是乱序的源头。

**回归**
- `tests/test_audiomixing.py`：
  - `TestChannelSourceSafety.test_stats_go_to_a_peer_with_a_route_but_no_audio_yet`
    （有路由、还没发出音频时也要把 `gain=1.56` 报过去）
  - `TestChannelSourceSafety.test_a_remote_gain_burst_lands_on_the_last_value`
    （远端连发 1.45 → 0.50，必须停在 0.50，不能被 1.2 秒 hold 挡掉）
  - `TestCustomPorts.test_old_default_discovery_port_migrates_but_custom_stays`
    （47830 → 17830 并落盘；17930 原样保留）
  - 全量 **367 项**通过（原 364 + 新增 3）。
- `tools/check_ui_syntax.py` PASS；`tools/check_apk.py` PASS。
- 真机（USB 调试，RMX3700 / Android 16）：
  - 电脑→手机：电脑建 1.56 路由，手机日志
    `采纳对端报的共享音量：192.168.0.111 → 我听它的 1.56`，卡片显示 1.56；
  - 手机→电脑：手机拖「我听它的」到 0.51，电脑日志
    `对端把「我送它的」的音量调成了 0.51`，两端最终停在 0.51（修复前会回弹 1.45）；
  - 手机「详细」页显示发现端口 **17830**（全新安装默认值）。
- 手机端的 node 反查没有 JVM 单测（`Diagnosis` 依赖安卓类），**缺，待补**；
  真机判据见下。
- 真机判据：
  1. 电脑拖「我送它的」→ 手机「我听它的」≤1 秒变成同一个值；
  2. 手机拖「我听它的 / 我送它的」→ 电脑对应滑杆 ≤1 秒变成同一个值；
  3. 电脑先调音量、**还没开声音** → 手机也能看到新值（不再是「—」/1.00）；
  4. 两端全新安装 → 默认发现端口 17830；老配置 47830 升级后自动变 17830；
     手动填过 17930 的两端仍然是 17930。
  5. 快速拖动滑杆、松手在一个中间值 → 两端最终都停在**松手值**，不会回弹到拖动过程中的某个值。

**兼容性**
- 协议字段一个都没加（`node` 电脑端早就在发），旧手机只是不读它；
  手机端解析不到 `node` 时退回原来的来源 IP 行为。
- 新旧混跑时，旧电脑的统计包没有 `node` → 手机走 `resolveHost` 的 fallback，
  行为和以前一致，不会更差。
- 默认端口迁移只在"值 == 47830"时发生；任何自定义值都不动。

---

## BUG-20261101-10 电脑改端口后不自动重启（看起来像"不能修改"）

> 仍是 **PC 2.15 / 手机 2.5**，不升版本号。

**现象**
1. 电脑「设置 → 端口设置」改了发现端口并保存，提示"重启后生效"；
   不手动重启的话，面板/设备仍然用旧端口；再打开设置页又显示旧值 ——
   用户看到的就是"电脑端口不能修改"。
2. 用户要求：修改端口号后**自动重启**。

**位置**
- `audiomixing/webui.py`
  - `AudioMixingApi.set_settings()`
  - 新增 `AudioMixingApi._schedule_restart()` / `_strip_port_args()`
- `audiomixing/ui/index.html` → `savePorts()` / 端口设置卡片
- `audiomixing_launcher.py` → `run_service()` 里创建 `WebUI`
- `audiomixing/cli.py` → `cmd_serve()` 里创建 `WebUI`
- `android/.../MainActivity.java` → `buildPortSettings()` / 新增 `restartDiscovery()`

**原因（代码级）**
1. 端口在进程启动那一刻就绑死了（web、web+1 手机长连接、audio、discovery），
   `set_settings` 只把新值写盘，运行中的 `node.config` 立刻还原成旧值 ——
   这是**故意的**（否则信标会广播一个还没监听的端口）。
   但没有任何人自动重启，所以新端口永远不生效。
2. `savePorts()` 原来把面板/音频端口的**隐藏输入值**也一起提交。隐藏值只在页面
   第一次加载 `/api/config` 时填一次；没填到就是空 → `parseInt` 得到 `NaN` →
   JSON 里变成 `null` → 后端 `int(None)` 抛错 → **整次保存被 400 拒绝**。
   用户改的发现端口明明合法，也会看到"保存失败"。
3. 后端只校验范围/重复，不检查新端口是否真的空闲；端口被别的程序占着也会
   返回"保存成功"，重启后启动失败。
4. `web_port + 1` 是手机长连接端口；面板/音频/发现三个值如果连号
   （例如 web=50930、audio=50931），旧代码会拒绝保存 —— 这是**正确**的，
   但错误文案容易被当成"端口不能改"。本次同时把真实占用检查补上。
5. **自测追加（2026-11-01，成品 exe `serve` 模式）** 抓到两个"看起来写好了、
   实际永远不触发"的坑：
   · `_is_frozen` 是 **AudioMixingApi** 的方法，第一版写成了 `WebUI._is_frozen()`
     → AttributeError 被 `except` 吞掉 → 接口永远返回 `restart_blocked="not-frozen"`
     → 保存成功但从不重启。
   · 辅助进程用了 `DETACHED_PROCESS(0x8)`：PowerShell 在没有控制台时**直接退出**，
     探针实测 0x8 / 0x208 都不产出任何结果；只留
     `CREATE_NEW_PROCESS_GROUP(0x200)` 才能在父进程退出后继续拉起新 exe。

**修复**
1. 电脑保存端口后**自动重启**：
   ```python
   # webui.py → AudioMixingApi._schedule_restart()
   # 1) 独立 PowerShell 进程等本进程退出（释放单实例互斥体 / 端口）
   # 2) 400ms 后启动同一个 exe
   #    桌面形态：无参数（启动器会重新读配置、开新面板窗口）
   #    exe serve：保留 --host/--simulate，去掉三个端口参数（改从新配置读）
   # 3) HTTP 先返回 restart_scheduled=True，0.8 秒后 os._exit(0)
   # 4) 辅助进程只加 CREATE_NEW_PROCESS_GROUP(0x200)；
   #    **绝不能加 DETACHED_PROCESS(0x8)** —— PowerShell 没有控制台会直接退出
   ```
   - 判断 `frozen` 必须用 `self._is_frozen()`（AudioMixingApi 的方法），
     不是 `WebUI._is_frozen()`（后者根本不存在，异常会被吞成 not-frozen）。
   - 只有**真正变化的端口**才触发；原样保存返回"端口没有变化"。
   - 新端口先做独占可用性检查（TCP web/web+1、UDP audio/discovery），被占直接 400。
   - 启动器创建 `WebUI(..., restart_argv=[])`（无参数重启）；
     `exe serve` 传 `restart_argv=["serve","--host",…]`（不含端口，改读新配置）；
     源码模式 / 单元测试 / 双节点自检默认 `restart_argv=None`，
     接口返回 `restart_blocked="not-enabled"`，绝不会杀掉测试进程。
2. 页面 `savePorts()` **只提交发现端口**，删掉两个隐藏输入：
   ```js
   const r = await api('/api/settings', {
     discovery_port: parseInt($('setDiscPort').value, 10),
   });
   if(r.restart_scheduled) toast('端口已保存，正在自动重启…', 'ok');
   ```
   不再出现 NaN / 隐藏旧值覆盖。
3. 手机保存发现端口后**自动重启发现引擎**：
   ```java
   engine.stop();
   engine.start();        // 用新的 configuredDiscoveryPort() 重新绑定
   refreshNow();          // 完成后再刷界面
   ```
   放在后台线程执行，避免 UI 卡在 Discovery/AudioRecv 的 join 上。
4. 只有发现端口需要人工对齐；面板/音频端口仍由程序自动选择并写进信标。

**回归**
- `tests/test_audiomixing.py`（全量 **370 项**）：
  - `TestCustomPorts.test_ports_are_saved_and_need_restart`
    （源码模式必须 `restart_scheduled=False`，返回"请手动重启"）
  - `TestCustomPorts.test_saving_the_same_ports_does_not_restart`
    （原样保存不能把程序重启一遍）
  - `TestCustomPorts.test_port_args_are_dropped_for_the_restart_command_line`
    （`serve` 重启命令行去掉三个端口参数）
  - `TestCustomPorts.test_the_restart_helper_is_reachable_when_frozen`
    （真机抓到的回归：`_is_frozen` 是 `AudioMixingApi` 的方法，
      写成 `WebUI._is_frozen()` 会被 AttributeError 吞成 `not-frozen`，
      表现就是"保存成功但从不自动重启"）
- `tools/check_ui_syntax.py` PASS；`tools/check_apk.py` PASS。
- 真机（USB 调试，RMX3700 / Android 16）：
  - 手机「详细 → 端口设置」17830 → **17831**，点保存：
    日志 `发现已启动：端口 17831`，Toast「发现端口 17831 已生效」；
    没有任何手动重启 App 的动作；
  - 再改回 **17830**：日志 `发现已启动：端口 17830`，同样自动生效。
- 电脑端自动重启（`exe serve`、独立配置目录）：
  - 保存发现端口后接口返回 `restart_scheduled=true`；旧进程退出，
    辅助进程拉起同一个 exe；新进程 `/api/config` 读到新端口并正常应答。
- 桌面形态（双击 EXE，启动器 `restart_argv=[]`）单独实测：
  - 临时配置 web 53060 / audio 53066 / discovery 53062；
  - POST 改成 53072 → `restart_scheduled=true`、`restart_blocked=""`；
  - 旧进程 0.5 秒退出，约 7 秒后面板 53060 重新应答，`/api/config` 的
    discovery 已是 **53072**，磁盘 config.json 同步；
  - 坑：测试配置若带 **UTF-8 BOM**，`Config.load` 会解析失败并静默退回默认端口
    （17800/17831/17830），表现为"配置没生效" —— 这是测试脚本的问题，不是程序回退。

**兼容性**
- 源码运行 / 其它 CLI 子命令不会自动重启，返回"请手动重启"的提示。
- 手机端 `engine.stop()→start()` 会清掉当前的收听/送声选择（端口换绑必须重建收发），
  配置与路由保留；重启后重新点「收听 / 送声音」即可。
- 面板端口也改时：旧页面会断开，启动器会在新地址打开新窗口；只改发现端口时
  面板地址不变，页面会自动恢复。

---

## BUG-20261101-11 电脑调音量手机端不同步（镜像回声把本机的值顶回去）+ 电脑「智连」不是真实反馈

> 首次在 2.15 / 2.5 的同版本重建里修好；本批正式随 **PC 2.16 / 手机 2.6** 发版。

**现象**
1. 电脑端拖「我送它的」音量，手机端「我听它的」停在旧值；松手后电脑自己的滑杆
   **两秒内又弹回**旧值（真机复现：电脑改成 1.05，0.7 秒后自己变回 1.30）。
2. 电脑端每台设备卡片上的「智连」：按钮一亮一暗都有，但看不出它到底**做了没有**；
   打开后不立刻生效，关掉后也要等下一轮才撤；界面上没有任何"已自动收听 / 等它送声音"
   的实况。

**位置**
- `audiomixing/engine.py`
  - `_link_gains()` / `_apply_shared_gain()` / `note_local_gain_change()`
  - 新增 `apply_smart_link_now()`
- `audiomixing/webui.py`
  - `AudioMixingApi.update_route()`
  - `AudioMixingApi.set_routes()`（整表保存里的 `route_id` NameError）
  - `AudioMixingApi.set_settings()`（`smart_link` 立刻生效）
  - 新增 `AudioMixingApi._peers_with_smart()`
- `audiomixing/ui/index.html` → `deviceCard()` 的「智连」按钮与标签、`toggleSmart()`

**原因（代码级）**
1. 共享增益这条链路是**双向镜像**：本机报 `gain`（我送它的），对端存成自己那份
   "我听它的"，每 2 秒又用 `gain_rx` 原样报回来。`_apply_shared_gain()` 在
   2026-11-01 为了修"快速拖动最终值丢失"，改成了**不看时间戳、最后到达的为准** ——
   于是这份**回声**（不是对端用户改的）会覆盖本机刚拖到的值。
2. 面板拖滑杆走的是 `POST /api/routes/update`（`update_route`），而这条路径**从未**
   调用 `note_local_gain_change()`。原本唯一调用它的 `POST /api/routes`（整表保存）
   写的是 `self.node.note_local_gain_change(route_id)`，但 **`route_id` 在这个函数里
   根本没定义** —— payload 一带 `gain` 就是 NameError。结果两条路径都没有"本机刚动过"
   的 1.2 秒保护。
3. 真机日志指纹：`对端把「我送它的」的音量调成了 1.30，本机同步过去了`
   （23:30:43 / 23:32:43 / 23:33:26）。把手机关掉后同样的改动就不再回滚 ——
   证明来源是对端的镜像回声，不是本机别的代码。
4. 「智连」旧实现只写 `config.smart_link`：打开后要等对端下一包 `want_listen`
   或下一轮流进来才建路由；关掉后要等清扫线程下一轮才撤。这段时间按钮已经变色，
   实际什么都没发生。卡片也只读页面自己那份 `smartList` 镜像，不读引擎实况。
5. **路由表根本没从配置读回来**：`config.routes` 一直有写盘，但
   `AudioMixingNode.start()` 从来没有把它载入 `self.routes` —— 每次重启
   （包括端口保存后的自动重启、用户自己重开）整张表都是空的。现场就是
   "两条音量滑杆全灰、手机端没有可同步的对象"，用户描述成"调音量不同步"。

**修复**
1. 本机改音量必须留痕：`update_route()` 在 `gain` **真的变了**时调
   `note_local_gain_change(route_id)`；`set_routes()` 改成对比改前/改后，
   把变了的每条都记一笔（并删掉那个未定义的 `route_id`）。
2. 回声抑制：`_link_gains()` 每次把报出去的值记进 `_gain_published[rid] = (value, 时刻)`；
   `_apply_shared_gain()` 收到"和自己最近 10 秒内报出去的值一样"的增益时，
   只对齐时间戳、**不采纳**（那是镜像，不是对端用户改的）。
   真正不同的值（对端用户拖的，例如 0.51）仍然照常落地。
3. 「智连」立刻生效：`AudioMixingApi.set_settings()` 处理 `smart_link` 后直接调
   `node.apply_smart_link_now()`：
   - 关掉的设备 → `_sweep_unwanted_auto_routes()` 当场撤回自动建的收听；
   - 打开且**对端此刻真有流进来**的设备 → 当场 `_grant_listen_request()` 建收听；
   - 没在送声音的不建空路由（不白占输出设备），只显示"等它送声音"。
4. 真实反馈：`/api/peers` 每台设备多带三个字段（引擎口径，不再是页面镜像）：
   `smart_on`（本机给它开没开）、`smart_auto`（是否真有一条替它自动建的收听路由）、
   `smart_blocked`（它想听本机但没开智连 → 已拦下不发）。
   页面据此显示标签「智连·已自动收听」/「智连·等它送声音」，按钮也以 `smart_on` 为准；
   打开时的提示会如实写"已立刻替它打开收听"（只有真的建了才写）。
5. 启动时恢复路由：`AudioMixingNode.start()` 在 receiver 起来、设备刷新之后、
   beacon 广播之前调用 `set_routes(config.routes, persist=False)`，
   把上次运行的路由（含每条的音量）载回内存；`persist=False` 不重复写盘。
   端口自动重启 / 用户重开之后，两条音量滑杆和手机端的共享音量对象都还在。

**回归**
- `tests/test_audiomixing.py`（全量 **375 项**，本次新增 5 条）：
  - `TestChannelSourceSafety.test_a_mirror_echo_never_undoes_the_local_volume_change`
    （回声 1.30 不能顶掉本机刚改的 1.05；对端真改的 0.51 仍要落地）
  - `TestChannelSourceSafety.test_the_panel_volume_slider_stamps_a_local_change`
    （`/api/routes/update` 必须写本地 hold）
  - `TestPeerLivenessAndAutoListen.test_turning_smart_link_on_takes_effect_at_once`
    （开着智连 + 对端正在送 → 当场建好收听；关掉 → 当场撤回）
  - `TestPeerLivenessAndAutoListen.test_smart_link_on_does_not_open_a_route_when_nobody_is_sending`
  - `TestPeerLivenessAndAutoListen.test_routes_from_the_config_come_back_after_a_restart`
    （重启后 `config.routes` 必须载回内存，否则滑杆全灰、无从同步）
- `tools/check_ui_syntax.py` PASS（页面新增字段/标签没有语法或接口缺口）。
- 真机（USB 调试，RMX3700 / Android 16）：见下节"真机验证"。

**兼容性**
- 旧对端（手机 2.3/2.4）仍然只报 `gain/gain_rx`：回声抑制按"值相同"判断，
  不依赖新字段，行为只会更稳（不会更差）。
- `smart_link` 的语义不变（默认空 = 一台都不自动，手机端只自动开"听"）；
  变的只是"打开/关闭立刻生效"和界面如实显示。
- 源码运行 / 单测同样走 `apply_smart_link_now()`，没有额外副作用。

---

## BUG-20261101-12 手机没在收听时「信号」永远写「还没收到它的心跳」

> 手机 2.6（配套电脑 2.16）。

**现象**
手机端没有点「收听」/「听广播」时，设备卡片的「信号」条永远是空的，
右边写「还没收到它的心跳」—— 可是对方的信标每 2 秒都在，设备卡片本身也显示在线。
用户因此认为"信号反馈是假的"。

**位置**
- `android/app/src/main/java/com/audiomixing/app/Engine.java` → `peerQualityFor(String host)`

**原因（代码级）**
`peerQualityFor` 先用 `recvStats()` 取接收器统计；接收器没跑（没在收听）时
`receiver == null` → `recvStats()` 返回 **null** → 旧代码第一句
`if (st == null || st.streams == null) return -1;` 直接返回 -1。
界面把 -1 解释成"不可测"，于是永远写「还没收到它的心跳」；
后面那段"没有流就用心跳新鲜度给分"的兜底**永远走不到**。

**修复**
```java
AudioRecv.Stats st = recvStats();
// 没有音频统计不再直接 -1：跳过流表遍历，继续走心跳兜底
if (st != null && st.streams != null) {
    for (AudioRecv.StreamStats ss : st.streams) {
        if (!host.equals(ss.host)) continue;
        return LinkPolicy.qualityScore(ss.received, ss.lost, ss.bufferedMs);
    }
}
Discovery.Peer peer = peerByHost(host);
if (peer != null && peer.online()) {
    return LinkPolicy.heartbeatScore(peer.ageMs());
}
return -1;
```
- 真正收到流 → 仍按收包/丢包/缓冲打分（口径不变）；
- 没在收听但对方在线 → 显示「N 分 · 在线」；
- 对方信标也过期了 → 仍然 -1（「还没收到它的心跳」这时是**事实**）。

**回归**
- `gradle clean assembleDebug` BUILD SUCCESSFUL；`tools/check_apk.py` PASS（2.6 / 20）；
- 真机（RMX3700 / Android 16）覆盖安装后：未点收听时卡片显示信号分，不再写心跳缺失；
- `LinkPolicy.heartbeatScore` / `qualityScore` 已有零桩门禁（PolicyProbe）覆盖算式本身。

**兼容性**
- 只改"没有依据时显示什么"，不改任何收发/增益逻辑；旧电脑端无需升级也能看到正确信号。

---

## BUG-20261102-01 测试路由与临时沙箱遗留进了用户环境

**现象**
监控页出现一条用户从没建过的路由「2024TSJ → f04304d2」，标签 `sync-test`，
状态「已配置 · 当前没有数据」；`%TEMP%` 里还堆着 28 个 `am-blocked-*` 和多个
`am-ports-* / am-mig-* / am-home-*` 测试目录，仓库里也有 `.tmp` 联调脚本与截图。

**位置**
- 用户配置：`%APPDATA%\AudioMixing\config.json` 的 `routes`
- `tests/test_audiomixing.py` → 模块级 `_TEST_SANDBOX` 以及多处直接调用 `tempfile.mkdtemp()`
- `tools/audit_leftovers.py` → `audit_environment()` 的 config 路由检查、%TEMP% 扫描、仓库 scratch 目录检查

**原因（代码级）**
1. 手工联调（音量同步）直接跑的是用户那份真实 EXE 与真实配置，没有用临时
   `AUDIOMIXING_HOME`；`set_routes()` 默认落盘，于是 `sync-test` 路由写进了用户的 `config.json`。
2. 测试里 `test_unwritable_config_is_reported_not_faked`、`TestSettingsPorts.setUp`
   （`am-ports-`）、`test_old_default_discovery_port_migrates_but_custom_stays`（`am-mig-`）、
   `test_launcher_reads_port_from_config`（`am-home-`）以及两处蓝牙状态目录都直接
   `tempfile.mkdtemp()`，没有 `addCleanup`/`rmtree`，每跑一轮就多一批目录（`am-blocked-` 已累积 28 个）。
3. `audit_leftovers.py` 的路由判据只看 id 前缀（`exe/w1/r1/...`），
   看不出 `099ad8e1` 这种"id 随机、label 叫 sync-test"的路由；%TEMP% 也只扫
   `audiomixing-verify-/check-/selftest-/diag-`，扫不到 `am-blocked-*`、`am-ports-*`；仓库 scratch 没查 `.tmp`。
4. 同一个工具还把"端口不是默认值"当成测试残留，`--fix` 会整份删除用户的 `config.json`
   —— 用户自己改过端口或启动时自动避开冲突都会被误删。

**修复**
```python
# tests/test_audiomixing.py：所有测试沙箱统一登记，退出时统一删
_SANDBOXES: list[pathlib.Path] = []

def _mkdtemp(prefix: str = "") -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    _SANDBOXES.append(path)
    return path

atexit.register(lambda: [shutil.rmtree(p, ignore_errors=True) for p in _SANDBOXES])
```
```python
# tools/audit_leftovers.py：只删测试路由，保留用户的端口与其它设置
def _looks_like_test(route: dict) -> bool:
    rid = str(route.get("id", ""))
    label = str(route.get("label") or "").strip().lower()
    return (rid.startswith(("exe", "w1", "r1", "rec", "in-", "many", "one-to",
                            "wild", "both", "self2"))
            or "test" in label or "测试" in label)
```
- `%TEMP%` 扫描扩展到 `am-blocked- / am-beacon / am-ports- / am-mig- / am-home- / am-p2p- / audiomixing-* / wavelink-*`；
- 仓库 scratch 增加 `.tmp`；"端口非默认"从 WARN+删配置改为仅提示，**绝不删配置文件**；
- 已删除遗留路由 `099ad8e1`（`routes` 现为 `[]`），保留用户端口 17901/17931/17830。

**回归**
- `python -m unittest discover -s tests -q` → **375 项 OK**；
- `python tools/audit_leftovers.py` → `config no test peers/routes`、`temp no test/verification leftovers`；
- 规则：**手工联调必须 `AUDIOMIXING_HOME=<临时目录>` + 非常用端口，跑完即关**，不得直接动用户配置。

**兼容性 / 生效条件**
- 只清理数据与开发工具，不改产品收发逻辑，**无需重新打包 EXE / APK**；重开面板即生效。

---

## BUG-20261102-02 智连自动收听路由不参与共享增益同步（两端 1.60 / 0.40）

**现象**
PC 设备卡片显示「我听它的」= 1.60；手机同一台 PC 的卡片显示「我送它的」= 0.40。
手机确实在送声、PC 确实在收；两端各自停在本地值，谁也没有采纳对方。

**位置**
- `audiomixing/engine.py`
  - `_route_between`（原 L2093 起）、`_listening_peer_targets`（原 L2364 起）、`_route_peer_targets`（原 L2413 起）
  - `_grant_listen_request`（约 L2914-L2920，自动路由的 `origin` 写成对端 node id）
  - `local_routes()`（约 L1328，只保留 `origin in (None, self.node_id)`）
- `android/.../Engine.java`
  - `linkGainChangedAtMs`（原 L1942）、`handleRemoteGain`（约 L1951）

**原因（代码级）**
1. `local_routes()` 的语义是"**这张面板可以编辑哪些行**"（`origin == 本机`）。
   智连自动建的 `从它到本机` 收听路由把 `origin` 记成**对端**（它请求的），于是：
   - `_route_between(peer_id, "in")` 找不到这条路由 → `_apply_shared_gain` 第一行 `return`，手机报来的 0.40 落不下来；
   - `_listening_peer_targets()` 也扫不到这条 → PC 单纯收听时**一个统计包都不发**，PC 的 1.60 也到不了手机。
   两端于是各自停在本地值，正好是截图里的 1.60 / 0.40。
2. 手机端两个方向共用同一个 `linkGainChangedAtMs`：本机刚动「我听它的」，
   接下来 1.2 秒连「我送它的」也不采纳对端值，两个互不相关的滑杆互相挡。

**修复**
```python
def _link_routes(self) -> list[dict[str, Any]]:
    """描述“我的链路”的整张表（含对端 origin 的自动路由）；不是“归我编辑”的那些。"""
    with self._routes_lock:
        return [dict(r) for r in self.routes]
```
- `_route_between`、`_listening_peer_targets`、`_route_peer_targets` 全部改用 `_link_routes()`；
- `local_routes()` 保持原语义，只服务面板新增/编辑；
- 手机端把 hold 拆成 `linkRecvGainChangedAtMs` / `linkSendGainChangedAtMs`，两个方向各自 1.2 秒。

**回归**
- 新增 `TestPeerLivenessAndAutoListen.test_shared_gain_works_on_an_auto_listen_route_authored_by_the_peer`：
  用 `origin=对端` 的 `auto-listen-*` 路由，断言 `_link_gains` 有 `gain_rx`、
  收件人含对端、手机报 0.40 后本机路由落到 0.40。
- 全量：**377 项 OK**；七条 Android 离线门禁全 PASS。

**兼容性 / 生效条件**
- 只改"读哪张表"，不改协议字段；两端都要升级才能同时看到两个方向的同步。

---

## BUG-20261102-03 运行中被卸载后仍显示「屏幕共享中」

**现象**
手机已授予「录制/投射」权限、正在送本机声音；此时直接卸载 App，
系统状态栏仍显示「屏幕共享中」，MediaProjection 没有释放。

**位置**
- `android/.../MixService.java`：`tick`（约 L68-L90）、`onTaskRemoved`（约 L191）、`onDestroy`（约 L220）
- `android/.../Engine.java`：`stop()` 里的 `releaseProjection()`（约 L344）

**原因（代码级）**
1. 卸载时系统通常直接杀进程，`onTaskRemoved` / `onDestroy` 都可能不执行；
   系统也不会给被卸载的应用发自己的 `PACKAGE_REMOVED`。
2. 清单里的 `stopWithTask="true"` 让系统直接停服务，`onTaskRemoved` **不会**被回调 ——
   原来指望它收尾的那条路径实际走不到。
3. 部分 ROM 在卸载后会让已卸载的进程/前台服务短暂存活，`MediaProjection` 就继续挂住状态栏。

**修复**
```java
private boolean stillInstalled() {
    try {
        getPackageManager().getPackageInfo(getPackageName(), 0);
        return true;
    } catch (PackageManager.NameNotFoundException e) {
        return false;
    } catch (Throwable t) {
        return true;      // 读失败不等于被卸载，别误停正常服务
    }
}
```
- 服务 `tick` 每 3 秒先查一次；返回 false → `shutdownNow()`：
  `engine.stop()`（内部 `MediaProjection.stop()`）→ 摘前台/通知 → `stopSelf()`；
- 保留 `onTaskRemoved` / `onDestroy` 的原有收尾作为兜底。

**回归**
- `gradle clean assembleDebug` BUILD SUCCESSFUL；`check_apk` PASS（2.7 / versionCode 21）。
- 真机：**待确认**（装 2.7 → 授权本机声音 → 运行中卸载 → 看状态栏「屏幕共享中」是否消失）。
  这是系统级行为，自动化只能验"编译通过 + 清理路径存在"，无法替代真机。

**兼容性**
- 只影响卸载/退出收尾，不改任何音频协议。

---

## BUG-20261102-04 手机系统音量键不再改变听到的声音

**现象**
早期版本按手机音量键/系统音量条能改变 App 听到的声音大小；现在按音量键没有反应，
但 App 内的「我听它的」滑杆仍有效。

**位置**
- `android/.../MainActivity.java` → `onCreate`（约 L296）
- `android/.../AudioRecv.java` → `openTrack`（约 L1125，AudioAttributes 已是 `USAGE_MEDIA` / `CONTENT_TYPE_MUSIC`）

**原因（代码级）**
`MainActivity` 从未调用 `setVolumeControlStream(...)`，界面在前台时音量键可能落在
铃声/通知流；播放用的 `AudioTrack` 虽然使用 `USAGE_MEDIA`，但没有把音量键显式指向
`STREAM_MUSIC`。App 内滑杆改的是 `AudioTrack.setVolume` + 每路 PCM 增益，所以它一直有效。

**修复**
```java
protected void onCreate(Bundle state) {
    super.onCreate(state);
    setVolumeControlStream(AudioManager.STREAM_MUSIC);
    ...
}
```
- 仍保持三层独立：系统音量（只由用户按键改，App 绝不 `setStreamVolume`）×
  `AudioTrack.setVolume` × 每路 PCM 增益；
- 打开/关闭 App 不主动改系统音量。

**回归**
- 编译 + `check_apk` PASS；门禁无法模拟真实音量键。
- 真机：**待确认**（App 前台时按音量键，声音应随系统媒体音量变化；详细页「系统音量」应同步变化）。

**兼容性**
- 只改按键落到哪个流，不改播放链路。

---

## BUG-20261102-05 共享增益发送端/接收端各乘一次（0.40 实际 0.16）

**现象**
两端音量条显示同一个值（例如 0.40），但实际听到的声音比预期小得多；
0.40 听起来接近 0.16。用户感觉"滑杆像没效果"。

**位置**
- `audiomixing/net/rtpaudio.py` → `SenderSession._send_chunk`（约 L996-L1030）
- `android/.../SendFanout.java` → `send()`（约 L262）
- 接收端：`audiomixing/engine.py` → `_start_sink`（`gain=rule["gain"]`，约 L4571-L4587）；
  `android/.../AudioRecv.java` → `Stream.store`（`scalePcm16`，约 L1231-L1279）

**原因（代码级）**
同一条链路的共享增益被两端各乘了一次：
- 发送端 `SenderSession.gain = rule["gain"]`，`_send_chunk` 先缩放样本；
- 接收端 sink `engine.gain = rule["gain"]` / 手机 `Stream.gain` 又缩放一次。
数学上就是 `0.40 × 0.40 = 0.16`；1.60 则可能削顶失真。

**修复**
- **只在接收端乘一次**：
  - PC `rtpaudio` 不再缩放样本，仍把 `gain_q14` 写进包头如实描述这条流的音量；
  - Android `SendFanout` 不再按目标缩放样本，`Target.gain` 只用于界面/诊断；
  - 「送」电平仍按 `peak × gain` 显示，口径不变（0.3×2.0=0.6 的旧测试继续通过）。
- 接收端保持原逻辑：PC sink 的 `engine.gain`、手机 `AudioRecv.Stream.gain`。

**回归**
- 新增 `TestOutputLevelAndBluetoothCleanup.test_the_sender_leaves_the_shared_gain_scaling_to_the_receiver`：
  发送端原样发 0.3、包头 `gain=0.4`、电平 0.12；
- `FanoutProbe` 第 8/9 组同步改成"发送端原样发、接收端缩放"（36 项 PASS）；
- 全量 377 项 OK。

**兼容性**
- 旧接收端同样会按共享增益缩放，所以只升级发送端也能听出音量变化；
- 两端都升级后，同一个滑杆才是"所见即所听"的一次增益。

---

## BUG-20261102-06 双端自查修掉的 6 个缺陷

| # | 现象 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 1 | 电脑开「广播我的声音」时，对端仍被请求自动打开收听（等于替别人开关"听"） | `audiomixing/engine.py` `_send_peer_stats`（约 L2240） | 会话键是 `channel\|<node>`，代码拿它和裸 `CHANNEL_ROUTE_ID` 全等比较，永远 False | 比较 `route_id.split("\|",1)[0]`；测试里的假会话键也改成真实格式 | `test_a_broadcast_session_never_asks_the_peer_to_listen` |
| 2 | 本机只监听 A 时，手机 B 的声音被挂到 A 的播放端（串台） | `engine.py` `_reattach_stream_routes`（约 L4839） | 重挂流兜底只看"只有一个 sink"，漏了 `_attach_stream_routes` 已有的"发送方认不出身份"边界 | 加 `and not self._sender_is_known(info.node_id)` | 既有串台回归 + 全量 377 |
| 3 | 音频端口被占时界面显示"已收听"却 0 个流；对端按旧端口发来的包全丢；Wi-Fi 锁常驻 | `AudioRecv.rxLoop`（约 L581）、`pickFreePort`（约 L565）、`Engine.ensureReceiver`（约 L1563） | bind 失败直接 `running=false; return`，没试下一个口，也没释放 WifiLock；Engine 启动后立刻读 `r.port()` 是旧值（那时还没绑） | `pickFreePort` 无空闲口返回 -1；rxLoop 最多试 6 个口，失败也 `releaseWifiLock()`；新增 `PortListener`，绑定成功后把实际端口同步到信标 | 编译 + 门禁；真机待确认（占住 47831 再启动） |
| 4 | 接收器重建（重开收听/保存端口）后，滑杆还是 0.40，实际增益回到 1.00 | `Engine.ensureReceiver`（约 L1591）、新增 `restoreRecvGains` | 只补了发送侧 `restoreGains`，`linkRecvGain` 没回放 | 接收器启动后遍历 `linkRecvGain` 调 `r.setHostGain` | 编译 + `check_apk` |
| 5 | 长时运行后内存/CPU 持续爬升，调音量变卡 | `audiomixing/net/rtpaudio.py` T_STATS/T_ROUTE 入列（约 L610-L629） | 只有 HELLO 限长，T_STATS 每 2 秒一条、永不停止；引擎每 0.5s 全量遍历 | 与 HELLO 相同：>200 条删前 100 条 | 全量 377 |
| 6 | 换机器/换检出目录构建 PyInstaller 包会失败，或把另一份旧代码打进 exe | `AudioMixing.spec` L4/L16/L17/L49 | 硬编码 `E:/AI/deep blue/AudioMixing` | 以 `SPECPATH` 为根拼 UI/launcher/icon 路径 | 构建脚本自检 |

**兼容性**
- 1/2/5/6 不涉及协议字段；
- 3/4 只影响手机端本机行为，旧电脑端无需升级也能收（换口后信标会公布新端口）。

---

## BUG-20261102-07 卸载后「屏幕共享中」仍残留（2.7 加强版）

**现象**
装手机 2.7 后，运行中（已授权本机声音）直接卸载 App，状态栏「屏幕共享中」仍然存在；
用户明确反馈 2.7 没解决。音量两条已正常。

**位置**
- `android/.../MixService.java`：`installWatch`（新增）、`stillInstalled()`、`shutdownNow()`
- `android/.../Engine.java`：`statsLoop`（约 L2174）、`packageStillInstalled()`（新增）

**原因（代码级）**
2.7 只有服务里的 3 秒 `getPackageInfo(getPackageName())` 检查，存在两个漏洞：
1. 卸载时系统可能**先删除 APK、后清理 PackageManager**；在这段窗口里
   `getPackageInfo` 仍可能命中进程内/系统里的旧缓存，返回成功；
2. 系统可能先停掉前台服务（`onDestroy` 来不及执行），服务里的 `installWatch` 一起消失，
   而**进程还活着** —— 这时没有任何人再检查包是否还在，`MediaProjection` 就一直挂着。
`MediaProjection.Callback.onStop` 只在进程活着且系统通知会话停止时才会来，卸载属于进程级事件，
不能指望它。

**修复**
```java
// MixService：1 秒循环；包名 + APK 文件双重判据
String apk = pi.applicationInfo.sourceDir;      // 或 getPackageCodePath()
if (apk != null && !apk.isEmpty() && !new File(apk).exists()) return false;
```
```java
// Engine：第二道保险（服务被停掉、进程仍在时仍能收尾）
if (app != null && !packageStillInstalled()) {
    stop();      // 内部 stopReceiver/stopSender/releaseProjection
    return;
}
```
- 两处命中都会 `engine.stop()` → `AudioSend.setProjection(null)` → `MediaProjection.stop()`；
- 服务侧再摘前台/通知、`stopSelf()`；引擎侧只释放资源（它无权停服务）。
- 仍未覆盖"系统瞬间杀进程"：那种情况只能由 Android 系统在 binder death 时收回会话，
  应用层没有任何执行机会。若真机仍残留，请按文末"真机取证"抓日志。

**回归**
- `gradle clean assembleDebug` BUILD SUCCESSFUL；`check_apk` PASS（2.8 / versionCode 22）。
- 全量 377 项 PC 测试不受影响；Android 七条门禁仍 PASS。
- 真机：**待确认**（2.8 装好后重做一次"授权 → 运行中卸载"）。

**真机取证（若仍残留）**
1. 卸载后立刻 `adb shell dumpsys media_projection`，看 `mProjections` 里还有没有
   `com.audiomixing.app`；
2. `adb shell ps -A | findstr audiomixing` 看进程是否还活着；
3. `adb logcat -d -s AM-Service AudioMixing` 看有没有
   `检测到本应用已被卸载` / `引擎检测到本应用已被卸载`；
4. 点状态栏「屏幕共享中」chip，看系统弹窗写的是哪个 App —— 若写的不是 Audio Mixing，
   说明是别的程序留下的会话，与本 App 无关。

**兼容性**
- 只改卸载/收尾检测，不改音频协议与音量。

---

## BUG-20261102-08 电脑在别的设备上显示成「本机」

**现象**
手机设备列表里，电脑卡片标题是「本机」，副标题却是 `Windows-11 · 192.168.0.111 · 有网页面板`；
用户根本分不清哪台才是自己。电脑真实主机名是 `2024TSJ`。

**位置**
- `audiomixing/config.py` → `Config.load`（约 L157-L171）
- `audiomixing/config.py` → `default_node_name()`（L61-L63，新默认已经是主机名）

**原因（代码级）**
`default_node_name()` 早就改成 `socket.gethostname()`，但**老配置里已经把默认名写死成「本机」**。
`Config.load()` 只读不迁移，于是这个旧默认值跟着配置一路存到现在；
手机看到的信标 `name` 就是「本机」，卡片标题自然写「本机」。
（`node_id` 是独立的 UUID，所以不是"被当成自己"，纯粹是名字不对。）

**修复**
```python
# config.py Config.load：只迁移"从没改过"的旧默认值
if str(getattr(cfg, "name", "") or "").strip() in ("", "本机", "我的电脑", "电脑"):
    cfg.name = default_node_name()
    if create:
        cfg.save(p)
```
- 用户自己起的名字（例如「客厅电脑」）一律不动；
- 迁移会落盘，下一次启动信标就用主机名。

**回归**
- `TestCustomPorts.test_legacy_default_name_migrates_to_hostname`：
  「本机 / 我的电脑」→ 主机名并落盘；「客厅电脑」保持不变。
- 全量 **378 项 OK**。

**兼容性**
- 只改本机显示名，不影响 node_id、路由、端口；对端下一次信标后自动更新。

---

## BUG-20261102-09 手机「App 音量」里看不到本应用

**现象**
手机音量面板分两条：左边「手机音量」（媒体总音量），右边「App 音量」（带应用图标）。
用户希望用「App 音量」控制 Audio Mixing 的声音；现在本应用不出现在右侧。

**位置**
- `android/.../Engine.java`：`ensureReceiver()` 末尾、`stopReceiver()`、新增 `ensureMediaSession()` / `releaseMediaSession()`

**原因（代码级）**
播放用的是 `AudioTrack`（`USAGE_MEDIA` / `CONTENT_TYPE_MUSIC`），但全项目**没有任何
`MediaSession`**。系统音量面板的分应用音量只认"正在播放媒体的、有媒体会话的 App"；
没有会话时只剩一条总的媒体音量，所以本应用无法和「App 音量」关联。

**修复**
```java
MediaSession s = new MediaSession(app, "AudioMixing");
s.setPlaybackToLocal(new AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_MEDIA)
        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build());
s.setPlaybackState(new PlaybackState.Builder()
        .setState(PlaybackState.STATE_PLAYING, PlaybackState.PLAYBACK_POSITION_UNKNOWN, 1.0f)
        .build());
s.setActive(true);
```
- 只在**开始收听**时创建（`ensureReceiver` 末尾），停止收听时 `setActive(false)+release()`；
  送声音时不建，避免常驻一个假"正在播放"的媒体会话。
- 不接管播放/暂停按键；实际音量仍是三层：系统/分应用音量 × `AudioTrack.setVolume` × 每路 PCM 增益。
- 仍保留 `setVolumeControlStream(STREAM_MUSIC)`，硬件键有会话时跟着会话走。

**回归**
- `gradle clean assembleDebug` BUILD SUCCESSFUL；`check_apk` PASS（2.9 / versionCode 23）；
  七条 Android 离线门禁全 PASS。
- 真机：**待确认**（手机播放声音时，展开音量面板应出现 Audio Mixing 的「App 音量」滑杆）。

**兼容性**
- 只影响音量面板的呈现，不改音频数据与协议；不建 MediaSession 时行为与 2.8 相同。

---

## BUG-20261102-10 单元测试会拉取用户正在运行的真实 App 路由

**现象**
用户桌面开着 Audio Mixing（监听 17800）时跑单元测试，
`TestTodayRegressions.test_listen_only_does_not_kill_ordinary_route_senders` 会偶发失败：
`'p2p' not found in set()`；单独跑或关掉 App 又通过。

**位置**
- `tests/test_audiomixing.py` 模块级
- `audiomixing/engine.py` → `_pull_peer_routes()`（L1300 起，HTTP `/api/routes`）

**原因（代码级）**
测试节点对自己加的手动对端（127.0.0.1）会发 `http://127.0.0.1:<port>/api/routes`。
如果用户正好开着真实 App（默认网页端口 17800），测试就把**用户真实路由表**拉进沙箱，
`kept` 保留 origin != 测试作者的真实路由，再把测试自己那条 `p2p` 冲掉 —— 假失败。

**修复**
```python
# tests/test_audiomixing.py
AudioMixingNode._pull_peer_routes = lambda self: None
```
测试只验证本机逻辑，不跨进程拉路由；用户开着 App 也能稳定跑。

**回归**
- 开着 v2.18 的情况下：**378 项 OK**；
- 之前失败的该测试单独/全量都通过。

**兼容性**
- 只改测试，不影响成品。

---

## 历史修复速查（v2.2 → v2.12）

> **给基于 v2.9 复刻的项目：v2.10 / v2.11 / v2.12 以及本文最上面的 BUG-20261101-01~12、BUG-20261102-01~10 必须补齐。**
> v2.2 → v2.8 是更早的坑，列全是为了避免"旧逻辑又被抄回去"。
> 行号会漂，**以函数名为准**；测试名都在 `tests/test_audiomixing.py`（除非另注）。

### v2.2（2026-10-21）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.2-1 | 换一台电脑双击完全没反应（`0xc000001d`） | `tools/build_nuitka.py` 的 `build_env()` / `check_portable_isa()` | 构建机（Ryzen 9800X3D）支持 AVX-512，Nuitka 用 zig 链接时从 compiler-rt 选了 AVX-512 的 `memcpy`；目标机（Intel Core Ultra 5 125H）不支持，第一条 `vmovups zmm0` 就非法指令 | `CFLAGS/CXXFLAGS/LDFLAGS` 全部加 `-mcpu=baseline`；显式 `--zig --target-arch=x86_64`；新增 `check_portable_isa()` 扫 `.text`，`zmm > 0` 拒绝出包 | `test_build_forces_a_baseline_cpu_isa`；另有构建自检/`verify_exe` |
| 2.2-2 | 诊断 BAT 在 cmd 里报 `'锘緻echo'`；PS1 报"路径中具有非法字符" | `release/诊断工具/` 下的 `.bat` 与 `.ps1` | BAT 带 UTF-8 BOM，cmd 把 BOM 当命令；BAT 把 `%~dp0`（尾部反斜杠）传给 PowerShell，闭合引号被转义 | BAT 改成无 BOM 的 ASCII；不再传目录参数；PS1 入口去尾部斜杠/引号并兜底脚本目录 | 本机验证（中文路径；无参数/带尾斜杠两种调用） |
| 2.2-3 | 启动失败静默退出，用户只看到"没反应" | `audiomixing_launcher.py` 的 `_main()` / `run_service()` | 异常写日志后 `return 1`，既没有控制台也没有弹窗；`launcher.log` 不记录运行环境 | 最外层异常弹窗（最后 8 行 + 日志路径）；`launcher.log` 增加"启动环境"四行；监控窗打不开也弹窗给出 URL | 本机验证（模拟异常弹窗、中文路径完整链路） |

### v2.3（2026-10-22，短命验证版；改动已被 v2.4 接管）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.3-1 | 监控页打开「本机」后，蓝牙行被整行隐藏 | `webui.py` 的 `/api/monitor` 本地端 + `ui/index.html` 筛选 | 本地端写的是「本机 / 本机声音」，筛选只拿节点名去比 | 筛选认节点名 + 本地几种写法 | v2.4 起的蓝牙/监控测试 |
| 2.3-2 | 蓝牙只有连接、没有媒体音频时，面板报"正在接收手机声音" | `bluetooth.py` 的端点状态 + 面板蓝牙卡片 | 只看"配对/连接"，没看 `endpoint` 是否为 true | 只有 `endpoint=true` 报 connected，否则 waiting（黄色提示）并给排查步骤 | `TestOutputLevelAndBluetoothCleanup`、`TestNetworkSelfCheckAndBluetoothRow` |

### v2.4（2026-10-22）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.4-1 | 手机已出声，蓝牙卡片/按钮/提示停在旧状态，必须手点刷新 | `bluetooth.py` 的 `_endpoint_state_from_registry()`；`ui/index.html` 的 `btPollState()` | 卡片只在打开/点刷新时全量枚举（PowerShell ~3 秒）；engine 保持连接时缓存 `max_age=600` 秒 | 每次轮询直接读 MMDevice 注册表 `DeviceState`；卡片每 2.5 秒轻量同步 | `TestOutputLevelAndBluetoothCleanup`、`TestNetworkSelfCheckAndBluetoothRow` |
| 2.4-2 | 手机已出声，提示仍写"没声音/去加音源" | `ui/index.html` 蓝牙卡片文案 + `webui.py` 对应接口 | 早期假设"Windows 不会直接播放 A2DP 采集流" | 改为「手机媒体音频已到 · 系统播放」；明确本机由 Windows 播放、不加音源；只有转发/广播才选 A2DP SNK | 本机浏览器 + API 集成验证 |

### v2.5（2026-10-26）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.5-1 | 任务管理器有两个程序，音频一卡一卡 | `audiomixing_launcher.py` 单实例检查；`webui.py` 端口占用；`engine.py` 渲染线程；`net/rtpaudio.py` 诊断 | ① Nuitka onefile 本来就是"引导器 + 子进程"两个进程；② 只靠 17800 健康应答判断单实例，而 `node.start()` 先占音频/发现端口、`ui.start()` 后才开网页口，中间几秒再双击会起第二个引擎；③ 诊断字段被截断，分不清卡在声卡还是网络 | 启动器加 Windows 命名互斥体；网页口被占不再静默顺延到 17801；诊断新增「进程」段、接收端抖动计数、播放端声卡缓冲/设备放空/喂块最长间隔；渲染线程登记 MMCSS「Audio」 | `TestTakeOverOldVersion`、`TestRenderLoop`；`verify_exe` / `selftest_twonode` |
| 2.5-2 | 诊断里播放端字段被 `[:240]` 截断，看不出设备级问题 | `webui.py` 诊断报告拼装 | 字段截断 | 播放端输出完整设备、缓冲、放空、喂块间隔 | 诊断报告相关测试 + 成品冒烟 |
| 2.5-3 | 快速上手把第一块虚拟网卡当成手机能连的地址 | `webui.py` 的 `_lan_host()` / `phone_listen_url()` | 取 `lan_report()` 第一块网卡 | 按 192.168/10 优先排序，跳过 127/169.254，和二维码/面板一致 | `TestQuickStartAndVersion` |

### v2.6（2026-10-26）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.6-1 | 蓝牙已在播放，面板仍写"媒体音频未到" | `bluetooth.py` 的 `_endpoint_state_from_registry()` | 只扫 Capture、要求名字含 `A2DP SNK`，且大小写/空格敏感；找不到就沿用枚举缓存里的 False | Capture + Render 都扫；名字去空白转小写；优先 A2DP/SNK；找不到才退回任何含设备名的端点 | `TestOutputLevelAndBluetoothCleanup` |
| 2.6-2 | 蓝牙智连把 A2DP SNK 自动塞进「音源」，出现回声/断续 | `engine.py` 的 `_auto_play_sources()` | Windows 已经直接播放手机声音，再采一遍/放一遍 | 蓝牙智连只保留"启动时自动尝试连接一次"，删除 A2DP 自动收进音源 | `TestAutoListenRevoke`、`TestBluetoothBridge` |
| 2.6-3 | Wi-Fi 智连只认对方 `want_listen`，本机开了也不主动收听 | `engine.py` 的 `_queue_auto_listen()` / `_grant_listen_request()` | 接收方向只等对方请求 | 收到无路由的流时按本机「智连」排队，由维护线程建 `auto-listen-*` 路由，不等 `want_listen` | `TestPeerLivenessAndAutoListen`、`TestAutoListenRevoke` |
| 2.6-4 | 监控窗无限开、重复开 | `audiomixing_launcher.py` 的 `_open_monitor()` / `_wait_for_monitor_window()`；`tray.py` | ① `FindWindowW` 精确匹配「Audio Mixing 监控」，标题一变就失配；② 服务刚就绪、窗口未出现时多个双击各开一个 Edge 窗；③ 托盘双击绕过启动器互斥体；④ Edge 已拉起但没识别出来又 `webbrowser.open` 补一个 | 监控窗前缀匹配任意面板页；跨进程 `AudioMixing-Monitor-Open` 互斥体串行化"检查→打开→等窗口"；`panel-profile` Edge 进程兜底；Edge 已拉起就不再退回浏览器；托盘统一走启动器注入的 `open_monitor` | `TestMonitorWindowReuse`、`TestTrayOpensAWindow` |

### v2.7（2026-10-26）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.7-1 | 部分 Intel 电脑蓝牙没声音 | `audio/backends/wasapi.py` 的 `_find_endpoint()` / `_a2dp_registry_endpoint_id()`；`engine.py` 的 `_bt_phone_endpoint()` / `_live_channel_sources()` | ① 部分 Intel 无线蓝牙驱动"连接正常、媒体端点 active，但系统不出声"；② A2DP SNK 在 MMDevices 注册表里 `DeviceState=1`，但 WASAPI `DEVICE_STATE_ACTIVE` 枚举不返回它，按名字采集直接失败；③ 旧自动收编写的是手机短名，解析不到完整端点名 | 蓝牙卡片新增「本机播放」：把完整 A2DP SNK 端点加进音源并开本机直通；枚举找不到时按注册表 GUID 拼 `{0.0.1.00000000}.{guid}` 直接 `GetDevice`；`_local_capture_names()` 纳入注册表 active 端点名；诊断写 CPU/蓝牙适配器/Intel 已知问题；UI 提示"听不到就点本机播放" | `TestBluetoothAddress`、`TestBluetoothBridge`、`TestNetworkSelfCheckAndBluetoothRow` |

### v2.8（2026-10-27）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.8-1 | AMD 机器的诊断仍写「Intel 适配」，诊断永远"未知" | `webui.py` 的 `diagnose()`；`bluetooth.py` 的 `radio_name()` | `diagnose()` 后半段 `from . import bluetooth as bt` 让 Python 把整个函数的 `bt` 当局部变量，开头 `bt.radio_name()` 抛 `UnboundLocalError` 被 `except` 吞掉；`radio_name()` 取 `Get-PnpDevice -Class Bluetooth` 第一个 Status=OK，常拿到协议服务 | 改用模块级 `bluetooth`；适配段改「适配（CPU / 蓝牙适配器）」，AMD 也写清；`radio_name()` 优先读 PnP 注册表（Bluetooth 类 GUID / `Service=BTHUSB`），PowerShell 兜底不含中文、空结果重试 | `TestNetworkSelfCheckAndBluetoothRow` |
| 2.8-2 | 诊断只能全选复制，要一键诊断包 | `webui.py` 新增 `/api/diagnose/bundle` | 只有文本 | 一次下载 zip（诊断报告、环境、运行日志、build/status/config、设备与蓝牙端点、launcher.log），诊断页/控制台/监控页/托盘都可导出 | 诊断包相关测试 + 成品冒烟 |
| 2.8-3 | 其他终端连上后没声音，切换 Windows 输出才恢复 | `engine.py` 的 `_apply_routes_once()` / `_sink_device_still_valid()`；`audio/backends/wasapi.py` 的 `WasapiSink`；`webui.py` `/api/audio/recover` | 选择器仍是"系统默认"时保留旧端点；用户切换默认输出后播放端还绑旧设备，旧端点 audio client 僵死（`padding` 恒满、`write` 收 0 帧） | 隐式默认输出改为跟随当前默认；渲染线程每秒看 `frames_submitted`，连续 3 秒不增长就自动关闭并重建 audio client；新增 `/api/audio/recover` 与「恢复声音」按钮/托盘项；诊断写自动重开次数/原因 | `TestAudioRecovery`、`TestWedgedCapture` |

### v2.9（2026-10-27）—— 这是复刻项目的基线

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.9-1 | 网络自检把几百条旧规则铺在一行；旧规则还能让"当前 exe"显示通过 | `netcheck.py` 的 `netcheck()`；`tray.py` 的 `add_firewall_rules()`；`webui.py` 诊断/监控展示 | ① `listed = "；".join(...)` 拼全部规则；② `tcp/udp` 只看"历史里任意一条"，不校验 Program 是否当前 exe；③ 没校验 Enabled / Profile；④ `add_firewall_rules()` 只删"程序文件已不存在"的规则，旧版本 exe 还在就永远清不掉 | 合并为「入站规则（当前程序）」：当前 `tray._real_exe()` + Inbound + Allow + Enabled + 当前 Profile；历史规则按 (名称, 路径) 聚合只回 total/unique_paths/missing_files/disabled/最多 5 条 examples；`add_firewall_rules()` 先删所有 `Audio Mixing*`/`WaveLink*` 旧规则再重建 TCP/UDP 入站+出站；文案改「放行并清理旧规则」 | `TestFirewallScriptCleansUpOldRules`；v2.9 新增的多版本聚合/旧规则不判通过/Profile 覆盖/清理四条回归 |

### v2.10（2026-10-30）—— 基于 v2.9 必须补

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.10-1 | 端口冲突 / 多人局域网冲突，要求自定义端口且只出现在「版本号」页 | `webui.py` 的 `set_settings()`；`audiomixing_launcher.py` 的 `_configured_web_port()` / `_running_status()` / `_take_over()` / `_existing_node_address()` / `_main()` | 面板 17800 / 音频 47831 / 发现 47830 全部写死；启动器多处写死 17800，改端口后第二次双击会去错端口、误报"没有实例在跑" | ① 左栏「版本号」改名「设置」，该页加端口卡片（面板/音频/发现 + 保存）；② 校验 1024–65534、三者不重复、面板+1（手机长连接口）不撞另两个；只落盘并返回 `restart_required`，不假装生效；③ 启动器全部按配置端口走 | `TestCustomPorts`；`verify_exe` / `selftest_twonode` |

### v2.11（2026-10-30）—— 基于 v2.9 必须补

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.11-1 | 任意网页可跨域读接口/下载诊断包/调用写接口 | `webui.py` 的 `_Handler` 响应头、`_origin_ok()` | 所有响应带 `Access-Control-Allow-Origin: *` | 删掉全局 ACAO；POST 校验 `Origin` 必须同源（无 Origin 的手机 App/curl 照旧） | `TestHttpSecurity` |
| 2.11-2 | `<img src=.../api/firewall>` 就能弹 UAC | `webui.py` 的 `/api/firewall`、`/api/audio/recover` | 只收 GET | 只收 POST；GET 返回 405 | `TestHttpSecurity.test_firewall_get_is_rejected` |
| 2.11-3 | 设备名/id 可注入页面脚本；`/plain`、`phone.html?name=` 反射 | `ui/index.html` 的 `jsArg()`；`webui.py` `/plain`；`ui/phone.html` 的 `kv()` | 设备名拼进单引号 `onclick`，`esc()` 不转单引号；`/plain` 反射节点名；`phone.html` 用 `innerHTML` | 新增 `jsArg()`（JSON.stringify + HTML 转义）替换全部单引号字面量；`/plain` 用 `html.escape`；`kv()` 改 `textContent` 并截断名字 | `TestHttpSecurity`；页面脚本检查 |
| 2.11-4 | 手机音源可被刷内存（名字/采样率无上限、槽位 900 秒回收） | `webui.py` `/api/mic/stream`；`audio/backends/netmic.py` | 无长度/速率上限；槽位回收慢 | 名字截 64、采样率限 8000–192000、最多 32 个槽位（满了先回收再拒绝）；HTTP 每连接 30 秒超时、最多 64 条并发（超了直接关） | `TestNetMicLimits`、`TestHttpSecurity` |
| 2.11-5 | 端口"保存成功"可能是假的 | `config.py` 的 `Config.save()` | 建目录/写 tmp 失败被静默吞掉 | `save(path=None, strict=False)`；`strict=True` 如实抛 `OSError`，启动仍兼容静默 | `TestCustomPorts` / `TestHttpSecurity` |
| 2.11-6 | 自定义端口被网络自检当故障；CLI 端口参数写回配置 | `netcheck.py`；`cli.py` | 把非 17800 当问题并"回退 17800"；CLI 参数顺手 `save()` | 自定义端口只 INFO；删除回退逻辑；端口参数不写回配置 | 网络自检/CLI 相关测试 |
| 2.11-7 | 蓝牙每次连接往 `%TEMP%` 丢删不掉的 `.ps1`；审计工具崩；Server 头泄露 Python 版本 | `bluetooth.py` helper 脚本；`tools/audit_leftovers.py`；`webui.py` Server 头 | 临时文件不在清理清单；审计扫 `promo/版本备份/release/dist_nuitka/node_modules` 会崩；默认 Server 头带 Python 版本 | helper 改存状态目录 `key.ps1` 并随连接清理；审计跳过第三方/备份目录并容忍单文件 OSError；Server 头只留 `AudioMixing`，加 `nosniff / SAMEORIGIN / no-referrer` | `audit_leftovers`（0 error）；`TestHttpSecurity` |

### v2.12（2026-10-30）—— 基于 v2.9 必须补

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.12-1 | 手机 APK 还是旧版本；扫码下载到的还是旧包 | `android/app/build.gradle`；`tools/build_nuitka.py`（内嵌 APK）；`release/GOGO/` | ① Android 仍是 versionCode 15 / versionName 2.1；② PC EXE 内置的是打包那一刻 `release\GOGO` 里的旧 APK，只换 release 文件不重打 EXE；③ 桌面旧进程占着 exe | `versionCode 16 / versionName 2.2` 并重新 `assembleDebug`；替换 6 处 APK 副本；PC 升 2.12 重打包，内置 APK 换 2.2；结束占用进程，桌面只留 v2.12 | `check_apk`、`verify_exe` 13 项、`selftest_twonode` 19 项；成品实测 `/app/AudioMixing-mic.apk` 哈希与 APK 一致 |

### 手机端 v2.1（v2.2 之前；基于 v2.9 的项目很可能仍是这一版）

| # | 问题 | 位置 | 原因 | 修复 | 回归 |
|---|---|---|---|---|---|
| 2.1-phone-1 | 断联超过 2 分钟的陈旧设备卡/提示堆在程序里 | `Discovery.java` 的 `peers()` / `sweep()` / `rxLoop()`；`LinkPolicy.java`；`Engine.java` 的 `collectSendTargets()` | ① `peers()` 返回全部条目，无新鲜度过滤；② 过期清扫 `sweep()` 挂在 `rxLoop()` 里，而 `rxLoop` 在发现 socket 为 null 时整段 `continue`，把 `sweep` 跳过了 | `LinkPolicy` 加 `CARD_STALE_MS=120000` + 纯函数 `cardStale(ageMs)`；`Engine.peers()` 过滤；`collectSendTargets()` 清掉陈旧送声目标与"已停止向…发送"提示；修好 `rxLoop` 里被跳过的 `sweep` | 策略门禁 72 项（含 6 条 `cardStale` 断言）；7 组离线探针 |
| 2.1-phone-2 | 手机自报版本号写死成 `1.0.0`，电脑设备卡片显示不准 | `android/.../Discovery.java` 的 `APP_VERSION`；`android/app/build.gradle` | `APP_VERSION` 是写死的字符串，没读 `BuildConfig` | 改读 `BuildConfig.VERSION_NAME`；打开 `buildConfig true`；`versionCode 15 / versionName 2.1` | 信标探针 28 项 |

> 手机端 v2.2 只是版本号 2.1 → 2.2、重新编译，功能代码与 v2.1 一致，没有独立 bug 修复。

---

## 每次更新的填写模板（复制到本文末尾）

```markdown
## BUG-YYYYMMDD-XX 一句话标题

**现象**
（用户原话 / 看到什么）

**位置**
- `文件路径`
  - `类/函数/方法`（约 Lxxx-Lxxx）

**原因（代码级）**
1. ...
2. ...

**修复**
（关键判断贴代码，3~10 行）

**回归**
- `测试文件` → `测试名`
- 全量：N 项通过
- 真机：已确认 / 待确认

**兼容性 / 生效条件**
（旧对端还能不能用；哪一端必须重新打包才生效）
```

---

## BUG-20261103-01 幽灵设备一直留在设备图 / 旧身份路由

**版本**：PC 2.20 / 2026-11-03（手机端不需要改）

**位置**
- `audiomixing/net/discovery.py` → `DiscoveryRegistry.update()` / `snapshot()` / 新增 `prune()` / `ever_seen()`
- `audiomixing/config.py` → `Config.load()` / 新增 `_is_synthetic_node_id()`
- `audiomixing/engine.py` → `tick()` / 新增 `_refresh_route_staleness()`、`routes_json()`

**现象**
设备图里 `xxxxxxxx-xxxx-…`（不在线/不可达）、`<旧手机名>`（192.168.0.100）一直挂着；本机已经换到别的网段，它们仍然不消失。手机重装/改 id 之后，路由还指着一个永远对不上的旧 id。

**原因**
1. `DiscoveryRegistry._peers` 只增不减：`update()` 只写新值，`snapshot()` 原样返回整张表，没有任何过期淘汰。手机重装换了 node_id，旧身份就永远留在列表里。
2. 用户配置被测试/联调污染过：`%APPDATA%\AudioMixing\config.json` 里 `node_id = "self-node"`、`smart_link = ["peer-1"]`。用假身份建出来的路由，端点天然对不上真实设备。
3. 路由端点找不到设备时没有 UI 态，前端只能按 id 现造一个"离线/不可达"节点画出来。

**修复**
- 发现表新增 `prune(ttl_s)`：超过 90 秒没有音讯的**自动发现**设备从界面列表移除；`manual`（用户手动添加）永不淘汰。另有 `_ever` 集合记录"这台进程里见过"，`ever_seen()` 供路由清理使用。
- `_route_is_orphan()` 改成 `get() or ever_seen()`：**不能**因为发现表刚把一台关机的真设备淘汰掉，就把它的路由当成"从没见过的假设备"删掉。
- `Config.load()` 识别 `self-node` / `node-self` / `peer-*` / `selftest-*` 等只会由测试生成的 id：换成真 uuid，并把路由里的旧 token 原地改写、清掉测试留下的 `smart_link`。**一条路由都不删**。
- 引擎每 5 秒刷新路由状态：端点持续 60 秒在当前发现表里找不到 → `stale=true`。前端关系图跳过 stale 路由，幽灵节点/幽灵边不再画；路由仍在配置和路由页里，设备回来自动恢复。

```python
if (self.registry.resolve(token) is not None
        or self._is_manual_peer(token)):
    continue
# 找不到 → _unknown_route_since 计时；满 ROUTE_STALE_AFTER_S 后只打 stale 标记
```

**回归**
- `Test20261103GhostsAndStats.test_a_forgotten_peer_leaves_the_list_but_stays_ever_seen`
- `test_a_manual_peer_is_never_pruned`
- `test_a_route_to_a_vanished_peer_is_flagged_stale_not_deleted`
- `test_a_synthetic_test_node_id_is_migrated_without_losing_routes`

**兼容性 / 生效条件**
路由格式不变，老对端无影响。只有电脑端要装 2.20；用户现有 `self-node` 配置在第一次启动 2.20 时自动迁移。

---

## BUG-20261103-02 监控数据自相矛盾（速率 0 / 状态写死 / 目标 25ms 缓冲 60ms / 条与文字不一致）

**版本**：PC 2.20 / 2026-11-03

**位置**
- `audiomixing/net/rtpaudio.py` → `Receiver.stats()`、`SenderSession.stats()`、`JitterBuffer.stats()`、`JITTER_TRIM_SLACK_MS`
- `audiomixing/webui.py` → 路由监控行（`state` / `rate_kbps` / `latency_ms`）
- `audiomixing/ui/index.html` → `deviceFlow()` / 设备卡「收/送」渲染 / `relBuild()`

**现象**
图3：路由行写「正在接收 / 数据量 9.2 MB」，但「速率 0 KB/s」；「缓冲 60.0 毫秒」而「目标 25.0 毫秒」，同时堆着裁剪/迟到/溢出/重同步计数。图2：本机卡片「送 没有在送」但电平条是亮的；对端「信号 93 分·在线」，`送` 在跑而 `收 没有在收`。

**原因**
1. **显示层（速率永远 0）**：`SenderSession` 有 `kbps`，`Receiver.stats()` 从来没算过这个字段；前端 `stream.get("kbps")` 永远拿到 `None` → `0.0`。而「数据量」是 `info.bytes` 累计值，所以看起来"有流量但 0 速率"。
2. **显示层（状态写死）**：`webui.py` 的路由行 `"state": "正在接收"` 是常量，只要还有 receiver stream 对象就写"正在接收"，不看最近一次收到音频的时间。
3. **显示层（统计口径混用）**：累计字节/计数器与"每秒速率"混在一行且没有标注；用户自然觉得互相矛盾。
4. **显示层（条/文字不同源）**：`outPct/inPct` 用 sender/receiver 的 `peak`，而断流后 `peak` 只会在下次发包时衰减，于是冻在旧值；文字又用 `kbps`，两者出现"条亮着却写没有在送"。
5. **数据面（目标缓冲失真）**：自适应在无欠载时把 `target_ms` 一路压到下限 25ms，但 trim 阈值是 `target + JITTER_TRIM_SLACK_MS(60)` = 85ms；实际深度 60ms **永远低于阈值**，于是显示目标 25ms、缓冲永远 60ms。`target` 实际上没有生效。

**修复**
- `Receiver.stats()` 增加 `kbps`：两次 status 之间真实收到的字节增量 / 时间；`dt < 0.2s` 时沿用上次结果，避免同一秒被轮询两次放大成天文数字。流消失时清掉速率缓存。
- `JitterBuffer.stats()` 增加 `seconds`（统计窗口起点）、`receiving`（最近 3 秒有没有真的收到包）、`age_s`。
- `SenderSession.stats()` 的 `kbps` 改成实时窗口（原来拿整个会话平均值）；新增 `sending`；只有"发过、又停了超过 1.5 秒"才把 `peak` 清零。
- `webui.py` 路由行状态改为 `receiving ? 正在接收 : 已停止（N 秒无音频）`；断流时 `latency_ms=None`（残留 depth 不再显示）；明细开头写清"自开始 N 秒（字节/计数器均为累计）"。
- `index.html`：`flows.in_receiving/out_sending` 由服务端真实状态决定；条子和文字同一个来源；非 live 时百分比为 0。
- `JITTER_TRIM_SLACK_MS` 60 → 20、`JITTER_TRIM_STEP_MS` 20 → 10：target 压到 25ms 时阈值 45ms，实际深度会跟着收敛，同时仍留 20ms 吸抖动。

**回归**
- `Test20261103GhostsAndStats.test_receiver_reports_a_live_rate_instead_of_always_zero`
- 既有 `TestOutputLevelAndBluetoothCleanup.test_the_sender_reports_how_loud_the_outgoing_sound_is`
- 既有 `TestStopsSendingWithoutThePanel.test_the_engine_stops_by_itself_when_the_peer_vanishes`

**兼容性 / 生效条件**
协议与音频数据面不变，只改统计字段和 UI；只有电脑端需要 2.20。

---

## BUG-20261103-03 手机媒体/App 音量不影响电脑端耳机

**版本**：手机 2.10 + PC 2.20 / 2026-11-03

**位置**
- Android `Engine.java` → `sendStatsRound()` / 新增 `mediaVolumeRatio()`
- Android `Diagnosis.java` → `json(..., mediaVolume)` 重载
- PC `audiomixing/engine.py` → `_handle_peer_requests()` / 新增 `_peer_media_volume()` / `_apply_routes_once()` 设置 `engine.gain`

**现象**
手机按音量键或拖音量面板里的「App 音量」，电脑耳机里听到的手机声音大小不变。早期版本有反应，2.19 没有实现。

**原因**
手机端只把媒体音量显示在诊断里（`systemVolumeText()`），从未把当前音量值放进统计包；电脑端自然不知道手机的音量层。手机采集/发送的是 PCM，不经过本机 `AudioTrack` 的媒体音量处理，所以系统音量对"送出去的声音"没有作用。

**修复**
- 手机端每次统计（2 秒一轮）读 `AudioManager.getStreamVolume(STREAM_MUSIC) / getStreamMaxVolume(STREAM_MUSIC)`，归一化成 `vol ∈ [0,1]`，作为 T_STATS JSON 的新键 `"vol"` 上报。**只读，仍然不调用 `setStreamVolume`**；读取失败返回 1.0，绝不会因为读音量失败把对端变静音。
- 电脑端在 `_handle_peer_requests()` 里记录 `node_id → (vol, 收到时刻)`，30 秒有效（丢几包不失效）。
- `_apply_routes_once()` 写播放端增益时：`engine.gain = rule.gain * peer_media_volume(rule)`。`vol ≤ 1`，所以有效增益只会**小于等于**用户在「我听它的」里设的上限；路由里的 `gain` 不写回，两端滑杆/共享增益回声逻辑完全不动。
- 老手机不发 `vol`，或超过 30 秒没再报 → 按 1.0，行为与 2.19 完全相同。协议只多一个 JSON 键，包头/包类型不变，因此 `tools/check_android_proto.py` 无需改动。

```python
# engine.py：只衰减、不越上限
engine.gain = float(rule.get("gain", 1.0)) * self._peer_media_volume(rule)
```

**回归**
- `Test20261103GhostsAndStats.test_the_listening_gain_is_attenuated_by_the_phone_volume_only`
- Android 离线门禁七条全 PASS；`check_apk.py` PASS（2.10 / versionCode 24）

**兼容性 / 生效条件**
- 必须**重新安装手机 APK 2.10** 才会发 `"vol"`；只升级电脑 2.20 不会让老手机的音量生效。
- 电脑 2.20 收老手机（2.9 及以前）的统计包：没有 `vol` → 1.0，不改变任何听感。
- 手机 2.10 连老电脑：`"vol"` 被当未知键忽略，不报错。
- 已知限制：部分 ROM 的「App 音量」是系统分应用音量，`getStreamVolume(STREAM_MUSIC)` 未必返回这个应用那一档；若真机上仍不生效，需要用户提供音量面板截图 + `adb shell dumpsys audio` 再按 ROM 适配。

---

## BUG-20261103-04 审计工具假 PASS：测试身份写在配置里却报"无残留"

**版本**：PC 2.20（补完）/ 2026-11-03

**位置**
- `tools/audit_leftovers.py` → 新增 `identity_findings(cfg)`（第 67 行），`audit_environment()` 调用它

**现象**
用户配置里 `node_id="self-node"`、`smart_link=["peer-1"]`、路由 `origin="self-node"` 都还在，
`python tools\audit_leftovers.py` 却打印 `[ok] config no test peers/routes`。
排查幽灵设备时被这条假 PASS 误导，以为配置是干净的。

**原因**
旧审计只扫 `manual_peers` 的测试网段和路由 id 的 `test-`/`sync-` 前缀；
身份字段（`node_id` / `smart_link` / 路由 `origin`、`from`/`to`）根本没读。
测试工具自身的盲区比产品 bug 更危险：它让"修好了"变得不可证伪。

**修复**
- `identity_findings()` 用与运行时同一份 `_is_synthetic_node_id()` 判定（不在工具里维护第二份清单，
  避免清单漂移），命中 `self-node`、`peer-1`、`node-self`、`selftest-*` 等一律 WARN。
- **只 WARN，不自动删**：改身份=改用户设备身份，改 smart_link=改用户勾过的设备，
  审计工具没有资格替用户决定。启动时由 `Config.load()` 备份后迁移（见 BUG-20261103-06）。

**回归**
- `TestAuditFindsSyntheticIdentities.test_synthetic_node_id_and_smart_link_are_reported`
- `TestAuditFindsSyntheticIdentities.test_real_uuid_and_real_devices_stay_quiet`

---

## BUG-20261103-05 面板"本机"显示 Hyper-V 虚拟网卡 172.26.192.1

**版本**：PC 2.20（补完）/ 2026-11-03

**位置**
- `audiomixing/webui.py` → `WebApi.peers()`（第 707/718 行）、新增 `_best_local_address()`
  （第 3872 行）、`_lan_host()`（第 3926 行）

**现象**
本机在 192.168.0.111、手机在 192.168.0.100，面板和二维码却把本机地址显示成 172.26.192.1，
手机按这个地址连不上。

**原因**
`peers()` 直接取 `lan_report()["interfaces"][0]`，没有任何"哪块网卡能连到对端"的判断；
Windows 上 Hyper-V 默认交换机（或 WSL）的地址常常排在真实网卡前面。

**修复**
- 选择顺序固定为：① 与**已知对端**（发现表 + manual_peers）同 /24 的地址；
  ② 私网网卡排序（192.168 → 10. → 172.16~31 最后）；③ 全离线才退回 127.0.0.1。
- `_lan_host()` 改为复用同一个函数，面板/二维码/手机直连不再各算一套。
- 不改端口、不改协议，只换本机上报的地址来源。

**回归**
- `TestWebApi.test_self_host_prefers_the_subnet_a_known_peer_is_on`
- `TestWebApi.test_self_host_falls_back_to_the_wired_private_range_without_peers`
- `TestWebApi.test_the_peers_payload_does_not_report_a_virtual_adapter`
- 既有 `test_the_phone_listen_address_skips_virtual_adapters`

---

## BUG-20261103-06 假身份迁移前没有备份（数据不可回退）

**版本**：PC 2.20（补完）/ 2026-11-03

**位置**
- `audiomixing/config.py` → `Config.load()` 的合成身份迁移块（第 207 行起）、
  新增 `_backup_before_migration()`（第 84 行）

**现象**
`self-node`/`peer-1` 会被自动迁成真 UUID，但用户看不到、也回退不了；
如果迁移逻辑某天判断错，用户的路由/身份就被永久改写。

**原因**
迁移直接 `cfg.save(p)` 覆盖原文件，没有留原件；这违反"动用户数据前先备份"的交付纪律。

**修复**
- 迁移前 `shutil.copy2` 出 `config.json.bak-<YYYYmmdd-HHMMSS>`（同秒冲突加 `-2/-3`，不覆盖旧备份）。
- **备份失败就不迁移**：打印原因到 stderr、原配置原样保留 —— 宁可这一轮仍是假身份，也不在无法回退时改写。
- 只改 `node_id`、`smart_link` 里的合成项和路由里指向旧身份的端点；**不删任何路由**。

**回归**
- 既有 `Test20261103GhostsAndStats` 的合成身份迁移测试新增断言：备份恰好一份、内容是迁移前原样、
  路由数量不变（`config.json.bak-*`）。

---

## BUG-20261103-07 「迟到」被读成丢音 + 自适应不看迟到/溢出

**版本**：PC 2.20（补完）/ 2026-11-03

**位置**
- `audiomixing/net/rtpaudio.py` → `JitterBuffer._adapt()`（第 402 行）、`read()` 首次播放重置（第 300 行）
- `audiomixing/webui.py` → 路由明细（第 1293~1298 行）、流详情（第 1808 行）
- `audiomixing/ui/index.html` → 流状态文案（`最低 ${x.target_ms}ms`）

**现象**
面板同时显示「目标 25.0 毫秒 / 缓冲 60.0 毫秒 / 迟到 4670 / 溢出 177120」，看起来自相矛盾；
用户把「迟到」理解成丢音，数字越多越像故障。

**原因**
- `dropped_late` 的真实语义是"整包到得晚、样本仍会播"，不是丢样本；旧文案「迟到」和「丢包」同义。
- `_adapt()` 只看 `underruns`：目标压到 25ms 后，即使 90% 的包都晚到也不会涨，目标值变成一句空话。
- 溢出/迟到计数从对象创建就开始累计，包含了"还没开始播放"的预滚阶段，永久挂在面板上。

**修复**
- 文案统一「晚到包」，并在诊断里写明"整包到得晚、样本仍会播，不代表丢音"；
  「目标」改为「最低」（它是下限，实际会按网络抖动上调）。
- `_adapt()` 每秒计算 `dropped_late` / `dropped_overflow` 的**增量**：
  晚到包比例 > 10% 时目标 +5ms，> 25% 时 +10ms，封顶 `JITTER_MAX_MS`；
  **溢出这一秒不涨目标**（队列已经太深/播放端停顿，涨只会更糟），只消费增量并走慢速收缩。
- 首次真正播放（`_primed` 置真）时把 `dropped_late` / `dropped_overflow` 及其增量基线清零一次。

**回归**
- `TestJitterBuffer.test_late_packet_ratio_above_ten_percent_raises_the_target`
- `TestJitterBuffer.test_a_handful_of_late_packets_does_not_inflate_the_target`
- `TestJitterBuffer.test_overflow_never_raises_the_target`
- `TestJitterBuffer.test_pre_roll_overflow_does_not_stay_on_the_panel`
- `Test20261103GhostsAndStats.test_late_packet_wording_does_not_imply_dropped_audio`

---

## BUG-20261103-08 交付 APK 仍是 2.9，check_apk 只查 debug 构建

**版本**：手机 2.10 / PC 2.20（补完）/ 2026-11-03

**位置**
- `tools/check_apk.py` → `main()` 新增 `--release` 门槛、`_manifest_has_version()`、`_sha256()`

**现象**
源码已经 2.10 且含 `mediaVolumeRatio`，但 `release\GOGO\AudioMixing-手机版.apk`、
`release\AudioMixing-mic.apk`、`tools\installer_payload\AudioMixing-mic.apk` 和 exe 内嵌那份
仍是 2.9 的旧包；用户装上后手机音量仍然不会联动电脑耳机。

**原因**
- 旧 `check_apk.py` 只检查 `android/app/build/outputs/apk/debug/app-debug.apk`，
  交付目录里的副本根本没被检查，版本号一致性没有任何门禁。
- 手工复制六处时漏掉任意一处，脚本也照样 PASS。

**修复**
- `--release` 模式新增三项硬门槛：`versionName == 2.10`（直接读 APK 二进制清单，两种字符串编码都查，
  不依赖 aapt2）、dex 含 `mediaVolumeRatio`、交付 APK 与 `installer_payload` 的 sha256 一致
  （`release/GOGO` 与 `release` 两份都查）。
- 重新构建 APK 2.10 并同步六处；exe 重新打包后内嵌的就是新 APK。

**回归**
- `TestApkReleaseGate.test_the_debug_apk_is_version_2_10_and_has_the_volume_symbol`（2.9 必须被判 False）
- `TestApkReleaseGate.test_delivery_copies_have_the_same_hash_as_the_installer_payload`
- 手工：`python tools\check_apk.py --release` → 三门槛全过。

---

## BUG-20261104-01 晚到包 597844 / 597850 ≈ 100%：时钟同步按 node_id 查、样本却记在 IP 名下

**版本**：PC 2.20（不升号）/ 2026-11-04

**位置**
- `audiomixing/net/rtpaudio.py`
  - `Receiver._handle_audio()`（原来只按 `info.node_id` 查 `self._clock`）
  - `Receiver._clock_for()`（新增：node_id → IP 回退）
  - `JitterBuffer.write()`（新增 `count_late` 参数）
  - `JitterBuffer._adapt()`（欠载率 + 收缩迟滞）
  - `JitterBuffer.stats()`（`late_pct` / `underrun_pct`）
  - `Receiver.stats()`（`clock_ready` / `clock_ms` / `clock_samples`）
  - `Receiver.clock_stats()`（同一个 ClockSync 按对象去重）
- `audiomixing/webui.py`（路由明细 + 诊断报告文案）
- `audiomixing/ui/index.html`（`deviceFlow()` 渲染「缓冲/目标」）

**现象**
监控页同一行：状态「正在接收」、自开始 5960.9 秒、缓冲 116.0ms / 「最低」150.0ms、
晚到包 595675、欠载 2934、裁剪 435360、重同步 1。数字互相矛盾，看起来像每个包都迟到
25~150ms，但声音是连续的。

**原因（用诊断包里的数证明）**
- `status.json`：`packets=597850`、`late=597844`、`lost=404`、`overflow=0`、`resyncs=1`
  → 99.999% 的包被判晚到，真正的序号缺口只有 404 个（0.07%）。这不可能是真链路。
- `receiver.clocks` 同时有两条：
  - `{"peer":"605a4964-...","samples":0,"offset_ms":0.0}`
  - `{"peer":"192.168.0.100","samples":8752,"offset_ms":-1173.839}`
- `_handle_audio()` 用 `info.node_id` 查时钟；手机的统计/音频包能解析出 node_id，
  但 PONG 里没有 `node` 字段时 `T_PONG` 分支按 `addr[0]` 建 ClockSync（见 `_handle`）。
  于是音频流拿到 `samples=0` 的那条 → `offset=0` → `capture_local_ns` 其实还是手机墙钟，
  比电脑慢 **1.17 秒** → `desired = ts + target` 比 `now` 早 1.1 秒 → 除开头 6 个包外全部
  `desired + 10ms < now` → `dropped_late` 全加 1。
- `late` 只计数、不丢样本（`write()` 里样本照常进缓冲），所以听感正常、数字却爆表。
- 目标被钉在 150ms 的原因：旧 `_adapt()` 判 `underruns > 0` 就涨 5ms。现场
  `underruns=2945 / 597850 包 ≈ 0.5%`，但按"这一秒出现过一次"算，几乎每秒都涨 →
  一路顶到 `JITTER_MAX_MS=150`。欠载并没有因此消失。
- 缓冲 116 < 「最低」150 不是数据面问题：`target` 是**目标**，实际队列围绕它浮动，
  旧文案把它写成下限，才产生"低于下限"的假矛盾。

**修复**
```python
# Receiver._clock_for()
cs = self._clock.get(node_id)
if cs is not None and cs.ready:
    return cs
alt = self._clock.get(addr[0])          # 同一台手机的 PONG 可能只按 IP 记
if alt is not None and alt.ready:
    self._clock[node_id] = alt          # 让 stats / 下一次查询也能看见真实偏移
    return alt
```
```python
# _handle_audio()
cs = self._clock_for(node_id, addr)
...
buf.write(samples, pkt.ts_ns, offset, ..., pkt.seq, count_late=cs.ready)
# write() 内部：if count_late and self._primed and desired + n < now: dropped_late += 1
```
```python
# _adapt()：涨要快、收要慢，防震荡
underrun_ratio = (underruns / float(reads)) if reads >= 1 else 0.0
grow_needed = overflow == 0 and (underrun_ratio > 0.02 or late_ratio > 0.10)
step = 10.0 if (underrun_ratio > 0.05 or late_ratio > 0.25) else 5.0
...
if underruns == 0 and late == 0 and overflow == 0:
    self._clean_s += 1
if self._clean_s >= 5 and target_ms > JITTER_MIN_MS:
    self.target_frames = max(_min_frames, int(self.target_frames * 0.98))
```
文案：`目标 %s 毫秒（不是下限）`、`缓冲(当前)=116ms / 目标=150ms`；
时钟未锁定时显示「时钟未同步（晚到不计数）」，不再给用户一个永远看不懂的计数器。

**回归**
- `Test20261104ClockAndJitter.test_a_stream_uses_the_clock_learned_under_the_ip_address`
- `Test20261104ClockAndJitter.test_unsynced_packets_are_not_counted_as_late`
- `Test20261104ClockAndJitter.test_a_few_underruns_do_not_pin_the_target_at_the_max`
- `Test20261104ClockAndJitter.test_the_target_shrinks_only_after_five_clean_seconds`
- `Test20261103GhostsAndStats.test_late_packet_wording_does_not_imply_dropped_audio`（文案改为「目标（不是下限）」）

**仍然证明不了的**
- 真丢包 404/597850、溢出 0、重同步 1：诊断包里没有"发送端 burst"或"每个包都晚到"
  的证据，因此**没有**改丢包 conceal / 重排窗口 / 发送节拍（避免盲调）。
- 目标值在新逻辑下会从起始值按"欠载率"决定：现场 0.5% 不会涨；若用户在真机上看到
  `欠载%` 持续 >2%，它会 5ms/秒地涨，属于预期闭环。

---

## BUG-20261104-02 手机音量仍无效：诊断包显示手机跑的是 2.9，`vol` 从未到电脑

**版本**：PC 2.20（不升号）/ 手机 2.10（不需要再出包）/ 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._peers_with_media_volume()`（新增）、`status()`
- `audiomixing/webui.py` → 诊断报告「对端」一行

**现象**
用户装了新 APK 后复测，手机音量仍然不影响电脑耳机。

**原因（诊断包证据，先排除代码）**
- `status.json` 的 `peers[0]`：`name="123"`、`host="192.168.0.100"`、`version="2.9"`、
  `platform="Android-17"`。也就是说 23:57 那一刻手机跑的还是 **2.9**，不是 2.10。
- 2.9 的 `Engine.sendStatsRound()` 不调用 `Diagnosis.json(..., mediaVolumeRatio())`，
  stats 里没有 `vol` → 电脑 `_peer_volume` 为空 → `_peer_media_volume()` 返回 1.0 →
  `status.sinks.gain = 0.49` 与路由的「我听它的」0.49 完全相等（没有被音量乘过）。
- 代码链路本身是闭合的（只在 2.10 才有输入）：
  `Diagnosis.json(..., vol)` → `Engine.sendStatsRound()` → `_handle_peer_requests()`
  → `_apply_routes_once()` → `engine.gain = rule.gain * vol`。tick 每轮都会重新 apply，
  所以 vol 变化最多 2 秒就能落到增益上。
- 分发侧有一个很可能的坑：`C:\Users\75341\Desktop\31\audio更新\01-软件\AudioMixing-手机版.apk`
  仍是 158,103 B（2.9），而 `release` 六处是 158,395 B（2.10）。用户如果从旧目录装，
  装到的就是 2.9。该目录本轮是只读，不能替用户改。

**修复**
不改协议、不改手机代码（2.10 已能发 `vol`）。只把"证据"放到用户能直接看到的地方：
```python
# status(): 之前是自读 snapshot()，现在补上每台的音量上报状态
"peers": self._peers_with_media_volume(),
# 每台 peer 增加：
#   media_vol: 0.0~1.0 或 None（None = 老版本/没发过）
#   media_vol_age_s: 距最后一次上报多少秒（>30 秒即过期，增益回到路由值）
```
诊断报告「对端」一行现在会写：
`版本=2.9 手机音量=未上报（旧版不发 vol，或不是 2.10）`
装 2.10 后会变成 `版本=2.10 手机音量=0.50（1.2 秒前）`。

**回归**
- `Test20261103GhostsAndStats.test_the_listening_gain_is_attenuated_by_the_phone_volume_only`
  （新增断言：`status()["peers"]` 里 `media_vol==0.5`；过期后必须是 `None`）

**验收 / 还没解决的**
- 必须先让手机真正跑 2.10（重装 `release\GOGO\AudioMixing-手机版.apk`，或 exe 内嵌的
  `audiomixing/ui/AudioMixing-手机版.apk`），再用诊断包确认 `版本=2.10` 且
  `手机音量` 会随手机音量条变化。
- 若 2.10 已确认、`media_vol` 会变，但耳机音量仍不变：下一步才是"ROM 的 per-app 音量
  不体现在 `getStreamVolume(STREAM_MUSIC)`"或"采集点在音量之后"。那需要改**播放增益**
  （会影响手机外放音量，属于产品决策），按约束必须先停下来问用户，不在本轮动。

---

## BUG-20261104-03 手机音量在「广播→只听」下永远无效：通配路由查不到来源

**版本**：PC 2.20（不升号）/ 手机 2.10（源码未改、未重打包）/ 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._peer_media_volume_source()` / `_peer_media_volume_for_node()` / `_apply_sink_gain()` / `_apply_stream_volume()`
- `audiomixing/net/rtpaudio.py` → `JitterBuffer.source_gain`、`Receiver.mix_by_route()`
- `audiomixing/webui.py` → 诊断「播放端」新增「音量链」一行；「接收端」新增「来源音量」
- `audiomixing/engine.py` → `SinkEngine.describe()` / `_SinkFeed.describe()` 增加 `route_gain / media_vol / media_vol_source`

**现象**
用户说"已经换了新包"，但调手机视频 App 的媒体音量，电脑耳机音量仍然不变。

**原因（代码级）**
- 上一轮只覆盖了**具名路由**：`engine.gain = rule.gain * self._peer_media_volume(rule)`，
  而 `_peer_media_volume` 是遍历 `rule["from"]` 里的 token 去查 `_peer_volume`。
- 广播「只听」那条规则的 `from` 是 `["*"]`，函数第一行就跳过通配 token → **恒返回 1.0**。
  换句话说：只要声音是通过"广播→只听"进来的（也就是 Q2 里"点对点没被接受、却仍能听到"的
  同一条路），手机音量就永远乘不进去，和手机上装的是 2.9 还是 2.10 无关。
- 一个播放端（频道那条）里可能**同时混着多台来源**，所以也不能用"整条路由一个 vol"来解决——
  必须按**每条流**各自乘自己的来源音量。

**修复**
```python
# 具名路由：播放端增益 = 路由增益 × vol（用户要的那条式子）
def _apply_sink_gain(self, engine, rule):
    route_gain = float(rule.get("gain", 1.0))
    vol, vol_key = self._peer_media_volume_source(rule)   # 键对齐：token / node_id / host / IP 反查
    engine.gain = route_gain * vol
    engine.route_gain, engine.media_vol, engine.media_vol_source = route_gain, vol, vol_key

# 通配路由：播放端只乘路由增益，vol 按流乘（避免平方衰减）
for ssrc, info in receiver.streams.items():
    wildcard = 这条流挂的是不是 from=["*"] 的路由
    buf.source_gain = vol(info.node_id) if wildcard else 1.0
```
`mix_by_route` 在读出块之后乘一次 `buf.source_gain`。

**回归**
- `Test20261103GhostsAndStats.test_phone_volume_scales_the_final_sink_gain`（新增）：
  `vol=0.5`、路由 gain `0.8` → `sink.gain == 0.4`、`route_gain==0.8`、`media_vol==0.5`、
  `media_vol_source=="peer-1"`；vol 过期后回到 `0.8`。

**兼容性 / 还没证明的**
- 不涉及协议与手机：手机 2.9 不发 `vol`，按 1.0（行为不变）；2.10 才有效果。
- **仍未证明**：用户这次到底装的是不是 2.10。诊断包里的 `packs`/`version` 是唯一硬证据；
  若诊断页仍显示 `版本=2.9`，那就是安装/分发问题（`Desktop\31\...` 是 2.9 的 158,103 B），
  本轮代码再对也不会有变化。

---

## BUG-20261104-04 未接受的点对点单播能从广播「只听」听到：接收端没有准入

**版本**：PC 2.20（不升号）/ 手机 2.10（源码未改）/ 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode.routes_for_sender()`（回退通配的兜底）、
  新增 `_stream_is_accepted()` / `_accepted_routes_for()` / `_peer_sends_point_to_point()`；
  `_handle_peer_requests()` 记录 `want_listen` 声明
- `audiomixing/net/rtpaudio.py` → `Receiver._admitted()`（新增）、`_recv_loop` 计数前准入、
  `packets_denied`

**现象**
手机用点对点把声音送到电脑；电脑**没有开点对点的「接受」**（这台设备没开收听），
只开着广播的「只听」，结果仍能听到这台手机的声音。

**原因**
- 手机"送声音"是本地 `sendIds`，电脑端要"接受"必须自己建一条具名路由；
  没建时 `routes_for_sender()` 直接 `return specific or wildcard` —— **无条件回退通配**。
  于是点对点单播被频道那条接住，直接进了混音。
- 接收端没有任何准入：`Receiver` 早有 `accept_source` 参数却从未被构造方传入，
  所以"哪些包能进流表"根本没有判断。
- 包头上没有"广播 / 单播"字段（28 字节头只有 flags 的 0x01/0x04 在用），手机端
  `AudioSend` 对同一包 `send()` 给所有目标，网络层也看不出区别。
  现成的判别证据只有 T_STATS 的 `want_listen`：手机端只在**用户单独点「送声音」**的
  那几台上置真（`Engine.java` `iSendToIt && targeted`，`targeted = sendIds.contains(...)`），
  广播线路为假。

**修复**
```python
# 1) 路由：没有具名路由、而它声明了在对我点对点送 → 一个播放端都不挂
if node_id and self._peer_sends_point_to_point(node_id):
    return set()
return wildcard      # 频道广播 / 认不出来源：保持老行为

# 2) 接收准入：在进入流表与 packets_received 之前丢包
def _admitted(self, pkt, host):
    if pkt.type != T_AUDIO or 没有回调: return True
    node_id = resolve_source(host)
    return True if not node_id else accept_source(node_id)
```
`want_listen=true` 有效期 `PEER_TARGETED_TTL_S=10s`；变成 false 立刻清除，
手机停送后频道收听自动恢复。

**回归**
- `test_unaccepted_point_to_point_is_not_attached_to_the_channel`：声明前→`{"channel"}`；
  声明后→空集、`_stream_is_accepted()==False`；建具名路由后→`{"named"}` 且立刻接受
- `test_a_broadcast_from_a_known_sender_still_reaches_the_channel`：`want_listen=false`
  的已登记手机仍然落进频道那条（修准入不误伤广播）
- `test_the_receiver_drops_a_denied_source_before_it_enters_the_stats`：`_admitted` 对
  T_AUDIO 返回 False、对 T_PING 放行、认不出来源放行、`packets_denied` 出现在 stats

**兼容性 / 还没解决的**
- 无协议改动：老手机 2.9 照老方式发；它点对点送时同样会报 `want_listen`，
  所以新电脑同样会挡（符合铁律）。
- 弱点（已记录）：判别完全依赖手机上报 `want_listen`；如果对端是不发统计的"哑设备"，
  电脑只能按频道处理（认不出来源一律放行）。要做到绝对判别，需要给音频包头加一个
  `FLAG_CHANNEL`（flags 还有 0x08 等空位、28 字节布局不变），但那要重打手机端——
  **本轮不动，需要用户确认后再做。**
- 发送侧已核对：手机点对点的目标集合来自用户手选的 `sendIds`，不是"发现表全播"；
  广播才发全网（那是产品本意）。所以发送侧无需改动。

---

## BUG-20261104-05 认不出身份的发送方仍被「单播放端兜底」收下（审计 A1）

**版本**：PC 2.21 / 手机 2.10（源码未改）/ 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._attach_stream_routes()`、
  `_reattach_stream_routes()`、`_on_stream_start()`
- `audiomixing/net/rtpaudio.py` → `StreamInfo.host`（新增字段）

**现象**
上一轮（BUG-20261104-04）修完"未接受的点对点"后，仍有一条路能听到同样的声音：
手机点对点送声音、电脑没接受，而电脑的发现表里恰好没有这台手机（Wi-Fi 丢信标 /
AP 隔离 / 手机换了 IP）时，声音照样从唯一那个播放端放出来。

**原因**
`_attach_stream_routes()` 的兜底条件是"`routes` 为空 + 只有一个播放端 +
`not _sender_is_known(node_id)`"。认不出身份（`node_id == ""`）不等于"用户手动添加的
对端"——**任何未知 IP 都满足这个条件**。`Receiver._admitted()` 对未知来源也一律放行
（它无法判断来源身份，只能放行）。两条加起来：没接受的点对点仍能混进来。

**修复**
兜底只给 `_is_manual_peer(host)` 为真的地址（用户在配置里明确加过的那几台）；
其余未知来源一个播放端都不挂，同时给 `StreamInfo` 增加 `host` 字段，
让重挂流（`_reattach_stream_routes`，此时拿不到 addr）也能用同一条边界。

**回归**
- `test_a_known_sender_without_any_route_is_never_played`：认得出身份→空集；
  未知且**未手动添加**的 127.0.0.1→空集（改前返回 `{"only-sink"}`）；
  手动添加的 10.0.0.9→`{"only-sink"}`（保留原意）

**兼容性**
不改协议；广播→只听仍走通配路由（不受影响）；手机端不必重装。

---

## BUG-20261104-06 时钟 offset 被「历史最小 RTT」冻死（审计 A2）

**版本**：PC 2.21 / 手机 2.10 / 2026-11-04

**位置**
- `audiomixing/proto.py` → `ClockSync.update()`

**现象**
对端墙钟被 NTP 校正（或用户手动改时间）后，本机仍按旧 offset 排播放头：
声音整体延迟 1 秒以上或抖动缓冲持续溢出，**不重启就永远好不了**。

**原因**
`if rtt <= self._best_rtt or self.samples == 0`，而 `_best_rtt = min(_best_rtt, rtt)`
只减不增。采集过几轮之后，后续样本几乎不可能再小于等于历史最小值，
offset 从此不再更新——名义上是"只信低 RTT 样本"，实际是把 offset 冻结了。

**修复**
① 常规样本允许在 `best_rtt × 1.5` 的窗口内更新（仍偏好低 RTT）；
② `|新 offset − 当前 offset| > 250ms` 且 RTT 不离谱（≤ max(best×3, 50ms)）时
视为对端校时，直接重锚并重置 `_best_rtt`。

**回归**
- `test_a_peer_clock_step_is_followed_after_the_best_rtt_is_settled`：
  先喂 4ms/5ms 两轮（改前第二轮就被丢弃），再喂 +500ms 跳变 →
  第一轮必须开始移动、第二轮必须落在 500ms（改前恒为 0）

**兼容性**
纯本地估算，不发包、不改协议。

---

## BUG-20261104-07 乱序包污染丢包计数 / 重复包重复入缓冲（审计 A3）

**版本**：PC 2.21 / 手机 2.10 / 2026-11-04

**位置**
- `audiomixing/net/rtpaudio.py` → `Receiver._handle_audio()`
- `audiomixing/net/rtpaudio.py` → `StreamInfo.duplicates`（新增字段）、`Receiver.stats()`

**现象**
诊断页丢包数虚高（一个乱序旧包能加几十）；偶发的重复包会让同一段 10ms 音频播两遍、
缓冲深度凭空上涨。

**原因**
接收端内联的序列号统计与 `proto.StreamStats.observe_seq()` 口径不一致：
旧包（`delta > 0x8000`）虽然不加丢包，却**无条件**执行 `info.last_seq = pkt.seq`
把游标退回去，下一包于是被算成丢了一截；`delta == 0` 的重复包既不丢也不计数，
继续写进抖动缓冲。

**修复**
完全对齐 `observe_seq()`：`delta == 0` → `duplicates += 1` 并直接返回（不写缓冲）；
`delta > 0x8000` → 音频照收但不更新 `last_seq`；其余按 `delta - 1` 计丢包。
stats 每流新增 `duplicates`。

**回归**
- `test_reordered_and_duplicate_packets_do_not_corrupt_the_counters`：
  序列 1,2,3,1,4 → `lost==0`、`last_seq==4`；重复 4 → `duplicates==1` 且
  `samples_written` 不增长（改前两种都会失败）

**兼容性**
纯接收端逻辑。

---

## BUG-20261104-08 具名多源路由只用第一台的手机音量（审计 A4）

**版本**：PC 2.21 / 手机 2.10 / 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._peer_media_volume_source()`

**现象**
一条具名路由的 `from` 写了两台设备（两台手机同时送进同一个播放端）时，
一台手机把音量调小/静音，播放端音量不受影响。

**原因**
遍历 `from` 的 token 时**命中第一个就 `return`**，整条 sink 只有一个 gain，
于是"先被查到的那台"决定了全部来源的音量。

**修复**
遍历全部具名 token 取**最小值**（任何一台关小都不该被另一台顶回大音量）；
单源路由行为不变。

**回归**
- `test_a_route_with_two_sources_uses_the_quietest_volume`：A=0.9、B=0.2 →
  路由音量 0.2（改前为 0.9）；单源 A 仍为 0.9

**兼容性**
纯本地计算。

---

## BUG-20261104-09 路由 gain 原样接收 NaN/Infinity（审计 A9）

**版本**：PC 2.21 / 手机 2.10 / 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._normalize_route()`

**现象**
向 `/api/routes` 提交 `{"gain": NaN}`（或 `Infinity`）后，那一路播放端彻底没声音，
改回正常数值也不恢复，必须重启程序。

**原因**
`json.loads` 默认接受 `NaN` / `Infinity` 字面量，`_normalize_route` 直接
`float(raw.get("gain", 1.0))` 落库；`apply_gain` 把 NaN 乘进每一声采样，
渲染链里从此全是 NaN。`sink_device` / `source_device` 同理会被写成对象/数组，
后续字符串比较抛 `TypeError`，异常在渲染线程里被吞掉。

**修复**
新增 `as_gain()`：非有限值（NaN/±inf）→ 1.0，其余夹到 `[0, 8]`；
新增 `as_text()`：设备字段一律转字符串。

**回归**
- `test_a_non_finite_route_gain_cannot_poison_the_sink`：NaN/inf/−inf→1.0、
  `"abc"`→1.0、100→8.0、−5→0.0、对象设备字段→字符串

**兼容性**
正常增益（0~2）的取值与行为完全不变。

---

## BUG-20261104-10 手动对端的 `manual:IP` 占位身份盖住真实 id（审计 B1，第 2 轮）

**版本**：PC 2.21 / 手机 2.10 / 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._node_id_for_host()`
- `audiomixing/engine.py` → `AudioMixingNode._on_stream_start()`
- `audiomixing/net/discovery.py` → `DiscoveryRegistry.add_manual()`（`pid = "manual:" + host`，本次不改它）

**现象**
手动添加的对端（或同一台机器上的两个节点）送声音过来，**整场听不到**：
流能收到包、计数器在涨，但播放端一个字节都不写。同机自检 `selftest_twonode`
三次里有一次 "B 实际播放了 A 的音频 played_frames=0"，就是这件事。

**原因**
`add_manual()` 先写一条 `node_id = "manual:<IP>"` 的占位记录；真实身份（信标 /
HTTP 探测）是**另一条**记录、同一个 host。`registry.resolve(host)` 按字典顺序
遍历，可能先命中占位 → `_on_stream_start()` 把流的 `node_id` 写成 `manual:IP`。
路由里写的是真实 id，永远匹配不上；而 `_refresh_stream_identities()` 只补**空**
身份，不会再改这条流 → 既挂不上具名路由、也不会建播放端，直到进程重启。

**修复**
- `_node_id_for_host()`：快速路径命中真实 id（且 host 精确相等）直接返回；
  命中占位时再扫一遍同 host 的记录，只取非 `manual:` 的真实 id（按 `last_seen`
  取最新）；都找不到就返回空串（"认不出来源"，交回兜底/频道逻辑）。
- `_on_stream_start()`：改用 `_node_id_for_host()`，并用真实 id 取回 peer
  （替换原来的 `registry.resolve(addr[0])`），从第一包起就不写占位身份。

**回归**
- `test_a_manual_placeholder_never_shadows_the_real_identity`
- `test_the_first_packet_records_only_the_real_identity`
- `test_a_stream_that_started_before_the_beacon_gets_its_identity_later`（改写：
  只登记手动占位时**不许**补身份；真实信标到了才补 `real-node`）
- `tools/selftest_twonode.py`：同机双节点连跑 2 次 19/19（改前 3 次里 1 次 18/19）

**兼容性**
纯 PC 端身份解析，协议不变；手机端 2.10 不需要重新打包。快速路径不改每包开销。

---

## BUG-20261104-11 广播 + 收听被 A1 准入闸门误杀（Q1，回退）

**版本**：PC 2.21 / 手机 2.10（Q1 本身只改 PC；2.11 是 Q3 重建的包） / 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._peer_send_mode_for()`（新增）
- `audiomixing/engine.py` → `AudioMixingNode._peer_sends_point_to_point()`（重写判据）
- `audiomixing/engine.py` → `AudioMixingNode._stream_is_accepted()`（闸门本身不变，判据变对）
- `audiomixing/engine.py` → `AudioMixingNode._handle_peer_requests()`（stats 分支记录显式模式）

**现象**
手机开「广播」、电脑开「收听」，两边界面都显示正常，**电脑耳机一点声音都没有**。

**触发链（哪一行把广播拒了）**
1. 手机广播时 `sendIds` 可能残留着电脑的 id（用户先单独「送声音」给电脑、再开广播，
   旧代码不会清 `sendIds`）；
2. 手机每 2 秒发的统计包里 `targeted = sendIds.contains(电脑)` → `want_listen = true`
   （手机端 `Engine.stats()`：`wantListen = targeted`）—— 这个字段本意是"我在单独点送某人"；
3. 电脑 `_handle_peer_requests()` 的 stats 分支看到 `want_listen` 就写
   `_peer_targeted[node_id] = now`；
4. `_peer_sends_point_to_point()`（旧实现只看 `_peer_targeted`）返回 **True**；
5. 于是 `routes_for_sender()` 不给广播流挂通配频道路由（返回空集），
   `_stream_is_accepted()` 见"没有具名已接受路由"又判定这是未接受的点对点 → **False**；
6. `Receiver._recv_loop` 的 `_admitted()` 在进流表前直接丢包（`packets_denied++`）。
   → 广播流一帧都没进抖动缓冲，播放端自然没声。

**修复**
- 判据改成**只认发送端显式声明**：手机 2.10 的 stats 已带 `sending`/`target`/`vol`，
  本次新增读取 `send_mode`（0/1/2）与 `broadcast`（bool）；手机 2.11 起正式发送这两个键。
  `broadcast=true` 永远不算点对点，哪怕同一包里还残留 `want_listen=true`。
- `_stream_is_accepted()` 的规则不变（有具名已接受路由 → 放行；否则"对端声明点对点"才拒），
  但"对端声明点对点"现在等于 `send_mode >= 1 && !broadcast`。
- **两道闸同一判据**：`routes_for_sender()` 的 wildcard 兜底用的也是
  `_peer_sends_point_to_point()`，不会出现"一处放行一处拒绝"。
- **退化行为**：拿不到 `send_mode`/`broadcast`（手机 2.9 / 字段过期 >10 秒）→
  `_peer_send_mode_for()` 返回 None → `_peer_sends_point_to_point()` 返回 False
  → **放行（能听）**，绝不静默失败。

**回归**
- `test_a_broadcast_from_a_known_sender_still_reaches_the_channel`（广播 + 残留 want_listen 可听）
- `test_a_legacy_phone_that_never_reports_its_mode_is_still_heard`（老手机缺字段 → 能听）
- `test_unaccepted_point_to_point_is_not_attached_to_the_channel`（点对点未接受 → 仍拒绝）
- `test_broadcast_listen_receives_real_audio_packets_end_to_end`（A 广播、B 只开收听 →
  `_admitted()` True 且流表收到帧；同用例里点对点未接受 → `_admitted()` False、流表为空）
- `Test20261103GhostsAndStats` 18 项全绿

**兼容性**
纯 PC 端判断，协议只多读两个可选键；手机 2.10 不发这两个键时按"能听"处理，行为回到历史上
广播可听的版本。手机端无需为此单独升级（2.11 起显式上报，语义更硬）。

---

## BUG-20261104-12 手机音量被乘两次（Q2，平方衰减）

**版本**：PC 2.21 / 2026-11-04

**位置**
- `audiomixing/engine.py` → `AudioMixingNode._observe_peer_volume()`（新增）
- `audiomixing/engine.py` → `AudioMixingNode._received_peak_for()`（新增）
- `audiomixing/engine.py` → `AudioMixingNode._volume_factor_for()`（新增）
- `audiomixing/engine.py` → `AudioMixingNode._apply_sink_gain()` / `_apply_stream_volume()`
- `audiomixing/config.py` → `Config.phone_volume_mode`
- `audiomixing/webui.py` → `/api/settings`（接受并校验 auto/off/on）
- `audiomixing/ui/index.html` → 设置页下拉 + 设备卡片诊断行

**现象**
手机媒体音量调小一半，电脑端听到的音量变成约 **四分之一**（平方），而不是一半。

**原因**
手机采集点（AudioPlaybackCapture）在**系统音量之后**：它送出的 PCM 已经带着
「系统音量 × 视频 App 音量」。PC 又按上报的 `vol` 乘一次 → 同一个音量被作用两次。

**判定逻辑（自动档）**
- 每收到一轮手机统计（2 秒）比较：`vol` 与该流**实际收到的峰值**（
  `JitterBuffer.peak`，解码后、任何增益之前）。
- 窗口 10 秒；窗口内峰值取最大（避开静音间隙）；峰值 < 0.02 视为静音样本作废。
- `vol` 变化 ≥ 0.15 才比：峰值比 ≈ vol 比（容差 `max(0.15, |vol比|×0.25)`）→
  **采集端已衰减 → PC 系数固定 1.0（不叠加）**；
  峰值比 ≈ 1（±0.15）→ **采集在音量之前 → 仍乘 vol**；
  两边都不像 → 样本不干净，不提交。
- 首次判定立即生效；推翻已有结论要连续 2 次一致（滞后）。
- `phone_volume_mode`：`auto`（默认）/`off`（强制不乘）/`on`（强制乘，回到旧行为）。
  样本不足（老手机 / 从没调过音量）按**叠加**处理（与 2.10 之前一致，不会丢音量）。

**诊断**
设备卡片第二行显示：`档位=… · vol=x.xx  收到峰值=y.yy  判定=…（系数 z.zz）`。
`/api/status` 的 peers 里也带 `vol_peak` / `vol_verdict` / `volume_factor` / `volume_note`；
播放端 `describe()` 带 `volume_factor` / `volume_note`。

**回归**
- `test_phone_volume_is_not_multiplied_twice_when_the_capture_is_already_attenuated`
  （vol 1.0→0.5、峰值 0.80→0.40 → 判定已衰减、`gain == 路由增益`、`media_vol` 仍显示 0.5；
  强制开 → `0.8 × 0.5`；强制关 → `0.8`）
- `test_phone_volume_is_still_applied_when_the_capture_precedes_the_volume`
  （峰值不变 → 判定需叠加、`gain == 路由增益 × 0.5`）
- `test_volume_decision_defaults_to_the_old_behaviour_without_samples`

**兼容性**
不看 `phone_volume_mode` 的老配置默认 auto；auto 在没有样本前等价于旧行为。
手机端无需改动（`vol` 从 2.10 起已在报）。

---

## BUG-20261104-13 「送声音」三档 + 显式上报档位（Q3，手机 2.11）

**版本**：手机 2.11 / versionCode 25 / 2026-11-04；PC 2.21 显示档位

**位置**
- `android/.../Engine.java` → `sendMode()` / `setSendMode(int)` / `loadSendMode()` / `saveSendMode()`
- `android/.../Diagnosis.java` → `json(...)` 新重载（追加 `send_mode` / `broadcast`）
- `android/.../MainActivity.java` → `showSendModeDialog()`（原「送声音」按钮改为三档入口）
- `audiomixing/engine.py` → `_peers_with_media_volume()` 输出 `send_mode` / `broadcast`
- `audiomixing/ui/index.html` → 设备卡片显示「档位=未激活（0）/送声+外放（1）/送声+静默（2）/未知」

**三档语义**
| 档 | 手机行为 | 电脑侧 |
|---|---|---|
| 0 未激活 | 不发声音 | 无流 |
| 1 送声+外放 | 发送，手机自己也出声 | 有流，可被接受 |
| 2 送声+静默 | 发送，本机外放不出声 | **未实现**（见下） |

**第 2 档为什么未实现（如实标记，不冒充）**
本应用通过 `AudioPlaybackCaptureConfiguration` 采集**其它 App 正在播放**的声音，
手机外放是系统直接把那个 App 的声音播给扬声器；`AudioSend` 里**没有本地 AudioTrack**，
工程里不存在"把本地播放降到 0"的通路。唯一能让外放静音的手段是改系统媒体音量
（`setStreamVolume` / `setStreamMute`），用户 2026-11-04 硬性禁止。
因此第 2 档在选择框里标灰，点它只弹出原因；档位统计永远不会上报 2。

**切换干净**
`setSendMode(0)` 只清 `sendIds` 并走已有的 `syncSenderTargets()`（目标集合变化不重建采集），
不会留下第二条流；`setSendMode(1)` 保持采集与目标不变。档位偏好存
`SharedPreferences("audiomixing")` 的 `send_mode`（老值 2 读回时按 1）。
第 0 档是**档位语义**（整机不再点对点送声），所以会清空所有 `sendIds`；广播是独立线路，
不受第 0 档影响（`sendMode()` 在广播开着时仍返回 1）。

**回归**
- `check_apk --release`：`versionName == 2.11`、dex 含 `send_mode`、六处副本哈希一致 → PASS
- `tests/test_audiomixing.py::TestApkReleaseGate` → 2.11 + `send_mode` 断言
- 手机离线门禁 `run_policy_test.sh` 等（见审计记录）不受影响：只增字段，旧解析忽略未知键

**兼容性**
手机 2.11 对老电脑是"多两个未知键"（解析只读认识的键）；电脑 2.21 对手机 2.10 按
"拿不到声明 → 能听"处理（见 BUG-20261104-11）。第 0/1 档双向兼容；第 2 档不存在历史数据。

---

## BUG-20261104-14 智连 = 自动接听（PC 2.22 / 手机 2.12）

**版本**：PC 2.22 / 手机 2.12 / versionCode 26 / 2026-11-04

**位置**
- `audiomixing/engine.py`
  - `_queue_auto_listen()` / `_apply_pending_auto_listen()`：收到对端 `want_listen` 后排一次自动收听
  - `_grant_listen_request(node_id, name, host)`：接收方判定 + 建「对方请求收听（自动）」路由
  - `_sweep_auto_listen_hangs()`：双判据挂断；`_auto_listen_hangs` 记录可观测字段
  - `smart_link_allows()` / `set_smart_link()`：广播互斥与手动开关
  - `note_manual_listen_off()`：手动关收听时同时摘掉智连
  - `_send_peer_stats()`：点对点目标注入 `listen_req:"start"`、宽限目标注入 `"stop"`；
    解析手机回的 `listen_ack_id/action/reason`
- `audiomixing/webui.py`
  - `_note_manual_auto_close(removed)`：`set_routes` / `delete_route` 删除自动收听路由前先关智连
  - settings 的 `smart_link`：广播开着且要新增键时返回 409
- `audiomixing/ui/index.html`：设备卡片用 `status.auto_listen.available/reason` 置灰并显示原因
- `android/.../Engine.java`
  - `toggleSmart()` / `setBroadcasting()`：广播时不接受打开智连
  - `handleWantListen()`：`want = !isBroadcasting() && ...`
  - `handleExplicitListenRequest()`：处理电脑发来的 `listen_req`（start/stop）并写 `listen_ack`
  - `revokeStaleAutoListens()`：音频 age + 对端统计 age **两个都超过 4000ms** 才收回
- `android/.../MainActivity.java`：`applySmartAvailability()` 广播时置灰、写原因

**现象**
1. 手机开了「智连」，电脑单独送声音过去，手机**不会自动接**（要手点）；
2. 多台手机同时送，只有一台能接上，其余被拒/排队；
3. 自动接上后，对端只是**静音**（仍然有音频包），4~12 秒后收听被误挂断；
4. 用户手动关掉自动打开的收听，下一包音频又把它打开（「你越关它越开」）；
5. 广播模式下智连仍可点，两种模式混在一起；
6. 电脑发「请开始/停止接收」，手机端完全忽略（用户实测，单向）。

**原因**
- 旧实现只在**发送方**看 `want_listen`，接收方没有「我开了智连就接」的判定；
- 挂断只看单一超时（`PEER_SILENCE_GRACE_S` / 统计包），静音流与死流分不开；
- 手动删除路由只删了路由，没有同步摘掉 `smart_link`；
- 广播与智连没有互斥判定，任一入口都能把两者组合；
- 手机端 `handleWantListen()` 只看自己的状态，电脑的请求没有进入处理分支，也没有回执。

**修复（关键判断）**
```python
# engine.py：广播时智连一律不可用（所有入口共用这一个判定）
def smart_link_allows(self, node_id: str, name: str) -> bool:
    return not self.config.broadcast and self._smart_link_matches(node_id, name)

# 双判据：音频包静默 + 心跳消失，两个都成立才收回
audio_age = now - grant.get("last_audio", now)
heartbeat_age = peer.age_s if peer else float("inf")
if audio_age >= AUTO_LISTEN_HANG_S and (heartbeat_age >= AUTO_LISTEN_HANG_S
                                        or self._auto_stop_requested.pop(node_id, False)):
    self._auto_listen_hangs[node_id] = {"audio_silence_s": round(audio_age, 2),
                                        "heartbeat_s": round(heartbeat_age, 2),
                                        "threshold_s": AUTO_LISTEN_HANG_S}
    self._revoke_auto_listen(node_id)
```
`AUTO_LISTEN_HANG_S = 4.0`（手机 `AUTO_LISTEN_HANG_MS = 4000`）。静音音频仍在发包 ⇒
`last_audio` 持续刷新 ⇒ `audio_age` 很小 ⇒ 永远不挂。

手动关闭：
```python
def note_manual_listen_off(self, node_id, name=""):
    removed = self._remove_smart_link(node_id, name)   # 摘 id 与 name 两种键
    if removed:
        self.config.save()
```
webui 在删除路由的**同一个请求**里先调用它，因此下一包音频到来时智连已经关了。

手机处理电脑请求：
```java
// Engine.handleExplicitListenRequest()
if ("start".equals(action)) { startListening(host); }
else if ("stop".equals(action)) { stopListening(); }
listenAckFor.put(host, new int[]{reqId, ok ? 1 : 0, reason});
```
回执随统计包回传（`listen_ack_id/action/reason`），PC 存进 `_peer_listen_ack`。

**四条规则与实现对应**
| 用户规则 | 实现 |
|---|---|
| 接收方开智连就自动接，发送方不用开 | `_grant_listen_request` 只看本机 `smart_link` |
| 多台同时送全接，不排队不拒绝 | 每台各建一条 `auto` 路由；无队列、无 `pending` 残留 |
| 没声音不挂，数据流没了才挂 | 音频 age + 心跳 age **双判据**，阈值 4 秒 |
| 手动关自动收听 → 智连一起关 | `note_manual_listen_off` + webui `_note_manual_auto_close` |
| 广播不让和智连组合 | `smart_link_allows` / `set_smart_link` / UI 置灰 / 手机双入口 |

**回归**
- `Test20261104SmartLinkAndIdentity.test_smart_link_auto_accepts_on_the_receiver_side_only`
- `test_two_sending_phones_are_both_answered_no_queue_no_refusal`
- `test_silent_audio_never_hangs_but_a_dead_stream_does`
- `test_manual_listen_off_also_switches_the_smart_link_off`
- `test_smart_link_is_unavailable_while_broadcasting`
- 手机端：编译通过 + APK 门槛（2.12 / `originId`）；通过 `handleExplicitListenRequest` 的代码路径，
  **真机回执未验**（见审计文末未验证清单）。

**兼容性**
老手机不发 `listen_ack_*` → PC 只记录"未回执"，不报错、不拒流；老电脑不发 `listen_req` →
手机 2.12 仍按原来的 `want_listen` 行为工作。智连只服务具名点对点，广播/频道不受影响。

---

## BUG-20261104-15 设备身份合并 + 防自环（第一阶段，PC 2.22 / 手机 2.12）

**版本**：PC 2.22 / 手机 2.12 / versionCode 26 / 2026-11-04

**位置**
- `audiomixing/proto.py`：`FLAG_EXT` / `EXT_SIZE` / `MAX_HOP` / `origin_id_of()`；`Packet.origin_id` / `hop`
- `audiomixing/net/rtpaudio.py`：`Receiver(self_origin_id=..., max_hop=...)`、`_admitted()` 先判自回声/hop；
  `packets_echo_dropped` / `packets_hop_dropped`
- `audiomixing/net/discovery.py`：`PeerEndpoints.device_id/instance_id/alias_ids/hosts/ext/
  observed_bt_mac/observed_ap/identity_conflict/merge_hint/merge_hint_with`；
  `update()` 以 device_id 为主键；`get()/resolve()` 别名感知；`merge_identity()`
- `audiomixing/config.py`：`device_id` / `instance_id` / `alias_ids` / `identity_migrated` /
  `identity_merges`；`load()` 迁移前 `_backup_before_migration()`
- `audiomixing/engine.py`：`self.origin_id`；`identity_merge()` / `_apply_identity_merges()`；
  SenderSession 传 `origin_id`（仅对端 `ext` 时）
- `audiomixing/webui.py`：`POST /api/identity/merge`
- `audiomixing/ui/index.html`：`mergeIdentity()` + 「疑似同一台设备…是否合并？」banner
- Android 对应：`Proto.java`（`FLAG_EXT`/`originIdOf`/`Pkt.originId/hop`）、`Discovery.java`
  （`loadOrCreateDeviceId` / `loadOrCreateInstanceId` / 信标 `device_id`+`instance_id`+`ext:1`）、
  `AudioSend.java`、`AudioRecv.java`（丢自回声/超跳并计数）、`Engine.java`（`selfOriginId()` /
  `targetsSupportExt()`）

**现象**
手机「一边蓝牙出、一边 WiFi 收」被当成两台设备：路由表里两个节点互相指向，声音绕成圈
（自回声/循环放大）。

**原因**
- 发现表与路由表的主键是 `node_id`（= 端点/IP/MAC 的产物），同一台手机的不同链路天然是不同主键；
- 音频帧里没有"原始声音是谁"的标识，也没有跳数上限，环路没有任何刹车。

**修复（关键判断）**
1. 身份层：
```python
# discovery.update()：同一 deviceId 的多条链路并进同一个节点
did = payload.get("device_id") or pid
old = self._peers.get(did)
iid = payload.get("instance_id") or ""
if old and iid and old.instance_id and iid != old.instance_id:
    old.identity_conflict = True       # 换机搬数据：两台真机共用一个 deviceId
    lookup = "%s@%s" % (did, iid)      # 绝不自动合并
else:
    peer = old; lookup = did
if peer and pid != peer.node_id:
    _add_alias(peer, pid)              # 旧 node_id 降级为 aliasId
_add_host(peer, host)                  # WiFi / 蓝牙 / 其它链路都记在 hosts
```
旧配置迁移：
```python
if not cfg.device_id:
    _backup_before_migration(path)     # 迁移前备份，绝不删用户路由
    cfg.device_id = cfg.node_id
    for rule in cfg.routes:
        for token in rule["from"] + rule["to"]:
            if token != cfg.node_id: rule.setdefault("aliasIds", []).append(token)
    cfg.identity_migrated = True
```
`manual:IP` / 旧 `node_id` 仍是合法 token，解析时按 `alias_ids` 命中同一个节点；**路由条数不变**。
辅助证据（对端观测到的蓝牙 MAC + 同一 AP）只写 `merge_hint` + `merge_hint_with`，面板弹
「疑似同一台设备，是否合并？」；用户点确认才走 `POST /api/identity/merge`，**没有任何静默合并**。

2. 防自环：
```python
# proto.Packet.encode：origin_id != 0 时置 FLAG_EXT，包头后插 8 字节
if self.origin_id:
    flags |= FLAG_EXT
origin = EXT.pack(self.origin_id, self.hop) if flags & FLAG_EXT else b""
# Receiver._admitted：先判自回声、再判跳数
if pkt.origin_id and self.self_origin_id and pkt.origin_id == self.self_origin_id:
    self.packets_echo_dropped += 1; return False
if pkt.hop > self.max_hop:
    self.packets_hop_dropped += 1; return False
```
`MAX_HOP = 8`；诊断页/status 显示 `loop_guard.echo_dropped` / `hop_dropped`。
扩展段只在**对端信标声明 `ext:1`** 时才发，老端永远收老包（28 字节头不变）。

**第二阶段（明确未做，本轮不实现）**
路由图有向环检测、epoch/CAS 提交、切断仲裁、声学探针。

**回归**
- `test_multiple_links_with_one_device_id_become_one_node`
- `test_the_same_device_id_from_two_real_phones_is_never_auto_merged`
- `test_old_config_migration_keeps_every_route_and_demotes_ids_to_aliases`
- `test_frames_whose_origin_is_ourselves_are_dropped_and_counted`
- `test_frames_that_exceed_the_hop_limit_are_dropped_and_counted`
- 跨语言字节：Python 与 Java 对同一带扩展段包编出 **44 字节完全相同**的 hex；
  `originIdOf("phone-a")` 两端都是 `29089828`
- `check_android_proto.py` 老样例 5/5 逐字节一致（28 字节头未变）

**兼容性**
- 老端不发 `device_id` / `ext` → 按老 `node_id` 路径工作，不报错、不拒流；
- 老端不收扩展段 → 新端只在看到 `ext:1` 时才加扩展段；
- 迁移只补字段不删路由，且迁移前留 `config.json.bak-<时间戳>`。

---

## BUG-20261106-01 图上一眼可见的三处 UI 残留（自启动复验 / 本机混进局域网 / Wi-Fi 还没测到）

**位置**
- `audiomixing/ui/index.html` → `allNodes()`、`renderDeviceCards()`、`deviceCard()` 的本机分支
- `audiomixing/webui.py` → `Api.peers()` / `_peers_with_smart()`、`_best_local_address()`
- `audiomixing/cli.py` → 启动横幅（`lan_urls`）

**现象（用户截图）**
「局域网里的设备」里有一张卡片：标题「自启动复验」、标签「本机 / 可当音响」、
状态「输出 … 现在没有声音 / 送 … 没有在送 / 信号 … Wi-Fi 还没测到」。

**原因**
1. 「自启动复验」不是产品生成的名字。上一轮验证开机自启动时用
   `exe serve --name 自启动复验 --web-port 18083 …`，**没有隔离 `AUDIOMIXING_HOME`**，
   CLI 覆盖被 `Config.save()` 持久化进真实 `%APPDATA%\AudioMixing\config.json`。
   单测/回归只测 API 字段，从不渲染 DOM，所以没拦住。
2. `allNodes()` 无条件把本机作为第一项（`local:true`），`renderDeviceCards()` 直接渲染
   `allNodes()` → 「局域网里的设备」必然出现本机卡；服务端 `_peers_with_smart()` 也只按
   `node_id` 一种身份判断，本机信标绕回时漏过滤。
3. 本机卡套用对端的 `outerScore(state.status)`，本机没有对端质量采样 → `null` →
   回落成「还没测到」，把"未采集"当成状态展示。
4. 附加（真实截图发现）：`renderNetwork()` 和启动横幅都取 `interfaces[0]`，这台机器第一项是
   WSL/Hyper-V 的 `172.23.128.1`，手机根本连不上；`--open-browser` 还会直接打开这个死地址。

**修复**
```js
// index.html：设备卡片只放对端；本机状态由「本机状态」页单独负责
const nodes = allNodes().filter(n => !n.local);
// 本机卡改成如实说明
qNum = (outer === null) ? '本机链路（面板不探测）' : (Math.round(outer) + ' 分 · Wi-Fi 链路');
```
```python
# webui.py：本机身份全集（node_id/device_id/instance_id/alias_ids/本机地址）
self_ids = _self_identity_keys(node)
peers = [p for p in peers if not _peer_is_self(p, self_ids) …]
# 且地址只作兜底 —— 同机两节点 host 相同，不能按地址判本机
if not ids:
    return bool(hosts & self_ids)
# 面板/横幅地址统一由服务端选
data["preferred"] = _best_local_address(node, data)
```
```python
# cli.py：启动横幅也把推荐地址排第一
lan_ips = _ordered_lan_ips(node, lan_report)
```

**回归**
- `TestUiResidueGate::test_test_node_names_are_rejected`
- `TestUiResidueGate::test_shipped_ui_has_no_residue_and_hides_the_local_card`
- `TestUiResidueGate::test_every_self_identity_is_filtered_from_the_lan_list`
- `TestUiResidueGate::test_two_nodes_on_one_machine_are_not_hidden`
- `TestQuickStartAndVersion::test_panel_banner_uses_the_best_address_not_interface_zero`
- `TestQuickStartAndVersion::test_startup_banner_lists_the_reachable_address_first`
- `selftest_twonode.py` 19/19 PASS（双节点同机，验证没有被误过滤）
- 新版门禁 `tools/check_ui_residue.py`：注入「自启动复验」→ FAIL(exit 1)；清理后 → PASS

**兼容性**
只改电脑端显示与筛选，不改协议；手机 2.19 与 PC 2.31 为本轮配套版本。

---

## 交付前检查（缺一项不算完成）

- [ ] 本次每个 bug 都有一条完整记录（位置/现象/原因/修复/回归）
- [ ] 位置写的是**函数名**，不是只有行号
- [ ] 修复贴了关键判断，不是"改好了"这种空话
- [ ] 回归写了具体测试名；没有测试的写明"缺，待补"
- [ ] 写清"旧版本对端是否兼容"与"哪一端必须重新打包"
- [ ] 没有修改 `C:\Users\75341\Desktop\31`（那是另一个项目，只读）
