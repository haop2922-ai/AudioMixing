package com.audiomixing.app;

import android.content.Context;
import android.media.projection.MediaProjection;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
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
    /** 同一时刻只送一路，换目标时先停旧的。 */
    private volatile AudioSend sender;
    /** 一个 UDP 端口收，收到几台就混几台 —— 这本来就是个"混音"工具。 */
    private volatile AudioRecv receiver;

    // ── ★ 送什么声音（用户指出过方向性缺陷：手机端不能只当对讲机）──

    /**
     * 用户选的音源。<b>默认"本机声音"</b>—— 这才是这个 App 的定位（把设备正在放的声音送出去），
     * 麦克风只是给对讲场景留的备选。
     *
     * <p>低于 Android 10 的机器没有 {@code AudioPlaybackCapture} 这个能力，
     * 构造时就直接定死麦克风 —— <b>不是"选了再说"，是根本不给选</b>，
     * 界面上会写明"你的安卓版本不支持"。
     */
    private volatile int sourceKind = AudioSend.playbackSupported()
            ? AudioSend.SRC_PLAYBACK : AudioSend.SRC_MIC;

    /**
     * 采本机声音要用的授权（MediaProjection）。<b>刻意在拿到之后一直留着</b>：
     * targetSdk 34 / Android 14 起，每次授权只能换一个 MediaProjection 对象，
     * 用完就丢的话用户每切一次音源都要再看一次系统弹窗。
     */
    private volatile MediaProjection projection;
    /** 授权失败的原因。界面和诊断报告必须把它写出来，不能让用户以为"选了就已经在送本机声音"。 */
    private volatile String projectionError = "";
    /** 授权弹窗已经弹出去、还没回来的那台设备 id（授权回来之后自动开始送）。 */
    private volatile String pendingSendId = "";
    /** 主线程 Handler：MediaProjection 的回调要用它。 */
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    /**
     * 授权被系统收回的回调。<b>刻意懒创建</b>：MediaProjection.Callback 是 API 29 才有的类，
     * 在构造器里 new 出来会让 Android 9 及以下的机器一开 App 就 NoClassDefFoundError。
     */
    private MediaProjection.Callback projectionCallback;

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

    /**
     * ★ 「听广播」（频道模式）：不挑发送方，谁往本机音频端口发声音就播谁。
     *
     * <p><b>为什么必须单独有这么一个开关</b>：我们的设计里广播和点对点是两条通道 ——
     * 电脑开着「广播我的声音」时，声音是发给<b>所有在线节点</b>的；而手机原来只会播
     * "用户点过🎧收听的那几台"（白名单），所以电脑广播过来的包会被按名单过滤掉。
     * 真机现场就是这样：电脑在广播，手机一个包都没播（"按名单过滤"那一条能看出来）。
     *
     * <p><b>为什么是"谁来都听"而不是"只听广播"</b>：音频包里没有路由标记（电脑端协议注释
     * 里写得很清楚：{@code An audio packet carries no route id}），接收端从包本身分不出
     * "这是广播给我的"还是"这是点对点给我的"。所以这里如实按"谁的都听"实现，
     * 界面上也照实这么写，不假装能分辨。
     */
    private volatile boolean channelListen;

    /**
     * ★ 「📢 广播我的声音」：我的声音同时送给发现列表里每一台在线设备。
     *
     * <p>和上面那个「听广播」是一对：一个是"我收谁的"，一个是"我发给谁"。
     * 采集永远只有一份（一次采集 → 一包编码 → 分发给 N 个目标），
     * 所以开着广播不会多占一份麦克风/MediaProjection。
     */
    private volatile boolean broadcastOn;
    /** 上一次同步给发送端的目标签名：没变就不重复设置（界面 tick 每 150ms 调一次）。 */
    private volatile String lastTargetSig = "";

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
            channelListen = false;      // 「停止」= 一切都停，重启后不要留着一个用户看不见的开关
            broadcastOn = false;
            stopReceiver();
            stopSender();
            // 采集停了就把投屏授权也放掉：一直攥着它，系统状态栏会一直挂着"正在投放"的提示。
            releaseProjection();
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
                // ★ 但"听广播"还开着、或者还在送声音时不能停：前者靠它收频道音频，
                //   后者靠它收对端的统计回话。
                stopReceiverIfIdle();
            } else {
                ensureReceiver();
                applyAllowedHosts();
            }
        }
    }

    /** ★ 「听广播」开关。打开之后接收端不再按名单过滤（谁来都听），并且保持接收器活着。 */
    public boolean isChannelListening() {
        return channelListen;
    }

    public void setChannelListen(boolean on) {
        synchronized (lock) {
            if (channelListen == on) {
                return;
            }
            channelListen = on;
            if (on) {
                ensureReceiver();       // 没有它一个包都收不到
                applyAllowedHosts();    // 名单留空 = 谁的都听
            } else {
                applyAllowedHosts();
                stopReceiverIfIdle();
            }
        }
    }

    /**
     * 没有任何理由再收包时才停接收器。
     *
     * <p>三个理由：还开着「听广播」、还在送声音（要收对端的统计回话）、还有收听目标。
     */
    private void stopReceiverIfIdle() {
        if (channelListen || sender != null || !listenIds.isEmpty()) {
            return;
        }
        stopReceiver();
    }

    /**
     * 点「送声音」：把我的声音送给这台（<b>可以同时送给好几台</b>，不再互斥）。
     *
     * <p>为什么改成可累加（2026-10-14 用户反馈）：用户要「📢 广播我的声音」和
     * "每台设备独立的收/送电平条"，这两条的前提都是"一次采集、同时送给多台"。
     * 采集仍然只有一份（见 {@link #syncSenderTargets()}），多送一台只是多一个收包人。
     */
    public void setSending(Discovery.Peer p, boolean on) {
        if (p == null) {
            return;
        }
        String id = idOf(p);
        synchronized (lock) {
            if (on) {
                sendIds.add(id);
            } else {
                sendIds.remove(id);
            }
            syncSenderTargets();
            if (sendIds.isEmpty() && !broadcastOn) {
                // 送声停了、又没有别的收听目标：接收器只是刚才为了收对端统计才开着的，
                // 一起收掉（否则它会一直占着 UDP 端口和一个 WifiLock）。
                // ★ 但「听广播」开着就不能收 —— 那正是它存在的理由。
                stopReceiverIfIdle();
            }
        }
    }

    // ────────────────── ★ 「📢 广播我的声音」（一次采集 → 送给每一台在线设备） ──────────────────

    /**
     * 「📢 广播我的声音」开关。
     *
     * <p>做法照电脑端：**对发现列表里每个在线节点各建一条单播发送**，
     * 而不是往广播地址盲发（Wi-Fi 上盲发不可靠，电脑端也没那么做）。
     * 对用户来说就是"我的声音，谁在线谁就能听见"。
     */
    public void setBroadcasting(boolean on) {
        synchronized (lock) {
            broadcastOn = on;
            syncSenderTargets();
            if (!on && sendIds.isEmpty()) {
                stopReceiverIfIdle();
            }
        }
    }

    public boolean isBroadcasting() {
        return broadcastOn;
    }

    /**
     * 把"现在应该送给谁"同步给发送端。
     *
     * <p><b>关键</b>：目标集合变化<b>不会重启采集</b> —— 采集器只在"从没送过/停了"时起一次，
     * 之后都是 {@code setTargets()}。所以对方上线/下线时正在送的声音不会断一下。
     */
    private void syncSenderTargets() {
        List<String> eps = collectSendTargets();
        // 这个函数每 150ms 会被界面 tick 调一次：目标没变就什么都不做，
        // 免得一直重建目标表（目标表的比较用字符串签名，几分之一微秒的事）。
        String sig = eps.toString();
        boolean changed = !sig.equals(lastTargetSig);
        lastTargetSig = sig;
        AudioSend s = sender;
        if (eps.isEmpty()) {
            if (s != null) {
                stopSender();
            }
            return;
        }
        if (s == null || !s.isRunning()) {
            try {
                // 用不带目标的构造：采集只有一份，目标随后由 setTargets 给。
                AudioSend fresh = new AudioSend(app);
                fresh.setGain(volume);
                // ★ 把"送什么声音"接上：音源 + 本机声音的授权。
                //   拿不到授权、或低于 Android 10 时，AudioSend 自己会退回麦克风并记下原因，
                //   界面拿 activeSourceKind() 如实显示"现在实际送的是哪一个"。
                fresh.setProjection(projection);
                fresh.setSourceKind(sourceKind);
                fresh.setTargets(eps);
                fresh.start();
                sender = fresh;
                lastSendError = "";
                // ★ 送声音的同时把接收器也开起来：对方会把"我收到了多少"用统计包发回来，
                //   没有接收器就收不到这句回话（白名单里没有收听目标，所以不会出声）。
                ensureReceiver();
            } catch (Throwable t) {
                // 拿不到采集（被别的 App 占用 / 没权限）时不要把界面搞崩。
                Log.w(TAG, "启动发送失败 -> " + eps, t);
                lastSendError = String.valueOf(t);
                sendIds.clear();
                broadcastOn = false;
            }
        } else if (changed) {
            s.setTargets(eps);      // ★ 不重启采集，只换目标
            Log.i(TAG, "发送目标已更新：" + eps);
        }
    }

    /** 算出现在该送给哪些地址："广播"给全部在线设备 + 用户单独勾的那几台。 */
    private List<String> collectSendTargets() {
        List<String> out = new ArrayList<String>();
        boolean needAll = broadcastOn;
        for (Discovery.Peer p : peers()) {
            if (p == null || p.host == null || p.host.isEmpty()) {
                continue;
            }
            String id = idOf(p);
            if (!needAll && !sendIds.contains(id)) {
                continue;
            }
            int port = p.audioPort > 0 ? p.audioPort : Proto.DEFAULT_AUDIO_PORT;
            String ep = p.host + ":" + port;
            if (!out.contains(ep)) {
                out.add(ep);
            }
        }
        // 「广播」时如果发现列表暂时空了（Wi-Fi 抖动/对端刚走），不要把 sendIds 里
        // 用户显式点过的那几台也一起丢掉 —— 它们还在等信标回来。
        if (needAll) {
            for (String id : sendIds) {
                Discovery.Peer p = findById(id);
                if (p != null && p.host != null && !p.host.isEmpty()) {
                    int port = p.audioPort > 0 ? p.audioPort : Proto.DEFAULT_AUDIO_PORT;
                    String ep = p.host + ":" + port;
                    if (!out.contains(ep)) {
                        out.add(ep);
                    }
                }
            }
        }
        return out;
    }

    /** 「📢 广播我的声音」界面状态用的人话。 */
    public String broadcastStateText() {
        int n = sendTargetCount();
        StringBuilder sb = new StringBuilder(200);
        if (!broadcastOn) {
            sb.append("没开：只有你单独点了「送声音」的设备会收到。");
            if (n > 0) {
                sb.append("现在送 ").append(n).append(" 台。");
            }
            return sb.toString();
        }
        sb.append("正在广播：我的声音同时送给 ").append(n).append(" 台在线设备");
        if (n == 0) {
            sb.append("（现在一台都没看见 —— 对面得先能被发现）");
        }
        sb.append("。");
        sb.append(bandwidthText());
        return sb.toString();
    }

    /**
     * ★ 带宽的实话。
     *
     * <p>PCM16 立体声 48kHz = 48000×2×2 = 192000 字节/秒 ≈ <b>0.19 MB/s（1.5 Mbit/s）每个目标</b>。
     * （注意别把 1536 kbit/s 念成 1536 KB/s —— 差 8 倍。这里写的是字节数。）
     * 真正先吃紧的不是带宽，而是每个 1948 字节的包会被 IP 层拆成 2 个分片、以及每多一个目标
     * 就多一次系统调用 —— 所以目标多时提示"可能卡"，而不是编一个更吓人的数字。
     */
    public String bandwidthText() {
        int n = Math.max(1, sendTargetCount());
        double mbPerSec = n * (Proto.SAMPLE_RATE * Proto.CHANNELS * 2) / 1048576.0;
        String s = String.format(java.util.Locale.US,
                "每台约 0.19 MB/s（1.5 Mbit/s）；%d 台合计约 %.2f MB/s。", n, mbPerSec);
        if (n >= 4) {
            s += "⚠ 目标较多：包会被拆成 IP 分片、空口占用也成倍增加，可能卡顿。";
        }
        return s;
    }

    /** 当前实际送给了几台。 */
    public int sendTargetCount() {
        AudioSend s = sender;
        return (s == null) ? 0 : s.targetCount();
    }

    /** 逐目标发送统计（界面"送"电平条 + 诊断报告）。发送器不在时返回空表。 */
    public List<SendFanout.TargetStats> sendTargetStats() {
        AudioSend s = sender;
        if (s == null) {
            return new ArrayList<SendFanout.TargetStats>();
        }
        try {
            return s.targetStats();
        } catch (Throwable t) {
            return new ArrayList<SendFanout.TargetStats>();
        }
    }

    /** 送给某一台的电平 0~1（界面"送"电平条用）。 */
    public float sendPeakFor(String host) {
        AudioSend s = sender;
        if (s == null || host == null || host.isEmpty()) {
            return 0f;
        }
        try {
            return clamp01(s.targetPeak(host));
        } catch (Throwable t) {
            return 0f;
        }
    }

    /** 在线设备数（含本机）。 */
    public int onlineCountIncludingSelf() {
        return peers().size() + 1;
    }

    public boolean isListening(Discovery.Peer p) {
        synchronized (lock) {
            return p != null && listenIds.contains(idOf(p));
        }
    }

    /** 这台设备现在会不会收到我的声音（单独勾了它，或者开着广播）。 */
    public boolean isSending(Discovery.Peer p) {
        synchronized (lock) {
            if (p == null) {
                return false;
            }
            return broadcastOn || sendIds.contains(idOf(p));
        }
    }

    public int listeningCount() {
        synchronized (lock) {
            return listenIds.size();
        }
    }

    public int sendingCount() {
        synchronized (lock) {
            if (broadcastOn) {
                // 广播时"送声几台"就是实际目标数（否则通知栏会写"送声 0"而其实在广播）
                return Math.max(sendIds.size(), sendTargetCount());
            }
            return sendIds.size();
        }
    }

    /** 「已连接几台」：收或发都算连上（同一台既收又发只算一台；广播时算上所有在线设备）。 */
    public int connectedCount() {
        synchronized (lock) {
            Set<String> all = new LinkedHashSet<String>(listenIds);
            all.addAll(sendIds);
            if (broadcastOn) {
                for (Discovery.Peer p : peers()) {
                    all.add(idOf(p));
                }
            }
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
            // ★ 「听广播」或「广播我的声音」开着时不能提前返回：那时用户很可能一台设备都没看见，
            //   而目标集合的同步（新设备上线要自动开始收我的声音）就靠下面那几行。
            if (!channelListen && !broadcastOn
                    && (!sawPeer || (sendIds.isEmpty() && listenIds.isEmpty()))) {
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
            //
            // ★ 发送侧改成"同步目标集合"（2026-10-14）：广播开着时，新上线的设备要自动开始收到
            //   我的声音；目标没了就自动少发一份。全程不重启采集（见 syncSenderTargets）。
            syncSenderTargets();
            if (changed && sendIds.isEmpty() && !broadcastOn) {
                Log.i(TAG, "送声音的目标没了，发送器已停");
            }
            // 自愈：只要还有收听目标（或开着听广播），接收器就必须活着（可能被别处停掉过）。
            if ((!listenIds.isEmpty() || channelListen) && receiver == null) {
                ensureReceiver();
            } else if (listenIds.isEmpty() && sender == null && receiver != null && !channelListen) {
                // 既不听也不送、也没开听广播：接收器只是刚才为了收对端统计才开着的，
                // 可以收掉了（省电、放端口）。
                stopReceiver();
            }
            applyAllowedHosts();
        }
    }

    // ────────────────────────── ★ 送什么声音（音源 + 授权） ──────────────────────────

    /** 用户选的音源。注意这可能和实际采的不一样，看 {@link #activeSourceKind()}。 */
    public int sourceKind() {
        return sourceKind;
    }

    /**
     * 换音源。正在送声音时是<b>热切换</b>：AudioSend 会在采集线程里重建 AudioRecord，
     * 既不中断发送，也不会出现两路采集叠在一起发（那会让对端听到重音）。
     */
    public void setSourceKind(int kind) {
        if (kind == AudioSend.SRC_PLAYBACK && !AudioSend.playbackSupported()) {
            kind = AudioSend.SRC_MIC;      // 低版本没有这个能力，不给选
        }
        sourceKind = kind;
        AudioSend s = sender;
        if (s != null) {
            try {
                s.setProjection(projection);
                s.setSourceKind(kind);
            } catch (Throwable t) {
                Log.w(TAG, "切换音源失败", t);
            }
        }
    }

    /** <b>实际</b>在采的音源：没在送声音时就是用户选的那个。界面显示"现在送的是什么"必须用它。 */
    public int activeSourceKind() {
        AudioSend s = sender;
        if (s != null && s.isRunning() && s.activeSourceKind() >= 0) {
            return s.activeSourceKind();
        }
        return sourceKind;
    }

    /** 用户选的音源和实际在采的不一样吗（= 界面上要显眼地写清楚原因）。 */
    public boolean sourceMismatch() {
        AudioSend s = sender;
        return s != null && s.isRunning() && s.activeSourceKind() >= 0
                && s.activeSourceKind() != sourceKind;
    }

    /** 这台机器支不支持采本机声音（Android 10+）。 */
    public boolean playbackSupported() {
        return AudioSend.playbackSupported();
    }

    public boolean hasProjection() {
        return projection != null;
    }

    public String projectionError() {
        return projectionError == null ? "" : projectionError;
    }

    /** 授权弹窗之前先记下"授权回来要送给哪一台"，拿不到授权就清掉。 */
    public void requestSendWithPlayback(Discovery.Peer p) {
        pendingSendId = (p == null) ? "" : idOf(p);
    }

    /** 用户放弃了授权（关掉了系统窗口/点了拒绝）：把待办清掉，别让界面一直转圈。 */
    public void cancelPendingSend(String why) {
        String id = pendingSendId;
        pendingSendId = "";
        if (id != null && id.length() > 0) {
            lastSendError = (why == null || why.length() == 0) ? "没有拿到送本机声音的授权" : why;
        }
    }

    /** 待办的那台设备 id（诊断报告里用：不是空的就是"卡在等授权"）。 */
    public String pendingSendId() {
        return pendingSendId == null ? "" : pendingSendId;
    }

    /**
     * 授权成功。由 {@link MixService} 调用 —— 而且必须在<b>已经启动的、
     * 类型含 mediaProjection 的前台服务里</b>调：targetSdk 34 下顺序反了系统直接抛
     * SecurityException（这是 Android 14 的硬性要求）。
     */
    public void onProjectionGranted(MediaProjection p) {
        if (p == null) {
            onProjectionFailed("系统没有返回授权对象");
            return;
        }
        String target;
        synchronized (lock) {
            projection = p;
            projectionError = "";
            try {
                // 必须注册回调：授权被系统收回时要立刻知道，否则界面会一直显示"送声中"
                // 而实际上一片静音 —— 那是最难查的一种假象。
                p.registerCallback(projectionCallback(), mainHandler);
            } catch (Throwable t) {
                Log.w(TAG, "注册投屏回调失败（继续用，只是收不到系统收回的通知）", t);
            }
            target = (pendingSendId == null) ? "" : pendingSendId;
            pendingSendId = "";
            // 正在送麦克风的话就热切到本机声音：用户点授权的意图很明确。
            AudioSend s = sender;
            if (s != null && sourceKind == AudioSend.SRC_PLAYBACK) {
                try {
                    s.setProjection(p);
                    s.setSourceKind(AudioSend.SRC_PLAYBACK);
                } catch (Throwable t) {
                    Log.w(TAG, "授权到手后切换到本机声音失败", t);
                }
            }
        }
        if (target.length() > 0) {
            Discovery.Peer t = findById(target);
            if (t != null && t.host != null && !t.host.isEmpty()) {
                setSending(t, true);       // 授权前用户点的那一台：现在真的开始送
            } else {
                lastSendError = "授权拿到了，但设备列表里已经找不到那台设备了";
                Log.w(TAG, lastSendError);
            }
        }
    }

    /** 授权失败（用户在系统窗口点了拒绝，或系统不给）。必须如实记下来。 */
    public void onProjectionFailed(String err) {
        String msg = (err == null || err.length() == 0) ? "授权失败（原因未知）" : err;
        synchronized (lock) {
            projectionError = msg;
            pendingSendId = "";
            // 正在用旧授权送本机声音的话，先退回麦克风：授权没了还继续采只会是静音。
            if (sourceKind == AudioSend.SRC_PLAYBACK && sender != null) {
                try {
                    sender.setProjection(null);
                    sender.setSourceKind(AudioSend.SRC_MIC);
                } catch (Throwable ignored) {
                    // 切不过去也不影响"如实报告"，界面看 activeSourceKind() 就知道
                }
            }
        }
        Log.w(TAG, "采本机声音的授权没拿到：" + msg);
    }

    /**
     * 系统把授权收回了（用户在状态栏点"停止投放"、或者被别的 App / 系统抢走）。
     *
     * <p>处理原则：<b>立刻退回麦克风 + 写明原因</b>。继续拿旧授权采集只会得到一片静音，
     * 而用户看到的却是"送声中"，那种假象比直接报错难查得多。
     */
    public void onProjectionStopped() {
        synchronized (lock) {
            boolean wasGranted = projection != null;
            projection = null;
            if (wasGranted) {
                projectionError = "系统收回了「录制/投射」授权（在状态栏点了停止，或被别的 App 抢走）";
            }
            AudioSend s = sender;
            if (s != null && s.activeSourceKind() == AudioSend.SRC_PLAYBACK) {
                try {
                    s.setProjection(null);
                    s.setSourceKind(AudioSend.SRC_MIC);
                } catch (Throwable t) {
                    Log.w(TAG, "授权被收回后切回麦克风失败", t);
                }
            }
        }
        Log.w(TAG, "采本机声音的授权被系统收回了");
    }

    /** 释放授权（停引擎时调用）。释放之后要再送本机声音就得重新授权一次。 */
    private void releaseProjection() {
        MediaProjection p;
        synchronized (lock) {
            p = projection;
            projection = null;
            pendingSendId = "";
        }
        if (p == null) {
            return;
        }
        try {
            MediaProjection.Callback cb = projectionCallback;
            if (cb != null) {
                p.unregisterCallback(cb);
            }
        } catch (Throwable ignored) {
            // 没注册成功过就会抛，忽略
        }
        try {
            p.stop();
        } catch (Throwable t) {
            Log.w(TAG, "停止投屏授权失败", t);
        }
    }

    private MediaProjection.Callback projectionCallback() {
        if (projectionCallback == null) {
            projectionCallback = new MediaProjection.Callback() {
                @Override
                public void onStop() {
                    onProjectionStopped();
                }
            };
        }
        return projectionCallback;
    }

    // ── 这些是给界面和诊断报告用的人话（只在这一处定义，免得两种说法把用户绕晕）──

    /** 「当前选择」那一行。 */
    public String sourceNameText() {
        return AudioSend.sourceName(sourceKind)
                + (sourceKind == AudioSend.SRC_PLAYBACK ? "（手机上正在放的音乐/视频/游戏）" : "（对讲用）");
    }

    /** 「实际采集」那一行：和用户选的不一样时，把原因一起写出来。 */
    public String activeSourceText() {
        AudioSend s = sender;
        if (s == null || !s.isRunning() || s.activeSourceKind() < 0) {
            return "没在采集（没点过任何一台的「送声音」）";
        }
        int act = s.activeSourceKind();
        StringBuilder sb = new StringBuilder(AudioSend.sourceName(act));
        if (act == sourceKind) {
            sb.append(" ✓");
        } else {
            sb.append(" ⚠（你要的是 ").append(AudioSend.sourceName(sourceKind))
                    .append("，采不到：").append(mismatchReason(s)).append("）");
        }
        return sb.toString();
    }

    /** 采集不到时给用户看的原因（优先用采集线程记下的那条）。 */
    public String mismatchReason(AudioSend s) {
        String e = (s == null) ? "" : s.lastError();
        if (e != null && e.length() > 0) {
            return e;
        }
        String p = projectionError();
        return p.length() > 0 ? p : "原因未知（看诊断报告）";
    }

    /** 「系统支持」那一行。 */
    public String playbackSupportText() {
        String ver = "Android " + Build.VERSION.RELEASE + "（API " + Build.VERSION.SDK_INT + "）";
        if (AudioSend.playbackSupported()) {
            return "支持 ✓ —— " + ver + "，可以用系统声音采集（AudioPlaybackCapture）";
        }
        return "不支持 ✗ —— " + ver + " 低于 Android 10，系统没有采集本机声音这个能力，只能用麦克风";
    }

    /** 「授权状态」那一行。 */
    public String projectionStateText() {
        if (!AudioSend.playbackSupported()) {
            return "不需要（低版本没有这个能力）";
        }
        if (projection != null) {
            return "已授权 ✓（授权一次之后一直用；App 重启后要再授权一次）";
        }
        String e = projectionError();
        if (e.length() > 0) {
            return "没有授权 ✗ —— " + e;
        }
        return "还没授权 —— 点任意一台的「送声音」会弹一次系统授权框，允许后就能送本机声音";
    }

    /** 「采集格式」那一行。 */
    public String captureFormatText() {
        AudioSend s = sender;
        if (s == null || !s.isRunning()) {
            return "（没在采集）";
        }
        return s.captureFormatText();
    }

    /**
     * 「静音情况」那一行 —— 采本机声音时最值钱的一条：
     * 包在发、但一直是零，几乎就是 DRM 保护内容或"禁止被采集的 App"。
     */
    public String silenceText() {
        AudioSend s = sender;
        if (s == null || !s.isRunning()) {
            return "（没在采集）";
        }
        long ms = s.silentMs();
        if (ms < 1500) {
            return "有声音 ✓（最近 " + (ms / 100) / 10f + " 秒内采到过内容）";
        }
        String sec = String.format(java.util.Locale.US, "%.1f", ms / 1000.0);
        if (!s.everLoud()) {
            return "一直是静音 ✗（已连续 " + sec + " 秒没有任何声音）"
                    + (s.activeSourceKind() == AudioSend.SRC_PLAYBACK
                    ? "—— 采本机声音时这通常意味着：这段内容是 DRM 保护的（部分音乐/影视 App）、"
                    + "或者那个 App 主动禁止了被采集；先在手机上放点声音试试。"
                    : "—— 麦克风没收到声音（手机静音了？离得远？）");
        }
        return "最近 " + sec + " 秒没有声音（之前在正常采）";
    }

    /** 界面主卡片里那句「现在送的是什么」。 */
    public String sourceStateText() {
        if (!AudioSend.playbackSupported()) {
            return "你的安卓版本（Android " + Build.VERSION.RELEASE + "）不支持采集本机声音，只能用麦克风。"
                    + "要送手机上正在放的声音，需要 Android 10 及以上。";
        }
        AudioSend s = sender;
        boolean sending = s != null && s.isRunning();
        if (!sending) {
            StringBuilder sb = new StringBuilder("现在没在送声音。");
            sb.append("选中「").append(AudioSend.sourceName(sourceKind))
                    .append("」之后，点任意一台设备的「送声音」开始。");
            if (sourceKind == AudioSend.SRC_PLAYBACK && projection == null) {
                sb.append("第一次会让你在系统窗口里点一次允许（授权一次，之后一直用）。");
            }
            return sb.toString();
        }
        int act = s.activeSourceKind();
        if (act == AudioSend.SRC_PLAYBACK) {
            return "正在送：🔊 本机声音（手机上正在放的音乐/视频/游戏）";
        }
        if (sourceKind == AudioSend.SRC_PLAYBACK) {
            return "⚠ 你要的是 🔊 本机声音，但采不到，现在实际送的是 🎤 麦克风 —— "
                    + mismatchReason(s);
        }
        return "正在送：🎤 麦克风（对讲用的）";
    }

    /** 采本机声音却一直是静音时的提醒；不需要提醒就返回空串。 */
    public String silenceHintText() {
        AudioSend s = sender;
        if (s == null || !s.isRunning() || s.activeSourceKind() != AudioSend.SRC_PLAYBACK) {
            return "";
        }
        long ms = s.silentMs();
        if (ms < 4000) {
            return "";
        }
        String sec = String.format(java.util.Locale.US, "%.0f", ms / 1000.0);
        if (!s.everLoud()) {
            return "⚠ 本机声音已经送了 " + sec + " 秒，但一直是静音："
                    + "DRM 保护的内容（部分音乐/影视 App）和主动禁止被采集的 App 采不到，属正常现象；"
                    + "先在手机上放点声音验证一下。";
        }
        return "本机声音最近 " + sec + " 秒是静的（之前采到过）。";
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
            if (channelListen) {
                // ★ 听广播：名单留空 = 谁的都听（AudioRecv 的语义就是"空 = 全收"）。
                //   这一行就是"手机能听电脑广播"的关键：原来这里一定会写进至少一个地址
                //   （没有收听目标时写一个不可能命中的占位地址），于是广播包全被过滤掉。
                //
                //   为什么不能做得更"精确"：音频包里没有路由标记，接收端分不出
                //   "广播给我的"和"点对点给我的"，所以只能如实按"谁来都听"实现。
                Log.i(TAG, "听广播已打开：接收端不再按名单过滤（谁来都听）");
                return;
            }
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
            // ★ 我正在送声音的每一台：逐台告诉它"我发给你多少、我又收了你多少"。
            //   （原来是单目标，只报一台；现在广播/多目标时每台都要收到自己那一份回话。）
            snd = sender;
            if (snd != null) {
                for (SendFanout.TargetStats ts : snd.targetStats()) {
                    if (ts.host != null && !ts.host.isEmpty()) {
                        targets.put(ts.host, ts.port > 0 ? ts.port : Proto.DEFAULT_AUDIO_PORT);
                    }
                }
            }
        }
        // ★ 听广播时，把"我正在收的每一路"也告诉对方：对面才知道它广播的东西到没到我这儿。
        //   （否则用户只能看到"手机没声音"，而电脑那边毫无反馈。）
        if (channelListen) {
            AudioRecv.Stats st0 = recvStats();
            if (st0 != null && st0.streams != null) {
                for (AudioRecv.StreamStats ss : st0.streams) {
                    if (ss.host == null || ss.host.isEmpty() || targets.containsKey(ss.host)) {
                        continue;
                    }
                    targets.put(ss.host, portForHost(ss.host));
                }
            }
        }
        if (targets.isEmpty() && !channelListen) {
            expirePeerStats();
            return;
        }

        // ★ 每台各自算"我有没有在发给你、发了多少"：广播时每台都是"在发"，各报各的包数。
        boolean sendingNow = snd != null && snd.isRunning();
        Map<String, SendFanout.TargetStats> perTarget = new LinkedHashMap<String, SendFanout.TargetStats>();
        if (snd != null) {
            for (SendFanout.TargetStats ts : snd.targetStats()) {
                perTarget.put(ts.host, ts);
            }
        }
        AudioRecv.Stats st = recvStats();
        int streams = (st == null) ? 0 : st.streamCount;

        for (Map.Entry<String, Integer> t : targets.entrySet()) {
            String host = t.getKey();
            if (host == null || host.isEmpty()) {
                continue;
            }
            int port = (t.getValue() == null || t.getValue() <= 0)
                    ? Proto.DEFAULT_AUDIO_PORT : t.getValue();
            // 只对"我正在送的那些"报已发包数；对别的设备要如实说"我没在发给你"，
            // 否则对面会以为那些包是发给它的（负载里的 target 字段就是干这个的）。
            SendFanout.TargetStats ts = perTarget.get(host);
            boolean iSendToIt = sendingNow && ts != null;
            long mySent = (ts == null) ? 0 : ts.packetsSent;
            long myBytes = (ts == null) ? 0 : ts.bytesSent;
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
                    iSendToIt ? mySent : 0, iSendToIt ? myBytes : 0, iSendToIt,
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

    /** 正在送给谁（可能多台；没在送就是"（无）"）。 */
    public String sendTargetText() {
        AudioSend s = sender;
        if (s == null || s.targetCount() == 0) {
            return "（无）";
        }
        int n = s.targetCount();
        String list = s.targetCount() <= 4 ? s.targetText() : (n + " 台");
        return n + " 台 · " + list + (s.isRunning() ? "" : "（已停止）");
    }

    // ────────────────────────── ★ 本机名称（可编辑） ──────────────────────────

    /**
     * 改本机名称。
     *
     * <p>用户原话：「本机id最好支持自己编辑，不排除多人用同型号设备」。
     * 注意分工：<b>用户编辑的是"名字"，节点 ID 是身份、永远保持持久唯一</b>（不能让人改成重复的，
     * 否则电脑端的路由表就指向不清了）。改完立刻生效：下一个信标（≤2 秒）带出去，
     * 别人列表里马上变。
     */
    public void setLocalName(String name) {
        Discovery d = discovery;
        if (d == null) {
            return;
        }
        d.setName(name);
    }

    /** 本机名称（信标里对外用的那个）。 */
    public String localName() {
        return nodeName();
    }

    public boolean localNameIsCustom() {
        Discovery d = discovery;
        return d != null && d.customName();
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
        // ★ 坏包分类：详情面板也要能一眼看出"坏在哪"（真机排查就卡在一个总数上）
        if (st.badPackets > 0) {
            sb.append("\n坏包明细 magic ").append(st.badMagic)
                    .append(" · 版本 ").append(st.badVersion)
                    .append(" · 太短 ").append(st.badShort)
                    .append(" · 编码 ").append(st.badCodec)
                    .append(" · 帧长 ").append(st.badFrames);
            String sample = (st.lastCodecSample != null && st.lastCodecSample.length() > 0)
                    ? st.lastCodecSample : st.lastMagicSample;
            if (sample != null && sample.length() > 0) {
                sb.append("\n").append(sample);
            }
        }
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
            // ★ 多目标：逐台列出"发了多少包 / 多少字节 / 失败几次 / 最近一次成功多久前"，
            //   用户才能看出"是哪一台没在收"（单目标时代只有一个数字，广播时根本不够用）。
            sb.append("目标 ").append(s.targetCount()).append(" 台");
            if (broadcastOn) {
                sb.append("（📢 广播中）");
            }
            sb.append("\n合计已发 ").append(s.packetsSent()).append(" 包 / ")
                    .append(String.format(java.util.Locale.US, "%.1f", mb)).append(" MB");
            if (s.sendErrors() > 0) {
                sb.append(" · 失败 ").append(s.sendErrors()).append(" 次");
            }
            for (SendFanout.TargetStats ts : s.targetStats()) {
                sb.append("\n· ").append(ts.host).append(':').append(ts.port)
                        .append(" 发 ").append(ts.packetsSent).append(" 包");
                if (ts.sendErrors > 0) {
                    sb.append(" 失败 ").append(ts.sendErrors).append(" 次");
                }
                if (ts.lastOkAgeMs < 0) {
                    sb.append("（还没成功发出过）");
                } else if (ts.lastOkAgeMs > 2000) {
                    sb.append("（最近一次成功 ").append(ts.lastOkAgeMs / 1000).append(" 秒前）");
                }
            }
            // ★ 音源写在详情里：用户看"送声音"到底送的是什么，这里是最直接的答案。
            int act = s.activeSourceKind();
            sb.append("\n音源 ").append(act < 0 ? "（还没打开）" : AudioSend.sourceName(act));
            if (act >= 0 && act != sourceKind) {
                sb.append("（你要的是 ").append(AudioSend.sourceName(sourceKind)).append("）");
            }
            sb.append(" · ").append(s.captureFormatText());
            if (s.targetCount() > 1) {
                sb.append("\n带宽 ").append(bandwidthText());
            }
            String hint = silenceHintText();
            if (hint.length() > 0) {
                sb.append('\n').append(hint);
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

    /** 界面「听广播」那一行的人话（带"收到几个包"这种能直接定性的数字）。 */
    public String channelStateText() {
        if (!channelListen) {
            return "没开：只有你点过「🎧 收听」的设备才会出声。";
        }
        AudioRecv.Stats st = recvStats();
        long recv = st == null ? 0 : st.received;
        int streams = st == null ? 0 : st.streamCount;
        StringBuilder sb = new StringBuilder(200);
        sb.append("正在听广播：谁往本机音频端口发声音就播谁（不用先点某台设备）。");
        if (!receiverRunning()) {
            sb.append("⚠ 接收器没起来（音频端口可能被别的 App 占了）。");
        } else if (recv == 0) {
            sb.append("已经收到 0 个音频包 —— 对面要开着「广播我的声音」，")
                    .append("而且它得先能看见本机（点「详情」看发现自检那几行）。");
        } else if (streams == 0) {
            sb.append("累计收到 ").append(recv).append(" 包，但当前没有活跃的流（对面可能刚停）。");
        } else {
            sb.append(recvStreamsText()).append("。");
            if (st != null && st.dupRoutePackets > 0) {
                sb.append("（另有同一台设备的重复流已被抑制，不会叠加播放）");
            }
        }
        return sb.toString();
    }

    /** 详情面板里的「发现统计」（含 ★ 发现层自检：列表空着时到底卡在哪一步）。 */
    public String discoveryStatsText() {
        Discovery d = discovery;
        if (d == null) {
            return "发现未启动";
        }
        try {
            StringBuilder sb = new StringBuilder(768);
            sb.append("发现端口 ").append(d.boundPort())
                    .append(" · 组播 ").append(Proto.DISCOVERY_GROUP).append(" · TTL 2\n");
            sb.append(d.selfCheckText().trim());
            String err = d.lastError();
            if (err != null && err.length() > 0) {
                sb.append("\n最近：").append(err);
            }
            return sb.toString();
        } catch (Throwable t) {
            return "发现统计不可用";
        }
    }

    /** 某台设备的音频端口（从发现列表里查；查不到就用默认端口）。 */
    private int portForHost(String host) {
        if (host == null || host.isEmpty()) {
            return Proto.DEFAULT_AUDIO_PORT;
        }
        try {
            for (Discovery.Peer p : peers()) {
                if (host.equals(p.host) && p.audioPort > 0) {
                    return p.audioPort;
                }
            }
        } catch (Throwable ignored) {
            // 查不到就用默认端口
        }
        return Proto.DEFAULT_AUDIO_PORT;
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
    public String localIpsText() {        String best = localIp();
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

    // ────────────────────── ★ 音量：只动我们这一层 ──────────────────────

    /**
     * 「我们这一层的音量」当前值（发送侧 / 播放侧各是多少）。
     *
     * <p><b>为什么要有这个方法</b>（2026-10-12 真机反馈，用户原话）：
     * 「手机在调节本机音量时（本意应当是使收听方音量更适合），会直接调节系统声音大小。
     * 我们的声音计算方式应当是：系统声音 × 被监听软件声音 × 我们软件声音 = 实际接收端声音大小」。
     * 所以滑杆从此只作用于<b>我们自己处理的 PCM</b>（发侧采样后乘增益 / 收侧 AudioTrack 音量），
     * <b>绝不碰系统媒体音量</b>。这一行就是"我们这一层"的如实值。
     */
    public String volumeText() {
        AudioSend s = sender;
        AudioRecv r = receiver;
        float send = (s != null) ? s.gain() : volume;
        float recv = (r != null) ? r.gain() : Math.min(1f, Math.max(0f, volume));
        return String.format(java.util.Locale.US,
                "发送 %.2f / 播放 %.2f（滑杆 %.2f）", send, recv, volume);
    }

    /**
     * 系统媒体音量：<b>只显示，永远不修改</b>。
     *
     * <p>用户要的分层是：系统音量 × 播放器自己的音量 × 我们这一层 = 实际听到的大小。
     * 我们只负责最后一层 —— 把系统音量读出来写在界面上，是让用户知道"这一层你得自己调"，
     * 而不是让我们替他调。
     */
    public String systemVolumeText() {
        if (app == null) {
            return "读不到（没有 Context）";
        }
        try {
            android.media.AudioManager am =
                    (android.media.AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
            if (am == null) {
                return "读不到（没有音频服务）";
            }
            int cur = am.getStreamVolume(android.media.AudioManager.STREAM_MUSIC);
            int max = am.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC);
            return cur + " / " + max + "（只显示，我们不修改它）";
        } catch (Throwable t) {
            return "读不到：" + t;
        }
    }

    /** 三层关系的人话（界面和报告共用一处，免得两种说法）。 */
    public String volumeExplainText() {
        return "实际听到的大小 = 系统音量 × 播放器自己的音量 × 我们这一层的音量。"
                + "这个滑杆只管最后一层，不动你的系统音量。";
    }

    /** 节点 ID：持久化保存，重启不变（电脑端据此把同一台手机认成同一台）。 */
    public String nodeIdText() {
        Discovery d = discovery;
        String id = (d != null) ? d.nodeId() : "";
        if (id == null || id.isEmpty()) {
            return "（还没有）";
        }
        return id + "（持久化，重启不变 ✓）";
    }

    /** 本机信标原文（诊断报告里贴一行，证明 id 不是空的、信标组得出来）。 */
    public String beaconPreviewText() {
        Discovery d = discovery;
        if (d == null) {
            return "发现未启动";
        }
        try {
            return d.beaconPreview();
        } catch (Throwable t) {
            return "读取信标失败：" + t;
        }
    }

    // ────────────────────── ★ 实际收到哪几路（界面要看得见） ──────────────────────

    /**
     * 「当前实际收到哪几路」——用户说的"路由模式和广播模式混淆"最后就落在这一行上：
     * 开关是意图，这一行才是事实。
     */
    public String recvStreamsText() {
        AudioRecv r = receiver;
        if (r == null || !r.isRunning()) {
            return "接收器没在跑（没开「听广播」，也没点过任何一台的「🎧 收听」）";
        }
        AudioRecv.Stats st = recvStats();
        if (st == null || st.streams == null || st.streams.isEmpty()) {
            return "当前没在收任何音频流（对面还没开始发，或者包没到）";
        }
        StringBuilder sb = new StringBuilder(96);
        sb.append("当前实际收到 ").append(st.streams.size()).append(" 路：");
        for (int i = 0; i < st.streams.size(); i++) {
            AudioRecv.StreamStats ss = st.streams.get(i);
            if (i > 0) {
                sb.append('、');
            }
            sb.append(nameOfHost(ss.host));
            if (ss.ageMs > 1000) {
                sb.append("（静了 ").append(ss.ageMs / 1000).append(" 秒）");
            }
        }
        return sb.toString();
    }

    /** 某台设备现在有没有真的在出声（设备卡片上显示"实际生效状态"用）。 */
    public boolean recvReceivingFrom(String host) {
        AudioRecv r = receiver;
        if (r == null) {
            return false;
        }
        try {
            return r.receivingFrom(host);
        } catch (Throwable t) {
            return false;
        }
    }

    /** 去重（同一台设备的第 2 条流被抑制）的情况，给人看的一句话；没有就返回空串。 */
    public String dupRouteStatsText() {
        AudioRecv.Stats st = recvStats();
        if (st == null || st.dupRoutePackets <= 0) {
            return "";
        }
        StringBuilder sb = new StringBuilder(160);
        sb.append("已抑制 ").append(st.dupRoutePackets).append(" 个包（同一台设备的第 2 条流，绝不叠加播放）");
        if (st.dupRouteConfirmed > 0) {
            sb.append("；其中 ").append(st.dupRouteConfirmed)
                    .append(" 个包内容指纹相同 → 确认是同一份声音从两条路进来（广播 + 点对点路由）");
        }
        if (st.dupRouteText != null && st.dupRouteText.length() > 0) {
            sb.append("；").append(st.dupRouteText);
        }
        return sb.toString();
    }

    /** 把一个 host 换成人能认的名字（找不到就退回 IP）。 */
    public String nameOfHost(String host) {
        if (host == null || host.isEmpty()) {
            return "?";
        }
        try {
            for (Discovery.Peer p : peers()) {
                if (host.equals(p.host)) {
                    return p.label() + "（" + host + "）";
                }
            }
        } catch (Throwable ignored) {
            // 查不到就用 IP
        }
        return host;
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

    /**
     * 本机名字（信标/通知/统计包里对外用的那个）。
     *
     * <p>注意必须取 {@link Discovery#name()}（用户可能改过），<b>不能</b>现算 {@code Build.MODEL}
     * —— 那样用户改的名字在通知和对端列表里就不生效了。
     */
    public String nodeName() {
        Discovery d = discovery;
        String n = (d != null) ? d.name() : null;
        if (n == null || n.length() == 0) {
            n = Discovery.defaultName();
        }
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
     * 「我们这一层的音量」滑杆（0~1.5）。
     *
     * <p>只作用于我们自己处理的 PCM：发送侧交给 AudioSend.setGain()（协议里的 gain_q14 会如实
     * 描述音量），接收侧交给 AudioRecv.setGain()（它只认 0~1，超出就削到 1）。
     * <b>刻意不碰系统媒体音量</b> —— 用户明确要求过这一点（系统音量是另一层，我们只显示它）。
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
