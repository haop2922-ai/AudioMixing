# 真机音量链路：系统媒体音量（STREAM_MUSIC）vs App 内部音量

> 结论先行，避免歧义：
>
> 1. **上一轮报告里的「A 项」验的是系统媒体流音量 `STREAM_MUSIC`，不是某个 App 的内部音量。**
> 2. Android **没有**给第三方 App 提供读取“其他 App 内部音量”的公开接口。直说：没有。
> 3. 当前手机端实现只在“App 滑杆就是 `STREAM_MUSIC`”这一类 App 上等价于“读 App 音量”；
>    对“音量写在 App 内部软件增益里”的 App，再乘 `STREAM_MUSIC` 就是**二次衰减**，属于错误。
> 4. 真实第三方 App 的滑杆到底改不改 `STREAM_MUSIC`，**尚未实测**。
>    在测完之前，**不能宣称“App 音量链路已验证”**。

---

## 1. 现在读的到底是什么

代码位置（手机端）：

- `android/app/src/main/java/com/audiomixing/app/Engine.java` → `mediaVolumeRatioFor()`
  - `getStreamVolume(STREAM_MUSIC)`（约 L3615）
  - `getStreamMaxVolume(STREAM_MUSIC)`（约 L3624）
  - `getStreamMinVolume(STREAM_MUSIC)`（约 L3628）
  - 第 2 档“手机不出声”时用 `PREF_SILENT_ORIG` 覆盖当前值（约 L3618-3622）
- `android/app/src/main/java/com/audiomixing/app/AudioSend.java`（约 L547-554）
  - 每 400ms 读一次 `mediaVolumeRatioFor()`
  - `total_q14 = gain_q14 * media_q14 / 16384`
  - 再把 `total_q14` 乘到 PCM 上

因此报告 A 项表里的：

| 名字 | 含义 |
| --- | --- |
| `vol_q14` | **就是 `STREAM_MUSIC` 的归一化值**（0~16384）。4096 = 25%，0 = 媒体静音 |
| `gain_q14` | 用户在 App/面板上拖的音量条 |
| `total_q14` | 两者相乘后真正乘到 PCM 上的系数 |
| `raw_peak` | 乘音量**之前**的 PCM 峰值 |
| `post_peak` | 乘音量**之后**的 PCM 峰值 |

它**不是**：

- 不是目标 App 的 `AudioTrack.setVolume()` 值；
- 不是网页播放器的 `GainNode.gain.value`；
- 不是“这个 App 自己以为的音量百分比”。

## 2. Android 的 API 边界

### 2.1 能读什么

`AudioManager` 能读的是**流（stream）**的音量：

```java
int cur = am.getStreamVolume(AudioManager.STREAM_MUSIC);
int max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
```

`STREAM_MUSIC` 是系统媒体流，B 站、抖音、系统播放器、多数网页播放器默认都挂在这条流上。

### 2.2 不能读什么

**没有公开 API 能读“另一个 App 的内部音量”。**

- App 内部的 `AudioTrack.setVolume()` / `ExoPlayer.setVolume()` / WebAudio `GainNode` 都发生在
  它自己的进程里，音量系数直接写进它自己的 PCM，系统不对外暴露这个系数。
- `AudioPlaybackCapture` 给的是**混音后的 PCM**，不附带“每个 App 各自音量是多少”的元数据。
- 能间接看到的只有 `dumpsys audio` / `AudioService` 里的**流音量**，仍然不是 App 内部增益。

所以：**“读某个 App 的内部音量”这件事，Android 第三方应用做不到。**
能做到的只有“读系统媒体流音量”，然后根据 App 类型判断它是否等价。

## 3. 两类 App：读 `STREAM_MUSIC` 什么时候成立

### 3.1 类型 A：滑杆直接改 `STREAM_MUSIC`

典型：B 站、抖音等把音量键/滑杆落到系统媒体流的 App。

链路：

```
App 解码 PCM（不含系统音量）
  → AudioPlaybackCapture 采集（在系统音量之前，v2.28 实测）
  → 手机乘一次 STREAM_MUSIC
  → 电脑不再乘（volume_factor=1.0）
```

结论：**读 `STREAM_MUSIC` 就是读 App 音量，当前实现成立。**

### 3.2 类型 B：音量写在 App 内部软件增益里

