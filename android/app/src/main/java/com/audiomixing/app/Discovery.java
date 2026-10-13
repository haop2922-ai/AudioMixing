package com.audiomixing.app;

import android.content.Context;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.MulticastSocket;
import java.net.NetworkInterface;
import java.net.SocketException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Enumeration;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 自主发现：往局域网里喊自己的名字，同时听别人喊。
 *
 * <p><b>为什么不做配对/房间号/二维码</b>：用户要的是"打开就该看见对方"。
 * 所以这里一句话都不需要用户输入 —— 每 2 秒发一份 JSON 信标到组播组
 * {@code 239.255.77.120:47830}，<b>同时</b>再往子网广播地址发一份：
 * 很多家用/商用 Wi-Fi 的 AP 隔离组播（IGMP snooping 配置烂、或者干脆挡组播），
 * 只发组播会出现"一台能看见另一台、反过来看不见"的诡异现象，广播是兜底通道。
 *
 * <p><b>为什么必须拿 MulticastLock</b>：安卓为了省电，Wi-Fi 固件默认会把
 * 组播/广播帧直接丢掉（不在 DTIM 唤醒时投递给应用）。不拿这个锁，
 * {@code receive()} 会一直超时 —— 表现就是"两台手机都在发，但谁也看不见谁"。
 * 这是整个"自主发现"能不能用的关键，所以宁可多要一个权限也要锁住。
 *
 * <p>信标格式与电脑端 {@code audiomixing/net/discovery.py} 保持一致（逐字段对齐），
 * 这样手机能看见电脑、电脑也能看见手机：
 * <pre>
 * {"wl":1,"id":"&lt;uuid&gt;","name":"小红的手机","platform":"Android-14",
 *  "version":"1.0.0","audio_port":47831,"web_port":0,
 *  "sources":[{"id":"mic","name":"麦克风","detail":"mic"}],
 *  "sinks":[{"id":"speaker","name":"本机扬声器","detail":"render"}],
 *  "busy":false,"scope":"lan","ts":1700000000}
 * </pre>
 *
 * <p><b>线程约定</b>：所有网络 IO（建 socket、加入组播组、收发）都在自己的线程里，
 * 主线程只调用 {@link #start()} / {@link #stop()} / {@link #peers()}（纯内存操作）。
 * {@link Listener} 的回调在后台线程触发，界面层自己用 {@code runOnUiThread} 切回去。
 */
public final class Discovery {

    private static final String TAG = "AM-Discovery";

    /** 信标间隔：2 秒（见设计文档第二节）。 */
    public static final int BEACON_INTERVAL_MS = 2000;
    /** 超过这么久没收到某台的信标，就从列表里摘掉。 */
    public static final int PEER_TIMEOUT_MS = 15000;
    /** 文档规定信标不超过 1200 字节，超了说明字段写爆了，只记日志不改变行为。 */
    private static final int MAX_BEACON_BYTES = 1200;
    /** 收包缓冲区：信标很小，4096 足够，也顺手挡掉别人的大包。 */
    private static final int RECV_BUF = 4096;
    /** 版本号写死：这属于协议字段，跟 build.gradle 的 versionName 解耦，免得改 UI 版本把协议也改了。 */
    private static final String PROTOCOL_VERSION = "1.0.0";

    /** 设备列表变化回调（后台线程）。 */
    public interface Listener {
        /** 列表有增/删/改时触发；{@code peers} 是快照，可以直接持有。 */
        void onPeersChanged(List<Peer> peers);

        /** 出错但不致命（组播加不进去、锁拿不到…），给界面拿去显示"详情"。 */
        default void onDiscoveryError(String message) {
        }
    }

    /**
     * 局域网里听到的一台设备。字段都是 volatile：网络线程写、界面线程读。
     */
    public static final class Peer {
        /** 节点 UUID（电脑端和手机端同一套）。 */
        public final String nodeId;
        public volatile String name = "";
        /** 对端 IP（明文点分十进制）。 */
        public volatile String host = "";
        public volatile int audioPort = Proto.DEFAULT_AUDIO_PORT;
        /** 手机没有网页面板，正常是 0；电脑端会填 17800。 */
        public volatile int webPort;
        public volatile String platform = "";
        public volatile String version = "";
        /** 对端正在传输时置真，界面可以显示"忙"。 */
        public volatile boolean busy;
        /** 对端能提供的音源名字（手机就是"麦克风"）。 */
        public volatile List<String> sources = Collections.emptyList();
        /** 对端能播放的名字（手机就是"本机扬声器"）。 */
        public volatile List<String> sinks = Collections.emptyList();
        /** 最后一次收到信标的墙钟毫秒（给人看的）。 */
        public volatile long lastSeen;
        /** 单调时钟（开机毫秒）：判断超时只能用这个，系统改时间不能把设备"改没了"。 */
        private volatile long seenTick;

        Peer(String nodeId) {
            this.nodeId = nodeId;
        }

        public String shortId() {
            return Proto.shortId(nodeId);
        }

        /** 距上次收到信标过了多少毫秒。 */
        public long ageMs() {
            long d = SystemClock.elapsedRealtime() - seenTick;
            return d < 0 ? 0 : d;
        }

        public boolean online() {
            return ageMs() < PEER_TIMEOUT_MS;
        }

        /** 界面上显示的名字，没名字时退回短 ID。 */
        public String label() {
            String n = name;
            return (n == null || n.isEmpty()) ? shortId() : n;
        }

        @Override
        public String toString() {
            return label() + "@" + host + ":" + audioPort + "(" + platform + ", " + ageMs() + "ms)";
        }

        @Override
        public boolean equals(Object o) {
            return o instanceof Peer && nodeId.equals(((Peer) o).nodeId);
        }

        @Override
        public int hashCode() {
            return nodeId.hashCode();
        }
    }

    private final Context appCtx;              // 可能为 null（拿不到 WifiManager 时也能发现，只是容易丢包）
    private final String nodeId;
    private final Object lock = new Object();
    private final Object socketLock = new Object();
    /** 按 nodeId 索引；LinkedHashMap 只是让 peers() 的顺序稳定一点（界面不闪）。 */
    private final Map<String, Peer> peers = new LinkedHashMap<>();

    private volatile String nodeName;
    private volatile int audioPort;
    private volatile int webPort;
    private volatile boolean busy;
    private volatile boolean running;
    private volatile MulticastSocket sock;
    private volatile int boundPort = Proto.DEFAULT_DISCOVERY_PORT;
    private volatile List<String> localIps = Collections.emptyList();
    private volatile Listener listener;
    private volatile String lastError = "";
    private volatile long beaconsSent;
    private volatile long beaconsReceived;

    // ── ★ 发现层自检（2026-10-12 真机反馈：74 秒只收到 8 个信标、列表里 0 台设备，
    //    而"到底是我们没发出去、还是对方的帧被 Wi-Fi 丢了"完全看不出来）。
    //    下面这些计数就是为了把这个问题一刀切开。──
    /** 组播/广播/单播各发出去多少份（哪条通道是通的，一眼可见）。 */
    private volatile long beaconsSentMc;
    private volatile long beaconsSentBc;
    private volatile long beaconsSentUni;
    /**
     * 自己发的信标绕回来收到几份。
     *
     * <p><b>这是最省事也最有用的一个自检</b>：socket 允许回环（setLoopbackMode(false)），
     * 所以只要"收组播"这条链路在本机是通的，我们就会收到自己的信标。
     * 如果这个数一直是 0，那说明本机根本收不到组播 —— 那就别再怀疑对方了，
     * 先解决网卡/AP 的组播问题（或者换广播/单播通道）。
     */
    private volatile long beaconsSelf;
    /** 收到但解析不出信标的帧（不是我们的协议 / 乱码），和 wl 版本不对的分开记。 */
    private volatile long beaconsBad;
    private volatile long beaconsWrongVersion;
    private volatile String lastBadFrameSample = "";
    /** 最近一次收到"自己"的信标是什么时候（elapsedRealtime）。 */
    private volatile long lastSelfBeaconMs;
    /** 最近一次收到别人信标的现场：谁、从哪个 IP、什么时候。 */
    private volatile String lastBeaconFrom = "";
    private volatile long lastBeaconAtMs;
    private volatile String lastBeaconNote = "";
    /** 最近一轮单播信标发给了几台已知设备（0 = 还没见过任何人，只能靠组播/广播）。 */
    private volatile int unicastTargets;
    /** 成功加入组播组的网卡名（空 = 一个都没加进去，只能靠广播）。 */
    private volatile String mcastIfaces = "";

    private WifiManager.MulticastLock mcastLock;
    private Thread txThread;
    private Thread rxThread;
    private boolean dirty;                     // 列表有变化但还没通知界面

    // ★ 节点身份必须持久化。
    //
    // 为什么这是硬需求（2026-10-12 真机反馈）：原来是每次启动都 UUID.randomUUID() 新建一个，
    // 于是**手机每重启一次，电脑的发现列表里就多出一台重名设备**（实测：同一台手机在同一 IP
    // 上挂了三份，三个不同的 node_id）。而且电脑端的路由表、ssrc 都是按 node_id 记的 ——
    // 换了 id，之前配好的路由就等于指向了一个不存在的节点。
    private static final String PREFS_NAME = "audiomixing";
    private static final String KEY_NODE_ID = "node_id";

    /** 取持久化的节点 ID；没有就生成一个存下来（装一次生成一次，之后永远不变）。 */
    private static String loadOrCreateNodeId(Context ctx) {
        if (ctx == null) {
            return java.util.UUID.randomUUID().toString();
        }
        try {
            android.content.SharedPreferences sp =
                    ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
            String id = sp.getString(KEY_NODE_ID, "");
            if (id != null && !id.isEmpty()) {
                return id;
            }
            String fresh = java.util.UUID.randomUUID().toString();
            sp.edit().putString(KEY_NODE_ID, fresh).apply();
            Log.i(TAG, "首次启动，生成节点 ID 并保存：" + fresh);
            return fresh;
        } catch (Throwable t) {
            // 存不了就只能退回"每次新建"（旧行为），但要让日志/诊断看得见。
            Log.w(TAG, "节点 ID 存不下来（这次会是临时的）：" + t);
            return java.util.UUID.randomUUID().toString();
        }
    }

    public Discovery(Context context) {
        this(context, null, null, Proto.DEFAULT_AUDIO_PORT, 0);
    }

    public Discovery(Context context, String nodeId, String nodeName, int audioPort, int webPort) {
        this.appCtx = context == null ? null : context.getApplicationContext();
        this.nodeId = (nodeId != null && !nodeId.isEmpty())
                ? nodeId : loadOrCreateNodeId(this.appCtx);
        this.nodeName = (nodeName == null || nodeName.isEmpty()) ? defaultName() : nodeName;
        this.audioPort = audioPort > 0 ? audioPort : Proto.DEFAULT_AUDIO_PORT;
        this.webPort = Math.max(0, webPort);
    }

    // ------------------------------------------------------------------ 对外

    /** 本机名：直接取机型（"Pixel 7"），用户一眼能认出哪台是哪台。 */
    public static String defaultName() {
        String model = Build.MODEL;
        return (model == null || model.isEmpty()) ? "Android 手机" : model;
    }

    public static String defaultPlatform() {
        String rel = Build.VERSION.RELEASE;
        return "Android-" + ((rel == null || rel.isEmpty()) ? "?" : rel);
    }

    /**
     * 开始发现。<b>立刻返回</b>：建 socket / 加入组播组 / 收锁都在后台线程做，
     * 因为安卓禁止主线程碰网络（NetworkOnMainThreadException），
     * 而且 Wi-Fi 抖动时这些调用可能卡住，界面绝不能跟着卡。
     */
    public synchronized void start() {
        if (running) {
            return;
        }
        running = true;
        txThread = new Thread(this::txLoop, "am-beacon-tx");
        rxThread = new Thread(this::rxLoop, "am-beacon-rx");
        txThread.setDaemon(true);
        rxThread.setDaemon(true);
        txThread.start();
        rxThread.start();
    }

    /**
     * 停止发现并释放组播锁。
     *
     * <p>注意：不要在 {@link Listener} 回调里直接调用它（回调就在本类的接收线程上，
     * join 自己会死等）。这里做了保护，但换个线程调用才是正解。
     */
    public synchronized void stop() {
        running = false;
        closeSocket();
        Thread tx = txThread;
        Thread rx = rxThread;
        txThread = null;
        rxThread = null;
        joinQuiet(tx);
        joinQuiet(rx);
        releaseMulticastLock();
        synchronized (lock) {
            peers.clear();
        }
        notifyPeers(true);
    }

    public boolean isRunning() {
        return running;
    }

    /** 当前在线设备快照（按名字排序，已过期的不会出现在这里）。 */
    public List<Peer> peers() {
        List<Peer> out;
        synchronized (lock) {
            out = new ArrayList<>(peers.values());
        }
        out.sort(Comparator.comparing(Peer::label, String.CASE_INSENSITIVE_ORDER));
        return out;
    }

    /** 设置回调；会立刻用当前列表回调一次，界面不用傻等到下一个信标。 */
    public void setListener(Listener l) {
        listener = l;
        if (l != null) {
            l.onPeersChanged(peers());
        }
    }

    public String nodeId() {
        return nodeId;
    }

    /**
     * 本机信标的原文（诊断报告里贴一行）。
     *
     * <p>为什么值得贴出来：「电脑那边看到我的 id 是空的」这类怀疑，只有把<b>我们自己真正
     * 发出去的字节</b>摆出来才能一次说清 —— 里面有 id、有 audio_port、有多少字节，
     * 不用再靠猜。顺带也能一眼看出信标有没有超协议上限（1200 字节）。
     */
    public String beaconPreview() {
        try {
            byte[] b = buildBeacon();
            String s = new String(b, StandardCharsets.UTF_8);
            return s.length() > 320 ? (s.substring(0, 320) + "…（共 " + b.length + " 字节）")
                    : (s + "（共 " + b.length + " 字节）");
        } catch (Throwable t) {
            return "信标组装失败：" + t;
        }
    }

    public String name() {
        return nodeName;
    }

    /** 改名字后下一个信标就带出去（2 秒内生效）。 */
    public void setName(String name) {
        if (name != null && !name.isEmpty()) {
            nodeName = name;
        }
    }

    public int audioPort() {
        return audioPort;
    }

    /** 音频端口如果被占，Engine 会换一个，这里要跟着改，信标里如实写。 */
    public void setAudioPort(int port) {
        if (port > 0) {
            audioPort = port;
        }
    }

    public boolean busy() {
        return busy;
    }

    public void setBusy(boolean b) {
        busy = b;
    }

    /** 本机在局域网里的 IPv4 地址（界面"详情"里显示用）。 */
    public List<String> localIps() {
        return localIps;
    }

    /** 实际绑定的发现端口（正常就是 47830）。 */
    public int boundPort() {
        return boundPort;
    }

    public long beaconsSent() {
        return beaconsSent;
    }

    public long beaconsReceived() {
        return beaconsReceived;
    }

    /** 组播/广播/单播各发出去多少份（哪条通道是通的，一眼可见）。 */
    public long beaconsSentMulticast() {
        return beaconsSentMc;
    }

    public long beaconsSentBroadcast() {
        return beaconsSentBc;
    }

    public long beaconsSentUnicast() {
        return beaconsSentUni;
    }

    /** 自己发的信标绕回来收到几份（>0 就证明本机收组播这条路是通的）。 */
    public long beaconsSelfReceived() {
        return beaconsSelf;
    }

    /** 收到但解析不出信标的帧数 / 版本不对的帧数。 */
    public long badFrames() {
        return beaconsBad;
    }

    public long wrongVersionFrames() {
        return beaconsWrongVersion;
    }

    public String lastBadFrameSample() {
        return lastBadFrameSample;
    }

    /** 最近一轮单播信标发给了几台已知设备。 */
    public int unicastTargets() {
        return unicastTargets;
    }

    /** 成功加入组播组的网卡（空 = 一张都没加进去，只能靠广播）。 */
    public String multicastIfaces() {
        return mcastIfaces;
    }

    public boolean multicastLockHeld() {
        WifiManager.MulticastLock l = mcastLock;
        try {
            return l != null && l.isHeld();
        } catch (Throwable t) {
            return l != null;
        }
    }

    /** 最近一次收到"自己的"信标距今多久（毫秒）；-1 = 从来没收到过。 */
    public long lastSelfBeaconAgeMs() {
        long t = lastSelfBeaconMs;
        return t <= 0 ? -1 : Math.max(0, SystemClock.elapsedRealtime() - t);
    }

    /** 最近一次收到别人的信标是谁、什么时候（人话）。 */
    public String lastBeaconText() {
        if (lastBeaconFrom.isEmpty()) {
            return "一个别人的信标都没收到";
        }
        return lastBeaconFrom + " " + lastBeaconNote + "，"
                + ageText(lastBeaconAtMs, SystemClock.elapsedRealtime());
    }

    /**
     * 发现层自检（多行，人话）—— 给诊断报告和界面"详情"直接用。
     *
     * <p>为什么值得占报告篇幅：真机上"列表里 0 台设备"可能是好几种完全不同的问题 ——
     * 本机没发出去 / 发出去了对端没收到 / 对端发了我没收到 / 收到了但不是我们的协议 /
     * 收到了但地址走错网卡。以前这些从报告里一个都看不出来，只能猜。
     */
    public String selfCheckText() {
        long now = SystemClock.elapsedRealtime();
        StringBuilder sb = new StringBuilder(640);
        sb.append("信标发  ：组播 ").append(beaconsSentMc)
                .append(" / 广播 ").append(beaconsSentBc)
                .append(" / 单播给已知设备 ").append(beaconsSentUni)
                .append("（本轮 ").append(unicastTargets).append(" 台）\n");
        sb.append("信标收  ：别人的 ").append(beaconsReceived)
                .append(" · 自己绕回来 ").append(beaconsSelf)
                .append(" · 不是我们的 ").append(beaconsBad)
                .append(" · 版本不对 ").append(beaconsWrongVersion).append('\n');
        sb.append("组播锁  ：").append(multicastLockHeld()
                ? "已持有 ✓" : "没有 ✗（安卓会把组播/广播帧丢掉）").append('\n');
        sb.append("加入组播：").append(mcastIfaces.isEmpty()
                ? "没有网卡加进去 ✗（只能靠广播这一路）" : mcastIfaces).append('\n');
        if (beaconsSelf > 0) {
            sb.append("自检    ：收到过自己的信标 ✓ → 本机收组播这条路是通的（最近 ")
                    .append(ageText(lastSelfBeaconMs, now)).append("）\n");
        } else {
            sb.append("自检    ：从没收到过自己的信标 ✗ → 本机收不到组播/广播。")
                    .append("问题在本机或 AP（Wi-Fi 省电、AP 的 IGMP/客户端隔离），")
                    .append("对方发得再好也没用\n");
        }
        sb.append("最近收到：").append(lastBeaconText()).append('\n');
        sb.append("本机网卡：").append(ifaceText()).append('\n');
        // ★ 节点身份：持久化之后每次启动都一样，电脑端才不会把同一台手机记成好几台。
        sb.append("我的节点 ID：").append(nodeId)
                .append("（持久化保存，重启不变 ✓）\n");
        if (lastBadFrameSample.length() > 0) {
            sb.append("非信标帧：").append(lastBadFrameSample).append('\n');
        }
        return sb.toString();
    }

    /** 每张网卡一行：名字、IPv4、支不支持组播。判断"是不是走错网卡"用。 */
    private String ifaceText() {
        StringBuilder sb = new StringBuilder(192);
        try {
            Enumeration<NetworkInterface> all = NetworkInterface.getNetworkInterfaces();
            while (all != null && all.hasMoreElements()) {
                NetworkInterface nif = all.nextElement();
                if (!nif.isUp() || nif.isLoopback() || !hasIpv4(nif)) {
                    continue;
                }
                if (sb.length() > 0) {
                    sb.append("；");
                }
                sb.append(nif.getName()).append('(');
                StringBuilder ips = new StringBuilder(24);
                Enumeration<InetAddress> as = nif.getInetAddresses();
                while (as != null && as.hasMoreElements()) {
                    InetAddress a = as.nextElement();
                    if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress()) {
                        if (ips.length() > 0) {
                            ips.append('/');
                        }
                        ips.append(a.getHostAddress());
                    }
                }
                sb.append(ips);
                sb.append(nif.supportsMulticast() ? " 支持组播" : " 不支持组播");
                try {
                    if (nif.isVirtual()) {
                        sb.append(" 虚拟");
                    }
                } catch (Throwable ignored) {
                    // 个别 ROM 不支持这个查询
                }
                sb.append(')');
            }
        } catch (Throwable t) {
            return "读取网卡失败：" + t;
        }
        return sb.length() == 0 ? "没有可用的非回环 IPv4 网卡" : sb.toString();
    }

    /** "3.2 秒前" 这种人话。 */
    private static String ageText(long atMs, long nowMs) {
        if (atMs <= 0) {
            return "从没收到过";
        }
        long d = Math.max(0, nowMs - atMs);
        if (d < 1000) {
            return d + " 毫秒前";
        }
        return String.format(java.util.Locale.US, "%.1f 秒前", d / 1000.0);
    }

    /** 对端地址和本机哪个地址同网段（/24）：判断"是不是走错网卡"最直接的线索。 */
    private String subnetNote(String host) {
        int dot = host.lastIndexOf('.');
        if (dot <= 0) {
            return "（地址格式看不懂）";
        }
        String prefix = host.substring(0, dot + 1);
        for (String ip : localIps) {
            if (ip.startsWith(prefix)) {
                return "（与本机 " + ip + " 同网段 ✓）";
            }
        }
        StringBuilder mine = new StringBuilder(48);
        for (String ip : localIps) {
            if (mine.length() > 0) {
                mine.append('/');
            }
            mine.append(ip);
        }
        return "（不在本机任何网段 ✗ 本机地址："
                + (mine.length() == 0 ? "没有非回环地址" : mine.toString()) + "）";
    }

    /** 一个"不是我们信标"的帧样本：来源 + 长度 + 开头 8 字节 hex + 能打印出来的字符。 */
    private static String sampleFrame(byte[] buf, int len, InetAddress from) {
        int n = Math.min(8, len);
        StringBuilder hex = new StringBuilder(32);
        for (int i = 0; i < n; i++) {
            int v = buf[i] & 0xFF;
            if (v < 16) {
                hex.append('0');
            }
            hex.append(Integer.toHexString(v)).append(' ');
        }
        StringBuilder txt = new StringBuilder(24);
        for (int i = 0; i < Math.min(24, len); i++) {
            int c = buf[i] & 0xFF;
            txt.append((c >= 0x20 && c < 0x7F) ? (char) c : '.');
        }
        return "来自 " + normalizeHost(from) + "，共 " + len + " 字节，开头 " + hex
                + "（可打印：" + txt + "）";
    }

    /** 最近一次错误，空串表示一切正常。 */
    public String lastError() {
        return lastError;
    }

    // ------------------------------------------------------- 发送（信标）线程

    private void txLoop() {
        // 第一件事就是收组播锁：放在这里而不是 start()，同样是为了不占主线程。
        acquireMulticastLock();
        while (running) {
            try {
                // 网卡地址变了（切 Wi-Fi / 开关热点 / VPN 起来）就必须重开 socket 重新加组，
                // 否则会一直对着旧网卡发，表现就是"换了个 Wi-Fi 就看不见设备了"。
                List<String> now = scanLocalIps();
                if (!now.equals(localIps)) {
                    localIps = now;
                    reopenSocket();
                } else if (sock == null) {
                    ensureSocket();
                }
                MulticastSocket s = sock;
                if (s != null) {
                    sendBeacon(s);
                }
            } catch (Throwable t) {
                // 这里绝不能让线程死掉：Wi-Fi 抖动、厂商 ROM 的奇怪异常都从这儿兜住。
                note("信标发送出错：" + t);
            }
            sleepQuiet(BEACON_INTERVAL_MS);
        }
    }

    private void sendBeacon(MulticastSocket s) {
        byte[] data = buildBeacon();
        if (data.length > MAX_BEACON_BYTES) {
            Log.w(TAG, "信标 " + data.length + " 字节，超过协议上限 " + MAX_BEACON_BYTES);
        }
        int sent = 0;
        for (InetSocketAddress target : beaconTargets()) {
            try {
                s.send(new DatagramPacket(data, data.length, target.getAddress(), target.getPort()));
                sent++;
                if (target.getAddress() != null && target.getAddress().isMulticastAddress()) {
                    beaconsSentMc++;
                } else {
                    beaconsSentBc++;
                }
            } catch (IOException | RuntimeException e) {
                // 某一个地址发不出去很正常（比如 /24 广播地址在当前网段不存在），
                // 只要还有一份发出去就算数，不能因为一个地址失败就放弃整轮。
            }
        }
        // ★ 单播给"已经见过面的设备"。
        //
        // 为什么必须加这一条：安卓 Wi-Fi 在省电模式下会大量丢组播/广播帧（真机实测：
        // 74 秒里对方发了 ~37 轮信标，我们只收到 8 个）。而发现列表 15 秒没信标就把设备
        // 摘掉，于是表现成"设备时有时无、列表里经常是 0 台"。更糟的是对面那台也一样：
        // 它把我们标成"不在线"之后，它的**广播音频就不会发给我们**（电脑端广播路由只发给
        // 在线节点），于是变成"电脑明明在广播，手机一个包都收不到"。
        // 单播帧不会被 AP 的组播过滤丢掉，只要互相见过一次面，这条路就一直通。
        int uni = 0;
        for (String host : knownPeerHosts()) {
            try {
                s.send(new DatagramPacket(data, data.length,
                        InetAddress.getByName(host), Proto.DEFAULT_DISCOVERY_PORT));
                uni++;
                beaconsSentUni++;
            } catch (IOException | RuntimeException e) {
                // 对端刚走、IP 已经不可达：很正常，忽略。
            }
        }
        unicastTargets = uni;
        if (sent > 0 || uni > 0) {
            beaconsSent++;
        } else {
            note("信标一个都没发出去（Wi-Fi 可能断了）");
        }
    }

    /** 已经见过面的设备 IP（单播信标用）。有上限：局域网里不会有这么多，防的是被人乱刷撑爆。 */
    private List<String> knownPeerHosts() {
        List<String> out = new ArrayList<>(8);
        synchronized (lock) {
            for (Peer p : peers.values()) {
                String h = p.host;
                if (h != null && !h.isEmpty() && !out.contains(h)) {
                    out.add(h);
                    if (out.size() >= 16) {
                        break;
                    }
                }
            }
        }
        return out;
    }

    /** 组播一份 + 受限广播一份 + 每个本机 /24 的定向广播一份。 */
    private List<InetSocketAddress> beaconTargets() {
        List<InetSocketAddress> out = new ArrayList<>(8);
        out.add(new InetSocketAddress(groupAddress(), Proto.DEFAULT_DISCOVERY_PORT));
        // 受限广播：有些 AP 放行这个但不放行组播。
        try {
            out.add(new InetSocketAddress(InetAddress.getByName("255.255.255.255"), Proto.DEFAULT_DISCOVERY_PORT));
        } catch (IOException ignored) {
        }
        // 定向广播（192.168.1.255）：比受限广播更可靠，路由器不会丢。
        for (String ip : localIps) {
            int dot = ip.lastIndexOf('.');
            if (dot <= 0) {
                continue;
            }
            try {
                out.add(new InetSocketAddress(
                        InetAddress.getByName(ip.substring(0, dot) + ".255"), Proto.DEFAULT_DISCOVERY_PORT));
            } catch (IOException ignored) {
            }
        }
        return out;
    }

    /**
     * 组播组的 InetAddress。只用 IPv4：IPv6 那边地址格式完全不同，
     * 而且安卓在双栈下 joinGroup 容易挑错网卡，局域网场景 IPv4 足够。
     */
    private InetAddress groupAddress() {
        try {
            return InetAddress.getByName(Proto.DISCOVERY_GROUP);
        } catch (IOException e) {
            throw new IllegalStateException("组播地址解析失败", e);
        }
    }

    private byte[] buildBeacon() {
        try {
            JSONObject o = new JSONObject();
            o.put("wl", 1);
            o.put("id", nodeId);
            o.put("name", nodeName);
            o.put("platform", defaultPlatform());
            o.put("version", PROTOCOL_VERSION);
            o.put("audio_port", audioPort);
            o.put("web_port", webPort);
            JSONArray sources = new JSONArray();
            sources.put(item("mic", "麦克风", "mic"));
            o.put("sources", sources);
            JSONArray sinks = new JSONArray();
            sinks.put(item("speaker", "本机扬声器", "render"));
            o.put("sinks", sinks);
            o.put("busy", busy);
            // scope/ts 是电脑端就有的字段，手机也带上，双方解析逻辑就完全对称了。
            o.put("scope", "lan");
            o.put("ts", System.currentTimeMillis() / 1000L);
            return o.toString().getBytes(StandardCharsets.UTF_8);
        } catch (Exception e) {
            // JSON 都是自己塞的常量，真出错也只可能是内存问题；给个最小可解析的信标保命。
            note("信标组装失败：" + e);
            return ("{\"wl\":1,\"id\":\"" + nodeId + "\",\"name\":\"" + nodeName
                    + "\",\"audio_port\":" + audioPort + "}").getBytes(StandardCharsets.UTF_8);
        }
    }

    private static JSONObject item(String id, String name, String detail) throws Exception {
        JSONObject o = new JSONObject();
        o.put("id", id);
        o.put("name", name);
        o.put("detail", detail);
        return o;
    }

    // ------------------------------------------------------- 接收（信标）线程

    private void rxLoop() {
        byte[] buf = new byte[RECV_BUF];
        DatagramPacket pkt = new DatagramPacket(buf, buf.length);
        long lastSweep = 0;
        while (running) {
            MulticastSocket s = sock;
            if (s == null) {
                ensureSocket();
                s = sock;
                if (s == null) {
                    sleepQuiet(300);
                    continue;
                }
            }
            try {
                pkt.setLength(buf.length);
                s.receive(pkt);
                handleBeacon(buf, pkt.getLength(), pkt.getAddress());
            } catch (SocketTimeoutException te) {
                // 超时是正常的：用它当心跳顺便做过期清理，省一个专用线程。
            } catch (IOException e) {
                if (!running) {
                    return;
                }
                // socket 被换掉（切网）时会走到这里；睡一下再取新的 socket，线程不能退出。
                sleepQuiet(100);
            } catch (Throwable t) {
                note("信标接收出错：" + t);
                sleepQuiet(100);
            }
            long now = SystemClock.elapsedRealtime();
            if (now - lastSweep >= 1000) {
                lastSweep = now;
                sweep();
            }
            notifyPeers(false);
        }
    }

    private void handleBeacon(byte[] buf, int len, InetAddress from) {
        if (len <= 0 || len > RECV_BUF) {
            return;
        }
        JSONObject o;
        try {
            o = new JSONObject(new String(buf, 0, len, StandardCharsets.UTF_8));
        } catch (Exception e) {
            // ★ 不是我们的信标（或者是别人家的 UDP 包）。计数 + 留一个样本：
            //   "一个都没收到"和"收到了但都不是我们的"是完全不同的两个问题。
            if (len > 0) {
                beaconsBad++;
                if (lastBadFrameSample.length() == 0) {
                    lastBadFrameSample = sampleFrame(buf, len, from);
                }
            }
            return;
        }
        if (o.optInt("wl", 0) != 1) {
            beaconsWrongVersion++;
            if (lastBadFrameSample.length() == 0) {
                lastBadFrameSample = "wl=" + o.optInt("wl", 0) + "（我们的是 1），" + sampleFrame(buf, len, from);
            }
            return;
        }
        String id = o.optString("id", "");
        if (id.isEmpty()) {
            return;
        }
        if (nodeId.equals(id)) {
            // ★ 自己的信标绕回来了 —— 说明"收组播/广播"这条链路在本机是通的。
            //   这个数一直是 0 就意味着本机根本收不到组播，问题在本机/AP，不在对方。
            beaconsSelf++;
            lastSelfBeaconMs = SystemClock.elapsedRealtime();
            return;   // 自己的信标不能进列表（否则会多出一台"自己"）
        }
        String host = normalizeHost(from);
        if (host.isEmpty()) {
            return;
        }
        beaconsReceived++;
        lastBeaconFrom = o.optString("name", "") + "@" + host
                + (o.optInt("audio_port", 0) > 0 ? (":" + o.optInt("audio_port", 0)) : "");
        lastBeaconAtMs = SystemClock.elapsedRealtime();
        lastBeaconNote = subnetNote(host);

        synchronized (lock) {
            Peer peer = peers.get(id);
            boolean fresh = peer == null;
            if (fresh) {
                peer = new Peer(id);
                peers.put(id, peer);
            }
            // 一台设备可能同时从 Wi-Fi 和 VPN 隧道发信标：优先留局域网地址，
            // 否则音频会被塞进慢且抖的隧道里（电脑端 address_rank 是同一个道理）。
            if (fresh || hostBetter(host, peer.host)) {
                if (!host.equals(peer.host)) {
                    peer.host = host;
                    dirty = true;
                }
            }
            String name = o.optString("name", "");
            if (!name.isEmpty() && !name.equals(peer.name)) {
                peer.name = name;
                dirty = true;
            }
            String platform = o.optString("platform", "");
            if (!platform.isEmpty()) {
                peer.platform = platform;
            }
            peer.version = o.optString("version", peer.version);
            int ap = o.optInt("audio_port", 0);
            if (ap > 0) {
                peer.audioPort = ap;
            }
            int wp = o.optInt("web_port", 0);
            if (wp >= 0) {
                peer.webPort = wp;
            }
            peer.busy = o.optBoolean("busy", false);
            peer.sources = readItems(o.optJSONArray("sources"));
            peer.sinks = readItems(o.optJSONArray("sinks"));
            peer.lastSeen = System.currentTimeMillis();
            peer.seenTick = SystemClock.elapsedRealtime();
            if (fresh) {
                dirty = true;
            }
        }
    }

    private static List<String> readItems(JSONArray arr) {
        if (arr == null || arr.length() == 0) {
            return Collections.emptyList();
        }
        List<String> out = new ArrayList<>(arr.length());
        for (int i = 0; i < arr.length(); i++) {
            JSONObject it = arr.optJSONObject(i);
            if (it == null) {
                continue;
            }
            String n = it.optString("name", "");
            out.add(n.isEmpty() ? it.optString("id", "") : n);
        }
        return out;
    }

    /** 摘掉超时设备（15 秒没信标就当它走了）。 */
    private void sweep() {
        long now = SystemClock.elapsedRealtime();
        synchronized (lock) {
            java.util.Iterator<Map.Entry<String, Peer>> it = peers.entrySet().iterator();
            while (it.hasNext()) {
                Peer p = it.next().getValue();
                if (now - p.seenTick > PEER_TIMEOUT_MS) {
                    it.remove();
                    dirty = true;
                    Log.i(TAG, "设备超时移除：" + p);
                }
            }
        }
    }

    /** 把"列表变了"告诉界面；不在锁里回调，避免界面层回调里调 peers() 造成死锁。 */
    private void notifyPeers(boolean force) {
        Listener l = listener;
        if (l == null) {
            return;
        }
        boolean changed;
        synchronized (lock) {
            changed = dirty;
            dirty = false;
        }
        if (!changed && !force) {
            return;
        }
        List<Peer> snapshot = peers();
        try {
            l.onPeersChanged(snapshot);
        } catch (Throwable t) {
            note("回调异常：" + t);   // 界面层的 bug 不能把发现线程带走
        }
    }

    // ------------------------------------------------------------ socket 管理

    // 这里两处都是"故意用旧 API"：
    // · setLoopbackMode 的替代品 DatagramSocket.setOption 要 API 33+，而 minSdk 是 24；
    // · joinGroup(InetAddress) 只在"没有任何网卡支持组播"的兜底分支里走。
    @SuppressWarnings("deprecation")
    private void ensureSocket() {
        if (sock != null) {
            return;
        }
        synchronized (socketLock) {
            if (sock != null || !running) {
                return;
            }
            try {
                MulticastSocket s = new MulticastSocket(null);   // 先不绑，才能先设 reuse
                s.setReuseAddress(true);   // 和电脑端同时开着时也能各收各的
                s.setBroadcast(true);      // 不设这个，广播那份信标会被内核拒掉
                boolean bound = false;
                for (int port : new int[]{Proto.DEFAULT_DISCOVERY_PORT, 0}) {
                    try {
                        s.bind(new InetSocketAddress(port));
                        boundPort = s.getLocalPort();
                        bound = true;
                        break;
                    } catch (IOException e) {
                        note("绑定发现端口 " + port + " 失败：" + e.getMessage());
                    }
                }
                if (!bound) {
                    closeQuietly(s);
                    return;
                }
                s.setTimeToLive(2);   // 只在本网段晃，不往路由器外面跑
                try {
                    // 注意这个 API 的参数是"是否禁用回环"，false 才是允许回环。
                    // 允许回环方便同机双进程自测；自己的信标靠 nodeId 过滤掉。
                    s.setLoopbackMode(false);
                } catch (SocketException ignored) {
                }
                joinGroups(s);
                s.setSoTimeout(500);   // 用超时驱动清理和列表变更通知
                sock = s;
                Log.i(TAG, "发现已启动：端口 " + boundPort + " 组播 " + Proto.DISCOVERY_GROUP
                        + " 本机地址 " + localIps);
            } catch (Throwable t) {
                note("打开发现 socket 失败：" + t);
            }
        }
    }

    /**
     * 加入组播组。逐个网卡尝试而不是 joinGroup(addr) 一把梭：
     * 手机同时有 wlan0 和 rmnet（数据）时，默认网卡可能被选成蜂窝，
     * 那样组播就永远收不到 —— 这是"发现时好时坏"的经典原因。
     */
    @SuppressWarnings("deprecation")
    private void joinGroups(MulticastSocket s) throws IOException {
        InetAddress group = groupAddress();
        InetSocketAddress sa = new InetSocketAddress(group, boundPort);
        NetworkInterface preferred = null;
        int preferredScore = -1;
        int joined = 0;
        StringBuilder joinedNames = new StringBuilder(32);
        Enumeration<NetworkInterface> all = NetworkInterface.getNetworkInterfaces();
        while (all != null && all.hasMoreElements()) {
            NetworkInterface nif = all.nextElement();
            try {
                if (!nif.isUp() || nif.isLoopback() || !nif.supportsMulticast() || !hasIpv4(nif)) {
                    continue;
                }
                s.joinGroup(sa, nif);
                joined++;
                if (joinedNames.length() > 0) {
                    joinedNames.append('/');
                }
                joinedNames.append(nif.getName());
                int score = ifaceScore(nif.getName());
                if (score > preferredScore) {
                    preferredScore = score;
                    preferred = nif;
                }
            } catch (IOException | RuntimeException e) {
                // 某张网卡加不进去（蜂窝网卡常见）不影响其它网卡。
                // ★ 但要记下来："一张都没加进去"和"加进去了但收不到"是两种不同的问题。
                if (joinedNames.length() > 0) {
                    joinedNames.append('/');
                }
                joinedNames.append(nif.getName()).append("失败");
            }
        }
        mcastIfaces = joinedNames.toString();
        if (joined == 0) {
            // 兜底：让系统自己挑默认网卡，总比一条都收不到强。
            s.joinGroup(group);
            mcastIfaces = "（没有网卡能加组播，已回退到默认网卡）" + mcastIfaces;
            note("没有网卡支持组播，已回退到默认网卡（可能只能靠广播发现）");
        }
        if (preferred != null) {
            try {
                s.setNetworkInterface(preferred);   // 发出去的组播也走同一张网卡
            } catch (SocketException ignored) {
            }
        }
    }

    /** Wi-Fi > 热点 > 有线 > 其它。 */
    private static int ifaceScore(String name) {
        if (name == null) {
            return 0;
        }
        if (name.startsWith("wlan")) {
            return 4;
        }
        if (name.startsWith("ap") || name.startsWith("swlan")) {
            return 3;
        }
        if (name.startsWith("eth")) {
            return 2;
        }
        return 1;
    }

    private static boolean hasIpv4(NetworkInterface nif) {
        Enumeration<InetAddress> addrs = nif.getInetAddresses();
        while (addrs != null && addrs.hasMoreElements()) {
            InetAddress a = addrs.nextElement();
            if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress()) {
                return true;
            }
        }
        return false;
    }

    private void reopenSocket() {
        Log.i(TAG, "网卡地址变化，重启发现 socket：" + localIps);
        closeSocket();
        ensureSocket();
    }

    private void closeSocket() {
        MulticastSocket s;
        synchronized (socketLock) {
            s = sock;
            sock = null;
        }
        closeQuietly(s);
    }

    private static void closeQuietly(MulticastSocket s) {
        if (s != null) {
            try {
                s.close();
            } catch (Throwable ignored) {
            }
        }
    }

    // ------------------------------------------------------------------ 杂项

    private void acquireMulticastLock() {
        if (mcastLock != null || appCtx == null) {
            return;
        }
        try {
            WifiManager wm = (WifiManager) appCtx.getSystemService(Context.WIFI_SERVICE);
            if (wm == null) {
                note("拿不到 WifiManager，组播可能被系统丢掉");
                return;
            }
            WifiManager.MulticastLock l = wm.createMulticastLock("audiomixing-discovery");
            // 交给系统做引用计数的话，别的组件一次 release 就把我们的锁放了；
            // 自己管引用计数，stop() 时释放一次即可。
            l.setReferenceCounted(false);
            l.acquire();
            mcastLock = l;
        } catch (Exception e) {
            note("MulticastLock 获取失败：" + e);
        }
    }

    private void releaseMulticastLock() {
        WifiManager.MulticastLock l = mcastLock;
        mcastLock = null;
        if (l != null) {
            try {
                if (l.isHeld()) {
                    l.release();
                }
            } catch (Exception ignored) {
            }
        }
    }

    /** 本机所有非回环 IPv4 地址。 */
    private static List<String> scanLocalIps() {
        List<String> out = new ArrayList<>(4);
        try {
            Enumeration<NetworkInterface> all = NetworkInterface.getNetworkInterfaces();
            while (all != null && all.hasMoreElements()) {
                NetworkInterface nif = all.nextElement();
                try {
                    if (!nif.isUp() || nif.isLoopback()) {
                        continue;
                    }
                    Enumeration<InetAddress> addrs = nif.getInetAddresses();
                    while (addrs != null && addrs.hasMoreElements()) {
                        InetAddress a = addrs.nextElement();
                        if (a instanceof java.net.Inet4Address && !a.isLoopbackAddress()) {
                            out.add(a.getHostAddress());
                        }
                    }
                } catch (SocketException ignored) {
                }
            }
        } catch (SocketException e) {
            return Collections.emptyList();
        }
        Collections.sort(out);
        return out;
    }

    /** DatagramPacket 里的地址有时带 "::ffff:" 前缀或 "/"（IPv6 映射），统一成点分十进制。 */
    private static String normalizeHost(InetAddress a) {
        if (a == null) {
            return "";
        }
        String h = a.getHostAddress();
        if (h == null) {
            return "";
        }
        int slash = h.indexOf('/');
        if (slash >= 0) {
            h = h.substring(slash + 1);
        }
        if (h.startsWith("::ffff:")) {
            h = h.substring(7);
        }
        return h;
    }

    /**
     * 这个地址是不是比现有的更好。分数高者胜，同分时后来的赢（对端换 IP 要跟得上）。
     * 判据：私有/链路本地 > 其它；同网段 > 不同网段。
     */
    private boolean hostBetter(String candidate, String current) {
        if (current == null || current.isEmpty()) {
            return true;
        }
        if (candidate.equals(current)) {
            return false;
        }
        return rank(candidate) >= rank(current);
    }

    private int rank(String host) {
        boolean lan = isPrivate(host);
        boolean sameSubnet = false;
        for (String ip : localIps) {
            int dot = ip.lastIndexOf('.');
            if (dot > 0 && host.startsWith(ip.substring(0, dot + 1))) {
                sameSubnet = true;
                break;
            }
        }
        return (lan ? 2 : 0) + (sameSubnet ? 1 : 0);
    }

    private static boolean isPrivate(String host) {
        String[] p = host.split("\\.");
        if (p.length != 4) {
            return false;
        }
        try {
            int a = Integer.parseInt(p[0]);
            int b = Integer.parseInt(p[1]);
            if (a == 10 || a == 127 || a == 169 && b == 254) {
                return true;
            }
            if (a == 192 && b == 168) {
                return true;
            }
            return a == 172 && b >= 16 && b <= 31;
        } catch (NumberFormatException e) {
            return false;
        }
    }

    private void note(String msg) {
        lastError = msg;
        Log.w(TAG, msg);
        Listener l = listener;
        if (l != null) {
            try {
                l.onDiscoveryError(msg);
            } catch (Throwable ignored) {
            }
        }
    }

    private static void sleepQuiet(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    private static void joinQuiet(Thread t) {
        if (t == null || t == Thread.currentThread()) {
            return;   // join 自己会死等，保护一下
        }
        try {
            t.join(1500);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
