# 手机 ↔ 手机 直传（P2P）协议 —— v1

> 目标：同一个局域网里，两台手机不装 App，用浏览器就能把声音直接传给对方。
> 电脑只做**信令转发**（WebSocket 转发几十字节的 JSON），**音频完全不经过电脑**。

## 一、为什么这么做

```
· 现有能力：手机 → 电脑 → 另一台手机（电脑要中转音频 ✓ 但多了两跳）
· 本协议：手机 ←WebRTC 直连→ 手机（音频 P2P ✓ 电脑只转发协商信息 ✓）
· 顺带好处：浏览器自带 Opus 编码 + 抖动缓冲 + 拥塞控制 + 回声消除
```

## 二、信令通道

```
WebSocket   /ws/p2p?role=host|guest&room=NNNN&name=XXX

  role=host   发起方（生成房间号的手机）
  role=guest  加入方（输入房间号的手机）
  room        4 位数字房间号（host 生成，局域网内够用）
  name        设备显示名（可选，默认"手机"）

★ 服务端只做三件事：
   1. 按 room 把两个连接配对（同 room 第三人直接收到 error 并被关闭）
   2. 把一方的文本消息原样转发给另一方（不做任何解析）
   3. 一方断开时通知另一方
★ 服务端【绝不】接收、缓存或转发任何音频数据。
```

## 三、消息格式（全部是 JSON 文本，UTF-8）

双向：

```json
{"t":"hello","role":"host","name":"小红"}
{"t":"peer","name":"小蓝"}                     // 服务端通知：对端已就位，可以开始协商
{"t":"offer","sdp":"..."}                      // host → guest
{"t":"answer","sdp":"..."}                     // guest → host
{"t":"ice","candidate":"...","mid":"0","mlin":0}   // 双向，可能多条
{"t":"bye"}                                    // 主动挂断
{"t":"error","msg":"房间号已满"}                 // 服务端 → 客户端
```

服务端在配对成功时，向双方各发一条：

```json
{"t":"peer","name":"对端的名字"}
```

**注意**：`hello`/`peer`/`error` 是**服务端产生**的；`offer`/`answer`/`ice`/`bye` 是**客户端产生、服务端原样转发**的。

## 四、协商流程

```
host 连上 → 拿到 room 号（页面显示，另一台手机输入）
guest 连上 → 服务端配对 → 双方各收到 {"t":"peer",...}

host:  pc = new RTCPeerConnection(ICE)
       track = 本地音频（麦克风 / 系统声音，看浏览器能力）
       pc.addTrack(track)
       offer = await pc.createOffer()
       await pc.setLocalDescription(offer)
       send({"t":"offer","sdp":pc.localDescription.sdp})

guest: 收到 offer → pc = new RTCPeerConnection(ICE)
       pc.ontrack = 播放（接到 <audio autoplay> 或 WebAudio）
       await pc.setRemoteDescription({type:"offer",sdp})
       answer = await pc.createAnswer()
       await pc.setLocalDescription(answer)
       send({"t":"answer","sdp":pc.localDescription.sdp})

双向:   pc.onicecandidate → send({"t":"ice", candidate:JSON.stringify(c)})
       收到 ice → await pc.addIceCandidate(JSON.parse(candidate))

ICE 配置：局域网内只需要 host 候选，不配 STUN/TURN：
   { "iceServers": [], "iceCandidatePoolSize": 0 }
```

## 五、页面行为（p2p.html）

```
顶部：Audio Mixing 标题 + 返回控制台的链接
选择角色：
   「我要发送」→ 显示 4 位房间号（大字，方便另一台手机看）
   「我要接收」→ 输入房间号 → 连接
中间：状态区（等待对方 / 已连接 / 正在传输 / 出错）
★ 实时电平条（发送方显示采集电平，接收方显示播放电平）—— 有没有声音一眼看出
底部：折叠的详情（连接状态 / ICE 状态 / 码率 / 已接收秒数）
★ 必须写清的限制：
   · 安卓 Chrome 可以直接用（http 局域网地址允许采集）
   · iPhone 的 Safari 要求 https 才能拿麦克风 —— 页面里要提示这一点
   · 想传"手机正在播放的音乐"，浏览器拿不到（系统限制），只能传麦克风
```

## 六、服务端实现位置

```
新文件：audiomixing/net/p2p.py      —— 房间表 + 配对 + 转发（纯标准库，不依赖 websockets 包）
接线：  audiomixing/webui.py        —— 在 WebSocket 升级处按路径分发 /ws/p2p
页面：  audiomixing/ui/p2p.html     —— 手机端页面
入口：  /p2p  → p2p.html；控制台加一个二维码/链接
```

**WebSocket 实现约定**：项目里的 `/ws/audio` 已有握手与帧读写代码，`p2p.py`
只负责"房间与转发"这一层，帧的编解码沿用同一套工具（不要另写一份）。

## 七、验收标准

```
□ 同一房间的两个连接能互相收到对方的消息（服务端单测）
□ 第三个连接收到 error 并被关闭（服务端单测）
□ 一方断开，另一方收到 bye（服务端单测）
□ 服务端不缓存任何二进制音频（代码里不出现对 payload 的存储）
□ p2p.html 能被页面脚本检查工具解析通过
□ /p2p 返回 200 且是 p2p.html 的内容
□ 两台手机（或两个浏览器标签）能连通并听到声音 —— 由用户在真机上确认
```