典型：带独立音量滑杆、用 `AudioTrack.setVolume()` / `GainNode` 的播放器
（用户举的例子：YouTube 播放器这类）。

链路：

```
App 内部增益已写进 PCM
  → AudioPlaybackCapture 采集到的 PCM 里已经含了这份衰减
  → 手机再乘 STREAM_MUSIC = 第二次衰减（错误）
```

结论：**这一类 App 上，手机端再乘 `STREAM_MUSIC` 是错的。**
不是电脑端乘错，而是手机端这一级不该乘。

### 3.3 同一个 App 也可能两种都有

有的 App 滑杆既改内部增益、又联动系统媒体流。这种只能靠实测判据确认，
不能按 App 名字猜。

## 4. 实测判据（重点）

### 4.1 判 App 属于哪一类：拖 App 滑杆，看 `dumpsys audio`

保持目标 App 正在播放，拖动它自己的音量滑杆，同时看：

```powershell
adb shell dumpsys audio | Select-String -Pattern "STREAM_MUSIC|Current:"
```

重点看 `STREAM_MUSIC` 段里的 `streamVolume`（部分版本叫 `Index`）和
`Current: 2 (speaker): X`。

两种结果的含义：

| 结果 | 含义 | 处置 |
| --- | --- | --- |
| `streamVolume` / `Current` **跟着变** | 滑杆就是 `STREAM_MUSIC`（类型 A） | 手机乘一次 `STREAM_MUSIC` 成立 |
| `streamVolume` / `Current` **不变** | 音量写在 App 内部增益（类型 B） | PCM 已含，手机与电脑都不能再乘 |

> 注意：这条判据必须在**目标 App 正在出声**时做。夜间不能放声音，所以本轮没有对
> 真实第三方 App（抖音/B 站）执行“拖滑杆 + dumpsys”这一步。
> `adb` 本身可用，缺的是“开 App 出声”这个条件，不是授权。

### 4.2 判采集点：只改 `STREAM_MUSIC`，看电脑端 `raw_peak`

保持 App 与其内部音量不变，只用音量键改系统媒体音量，观察电脑侧：

| 结果 | 含义 | 处置 |
| --- | --- | --- |
| `raw_peak` **不变**，`post_peak` 跟着变 | 采集点在系统音量**之前** | 必须由手机乘一次 |
| `raw_peak` **跟着变** | 采集点在系统音量**之后** | 禁止再乘（否则二次衰减） |

**本轮已在真机完成这一条（反向确认）：**

| `vol_q14` | `raw_peak` | `post_peak` |
| --- | --- | --- |
| 4096 | 0.3662 | 0.0916 |
| 3072 | 0.3662 | 0.0687 |
| 2048 | 0.3662 | 0.0458 |
| 1024 | 0.3662 | 0.0229 |
| 0 | 0.3662 | 0 |

`raw_peak` 恒定、`post_peak` 与 `vol_q14` 成正比 → **采集在系统音量之前**，
手机乘一次 `STREAM_MUSIC` 是唯一的一级系统音量衰减，不是二次衰减。

### 4.3 本轮已有的“内部增益”真机证据（替代验证）

在本机起了一个 WebAudio 测试页，滑杆只改 `GainNode.gain.value`（不碰系统音量）：

- 开始测试音前：`streamVolume: 0`
- 点“开始测试音”后：`streamVolume: 0`
- 把内部增益从 100% 拖到 21%：`streamVolume` 仍为 `0`
  （`Current: 2 (speaker): 0, 80 (bt_a2dp): 8, 40000000 (default): 10`）
- 截图证据：`g20.png` 显示 `21%` 且按钮为「停止测试音」

结论：**App 内部软件增益不经过 `STREAM_MUSIC`。**
这证明了类型 B 的链路存在，但不等于“已证明某个真实 App 的滑杆属于哪一类”。

## 5. 如果落到“内部增益”这一支，怎么修

> ★ 2026-11-08 更新：下面的 §5.3–§5.5 是当时的候选方案（向用户暴露
> `system` / `capture` 开关），**已被用户方案替代** —— 音量算法直接绑到送声档位上，
> 不再让用户选“系统音量 / 采集音量”。最终落地见 **§9**。

### 5.1 唯一不变量：两端合计只乘一次

设：

- `g` = 用户音量条
- `m` = 手机读到的 `STREAM_MUSIC`
- `f` = 电脑侧 `volume_factor`

