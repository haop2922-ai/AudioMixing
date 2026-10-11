package com.audiomixing.app;

import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Enumeration;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 引擎：把「自主发现（Discovery）」「送声音（AudioSend）」「收听（AudioRecv）」串成一台可用的节点。
 *
 * <p>设计意图（见 docs/Android端设计.md 第四、五节）：
 * <ul>
 *   <li><b>没有配对、没有房间号、没有二维码。</b>Discovery 收到谁的信标，谁的卡片就出现在界面上，
 *       点一下就能用。电脑在不在都一样 —— 手机之间直连，不借任何中转。</li>
 *   <li>引擎是<b>进程级单例</b>：界面（MainActivity）和前台服务（MixService）共用同一个实例，
 *       所以 App 退到后台、界面被销毁，声音也不会断。</li>
 *   <li>发现 / 采集 / 播放各自在自己的线程里跑（见各自的类注释），这里只做状态簿记，
 *       <b>绝不阻塞界面线程</b>。</li>
 * </ul>
 *
 * <p>线程约定：本类的方法都可以从界面线程调。除了 {@link #peers()} 与
 * {@link #recvStats()} 会去读对方的快照（都是纯内存操作、对方也做了同步），其余纯状态读写。
 */
public final class Engine {

    private static final String TAG = "AudioMixing";

    /** 统计快照的复用窗口：界面每 150ms 刷一次，没必要每次都新建一个 Stats。 */
    private static final long STATS_TTL_MS = 120L;

    /** ★ 对端统计的交换周期：每 2 秒互报一次（用户说的"让俩设备自己交流"）。 */
    private static final long STATS_INTERVAL_MS = 2000L;
    /** 收到的对端统计保留多久：过期的留在内存里没意义（界面会显示"没在回话"）。 */
    private static final long PEER_STATS_KEEP_MS = 60000L;
    /** 最多记多少台的对端统计：局域网里不会有这么多，防的是被人乱刷撑爆内存。 */
    private static final int PEER_STATS_MAX = 32;

    private static volatile Engine sInstance;

    /** 取进程内唯一的引擎（Activity 与 Service 共用，保证退到后台不断流）。 */
    public static Engine get(Context ctx) {
        Engine e = sInstance;
        if (e != null) {
            return e;
        }
        synchronized (Engine.class) {
            if (sInstance == null) {
                Context app = (ctx != null) ? ctx.getApplicationContext() : null;
                sInstance = new Engine(app != null ? app : ctx);
            }
            return sInstance;
        }
    }

    private final Context app;
    private final Object lock = new Object();

    private volatile Discovery discovery;
    /** 同一时刻只送一路（麦克风只有一支），换目标时先停旧的。 */
    private volatile AudioSend sender;
    /** 一个 UDP 端口收，收到几台就混几台 —— 这本来就是个"混音"工具。 */
    private volatile AudioRecv receiver;

    /** 用户点了「收听」的设备 id（可以多台）。 */
    private final Set<String> listenIds = new LinkedHashSet<String>();
    /**
     * 收听目标的地址（按 id 记）。
     *
     * <p>为什么要单独记：白名单原本是在 applyAllowedHosts() 里现查 peers() 得到的，
     * 而 Wi-Fi 上偶尔丢一两个信标是常态。设备只要在列表里闪一下，白名单就会退化成
     * "只允许 255.255.255.255"，于是一个包都收不进来 —— 真机现场表现就是
     * "界面还显示收听中，但缓冲 0ms、流 0"，而且自己永远好不了。
     * 把地址记在用户点选的那一刻，就不受设备列表抖动影响了。
     */
    private final Map<String, String> listenHosts = new LinkedHashMap<String, String>();
    /** 用户点了「送声音」的设备 id（只会有 0 或 1 台）。 */
    private final Set<String> sendIds = new LinkedHashSet<String>();

    /** Discovery 回调线程推过来的快照；peers() 优先问 Discovery，失败时退回这份。 */
    private volatile List<Discovery.Peer> cache = Collections.emptyList();

    private volatile boolean started;
    private volatile boolean sawPeer;      // 这次运行里至少见过一台设备（用于判断"目标是真的走了"）
    private volatile float volume = 1f;
    private volatile int audioPort = Proto.DEFAULT_AUDIO_PORT;
    private volatile String lastSendError = "";

    private volatile AudioRecv.Stats statsCache;
    private volatile long statsCacheAt;

    // ── ★ 对端统计交换（用户要的"让俩设备自己交流"）──

    /** 对端自己报的统计，按来源 IP 存。key = host，value 每次收到都换成新对象（不再修改）。 */
    private final Map<String, Diagnosis.PeerStats> peerStats =
            new LinkedHashMap<String, Diagnosis.PeerStats>();
    /**
     * 专门保护 {@link #peerStats} 的锁，<b>刻意不复用 {@link #lock}</b>。
     *
     * <p>为什么：写这份统计的是<b>收包线程</b>（AudioRecv 的回调），而 {@link #lock} 会被
     * 界面线程拿着去停接收器（{@code stopReceiver()} 里有 join 等待收包线程退出）。
     * 如果两边抢同一把锁，就变成"界面线程等收包线程、收包线程等界面线程"——
     * 靠 join 的超时能解开，但界面会白卡一下。分开锁就完全没有这个交集。
     */
    private final Object peerLock = new Object();
    /**
     * 收听目标的端口（和 listenHosts 一起、在用户点选那一刻记下来）。
     *
     * <p>为什么要单独记端口：统计包要单播到对方的 audio_port（信标里公布的那个）。
     * 跟地址同样的道理 —— 设备列表抖一下不至于让统计发不出去。
     */
    private final Map<String, Integer> listenPorts = new LinkedHashMap<String, Integer>();

    /** 统计包发送线程：每 2 秒给"和我有关系的每一台"各发一个。 */
    private volatile Thread statsThread;
    private volatile boolean statsRunning;
    private volatile long statsSent;
    private volatile String statsLastError = "";
    /** 统计包的固定 ssrc：不是音频流，只为了在对端日志里能认出是同一个人发的。 */
    private final int statsSsrc = ((int) System.nanoTime()) | 1;

    private Engine(Context ctx) {
        this.app = ctx;
    }

    // ────────────────────────────── 生命周期 ──────────────────────────────

    /** 启动发现。可以重复调用（前台服务与界面都会叫一次，第二次是空操作）。 */
    public void start() {
        synchronized (lock) {
            if (started) {
                return;
            }
            started = true;
            sawPeer = false;
            audioPort = Proto.DEFAULT_AUDIO_PORT;
            try {
                Discovery d = new Discovery(app);
                // 回调在发现线程上，这里只做一件事：把列表存下来，别在这里碰界面。
                d.setListener(new Discovery.Listener() {
                    @Override
                    public void onPeersChanged(List<Discovery.Peer> p) {
                        if (p != null) {
                            cache = p;
                        }
                    }
                });
                d.start();
                discovery = d;
                // 端口真被占时 Discovery 会换一个，接收端必须跟着它走，否则信标里公布的端口没人听。
                audioPort = d.audioPort() > 0 ? d.audioPort() : Proto.DEFAULT_AUDIO_PORT;
            } catch (Throwable t) {
                // Wi-Fi 没开、组播锁拿不到等等：记下来但不让界面挂掉。
                Log.w(TAG, "启动自主发现失败", t);
            }
            // 对端统计的发送线程：和发现一起起来，它自己会在"没有任何关系"时静默空转。
            synchronized (peerLock) {
                peerStats.clear();
            }
            ensureStatsThread();
        }
    }

    /** 停止所有传输并关掉发现。 */
    public void stop() {
        synchronized (lock) {
            started = false;
            listenIds.clear();
            listenHosts.clear();
            listenPorts.clear();
            sendIds.clear();
            stopReceiver();
            stopSender();
            stopStatsThread();
            synchronized (peerLock) {
                peerStats.clear();
            }
            Discovery d = discovery;
            discovery = null;
            if (d != null) {
                try {
                    d.stop();
                } catch (Throwable t) {
                    Log.w(TAG, "停止发现失败", t);
                }
            }
            cache = Collections.emptyList();
            statsCache = null;
        }
    }

    public boolean isStarted() {
        return started;
    }

    // ────────────────────────────── 设备（自主发现） ──────────────────────────────

    /** 当前局域网里看到的设备。返回的是拷贝，界面线程随便遍历，不受发现线程影响。 */
    public List<Discovery.Peer> peers() {
        Discovery d = discovery;
        if (d != null) {
            try {
                List<Discovery.Peer> l = d.peers();
                if (l != null) {
                    cache = l;
                    if (!l.isEmpty()) {
                        sawPeer = true;
                    }
                    return new ArrayList<Discovery.Peer>(l);
                }
            } catch (Throwable t) {
                Log.w(TAG, "读取设备列表失败，用上一次的快照", t);
            }
        }
        return new ArrayList<Discovery.Peer>(cache);
    }

    /** 设备身份：优先用节点 UUID，没有就退回 IP（同一台机器不会因为换字段就变成两张卡片）。 */
    public static String idOf(Discovery.Peer p) {
        if (p == null) {
            return "";
        }
        String id = p.nodeId;
        if (id != null && id.length() > 0) {
            return id;
        }
        return p.host == null ? "" : p.host;
    }

    // ────────────────────────────── 收听 / 送声音 ──────────────────────────────

    /** 点「收听」：收这台发来的声音。可以同时收听多台，声音会混在一起。 */
    public void setListening(Discovery.Peer p, boolean on) {
        if (p == null) {
            return;
        }
        String id = idOf(p);
        synchronized (lock) {
            if (on) {
                listenIds.add(id);
                if (p.host != null && !p.host.isEmpty()) {
                    // 记下地址：设备列表以后怎么抖，这条收听都不受影响。
                    listenHosts.put(id, p.host);
                    // 端口一起记住：对端统计要单播到它的 audio_port（信标里公布的那个）。
                    listenPorts.put(id, p.audioPort > 0 ? p.audioPort : Proto.DEFAULT_AUDIO_PORT);
                }
            } else {
                listenIds.remove(id);
                listenHosts.remove(id);
                listenPorts.remove(id);
            }
            if (listenIds.isEmpty()) {
                // 用户明确取消了全部收听：这时候才停接收器。
                // （注意顺序：上面已经把 id 移除，所以这里判断的是"用户真的都取消了"。）
                stopReceiver();
            } else {
                ensureReceiver();
                applyAllowedHosts();
            }
        }
    }

    /** 点「送声音」：把我的麦克风送给这台。换目标会自动切换，不需要先关。 */
    public void setSending(Discovery.Peer p, boolean on) {
        if (p == null) {
            return;
        }
        String id = idOf(p);
        synchronized (lock) {
            if (on) {
                sendIds.clear();
                sendIds.add(id);
            } else {
                sendIds.remove(id);
            }
            stopSender();
            if (sendIds.isEmpty()) {
                // 送声停了、又没有别的收听目标：接收器只是刚才为了收对端统计才开着的，
                // 一起收掉（否则它会一直占着 UDP 端口和一个 WifiLock）。
                if (listenIds.isEmpty()) {
                    stopReceiver();
                }
                return;
            }
            Discovery.Peer target = findById(id);
            if (target == null || target.host == null || target.host.isEmpty()) {
                return;
            }
            int port = target.audioPort > 0 ? target.audioPort : Proto.DEFAULT_AUDIO_PORT;
            try {
                // 用带 Context 的构造：它会顺手拿一个 WifiLock，息屏后不至于一顿一顿的。
                AudioSend s = new AudioSend(app, target.host, port);
                s.setGain(volume);
                s.start();
                sender = s;
                lastSendError = "";
                // ★ 送声音的同时把接收器也开起来：对方会把"我收到了多少"用统计包发回来，
                //   没有接收器就收不到这句回话（白名单里没有收听目标，所以不会出声）。
                //   两边互报状态，用户才能一眼看出"是哪一端没在发/没收"。
                ensureReceiver();
            } catch (Throwable t) {
                // 拿不到麦克风（被别的 App 占用 / 没权限）时不要把界面搞崩。
                Log.w(TAG, "启动送声音失败 -> " + target.host + ":" + port, t);
                lastSendError = String.valueOf(t);
                sendIds.clear();
            }
        }
    }

    public boolean isListening(Discovery.Peer p) {
        synchronized (lock) {
            return p != null && listenIds.contains(idOf(p));
        }
    }

    public boolean isSending(Discovery.Peer p) {
        synchronized (lock) {
            return p != null && sendIds.contains(idOf(p));
        }
    }

    public int listeningCount() {
        synchronized (lock) {
            return listenIds.size();
        }
    }

    public int sendingCount() {
        synchronized (lock) {
            return sendIds.size();
        }
    }

    /** 「已连接几台」：收或发都算连上（同一台既收又发只算一台）。 */
    public int connectedCount() {
        synchronized (lock) {
            Set<String> all = new LinkedHashSet<String>(listenIds);
            all.addAll(sendIds);
            return all.size();
        }
    }

    /**
     * 把已经消失的设备从「收听 / 送声」名单里摘掉。
     *
     * <p>为什么要这个：对端关机或走出 Wi-Fi 后，Discovery 15 秒会把它的卡片摘掉，
     * 卡片一没，用户就再也点不到那个开关 —— 麦克风却还开着。这既是隐私问题也是体验问题。
     * 所以卡片消失 = 自动停掉对应的传输。
     *
     * <p>只有"确实见过设备、且发现服务在跑"时才清理：否则 Wi-Fi 刚打开、列表还是空的时候，
     * 用户刚点下的开关会被立刻清掉。
     */
    public void pruneGoneTargets() {
        synchronized (lock) {
            if (!sawPeer || (sendIds.isEmpty() && listenIds.isEmpty())) {
                return;
            }
            Discovery d = discovery;
            if (d == null || !d.isRunning()) {
                return;
            }
            Set<String> alive = new LinkedHashSet<String>();
            for (Discovery.Peer p : peers()) {
                alive.add(idOf(p));
            }
            boolean changed = false;
            for (String id : new ArrayList<String>(sendIds)) {
                if (!alive.contains(id)) {
                    sendIds.remove(id);
                    changed = true;
                }
            }
            // ★ 关键修复（2026-10-12 真机反馈）：不再因为"设备列表里暂时没有"就取消
            // 用户的「收听」选择。Wi-Fi 上偶尔丢一两个信标是常态（15 秒没收到就当设备走了），
            // 设备只要在列表里闪一下，下面这段就会把 listenIds 清空 → 接收器被停 →
            // 而且再也不会自己起来。现场表现正是"界面显示收听中，但缓冲 0ms、流 0"。
            // 用户的勾选只有用户自己能取消；对方真的关机了，收不到包自然就没声音，不需要停。
            if (sendIds.isEmpty()) {
                stopSender();
            } else if (changed) {
                // 送声音的目标没了就真的没得送了（麦克风不该一直开着）。
                stopSender();
            }            // 自愈：只要还有收听目标，接收器就必须活着（可能被别处停掉过）。
            if (!listenIds.isEmpty() && receiver == null) {
                ensureReceiver();
            } else if (listenIds.isEmpty() && sender == null && receiver != null) {
                // 既不听也不送：接收器只是刚才为了收对端统计才开着的，可以收掉了（省电、放端口）。
                stopReceiver();
            }
            applyAllowedHosts();
        }
    }

    private Discovery.Peer findById(String id) {
        for (Discovery.Peer p : peers()) {
            if (id.equals(idOf(p))) {
                return p;
            }
        }
        return null;
    }

    private void ensureReceiver() {
        if (receiver != null) {
            return;
        }
        try {
            // 带 Context 的构造会拿 WifiLock：不收包时 Wi-Fi 进省电模式会把抖动缓冲冲垮。
            AudioRecv r = new AudioRecv(app, audioPort);
            // ★ 对端统计的回话入口：对方每 2 秒会说一次"我发了多少、是不是正在发"。
            //   回调在收包线程上，所以只做"解析 + 存起来"；解析失败就当没收到，
            //   绝不能让一份坏包把"收听"整条链路带走。
            r.setStatsListener(new AudioRecv.StatsListener() {
                @Override
                public void onStats(String host, byte[] payload) {
                    onPeerStats(host, payload);
                }
            });
            r.start();
            receiver = r;
            applyAllowedHosts();
        } catch (Throwable t) {
            Log.w(TAG, "启动收听失败（端口 " + audioPort + " 可能被占）", t);
        }
    }

    /**
     * 把「要听哪几台」同步给接收端。
     *
     * <p>地址取自 listenHosts（用户点选那一刻记下来的），**不**在这里现查设备列表 ——
     * 现查的话，设备只要在列表里闪一下，白名单就会退化成"只允许 255.255.255.255"，
     * 一个包都收不进来，而且不会自己恢复（这是真机上遇到的头号问题）。
     *
     * <p>AudioRecv 的语义是"名单空 = 全收"，所以这里必须把名单显式写满 ——
     * 一台都没有时留一个不可能命中的占位地址，避免退化成全收。
     */
    private void applyAllowedHosts() {
        AudioRecv r = receiver;
        if (r == null) {
            return;
        }
        try {
            r.clearAllowed();
            int n = 0;
            for (String id : listenIds) {
                String host = listenHosts.get(id);
                if (host != null && !host.isEmpty()) {
                    r.allow(host, true);
                    n++;
                }
            }
            if (n == 0) {
                r.allow("255.255.255.255", true);
            }
        } catch (Throwable t) {
            Log.w(TAG, "同步收听名单失败", t);
        }
    }

    private void stopReceiver() {
        AudioRecv r = receiver;
        receiver = null;
        statsCache = null;
        if (r != null) {
            try {
                r.stop();
            } catch (Throwable t) {
                Log.w(TAG, "停止收听失败", t);
            }
        }
    }

    private void stopSender() {
        AudioSend s = sender;
        sender = null;
        if (s != null) {
            try {
                s.stop();
            } catch (Throwable t) {
                Log.w(TAG, "停止送声音失败", t);
            }
        }
    }

    // ────────────────────────── ★ 对端统计交换（两机互报） ──────────────────────────

    /**
     * 收到一份对端统计（<b>跑在收包线程上</b>）。
     *
     * <p>为什么这里还要再兜一层 try/catch：{@link AudioRecv} 的收包线程虽然也兜了，
     * 但真机 Wi-Fi 上什么怪包都有，解析失败顶多是这一份统计丢掉，
     * <b>绝不能让"收听"整条链路陪葬</b>（那条链路刚刚才修好）。
     */
    private void onPeerStats(String host, byte[] payload) {
        try {
            if (host == null || host.isEmpty() || payload == null) {
                return;
            }
            Diagnosis.PeerStats ps = Diagnosis.parse(payload, payload.length);
            if (ps == null) {
                return;                 // 不是统计包/被截断了，静默丢掉
            }
            ps.host = host;
            ps.receivedAtMs = SystemClock.elapsedRealtime();
            synchronized (peerLock) {
                peerStats.remove(host);
                peerStats.put(host, ps);   // 重新插入 = 最新的排在最后，下面按顺序淘汰最老的
                while (peerStats.size() > PEER_STATS_MAX) {
                    java.util.Iterator<String> it = peerStats.keySet().iterator();
                    if (!it.hasNext()) {
                        break;
                    }
                    it.next();
                    it.remove();
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "对端统计解析失败（已忽略这一份）", t);
        }
    }

    /** 某一台自己报告的状态；没有就是 null（界面据此显示"等它回话…"）。 */
    public Diagnosis.PeerStats peerStats(String host) {
        if (host == null) {
            return null;
        }
        synchronized (peerLock) {
            return peerStats.get(host);
        }
    }

    /** 所有对端报告的统计（按最近回话排序）。返回拷贝，界面线程随便遍历。 */
    public List<Diagnosis.PeerStats> peerStatsList() {
        synchronized (peerLock) {
            return new ArrayList<Diagnosis.PeerStats>(peerStats.values());
        }
    }

    /** 本机已经发出去了多少个统计包（诊断报告里用：为 0 说明统计交换根本没跑起来）。 */
    public long statsPacketsSent() {
        return statsSent;
    }

    private void ensureStatsThread() {
        if (statsRunning) {
            return;
        }
        statsRunning = true;
        Thread t = new Thread(this::statsLoop, "am-stats-tx");
        t.setDaemon(true);
        statsThread = t;
        t.start();
    }

    private void stopStatsThread() {
        statsRunning = false;
        Thread t = statsThread;
        statsThread = null;
        if (t != null && t != Thread.currentThread()) {
            try {
                // 循环里每 100ms 看一眼开关，所以这里很快就能等到它退出，不用干等一轮 2 秒。
                t.join(600);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    /**
     * 统计包发送循环：每 2 秒给"和我有关系的每一台"单播一个 {@code T_STATS}。
     *
     * <p><b>为什么放在引擎这一层、而不是 AudioSend 里</b>：用户完全可能"只收听、不送声音"，
     * 那时候 AudioSend 根本不存在 —— 可恰恰是这种时候最需要告诉对方"我没在发"。
     * 否则对面看到的就是"连上了、数字在动、就是没声音"，还是只能靠猜。
     * 放在这里，统计和具体的音频收发解耦：只要和某台有收听/送声关系，就互报。
     *
     * <p>socket 用临时端口（和 AudioSend 一样），统计包一律发到<b>对方的音频端口</b>
     * （信标里公布的 {@code audio_port}，由对方的 AudioRecv 收）。
     */
    private void statsLoop() {
        DatagramSocket s;
        try {
            s = new DatagramSocket();
            s.setSendBufferSize(16 * 1024);
        } catch (Throwable t) {
            // 统计发不出去不影响音频，只是用户少了"对方视角"这一半信息。
            statsLastError = "统计 socket 打不开：" + t;
            Log.w(TAG, statsLastError, t);
            statsRunning = false;
            return;
        }
        try {
            while (statsRunning) {
                try {
                    sendStatsRound(s);
                } catch (Throwable t) {
                    // 对端还没起来/刚走，send 报错很正常，绝不能因此退出循环。
                    statsLastError = "发统计失败：" + t;
                    Log.w(TAG, statsLastError);
                }
                // 拆成 100ms 小步睡：stop() 时能立刻醒来，不用等满 2 秒。
                for (int i = 0; i < STATS_INTERVAL_MS / 100 && statsRunning; i++) {
                    sleepQuiet(100);
                }
            }
        } finally {
            try {
                s.close();
            } catch (Throwable ignored) {
            }
        }
    }

    /** 发一轮统计：给每台"有关系的设备"各发一个（内容按方向区分）。 */
    private void sendStatsRound(DatagramSocket s) {
        AudioSend snd;
        Map<String, Integer> targets = new LinkedHashMap<String, Integer>();
        synchronized (lock) {
            // 我收听的那些：告诉它们"我收了你多少、我有没有在发"。
            for (Map.Entry<String, String> e : listenHosts.entrySet()) {
                String host = e.getValue();
                if (host == null || host.isEmpty()) {
                    continue;
                }
                Integer port = listenPorts.get(e.getKey());
                targets.put(host, (port != null && port > 0) ? port : Proto.DEFAULT_AUDIO_PORT);
            }
            // 我正在送声音的那台：告诉它"我发了多少、我又收了你多少"。
            snd = sender;
            if (snd != null && snd.host() != null && !snd.host().isEmpty()) {
                targets.put(snd.host(), snd.port() > 0 ? snd.port() : Proto.DEFAULT_AUDIO_PORT);
            }
        }
        if (targets.isEmpty()) {
            expirePeerStats();
            return;
        }

        String sendHost = (snd == null) ? null : snd.host();
        boolean sendingNow = snd != null && snd.isRunning();
        long sent = (snd == null) ? 0 : snd.packetsSent();
        long bytes = (snd == null) ? 0 : snd.bytesSent();
        AudioRecv.Stats st = recvStats();
        int streams = (st == null) ? 0 : st.streamCount;

        for (Map.Entry<String, Integer> t : targets.entrySet()) {
            String host = t.getKey();
            if (host == null || host.isEmpty()) {
                continue;
            }
            int port = (t.getValue() == null || t.getValue() <= 0)
                    ? Proto.DEFAULT_AUDIO_PORT : t.getValue();
            // 只对"我正在送的那台"报已发包数；对别的设备要如实说"我没在发给你"，
            // 否则对面会以为那些包是发给它的（负载里的 target 字段就是干这个的）。
            boolean iSendToIt = sendingNow && host.equals(sendHost);
            // 这条链路的收包数单独取：对方最想知道的就是"我发出去的它到底收到多少"。
            long recv = 0;
            long lost = 0;
            int buf = 0;
            if (st != null && st.streams != null) {
                for (AudioRecv.StreamStats ss : st.streams) {
                    if (host.equals(ss.host)) {
                        recv = ss.received;
                        lost = ss.lost;
                        buf = ss.bufferedMs;
                        break;
                    }
                }
            }
            byte[] payload = Diagnosis.json(nodeName(),
                    iSendToIt ? sent : 0, iSendToIt ? bytes : 0, iSendToIt,
                    recv, lost, buf, streams, host);
            Proto.Pkt pkt = new Proto.Pkt(Proto.T_STATS);
            pkt.ssrc = statsSsrc;                                  // 固定一个，对端日志里好认
            pkt.tsNs = System.currentTimeMillis() * 1_000_000L;
            pkt.payload = payload;
            byte[] raw = Proto.encode(pkt);
            try {
                s.send(new DatagramPacket(raw, raw.length, InetAddress.getByName(host), port));
                statsSent++;
            } catch (Throwable e) {
                statsLastError = "统计包发不到 " + host + ":" + port + "：" + e;
                Log.w(TAG, statsLastError);
            }
        }
        expirePeerStats();
    }

    /** 过期的对端统计定期清掉：界面会显示"没在回话"，没必要一直占着内存。 */
    private void expirePeerStats() {
        long now = SystemClock.elapsedRealtime();
        synchronized (peerLock) {
            java.util.Iterator<Map.Entry<String, Diagnosis.PeerStats>> it =
                    peerStats.entrySet().iterator();
            while (it.hasNext()) {
                Diagnosis.PeerStats ps = it.next().getValue();
                if (ps == null || now - ps.receivedAtMs > PEER_STATS_KEEP_MS) {
                    it.remove();
                }
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

    // ────────────────────────────── 电平 ──────────────────────────────

    /** 送声音的实时电平 0~1（AudioSend 已经归一化过）。 */
    public float sendPeak() {
        AudioSend s = sender;
        if (s == null) {
            return 0f;
        }
        try {
            return clamp01(s.peak());
        } catch (Throwable t) {
            return 0f;
        }
    }

    /** 收听的混音总电平 0~1。 */
    public float recvPeak() {
        AudioRecv r = receiver;
        if (r == null) {
            return 0f;
        }
        try {
            return clamp01(r.peak());
        } catch (Throwable t) {
            return 0f;
        }
    }

    /** 某一台正在发来的声音的电平 0~1（这一行自己的电平条）。 */
    public float recvPeakFor(String host) {
        if (host == null || host.isEmpty()) {
            return 0f;
        }
        AudioRecv.Stats st = recvStats();
        if (st == null || st.streams == null) {
            return 0f;
        }
        for (AudioRecv.StreamStats ss : st.streams) {
            if (host.equals(ss.host)) {
                return clamp01(ss.peak);
            }
        }
        return 0f;   // 这台还没在发（点了收听但对方没点送声音）
    }

    private static float clamp01(float v) {
        if (Float.isNaN(v) || v <= 0f) {
            return 0f;
        }
        return v > 1f ? 1f : v;
    }

    /**
     * 接收统计快照。120ms 内复用同一个对象：界面每 150ms 刷一次、每行都要读电平，
     * 不缓存的话一秒会新建几十个 Stats，没必要。
     */
    public AudioRecv.Stats recvStats() {
        AudioRecv r = receiver;
        if (r == null) {
            return null;
        }
        long now = System.currentTimeMillis();
        AudioRecv.Stats st = statsCache;
        if (st != null && now - statsCacheAt < STATS_TTL_MS) {
            return st;
        }
        try {
            st = r.stats();
            statsCache = st;
            statsCacheAt = now;
            return st;
        } catch (Throwable t) {
            return statsCache;
        }
    }

    // ────────────────────────────── 统计 / 本机信息 ──────────────────────────────

    /**
     * 只看一眼当前实例，<b>不创建</b>。
     *
     * <p>给 {@link Diagnosis#build(android.content.Context)} 用：生成一份诊断报告
     * 不该顺手把引擎建起来（那会真的去开组播端口、拿 WifiLock）。没有实例就返回 null，
     * 报告里照实写"引擎没在运行"。
     */
    public static Engine peek() {
        return sInstance;
    }

    /** 接收器在跑吗（诊断报告和界面都要用）。 */
    public boolean receiverRunning() {
        AudioRecv r = receiver;
        return r != null && r.isRunning();
    }

    /** 发送器在跑吗。注意 start() 之后麦克风还有可能打开失败，所以这里问的是它自己的状态。 */
    public boolean senderRunning() {
        AudioSend s = sender;
        return s != null && s.isRunning();
    }

    public boolean discoveryRunning() {
        Discovery d = discovery;
        return d != null && d.isRunning();
    }

    /** 正在收听哪几台的地址（给诊断报告用的人话）。 */
    public String listenTargetsText() {
        synchronized (lock) {
            StringBuilder sb = new StringBuilder();
            for (String h : listenHosts.values()) {
                if (h == null || h.isEmpty()) {
                    continue;
                }
                if (sb.length() > 0) {
                    sb.append(" / ");
                }
                sb.append(h);
            }
            return sb.length() == 0 ? "（无）" : sb.toString();
        }
    }

    /** 正在送声音给谁（"host:port"，没在送就是"（无）"）。 */
    public String sendTargetText() {
        AudioSend s = sender;
        if (s == null || s.host() == null || s.host().isEmpty()) {
            return "（无）";
        }
        return s.host() + ":" + s.port() + (s.isRunning() ? "" : "（已停止）");
    }

    public long sentPackets() {
        AudioSend s = sender;
        return s == null ? 0 : s.packetsSent();
    }

    public long sentBytes() {
        AudioSend s = sender;
        return s == null ? 0 : s.bytesSent();
    }

    public int sendErrors() {
        AudioSend s = sender;
        return s == null ? 0 : s.sendErrors();
    }

    /** 发送侧最近一次错误（没有发送器时退回"启动失败"的那条）。 */
    public String sendLastError() {
        AudioSend s = sender;
        String e = (s == null) ? lastSendError : s.lastError();
        return e == null ? "" : e;
    }

    public String recvLastError() {
        AudioRecv r = receiver;
        String e = (r == null) ? "" : r.lastError();
        return e == null ? "" : e;
    }

    public String discLastError() {
        Discovery d = discovery;
        String e = (d == null) ? "" : d.lastError();
        return e == null ? "" : e;
    }

    /** 详情面板里的「丢包统计」。 */
    public String statsText() {
        if (receiver == null) {
            return "未在收听（点某台的「收听」开始）";
        }
        AudioRecv.Stats st = recvStats();
        if (st == null) {
            return "暂无统计";
        }
        StringBuilder sb = new StringBuilder();
        sb.append("收 ").append(st.received).append(" 包 · 丢 ").append(st.lost)
                .append(" · 迟到 ").append(st.late).append(" · 坏包 ").append(st.badPackets);
        sb.append("\n缓冲 ").append(st.bufferedMs).append("ms（目标 ").append(st.targetMs)
                .append("ms）· 流 ").append(st.streamCount);
        if (st.duplicates > 0 || st.filtered > 0) {
            sb.append("\n重复 ").append(st.duplicates).append(" · 按名单过滤 ").append(st.filtered);
        }
        if (st.trackErrors > 0) {
            sb.append("\n音频轨错误 ").append(st.trackErrors);
        }
        AudioRecv r = receiver;
        if (r != null) {
            String err = r.lastError();
            if (err != null && err.length() > 0) {
                sb.append("\n最近：").append(err);
            }
            // 每一路单独一行：谁在发、发了多少、丢了多少，一眼能对上号。
            if (st.streams != null) {
                for (AudioRecv.StreamStats ss : st.streams) {
                    sb.append("\n· ").append(ss.host)
                            .append(" 收 ").append(ss.received)
                            .append(" 丢 ").append(ss.lost)
                            .append(" 缓冲 ").append(ss.bufferedMs).append("ms");
                }
            }
        }
        return sb.toString();
    }

    /** 详情面板里的「发送统计」。 */
    public String sendStatsText() {
        AudioSend s = sender;
        if (s == null) {
            return lastSendError.length() > 0 ? ("未在送声音\n" + lastSendError) : "未在送声音";
        }
        try {
            double mb = s.bytesSent() / 1048576.0;
            StringBuilder sb = new StringBuilder();
            sb.append(s.host()).append(':').append(s.port());
            sb.append("\n已发 ").append(s.packetsSent()).append(" 包 / ")
                    .append(String.format(java.util.Locale.US, "%.1f", mb)).append(" MB");
            if (s.sendErrors() > 0) {
                sb.append(" · 失败 ").append(s.sendErrors()).append(" 次");
            }
            String last = s.lastError();
            if (last != null && last.length() > 0) {
                sb.append("\n最近：").append(last);
            }
            return sb.toString();
        } catch (Throwable t) {
            return "发送统计不可用";
        }
    }

    /** 详情面板里的「发现统计」。 */
    public String discoveryStatsText() {
        Discovery d = discovery;
        if (d == null) {
            return "发现未启动";
        }
        try {
            StringBuilder sb = new StringBuilder();
            sb.append("信标 收 ").append(d.beaconsReceived()).append(" / 发 ").append(d.beaconsSent());
            sb.append("\n发现端口 ").append(d.boundPort()).append(" · 组播 ").append(Proto.DISCOVERY_GROUP);
            String err = d.lastError();
            if (err != null && err.length() > 0) {
                sb.append("\n最近：").append(err);
            }
            return sb.toString();
        } catch (Throwable t) {
            return "发现统计不可用";
        }
    }

    /** 本机 IP：和电脑端 netcheck 同一套判定 —— 只要私有地址，避开 VPN 叠加网段。 */
    public String localIp() {
        try {
            Enumeration<NetworkInterface> ifs = NetworkInterface.getNetworkInterfaces();
            String best = null;
            int bestScore = Integer.MIN_VALUE;
            while (ifs != null && ifs.hasMoreElements()) {
                NetworkInterface ni = ifs.nextElement();
                if (ni == null || !ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                String nic = ni.getName() == null ? "" : ni.getName().toLowerCase();
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    if (ia == null) {
                        continue;
                    }
                    InetAddress a = ia.getAddress();
                    if (!(a instanceof Inet4Address) || a.isLoopbackAddress()) {
                        continue;
                    }
                    String ip = a.getHostAddress();
                    int score = scoreIp(ip, nic);
                    if (score > bestScore) {
                        bestScore = score;
                        best = ip;
                    }
                }
            }
            if (best != null) {
                return best;
            }
        } catch (Throwable t) {
            Log.w(TAG, "读取本机 IP 失败", t);
        }
        return "—";
    }

    /** 本机所有局域网地址（主地址在前），Discovery 那边扫到的也一起列出来。 */
    public String localIpsText() {
        String best = localIp();
        StringBuilder sb = new StringBuilder(best);
        Discovery d = discovery;
        if (d != null) {
            try {
                for (String ip : d.localIps()) {
                    if (ip != null && !ip.equals(best)) {
                        sb.append(" / ").append(ip);
                    }
                }
            } catch (Throwable ignored) {
                // 拿不到就只显示主地址
            }
        }
        return sb.toString();
    }

    private static int scoreIp(String ip, String nic) {
        int score;
        if (ip.startsWith("192.168.")) {
            score = 50;                       // 家用 Wi-Fi 最常见
        } else if (ip.startsWith("10.")) {
            score = 40;
        } else if (ip.startsWith("172.")) {
            int second = -1;
            try {
                second = Integer.parseInt(ip.split("\\.")[1]);
            } catch (Throwable ignored) {
                // 保持 -1
            }
            score = (second >= 16 && second <= 31) ? 40 : 5;
        } else if (ip.startsWith("169.254.")) {
            score = -20;                      // 没拿到 DHCP 的自分配地址
        } else {
            score = 0;
        }
        // VPN / 隧道网卡优先级压低：真正能通局域网的是 wlan0。
        if (nic.startsWith("tun") || nic.startsWith("ppp") || nic.startsWith("tap")) {
            score -= 30;
        }
        if (nic.startsWith("wlan")) {
            score += 5;
        }
        return score;
    }

    /** 音频端口（接收绑的端口，也是信标里对外公布的端口）。 */
    public int audioPort() {
        return audioPort;
    }

    public String nodeName() {
        String n = Discovery.defaultName();
        return (n == null || n.length() == 0) ? "Android 设备" : n;
    }

    public String platform() {
        return Discovery.defaultPlatform();
    }

    // ────────────────────────────── 增益 ──────────────────────────────

    public float volume() {
        return volume;
    }

    /**
     * 本机音量滑杆（0~1.5）。
     * 发送侧交给 AudioSend.setGain()（协议里的 gain_q14 会如实描述音量），
     * 接收侧交给 AudioRecv.setGain()（它只认 0~1，超出就削到 1）。
     */
    public void setVolume(float v) {
        volume = v;
        AudioSend s = sender;
        if (s != null) {
            try {
                s.setGain(v);
            } catch (Throwable ignored) {
                // 调不了音量不影响传输
            }
        }
        AudioRecv r = receiver;
        if (r != null) {
            try {
                r.setGain(Math.min(1f, Math.max(0f, v)));
            } catch (Throwable ignored) {
                // 同上
            }
        }
    }
}