要求：

- 采集点在系统音量**之前**（类型 A）：`total = g * m`，电脑 `f = 1`
- PCM 已含音量（类型 B）：`total = g`，手机 `m` 不乘，电脑 `f = 1`

**任何情况下都不允许 `m * f` 两头都乘。**

### 5.2 电脑端已有的一半

`audiomixing/engine.py` → `_volume_factor_for()`（约 L2932-2944）：

```python
mode == 'off'  -> (1.0, '强制关（不叠加）')
mode == 'on'   -> (vol, '强制开（叠加）')
verdict is None -> (vol, '自动·样本不足（按叠加）')
verdict True   -> (1.0, '采集端已衰减（不叠加）')
verdict False  -> (vol, '采集在音量之前（叠加）')
```

`phone_volume_mode` 的取值：`auto` / `off` / `on`（`audiomixing/config.py` 默认 `auto`）。
手机在统计包里声明 `vol_in_capture=true` 时，`_handle_peer_requests()`（约 L2699-2709）
会把该对端判为“采集端已衰减”，电脑不再乘。

**但电脑端这半只解决了“电脑不乘”，解决不了“手机已经乘了”的问题。**

### 5.3 手机端需要补的开关

当前手机端是**无条件**乘 `media_q14`（`AudioSend.java` L547-554），
没有“关闭这一级”的开关，也没有在统计包里声明 `vol_in_capture`。

建议的修法（两条路选一条，不要两头都做）：

**方案 1：手机端设置项（推荐，用户可自己判）**

- 新增 `vol_source`：
  - `system`（默认）：现状，`total = g * m`，统计包带 `vol_in_capture=false`
  - `capture`：`media_q14` 固定为 16384（不乘），`total = g`，统计包带 `vol_in_capture=true`
- UI 文案直接写清：
  - 「系统媒体音量（适合滑杆就是手机音量的 App）」
  - 「App 内部音量（适合自带音量、采集已含衰减的播放器）」

**方案 2：电脑端远程下发**

- 复用现有控制通道，在 `T_STATS`/控制包里加一个 `vol_in_capture` 指令；
- 电脑端 `phone_volume_mode` 改成 `auto/off/on` 后下发给手机；
- 手机收到后切换是否乘 `media_q14`，并在统计包里回报实际状态。

### 5.4 判定方式

- 首选：按 4.1 的 `dumpsys audio` 判据，人工确认后切换；
- 兜底：保留电脑端 `auto` 的“峰值相关性”判定（`_observe_peer_volume`），
  但必须和手机端开关联动，**不能只让电脑单方面不乘**；
- 默认值保持 `system`（现状），避免把类型 A 的用户改坏。

### 5.5 当前代码在类型 B 下的真实状态（必须写明）

- 电脑端：会自动判“采集端已衰减”，`volume_factor=1.0`；
- 手机端：**仍然无条件乘 `media_q14`**；
- 所以类型 B 下**当前链路是错的**（手机端二次衰减），
  这不是电脑端能单独修好的，需要 5.3 的手机端开关。

## 6. 已验证 / 未验证清单

### 已验证

- [x] 手机读的是 `STREAM_MUSIC` 归一化值（`Engine.mediaVolumeRatioFor`）；
- [x] 每 400ms 采样一次，与用户增益合成 `total_q14`（`AudioSend.java`）；
- [x] 采集点在系统音量之前：只改 `STREAM_MUSIC` 时 `raw_peak` 恒定、`post_peak` 按比例变化；
- [x] App 内部软件增益不进 `STREAM_MUSIC`：WebAudio 内部增益 100%→21%，`STREAM_MUSIC` 始终 0（真机截图 `g20.png`）；
- [x] 第 2 档静音用 `PREF_SILENT_ORIG` 还原“用户最后一次设定值”，不会把 PCM 乘成 0；
- [x] 电脑端 `phone_volume_mode=off/on/auto` 的系数行为（`check_volume_chain.py`）。

### 未验证

- [ ] **真实第三方 App（抖音/B 站等）拖滑杆时 `STREAM_MUSIC` 是否变化**——需要开 App 出声，夜间限制未做；
- [ ] **真实“内部增益类” App 的端到端听感**（手机不乘 `media` 后是否正好）；
- [ ] Android 上读取其他 App 内部音量——**系统没有公开接口，无法验证，也不可能实现**；
- [ ] 手机端 `vol_source` / `vol_in_capture` 开关尚未实现（5.3 是修法，不是现状）。

### 结论表述规范

- 可以说：“已验证 `STREAM_MUSIC` 读取链路、采集点位置、内部增益不进 `STREAM_MUSIC`。”
- **不可以说**：“App 音量链路已验证。”
- 正确表述：“A 项验的是系统媒体音量；App 音量链路未验证，等真实 App 判据实测后再下结论。”

## 7. 复现命令

```powershell
# 看系统媒体流音量（Android 16 / realme 实测字段）
adb shell dumpsys audio | Select-String -Pattern "STREAM_MUSIC|Current:"

# 电脑侧乘数链路（raw/post 与 vol 的关系）
python tools/check_volume_chain.py
```

---

## 8. 附：`selftest_twonode` “静音后停止发送/点名收听”偶发失败定位

### 现象

- 6 次复跑里出现 1 次（历史 2/5 次）：
  `[FAIL] 点名收听：同一条点对点流被接住 — denied=40 received=2729 rejected=0 echo=0 ssrcs=3a959cbf`
- 同一轮里：
  - `静音后 A 停止发送` PASS；
  - `packets_denied` 不增长 PASS（40→40）；
  - `混合表里该发送方仍只有一条` PASS；
  - 只有注入的 `0xB0A7` 没进流表。

### 根因（已定位，不是产品缺陷）

1. 7a 在 B 上加了频道通配路由 `from:["*"] → to:["self"]`；
2. `WebUI.add_route()` → `on_routes_changed` → `broadcast_routes()` 把整张路由表推给 A；
3. A 收到 `from:["*"] → B` 后，按频道“源”规则**真的重新启动了一路采集发送**（真实 ssrc `3a959cbf`）；
4. B 的接收端 `_evict_superseded` 对同一个 owner 只保留一条流：真实流与注入的测试流互相顶替，
   真实流后到就把 `0xB0A7` 顶掉，`wait_stream(0xB0A7)` 超时 → 报错。

证据：失败现场 `received` 增加 460、`denied` 不变、流表里只剩真实 ssrc `3a959cbf`，
说明注入包是被“真实流”顶掉，而不是被准入拒收。

### 修法（测试隔离，不动产品行为）

在广播→只听断言做完后，立刻删除 B 上的通配路由（`removed` 会一并推给 A），
并等 A 的真实发送会话清零，再做后面的点对点注入。

已改：`tools/selftest_twonode.py`（7a 之后新增删除通配路由 + 等待 `node_a.sender.sessions` 清空）。

### 复跑结果

修完后连跑 6 次，**6/6 全部 34 项通过、0 失败**：

| 轮次 | 结果 |
| --- | --- |
| RUN 1 | 34/34 |
| RUN 2 | 34/34 |
| RUN 3 | 34/34 |
| RUN 4 | 34/34 |
| RUN 5 | 34/34 |
| RUN 6 | 34/34 |

结论：**已定位并修复（测试隔离问题，产品代码未改）**，不再是“偶发未定位”。

---

## 9. 2026-11-08 落地：音量算法绑到送声档位（最终方案）

> 用户原话（转述）：“外放档就是读取系统媒体流音量和内部软件增益。静音档就只读内部软件增益。
> 反正我们的静音方式就是锁定系统媒体流音量为 0。”
> 因此**不再向用户暴露 `system` / `capture` 概念**，只保留送声档位。

### 9.1 口径

| 档位 | 手机端 `media_q14` | 实际乘到 PCM 的 `total_q14` | 统计包 `vol_in_capture` | 电脑端系数 |
|---|---|---|---|---|
| **外放档**（sendMode=1） | 当前 `STREAM_MUSIC` | `gain × media / 16384` | `false` | 恒 `1.0` |
| **静音档**（sendMode=2，`internal_only`） | 固定 `16384`（不乘） | `gain` | `true` | 恒 `1.0` |
| **静音档**（sendMode=2，`last_nonzero`） | 进静音前最后一次非零系统音量 | `gain × media / 16384` | `true` | 恒 `1.0` |

不变式：**两端合计只乘一次**。手机端两档都会先处理一次；电脑端看到
`vol_source=external|silent` 就返回 `1.0`，不再叠加（`_volume_factor_for()`）。

代码位置：

- `android/app/src/main/java/com/audiomixing/app/SendVolume.java` —— 纯逻辑：
  `mediaQ14()` / `totalQ14()` / `volInCapture()` / `modeText()` / `policyText()`；
- `AudioSend.java` → `applyVolumePolicy()`（每 400 ms 或首帧重算一次）；
- `Diagnosis.java` → 统计包字段 `vol_mode` / `vol_source` / `vol_policy` / `vol_in_capture`；
- `audiomixing/engine.py` → `_volume_factor_for()` / `_peer_vol_source_for()` /
  `_peers_with_media_volume()`；
- `audiomixing/config.py` → `phone_silent_vol_policy`（默认 `internal_only`）；
- `audiomixing/webui.py` + `audiomixing/ui/index.html` → 设置项与中文提示。

### 9.2 为什么默认 `internal_only`（乘 1.0）

静音档里 `STREAM_MUSIC` 被我们**恒定为 0**，这个 0 没有信息量；拿它当乘数会把 PCM 乘成
静音，拿“旧值”又会让用户**调了没反应**。所以默认只保留 PCM 里已经存在的 App 内部增益：

- 调音量 → 由 App 自己的内部增益负责（类型 B 有效）；
- 类型 A 在静音档里本来就调不动（见 9.5 限制 2）。

### 9.3 可选 `last_nonzero`（沿用进静音前的音量）

给“外放档压到 25% 的信息不想在切静音档时丢掉”的用户：

- 手机端记住进入静音前最后一次非零系统音量，静音档继续乘它；
- 设置里中文写作「沿用进静音前的音量」；
- 没有可用旧值时退回 1.0（绝不把 PCM 乘成 0）。

默认仍是 `internal_only`，理由见 9.2。

### 9.4 门槛与证据

- 单元测试（`tests/test_audiomixing.py`）：
  - 两档声明 → 电脑端 `volume_factor` 恒 `1.0`；
  - 静音档策略下发控制包、对端行暴露 `vol_source` / `vol_policy`；
  - APK dex 含 `silent_vol_policy` / `vol_policy`。
- Java 纯逻辑探针（`android/tools/SendVolumeProbe.java`）：
  - 外放档 `post/raw ≈ media×gain/16384`；
  - 静音档 `internal_only` → `post/raw ≈ gain/16384`；
  - 静音档 `last_nonzero` 正确、无旧值时退回 1.0；
  - 上下限、策略归一化。
- 电脑侧链路门禁（`python tools/check_volume_chain.py`）：
  - A 段：两档下 `post/raw ≈ total/16384`，电脑系数恒 `1.0`；
  - D 段：`external` / `silent` × `internal_only` / `last_nonzero`
    的声明与电脑端系数全部为 `1.0`，诊断写出档位与乘数来源。

### 9.5 两条限制（界面与文档都必须写）

1. **静音档乘数为 1.0 时，耳机侧相当于满格放。** 从外放档切到静音档可能**明显变响** ——
   外放档里“压到 25%”的那部分信息在静音档不存在。
2. **滑杆本身就是在改 `STREAM_MUSIC` 的 App（B 站、抖音这类），在静音档里调音量无效。**
   系统音量被我们钉死 0，既改不动也读不到；这类 App 要用滑杆请切**外放档**。
   内部增益类 App（YouTube 播放器、VLC）不受影响。

这两条同时写在：

- 手机端「详情」页的音量策略设置下方；
- 电脑端设置页「静音档乘数策略」下方；
- `docs/测试App清单.md`。

### 9.6 电脑端能看到/能改什么

- 设置页：`只用 App 内部音量（推荐）` / `沿用进静音前的音量`；
- 读到手机当前档位与乘数来源：
  - 设备卡片：`vol_mode` / `vol_source` / `vol_policy` / `volume_note`；
  - 诊断「对端」与「手机采集层」：`档位=外放档/静音档 …　乘数来源=…`。

### 9.7 未验

- [ ] 真实第三方 App（B 站/抖音/QQ 音乐/腾讯视频/VLC）拖滑杆时的端到端听感 ——
  **未验（需出声条件）**；
- [ ] 静音档切外放档的“变响”幅度主观量级 —— 需真机耳机试听。

真实设备判据与下载入口见 `docs/测试App清单.md`；一键取证脚本见
`tools/adb_test_flow.ps1`。
