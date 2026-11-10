package com.audiomixing.app;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.projection.MediaProjection;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import java.io.File;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.nio.charset.StandardCharsets;
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
     * ★ 音源开关（2026-10-19 用户第 4 条）：true = 按 sourceKind 采集，false = 已停止。
     *
     * <p>用户原话：「这俩按键要真实显示用户设备实际情况。点击未激活按键即为激活，再次点击即为关闭」。
     * 由用户点「🔊 本机声音」/「🎤 麦克风」切换（见 {@link SourcePolicy#toggle}）。
     * 它与 {@link #sourceKind} 一起，就是界面那两颗按钮的唯一真实来源。
     */
    private volatile boolean sourceEnabled = true;
    /** 上一次"音源被停掉"的原因（如实写进状态行；空 = 没停过）。 */
    private volatile String sourceStopReason = "";

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
     * ★ 2026-11-04（Q3）：「送声音」档位 —— 0 未激活 / 1 送声+外放 / 2 送声+静默。
     *
     * <p>为什么要三档：真机复测时用户发现「手机把声音送出去的同时本机外放也在响」，
     * 而他要的是"只听电脑那头的"。档位只影响<b>本机外放的静音</b>，不影响送不送；
     * 送不送仍然看 {@link #sendIds} / {@link #broadcastOn}。
     *
     * <p>档位随统计包上报（{@code send_mode} + {@code broadcast}），电脑端据此决定准入 ——
     * 第 1/2 档都属于"对端声明要送"，区别只是本地外放。
     */
    private volatile int sendModePref = 1;
    /** 采集/发送是否**真的在跑**（目标集非空且音源可用）。第 2 档只在这时候压音量。 */
    private volatile boolean sendingActive;

    /**
     * ★ 2026-11-08（用户方案）：静音档乘数策略 —— {@code internal_only}（默认，1.0）
     * 或 {@code last_nonzero}（沿用进入静音前最后一次非零系统音量）。
     *
     * <p>外放档永远乘当前系统媒体音量；这个开关只影响静音档。手机界面与电脑设置
     * 共用这一份状态（电脑通过 T_STATS 下发）。
     */
    private volatile String silentVolPolicy = SendVolume.POLICY_INTERNAL;
    /** 每台电脑发来的静音档策略请求号（同一个号只处理一次）。 */
    private final Map<String, Long> silentPolicyReqSeen = new java.util.HashMap<String, Long>();

    /**
     * ★ ④ 用户在哪几台上选了「走蓝牙」（按地址记）。
     *
     * <p>用户原话（2026-10-15）：「手机确认走蓝牙后应当由电脑发送蓝牙请求并配对互相连接。」
     * 所以手机这边只负责**把这件事告诉电脑**（每 2 秒跟着统计包重复请求，幂等），
     * 真正去连的是电脑那边（电脑手里才有蓝牙设备列表）。
     *
     * <p>按地址而不是按 id：统计包是按地址逐台发的，这里必须能和那一处对上。
     * 界面线程添/删、发包线程读，所以用同步包装。
     */
    private final Map<String, Integer> btWantPorts =
            java.util.Collections.synchronizedMap(new LinkedHashMap<String, Integer>());

    /**
     * ★ 刚停掉「送声音」/「收听」之后，还要**再报几轮统计**的设备。
     *
     * <p>为什么需要（2026-10-15 用户反馈④）：「手机 WiFi 送声音关闭后，电脑听的选项不会自动关」。
     * 因为统计包的收件人就是"和我有关系的那些设备"——我这边一停，收件人立刻没了，
     * 电脑**再也收不到 want_listen=false**，只能一直以为自己还在被收听。
     * 所以停掉之后继续报 {@link #STATS_STOP_GRACE_MS} 毫秒，把"我停了"这件事送达。
     */
    private static final long STATS_STOP_GRACE_MS = 30000L;

    /**
     * ★ 对端"彻底没消息"多久之后，把它【替我们自动打开】的收听收回来（2026-10-15 自查补的缺口）。
     *
     * <p>为什么光靠 want_listen=false 不够：那条消息只有在<b>对端还在跑</b>时才送得到。
     * 对端（电脑）直接把 App 关掉、或者整台机器走了，就没有任何人来说这句 false ——
     * 那条自动打开的收听会一直挂着，界面显示"正在收听 XXX"，其实那边什么都没在发。
     * （用户第 3 条抱怨的正是这种"停不下来"，只是成因是关程序而不是关送声音。）
     *
     * <p>90 秒：对端每 2 秒就报一次统计，这个时长足够容忍 Wi-Fi 抖动和几轮丢包；
     * <b>只收自动开的那些</b>，用户自己点开的收听一律不碰（和 want_listen=false 一个边界）。
     */
    private static final long AUTO_LISTEN_STALE_MS = 90000L;
    /**
     * ★ 2026-11-04（A3）：智连自动收听的双判据阈值（毫秒），与电脑端
     * {@code AUTO_LISTEN_HANG_S = 4.0} 一致。
     * 音频包静默 + 对端统计心跳都超过它才收回；静音音频仍有包 ⇒ 永不误挂。
     */
    private static final long AUTO_LISTEN_HANG_MS = 4000L;
    /** 上一轮真正发过统计的地址（用来发现"刚刚停掉的那一台"）。 */
    private final Set<String> lastStatsHosts = new LinkedHashSet<String>();
    private final Map<String, Integer> statsGracePorts = new LinkedHashMap<String, Integer>();
    private final Map<String, Long> statsGraceUntil = new LinkedHashMap<String, Long>();

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

    /** 系统媒体会话：让 Android 把我们识别成"正在放媒体的 App"（分应用音量用）。 */
    private volatile MediaSession mediaSession;

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

    // ── ★ ④ 对端活性（安全检查：对方关掉了就不能再发）──
    /** 目标 id → 它的地址（发现列表里掉出去之后还要靠它判断/恢复）。 */
    private final Map<String, String> sendHosts = new LinkedHashMap<String, String>();
    /** 目标 id → 最近一次"确认它还活着"的时刻（elapsedRealtime）：信标或心跳都算。 */
    private final Map<String, Long> sendLastAliveMs = new LinkedHashMap<String, Long>();
    /** "已停止向 XXX 发送"的说明（空 = 没有暂停的目标）。 */
    private volatile String sendSuspendedText = "";

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
    /** 谁刚动了音量就置位（见 {@link #pokeStats()}）：让发送循环立刻醒一轮。 */
    private volatile boolean statsPoke;
    private final Object statsWake = new Object();
    /** 统计包的固定 ssrc：不是音频流，只为了在对端日志里能认出是同一个人发的。 */
    private final int statsSsrc = ((int) System.nanoTime()) | 1;

    private Engine(Context ctx) {
        this.app = ctx;
        // ★ 第 2 档崩溃兜底：上次进程被杀时如果停在"手机静音"，这里先还原再继续。
        //   必须在 loadSendMode 之前 —— 否则会先按第 2 档去 sync，短暂再压一次音量。
        restoreSilentVolumeIfNeeded();
        loadSmartHosts();   // 从本地把「智连」名单读回来（默认空 = 一台都不自动听）
        loadSendMode();     // ★ 2026-11-04（Q3）：上次选的送声音档位（默认第 1 档）
        loadSilentVolPolicy();   // ★ 2026-11-08：静音档乘数策略（默认 internal_only）
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
            // ★ 2026-11-01 用户：手机端没有端口修改，电脑改端口后手机就连不上。
            //   本机端口现在从设置里读（发现端口默认 17830，音频端口固定自动选）。
            audioPort = configuredAudioPort();
            // ★ 接收器这次【不再】一开机就常驻。
            //
            // 上一个版本为了收对端那句"我在给你发声音、请收听我"（T_STATS 的 want_listen），
            // 把接收器一直开着、名单钉成一个不可能命中的地址 —— 用户当场就问过：
            // 「这一步不是瞎搞吗。我们设计了同一局域网设备能互相发现。那它本身就是
            //   多个设备都在互相同步数据啊」。
            //
            // 他说得对：那句话现在走**发现层**（见 Discovery 的类注释，onControl 回调），
            // 而那一层每 2 秒一发一收、本来就一直活着。接收器于是回到"按需启停"：
            // 真要收声音才起来（点「收听」/「听广播」/「送声音」那三条路各自会拉起它）。
            try {
                Discovery d = new Discovery(app, null, null, audioPort, 0);
                d.setDiscoveryPort(configuredDiscoveryPort());
                // 回调在发现线程上，这里只做一件事：把列表存下来，别在这里碰界面。
                d.setListener(new Discovery.Listener() {
                    @Override
                    public void onPeersChanged(List<Discovery.Peer> p) {
                        if (p != null) {
                            cache = p;
                        }
                    }

                    @Override
                    public void onControl(String host, String json) {
                        // ★ 控制消息现在从发现层来（见 Discovery 的类注释）：这一层一直活着，
                        //   所以接收器停着也收得到"请自动收听我"。正文和音频层那份对端统计
                        //   是同一份，所以这里直接走同一个入口 —— 两处绝不能有两套处理逻辑。
                        if (host == null || host.isEmpty() || json == null || json.isEmpty()) {
                            return;
                        }
                        try {
                            onPeerStats(host, json.getBytes(StandardCharsets.UTF_8));
                        } catch (Throwable t) {
                            Log.w(TAG, "发现层的控制消息处理失败（已忽略这一条）", t);
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
            autoListenHosts.clear();
            sendIds.clear();
            channelListen = false;      // 「停止」= 一切都停，重启后不要留着一个用户看不见的开关
            broadcastOn = false;
            // ★ 第 2 档：服务销毁/退出 App 时一定要把媒体音量还回去（不能把用户手机留在哑的状态）。
            sendingActive = false;      // 先摘掉"真的在送"这个条件，否则 sync 会以为还在送、又压回去
            syncSilentVolume();
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
                    List<Discovery.Peer> fresh = dropStale(l);
                    cache = fresh;
                    if (!fresh.isEmpty()) {
                        sawPeer = true;
                    }
                    return new ArrayList<Discovery.Peer>(fresh);
                }
            } catch (Throwable t) {
                Log.w(TAG, "读取设备列表失败，用上一次的快照", t);
            }
        }
        return new ArrayList<Discovery.Peer>(dropStale(cache));
    }

    /**
     * ★ 断联超过 2 分钟的设备不再出现在列表里（口径见 {@link LinkPolicy#CARD_STALE_MS}）。
     *
     * <p>为什么这道过滤必须存在：设备列表的过期原本<b>只</b>靠 Discovery 里的 {@code sweep()}，
     * 而那一步在发现 socket 短暂不可用时会被整段跳过 —— 于是对端早就断联两分多钟了，
     * 卡片还挂在界面上（用户真机截图：一张 2025BJB 的卡片写着"还没收到它的心跳"，
     * 上面还有一行"已停止向 192.168.0.103 发送 —— 它已经 128 秒没有任何消息了"）。
     * 在这里按"它多久没消息了"再兜一道：超过 2 分钟的直接不返回，卡片就没了。
     */
    private static List<Discovery.Peer> dropStale(List<Discovery.Peer> in) {
        if (in == null || in.isEmpty()) {
            return new ArrayList<Discovery.Peer>();
        }
        List<Discovery.Peer> out = new ArrayList<Discovery.Peer>(in.size());
        for (Discovery.Peer p : in) {
            if (p == null || LinkPolicy.cardStale(p.ageMs())) {
                continue;
            }
            out.add(p);
        }
        return out;
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

    /**
     * ★ ④ 在某台上选了「走蓝牙 / 走 WiFi」。
     *
     * <p>用户原话（2026-10-15）：「手机确认走蓝牙后应当由电脑发送蓝牙请求并配对互相连接。」
     * 手机这边只做两件事：① 记住这个选择 ② 每 2 秒跟着统计包把 {@code want_bt} 报给电脑
     * （幂等）。真正打开蓝牙 sink 的是电脑 —— 蓝牙设备列表在它手里。
     *
     * <p>选「走 WiFi」时传 false：请求自然撤回，不用额外的取消消息。
     */
    public void setBtWant(Discovery.Peer p, boolean on) {
        if (p == null || p.host == null || p.host.isEmpty()) {
            return;
        }
        if (on) {
            // 必须连端口一起记：它要进统计包的发送名单，而统计包是按 (地址, 端口) 发的。
            btWantPorts.put(p.host, p.audioPort > 0 ? p.audioPort : Proto.DEFAULT_AUDIO_PORT);
        } else {
            btWantPorts.remove(p.host);
        }
    }

    /** 这一台是不是被用户选了「走蓝牙」（发包线程读）。 */
    public boolean btWantTo(String host) {
        return host != null && !host.isEmpty() && btWantPorts.containsKey(host);
    }

    /** 点「收听」：收这台发来的声音。可以同时收听多台，声音会混在一起。 */
    public void setListening(Discovery.Peer p, boolean on) {
        if (p == null) {
            return;
        }
        String id = idOf(p);
        synchronized (lock) {
            if (on) {
                listenIds.add(id);
                // ★ 用户自己点开的：从"我们自动开的"里去掉 —— 以后对端说"我不送了"也不能收它。
                autoListenHosts.remove(p.host);
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
                autoListenHosts.remove(p.host);     // 已经不在收听名单里了，不欠这一笔
                // ★ 用户手动关掉这条收听【不再】写进任何黑名单：要不要自动只由这台设备的
                //   「智连」开关说了算。旧代码在这里把设备永久记进 listenRevoked，正是用户第 1 条
                //   「电脑主动发送声音手机不会主动接收」卡死的根因（只要电脑一直在请求，那份记忆
                //   就永远不过期），已彻底拆掉。
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
     * <p>三个理由：还开着「听广播」、还在送声音、还有收听目标。
     *
     * <p>★ 2026-10-15：这个方法以前是<b>反着写</b>的 —— 条件满足时不是停，而是把接收器
     * 拉起来并常驻（名单钉成一个不可能命中的地址，谁都播不了），只为了收对端那句
     * "请自动打开收听"。那是绕路，而且那条常驻的 Wi-Fi 锁会一直握着。用户当场就质疑过：
     * 「这一步不是瞎搞吗。我们设计了同一局域网设备能互相发现。那它本身就是多个设备都在
     *   互相同步数据啊」。
     *
     * <p>现在控制消息（want_listen / want_bt / 共享音量）都走发现层那个常驻 socket
     * （见 {@link Discovery} 的类注释与 {@code onControl} 回调），接收器不必再为了收请求
     * 而活着，所以这里回到本来的语义：真的没人要听、也没在送，就停掉。
     *
     * <p>「还在送声音」这一条留着：送的时候对端会把"我收了你多少"回过来，老版本对端
     * 是发到音频端口的，收不到那一句界面就只能显示"对方：等它回话"。
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
            // ★ 第 2 档：开始/停止送声都要让静音状态跟上（停了最后一台就立刻还原音量）。
            syncSilentVolume();
            if (sendIds.isEmpty() && !broadcastOn) {
                // 送声停了、又没有别的收听目标：接收器只是刚才为了收对端统计才开着的，
                // 一起收掉（否则它会一直占着 UDP 端口和一个 WifiLock）。
                // ★ 但「听广播」开着就不能收 —— 那正是它存在的理由。
                // ★ 2026-11-01：用户主动停掉最后一台「送声音」＝不再需要投屏，
                //   立刻释放授权，系统那条「屏幕共享中」随之消失。
                releaseProjectionIfIdle();
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
            // ★ 第 2 档：广播也是"在送声"，第 2 档同样适用；关掉广播且没有逐台送声就还原音量。
            syncSilentVolume();
            if (!on && sendIds.isEmpty()) {
                // 广播关了、也没有逐台送声 → 不再需要采本机声音，释放投屏授权。
                releaseProjectionIfIdle();
                stopReceiverIfIdle();
            }
        }
    }

    public boolean isBroadcasting() {
        return broadcastOn;
    }

    // ────────────────── ★ 2026-11-04（Q3 + 第 2 档）：送声音三档 ──────────────────

    /**
     * 当前对外的送声音档位：<b>0 = 没有任何声音在往外送</b>（既没点对点也没广播），
     * 1 = 送声+外放，2 = 送声+手机静音（音频**照常**继续发给电脑，只把本机媒体音量降到 0）。
     */
    public int sendMode() {
        synchronized (lock) {
            if (sendIds.isEmpty() && !broadcastOn) {
                return 0;
            }
            return sendModePref == 2 ? 2 : 1;
        }
    }

    /** 用户选的档位偏好（0 / 1 / 2）。没在送声时也保留，下一次送声按这个档位开始。 */
    public int sendModePref() {
        return sendModePref;
    }

    /**
     * 切换档位。即时生效、不动采集（音频流一秒都不会断）：
     * <ul>
     *   <li>0 —— 停掉所有点对点「送声音」，并<b>恢复系统媒体音量</b>（广播是独立线路，不归这里管）；</li>
     *   <li>1 —— 继续送，本机外放照常，并<b>恢复系统媒体音量</b>；</li>
     *   <li>2 —— 继续送，把 <b>STREAM_MUSIC 临时降到 0</b>（只这一条流；铃声/通知/通话都不碰），
     *       手机扬声器不响、但音频数据照常完整发送，电脑那头照常能听。</li>
     * </ul>
     *
     * <p><b>已知副作用</b>：因为是压低系统媒体音量，手机上其它 App 的媒体声音也会一起变哑 ——
     * 这是用户明确接受的代价；本应用做不到"只压这一路"（采集的是别的 App，没有独立播放通道）。
     *
     * <p>切换档位只改目标集合与音量，<b>不重建发送会话</b>，所以不会出现"断一下再连"。
     */
    public void setSendMode(int mode) {
        int m = mode <= 0 ? 0 : (mode >= 2 ? 2 : 1);
        synchronized (lock) {
            boolean changed = sendModePref != m;
            sendModePref = m;
            if (m == 0) {
                sendIds.clear();
                syncSenderTargets();
                if (!broadcastOn) {
                    releaseProjectionIfIdle();
                    stopReceiverIfIdle();
                }
            } else if (changed) {
                syncSenderTargets();
            }
            // ★ 第 2 档：真正的开关只在这一处；0/1 档一律恢复（不会把用户的音量留在 0）。
            syncSilentVolume();
            // ★ 2026-11-08：音量乘数跟着档位即时变化（外放档乘系统音量；静音档默认不乘）。
            AudioSend s = sender;
            if (s != null) {
                s.setSendMode(m >= 2 ? 2 : 1);
            }
            if (changed) {
                saveSendMode();
            }
        }
    }

    /** 第 2 档对用户的一句话（UI 详情/长按用）。 */
    public static final String SEND_MODE_SILENT_NOTE =
            "第 2 档：手机会静音（系统媒体音量临时降到 0），声音仍在完整传给电脑。"
            + "退出 App、停止送声、切到第 0/1 档都会自动还原音量。"
            + "注意：手机其它 App 的媒体声音也会一起变哑 —— 本应用采集的就是系统媒体声，做不到只压一路。";

    private static final String PREF_SEND_MODE = "send_mode";
    // ★ 第 2 档崩溃兜底：进入静默前先落盘"原音量 + 进入时间"，下次启动若发现残留就立即还原。
    private static final String PREF_SILENT_ACTIVE = "silent_vol_active";
    private static final String PREF_SILENT_ORIG = "silent_vol_orig";
    private static final String PREF_SILENT_AT = "silent_vol_at";

    private void loadSendMode() {
        if (app == null) {
            return;
        }
        try {
            int v = app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                    .getInt(PREF_SEND_MODE, 1);
            sendModePref = v <= 0 ? 0 : (v >= 2 ? 2 : 1);
        } catch (Throwable t) {
            sendModePref = 1;
            Log.w(TAG, "读送声音档位失败（按第 1 档处理）：" + t);
        }
    }

    private void saveSendMode() {
        if (app == null) {
            return;
        }
        try {
            app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE).edit()
                    .putInt(PREF_SEND_MODE, sendModePref).apply();
        } catch (Throwable t) {
            Log.w(TAG, "存送声音档位失败（下次仍按默认第 1 档）：" + t);
        }
    }

    // ── ★ 2026-11-08：静音档乘数策略（internal_only / last_nonzero） ──

    private static final String PREF_SILENT_VOL_POLICY = "silent_vol_policy";

    private void loadSilentVolPolicy() {
        if (app == null) {
            return;
        }
        try {
            String v = app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                    .getString(PREF_SILENT_VOL_POLICY, SendVolume.POLICY_INTERNAL);
            silentVolPolicy = SendVolume.normalizePolicy(v);
        } catch (Throwable t) {
            silentVolPolicy = SendVolume.POLICY_INTERNAL;
            Log.w(TAG, "读静音档乘数策略失败（按 internal_only 处理）：" + t);
        }
    }

    /** 静音档乘数策略（internal_only / last_nonzero）。 */
    public String silentVolPolicy() {
        return silentVolPolicy;
    }

    /**
     * 切换静音档乘数策略。手机界面点击和电脑下发都走这里 —— 状态只有一份。
     *
     * @param persist true = 写进本地（两个入口都写，重启后保持一致）
     */
    public void setSilentVolPolicy(String policy, boolean persist) {
        String p = SendVolume.normalizePolicy(policy);
        silentVolPolicy = p;
        AudioSend s = sender;
        if (s != null) {
            s.setSilentVolPolicy(p);
        }
        if (persist && app != null) {
            try {
                app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE).edit()
                        .putString(PREF_SILENT_VOL_POLICY, p).apply();
            } catch (Throwable t) {
                Log.w(TAG, "存静音档乘数策略失败（本次仍生效）：" + t);
            }
        }
        Log.i(TAG, "静音档乘数策略 = " + p + "（" + SendVolume.policyText(p) + "）");
    }

    /** 手机界面/诊断报告里的一行中文（当前档位 + 策略 + 实际乘数）。 */
    public String volumeModeText() {
        int volQ14 = SendVolume.q14(mediaVolumeRatio());
        int mediaQ14 = SendVolume.mediaQ14(sendModePref, silentVolPolicy, volQ14, volQ14);
        return SendVolume.text(sendModePref, silentVolPolicy, mediaQ14);
    }

    /** 当前实际生效的档位名（external/silent）—— 统计包用。 */
    public String volumeSource() {
        return SendVolume.modeName(sendModePref);
    }

    /**
     * ★ 第 2 档的唯一实现点：**整个工程里只有这里允许调用 setStreamVolume**。
     *
     * <p>为什么集中成一个函数：这条路径要能被审计（"到底哪里动了用户音量"），
     * 也保证 `setStreamMute` 一处都不出现 —— 静音用"音量归零"而不是 mute 标志，
     * 这样用户自己调音量条也能立刻听出来、不会被我们偷偷锁住。
     *
     * <p>进入前先把原音量与时间写进 SharedPreferences（用 commit 同步落盘），
     * 进程被杀/崩溃也不会把用户手机留在哑的状态；{@link #restoreSilentVolumeIfNeeded()}
     * 在下次启动时兜底还原。
     */
    private void applySilentVolume(boolean on) {
        if (app == null) {
            return;
        }
        try {
            AudioManager am = (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
            if (am == null) {
                return;
            }
            if (on) {
                android.content.SharedPreferences sp =
                        app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE);
                if (!sp.getBoolean(PREF_SILENT_ACTIVE, false)) {
                    int orig = am.getStreamVolume(AudioManager.STREAM_MUSIC);
                    sp.edit()
                            .putBoolean(PREF_SILENT_ACTIVE, true)
                            .putInt(PREF_SILENT_ORIG, orig)
                            .putLong(PREF_SILENT_AT, System.currentTimeMillis())
                            .commit();          // 先落盘再静音：崩在下一行也能还原
                } else {
                    // ★ 2026-11-06（Q1）：现场「静音档下把 App 音量 100→10，耳机几乎不变」。
                    //   根因就在这里：旧实现只在进入静音那一刻记一次 orig，之后永远拿
                    //   这个旧值当乘数（实测 0.11 一直不动）。而我们的静音手段是把
                    //   STREAM_MUSIC 钉在 0，用户/App 每次调整都会先落到这个值上 ——
                    //   现在把它当成"用户最新想用的音量"记下来，再归零。
                    //   · 绝对值（>1）→ 直接采信（App 自己的音量滑杆多半走 setStreamVolume）；
                    //   · 系统已被钉在 0 时，硬件键一次只能抬到 1 档 → 这一档按"再加一档"
                    //     解释，用户才能连续调节（否则按一次就到顶、怎么按都停在 0.11）。
                    int cur = am.getStreamVolume(AudioManager.STREAM_MUSIC);
                    if (cur > 0) {
                        int user = sp.getInt(PREF_SILENT_ORIG, cur);
                        int max = Math.max(1, am.getStreamMaxVolume(AudioManager.STREAM_MUSIC));
                        int next = cur;
                        if (cur == 1 && user >= 1) {
                            next = Math.min(max, user + 1);
                        }
                        next = Math.max(0, Math.min(max, next));
                        if (next != user) {
                            sp.edit().putInt(PREF_SILENT_ORIG, next).apply();
                        }
                    }
                }
                // 已经是 0 就不再重复写（本函数每 150ms 会被状态同步调到，不能去骚扰系统）。
                if (am.getStreamVolume(AudioManager.STREAM_MUSIC) != 0) {
                    am.setStreamVolume(AudioManager.STREAM_MUSIC, 0, 0);
                }
            } else {
                restoreSilentVolumeIfNeeded();
            }
        } catch (Throwable t) {
            Log.w(TAG, "第 2 档音量切换失败（不改用户音量，如实记录）：" + t);
        }
    }

    /** 有残留标记（上次进程被杀/崩溃）就立刻还原，并把标记清掉。可重复调用。 */
    private void restoreSilentVolumeIfNeeded() {
        if (app == null) {
            return;
        }
        try {
            android.content.SharedPreferences sp =
                    app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE);
            if (!sp.getBoolean(PREF_SILENT_ACTIVE, false)) {
                return;
            }
            int orig = sp.getInt(PREF_SILENT_ORIG, -1);
            AudioManager am = (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
            if (am != null && orig >= 0) {
                am.setStreamVolume(AudioManager.STREAM_MUSIC, orig, 0);
            }
            sp.edit()
                    .remove(PREF_SILENT_ACTIVE)
                    .remove(PREF_SILENT_ORIG)
                    .remove(PREF_SILENT_AT)
                    .commit();
            Log.i(TAG, "第 2 档残留标记已还原媒体音量：" + orig);
        } catch (Throwable t) {
            Log.w(TAG, "还原第 2 档音量失败（标记保留，下次启动再试）：" + t);
        }
    }

    /**
     * 让"系统媒体音量"跟当前档位/发送状态对齐（幂等，可每次状态变化都调）。
     * 只在 `syncSilentVolume` 里判条件，真正的 setStreamVolume 仍集中在 {@link #applySilentVolume}。
     */
    private void syncSilentVolume() {
        // 只有"真的在送"才压音量：目标都掉线/蓝牙断了/音源没授权时，手机不该继续哑着。
        applySilentVolume(sendModePref == 2 && sendingActive && sourceEnabled);
    }

    /**
     * 把"现在应该送给谁"同步给发送端。
     *
     * <p><b>关键</b>：目标集合变化<b>不会重启采集</b> —— 采集器只在"从没送过/停了"时起一次，
     * 之后都是 {@code setTargets()}。所以对方上线/下线时正在送的声音不会断一下。
     */
    private void syncSenderTargets() {
        // ★ 2026-11-06（Q2）：把当前送声身份同步进信标。这个函数每 150ms 被 tick
        //   调一次，值没变时 Discovery 内部直接跳过，不会增加网络负担。
        if (discovery != null) {
            discovery.setSendState(sendMode(), broadcastOn);
        }
        if (!sourceEnabled) {
            // ★ 音源关了 = 不采也不发（"送给谁"的意图已在 applySourceState 里清掉）。
            sendingActive = false;
            syncSilentVolume();      // ★ 第 2 档：音源不可用就还原音量，不能把手机留在哑的状态
            stopSender();
            return;
        }
        List<String> eps = collectSendTargets();
        // 这个函数每 150ms 会被界面 tick 调一次：目标没变就什么都不做，
        // 免得一直重建目标表（目标表的比较用字符串签名，几分之一微秒的事）。
        String sig = eps.toString();
        boolean changed = !sig.equals(lastTargetSig);
        lastTargetSig = sig;
        AudioSend s = sender;
        // ★ 2026-11-04（B 第一阶段）：扩展段只在**所有当前目标**都声明 ext:1 时启用。
        //   只要目标里还有一台老电脑，就整条链路发老包 —— 老电脑不能因为多出的 8 字节
        //   听到"开头 8 个字节被吃掉"的杂音。这是向后兼容的硬要求。
        boolean extOk = targetsSupportExt(eps);
        if (s != null) {
            s.setSelfOriginId(selfOriginId());
            s.setExtEnabled(extOk);
        }
        if (eps.isEmpty()) {
            // ★ 第 2 档：没有收包人了（关机/换网/蓝牙断）→ 还原音量，别让手机白哑着。
            sendingActive = false;
            syncSilentVolume();
            if (s != null) {
                stopSender();
            }
            // 注意：这里不要顺手 releaseProjection —— onProjectionGranted 会先 sync 再把
            // 刚授权的目标加进 sendIds；在这里释放会把刚拿到的授权当场作废。
            // 「用户主动停送 / 停广播」两个入口会各自调 releaseProjectionIfIdle()。
            return;
        }
        if (s == null || !s.isRunning()) {
            try {
                // 用不带目标的构造：采集只有一份，目标随后由 setTargets 给。
                AudioSend fresh = new AudioSend(app);
                fresh.setGain(volume);
                fresh.setSelfOriginId(selfOriginId());
                fresh.setExtEnabled(extOk);
                // ★ 2026-11-08：音量乘数跟着送声档位走（外放档乘系统音量、静音档默认不乘）。
                fresh.setSendMode(sendModePref);
                fresh.setSilentVolPolicy(silentVolPolicy);
                // ★ 把"送什么声音"接上：音源 + 本机声音的授权。
                //   拿不到授权、或低于 Android 10 时，AudioSend 自己会退回麦克风并记下原因，
                //   界面拿 activeSourceKind() 如实显示"现在实际送的是哪一个"。
                fresh.setProjection(projection);
                fresh.setSourceKind(sourceKind);
                fresh.setTargets(eps);
                restoreGains(fresh);     // ★ 把（用户或对端同步来的）逐目标音量补回去
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
        // ★ 第 2 档：以"发送器真的在跑"为准，而不是只看勾选意图 —— 采集失败时会还原音量。
        AudioSend nowSending = sender;
        sendingActive = nowSending != null && nowSending.isRunning();
        syncSilentVolume();
    }

    /**
     * 算出现在该送给哪些地址："广播"给全部在线设备 + 用户单独勾的那几台。
     *
     * <p><b>并且判"对端是不是确认走了"</b>（2026-10-14 用户反馈的安全问题：
     * 「电脑关闭应用，手机声音还在向电脑发送」）：勾选/广播是<b>意图</b>，
     * 这里再叠一层<b>活性</b> —— 确认走了的就不发，但意图保留，对方回来自动恢复。
     */
    /** ★ 2026-11-04：本机 originId = device_id 的稳定哈希（和电脑端 CRC32 口径一致）。 */
    private int selfOriginId() {
        Discovery d = discovery;
        return Proto.originIdOf(d == null ? "" : d.deviceId());
    }

    /**
     * ★ 2026-11-04（B 第一阶段）：这些发送目标是否**全部**声明 ext:1。
     *
     * <p>只认"发现列表里同一 host 的那台设备"；有一台找不到或 ext=false 就返回 false，
     * 整条链路退回老包。宁可少一层防环，也不能把老端的音频解码搞坏。
     */
    private boolean targetsSupportExt(List<String> eps) {
        if (eps == null || eps.isEmpty()) {
            return false;
        }
        Discovery d = discovery;
        List<Discovery.Peer> peers = d == null ? null : d.peers();
        if (peers == null || peers.isEmpty()) {
            return false;
        }
        for (String ep : eps) {
            if (ep == null) {
                return false;
            }
            int colon = ep.lastIndexOf(':');
            String host = colon > 0 ? ep.substring(0, colon).trim() : ep.trim();
            boolean found = false;
            for (Discovery.Peer p : peers) {
                if (p != null && p.host != null && p.host.equalsIgnoreCase(host)) {
                    found = true;
                    if (!p.ext) {
                        return false;
                    }
                    break;
                }
            }
            if (!found) {
                return false;
            }
        }
        return true;
    }

    private List<String> collectSendTargets() {
        List<String> out = new ArrayList<String>();
        long nowMs = SystemClock.elapsedRealtime();
        StringBuilder gone = new StringBuilder();

        // ① 发现层看得见的设备：能看见 = 它活着（信标就是活性证据）
        for (Discovery.Peer p : peers()) {
            if (p == null || p.host == null || p.host.isEmpty()) {
                continue;
            }
            String id = idOf(p);
            if (!broadcastOn && !sendIds.contains(id)) {
                continue;
            }
            sendHosts.put(id, p.host);
            sendLastAliveMs.put(id, nowMs);
            int port = p.audioPort > 0 ? p.audioPort : Proto.DEFAULT_AUDIO_PORT;
            String ep = p.host + ":" + port;
            if (!out.contains(ep)) {
                out.add(ep);
            }
        }
        // ② 用户单独勾过、但发现列表里暂时没有的：看心跳（T_STATS）还在不在，
        //    以及"上次确认它还活着"距今多久。超过宽限期就不发（意图保留）。
        //    ★ 但断联超过 2 分钟（LinkPolicy.CARD_STALE_MS）就直接删掉：这个对端不会回来了，
        //      连"送声音"的意图一起清（用户原话「这种已经信号断联两分钟的就直接删掉就好了，
        //      不然全堆积在程序里」）—— 那行"已停止向 XXX 发送"的陈旧提示也随之消失。
        List<String> staleIds = new ArrayList<String>();
        for (String id : new ArrayList<String>(sendIds)) {
            String host = sendHosts.get(id);
            Long last = sendLastAliveMs.get(id);
            if (host != null && last != null && LinkPolicy.cardStale(nowMs - last)) {
                staleIds.add(id);
                continue;
            }
            if (out.contains(endpointOf(id))) {
                continue;
            }
            if (broadcastOn && out.size() > 0) {
                continue;   // 广播模式下别的设备已经在发了，这台单独走下面的活性判断
            }
            if (host == null || last == null) {
                continue;   // 还没见过它长什么样：交给发现层去发现，不算"走了"
            }
            long silent = nowMs - last;
            if (silent > LinkPolicy.GRACE_MS) {
                gone.append(LinkPolicy.goneText(nameOfHost(host), silent)).append('\n');
            } else {
                Discovery.Peer p = findById(id);
                int port = (p != null && p.audioPort > 0) ? p.audioPort : Proto.DEFAULT_AUDIO_PORT;
                String ep = host + ":" + port;
                if (!out.contains(ep)) {
                    out.add(ep);
                }
            }
        }
        for (String id : staleIds) {
            // 意图、地址、活性记录一起清 —— 不留任何"堆在程序里"的陈旧条目。
            sendIds.remove(id);
            sendHosts.remove(id);
            sendLastAliveMs.remove(id);
            Log.i(TAG, "对端断联超过 2 分钟，已清掉送声目标：" + id);
        }
        if (broadcastOn && out.isEmpty() && !sendIds.isEmpty()) {
            gone.append("广播开着，但现在一台设备都联系不上（都离线了）。\n");
        }
        String gt = gone.toString().trim();
        if (!gt.equals(sendSuspendedText)) {
            sendSuspendedText = gt;
            if (gt.length() > 0) {
                Log.i(TAG, "发送目标被暂停：" + gt.replace('\n', ' '));
            }
        }
        return out;
    }

    /** 取 map 里的增益；没有就返回 GAIN_UNSET（= "我没报过"，对端会保持它自己的值）。 */
    private static float gainOf(Map<String, Float> m, String host) {
        Float v = m.get(host);
        return v == null ? Diagnosis.GAIN_UNSET : v;
    }

    private static long tsOf(Map<String, Long> m, String host) {
        Long v = m.get(host);
        return v == null ? 0L : v;
    }

    /** 某一台的目标地址（"host:port"）；不知道就返回空串。 */
    private String endpointOf(String id) {        String host = sendHosts.get(id);
        if (host == null || host.isEmpty()) {
            return "";
        }
        Discovery.Peer p = findById(id);
        int port = (p != null && p.audioPort > 0) ? p.audioPort : Proto.DEFAULT_AUDIO_PORT;
        return host + ":" + port;
    }

    /** ★ 因为"确认走了"而暂停发送的说明（没有就返回空串）。界面/通知/报告都用它。 */
    public String sendSuspendedText() {
        return sendSuspendedText == null ? "" : sendSuspendedText;
    }

    /** 某台设备现在是不是"用户想送、但确认走了所以暂停了"（界面显示用）。 */
    public boolean isSuspendedTarget(String host) {
        if (host == null || host.isEmpty()) {
            return false;
        }
        AudioSend s = sender;
        if (s != null) {
            for (SendFanout.TargetStats ts : s.targetStats()) {
                if (host.equals(ts.host)) {
                    return false;      // 还在目标表里 = 还在发
                }
            }
        }
        for (Map.Entry<String, String> e : sendHosts.entrySet()) {
            if (host.equals(e.getValue()) && (sendIds.contains(e.getKey()) || broadcastOn)) {
                return true;
            }
        }
        return false;
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

    // ────────────────── ★ 每台设备自己的可调音量（"我听它的 / 我送它的"） ──────────────────
    //
    // 用户原话：「我要求的是各设备【可调节】的音量条。当然现在有的显示实际音量条也不错」
    // —— 所以电平条（实时显示）保留，旁边再加一条可调滑杆。
    // 两条滑杆控制的都是【我们这一层】的增益（绝不碰手机系统音量，这条口径和回归测试都在）：
    //   · 我听它的 → AudioRecv 里只乘这一路的样本
    //   · 我送它的 → SendFanout 里只缩放发给这一台的那份包
    // 主音量滑杆（"我们这一层的音量"）是总增益，叠在这些单台增益之上，互不冲突。

    /** 设定"我听它的"增益（0~1.5，1.0 = 原样）。立刻生效，并且会同步给对端（共享增益）。 */
    public void setPeerRecvGain(String host, float g) {
        float v = LinkPolicy.clampGain(g);
        applyRecvGain(host, v);
        linkRecvGainTs.put(host, System.currentTimeMillis());     // 本地改的：时间戳最新
        linkRecvGainChangedAtMs.put(host, SystemClock.elapsedRealtime());
        pokeGainStats();      // 别让对端等满 2 秒才看到新值（拖动时按 ~120ms 节流）
    }

    /** 某一台现在的"我听它的"增益。 */
    public float peerRecvGain(String host) {
        Float v = linkRecvGain.get(host);
        if (v != null) {
            return v;
        }
        AudioRecv r = receiver;
        if (r == null || host == null) {
            return 1f;
        }
        try {
            return r.hostGain(host);
        } catch (Throwable t) {
            return 1f;
        }
    }

    /** 设定"我送它的"增益（0~1.5，1.0 = 原样）。立刻生效，并且会同步给对端（共享增益）。 */
    public void setPeerSendGain(String host, float g) {
        float v = LinkPolicy.clampGain(g);
        applySendGain(host, v);
        linkSendGainTs.put(host, System.currentTimeMillis());
        linkSendGainChangedAtMs.put(host, SystemClock.elapsedRealtime());
        pokeGainStats();      // 同上：对端不等那 2 秒（拖动时按 ~120ms 节流）
    }

    /** 上一次因为"拖音量"叫醒统计线程的时刻（elapsedRealtime）。 */
    private volatile long lastGainPokeMs;

    /**
     * ★ 2026-11-01 真机自测（USB 调试）：拖动滑杆时每一帧都会调 setPeerRecvGain/
     * setPeerSendGain，原来每一帧都 {@link #pokeStats()}，一次拖动就是几十个统计包。
     * 电脑端按到达顺序落增益，UDP 乱序时最后可能停在**中间值**（实测 0.51 被 1.45 顶掉）。
     * 这里按 120ms 节流：拖动过程仍有连续的视觉反馈，松手时界面会再补一次
     * {@link #pokeStats()}（见 MainActivity.onStopTrackingTouch），保证**最终值**最后一个发出。
     */
    private void pokeGainStats() {
        long now = SystemClock.elapsedRealtime();
        if (now - lastGainPokeMs < 120L) {
            return;
        }
        lastGainPokeMs = now;
        pokeStats();
    }

    /** 某一台现在的"我送它的"增益。 */
    public float peerSendGain(String host) {
        Float v = linkSendGain.get(host);
        if (v != null) {
            return v;
        }
        return 1f;
    }

    public boolean isListening(Discovery.Peer p) {
        synchronized (lock) {
            return p != null && listenIds.contains(idOf(p));
        }
    }

    /**
     * 这台设备现在会不会收到我的声音（**用户单独勾了它**才算）。
     *
     * <p>★ 2026-10-16 用户澄清（原话）：「手机点击开广播，手机上就会直接打开所有路由上的"送声音"…
     * 广播是设备只有开关本机"听""送"的权利的」——
     * 广播是**一条独立的线路**，不是"逐台针对性发送"。所以这一台卡片上的「送声音」
     * 永远只反映用户自己勾过的选择；广播开着时不再把每张卡都点亮
     * （"我正在广播"只由上面那个 📢 开关显示）。
     * 广播期间逐台那个按钮会被画成灰的、点了给一句说明（见 MainActivity.onSendClick）。
     */
    public boolean isSending(Discovery.Peer p) {
        synchronized (lock) {
            if (p == null) {
                return false;
            }
            return sendIds.contains(idOf(p));
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
                    // ★ 不再把用户的"送声音"意图删掉（2026-10-14 安全检查）：
                    //   对方关掉 App 之后我们要停止发送，但**意图要留着** —— 它回来自动恢复，
                    //   不该让用户再点一次。真正决定"发不发"的是 collectSendTargets 里的活性判断。
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

    /** 音源开关：true = 在采（按 sourceKind），false = 已停止。 */
    public boolean sourceEnabled() {
        return sourceEnabled;
    }

    /**
     * 音源<b>真实的状态</b>（{@link SourcePolicy} 的 SRC_OFF / SRC_MIC / SRC_PLAYBACK）。
     *
     * <p>界面那两颗按钮的视觉状态只许从它读，而且<b>每次刷新都重新算</b>（实时问采集端
     * {@link #activeSourceKind()}）—— 绝不用缓存的标志位，否则就会出现用户报过的
     * "实际在采麦克风、按钮却还是灰的"。
     */
    public int sourceState() {
        return SourcePolicy.state(sourceEnabled, activeSourceKind());
    }

    /** 这一颗音源按钮现在该不该点亮（供界面读；返回的就是引擎当下的真实状态）。 */
    public boolean sourceActive(int kind) {
        return SourcePolicy.lit(sourceState(), kind);
    }

    /**
     * 应用一个音源状态（值来自 {@link SourcePolicy} 的纯函数，界面据此点亮/熄灭按钮）。
     *
     * <p>OFF = 停止采集：顺带把"送给谁 / 广播"的意图也清掉 —— 停止采集还显示"在送声音"是骗人的。
     * MIC / PLAYBACK = 打开（或热切）那一路；<b>同一时刻只有一路</b>（采集端只有一份 AudioRecord）。
     *
     * @param state           {@link SourcePolicy} 的状态值
     * @param whyIfStopping   停止时的原因（写进界面；空则用"你手动停止了采集"）
     */
    public void applySourceState(int state, String whyIfStopping) {
        synchronized (lock) {
            boolean on = state != SourcePolicy.SRC_OFF;
            sourceEnabled = on;
            if (on) {
                sourceKind = state;
                sourceStopReason = "";
            } else {
                String why = (whyIfStopping == null) ? "" : whyIfStopping;
                sourceStopReason = why.length() > 0 ? why : "你手动停止了采集";
                sendIds.clear();
                broadcastOn = false;
            }
            AudioSend s = sender;
            if (on && s != null) {
                try {
                    s.setProjection(projection);
                    s.setSourceKind(sourceKind);
                } catch (Throwable t) {
                    Log.w(TAG, "切换音源失败", t);
                }
            }
            syncSenderTargets();
            // ★ 2026-11-01 真机：关闭 App 后系统仍挂着「Audio Mixing 正在获取屏幕」，
            //   卸载后也还在。根因是 MediaProjection 被刻意长期持有，只有 Engine.stop()
            //   才释放；而前台服务是 START_STICKY、界面关了它还活着。
            //   现在只要不再采「本机声音」（手动停止 / 切到麦克风），就立刻 stop() 掉授权：
            //   系统那条「屏幕共享中」随之消失。下次再选本机声音＝重新弹一次系统授权，
            //   这是隐私上正确的代价。
            if (!on || sourceKind != AudioSend.SRC_PLAYBACK) {
                releaseProjection();
            }
            if (!on) {
                stopReceiverIfIdle();
            }
        }
    }

    /** 这台机器支不支持采本机声音（Android 10+）。 */
    public boolean playbackSupported() {
        return AudioSend.playbackSupported();
    }

    public boolean hasProjection() {
        return projection != null;
    }

    // ── ★ 端口设置（2026-11-01）：电脑端改过端口后，手机必须能填同样的值 ──

    private int savedPort(String key, int def) {
        try {
            int v = app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                    .getInt(key, def);
            return (v >= 1024 && v <= 65534) ? v : def;
        } catch (Throwable t) {
            return def;
        }
    }

    /**
     * 保存的发现端口（默认 17830）。
     *
     * ★ 2026-11-01 用户：两端默认端口选 17830。老版本存下来的是旧默认 47830 ——
     * 那是"从没改过"的值，升级时自动搬到 17830，和电脑端一起换（否则两端都停在
     * 47830 也还能连，但新的默认就永远落不了地）。用户自己填过的其它值（如 17930）
     * 一律不动。
     */
    public int configuredDiscoveryPort() {
        int v = savedPort("disc_port", Proto.DEFAULT_DISCOVERY_PORT);
        if (v == Proto.LEGACY_DISCOVERY_PORT) {
            v = Proto.DEFAULT_DISCOVERY_PORT;
            try {
                app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                        .edit().putInt("disc_port", v).apply();
            } catch (Throwable t) {
                Log.w(TAG, "迁移旧发现端口失败（本次仍按 17830 用）", t);
            }
        }
        return v;
    }

    /** 音频端口不再让用户填：自动选空闲口，并在信标里公布，电脑按它发。 */
    public int configuredAudioPort() {
        return Proto.DEFAULT_AUDIO_PORT;
    }

    /** 当前实际绑定的发现端口（没启动时返回配置值）。 */
    public int discoveryPort() {
        Discovery d = discovery;
        return (d != null) ? d.boundPort() : configuredDiscoveryPort();
    }

    /**
     * 只保存发现端口。音频端口自动选空闲口并通过信标公布，不再让用户填。
     * 端口在 {@link #start()} 时生效，所以界面保存后要提示"重启 App"。
     */
    public boolean savePorts(int disc) {
        if (disc < 1024 || disc > 65534) {
            return false;
        }
        try {
            app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                    .edit()
                    .putInt("disc_port", disc)
                    .remove("audio_port")
                    .apply();
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "端口设置存不下来", t);
            return false;
        }
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
            // ★ 2026-11-01：授权弹窗还开着时用户已经关掉了「本机声音」/停止采集 →
            //   这张授权回来也不能留：立刻 stop 掉，否则又会出现"已经停了，
            //   系统却一直显示正在获取屏幕"。
            if (!sourceEnabled) {
                try {
                    p.stop();
                } catch (Throwable ignored) {
                }
                projectionError = "授权回来时采集已经停止，已立即关闭投屏授权";
                return;
            }
            projection = p;
            projectionError = "";
            sourceStopReason = "";
            try {
                // 必须注册回调：授权被系统收回时要立刻知道，否则界面会一直显示"送声中"
                // 而实际上一片静音 —— 那是最难查的一种假象。
                p.registerCallback(projectionCallback(), mainHandler);
            } catch (Throwable t) {
                Log.w(TAG, "注册投屏回调失败（继续用，只是收不到系统收回的通知）", t);
            }
            target = (pendingSendId == null) ? "" : pendingSendId;
            pendingSendId = "";
            // ★ 用户点了系统授权窗口 = 明确要本机声音：把音源开关打开，再热切到本机声音。
            if (sourceKind == AudioSend.SRC_PLAYBACK) {
                sourceEnabled = true;
            }
            AudioSend s = sender;
            if (s != null && sourceEnabled && sourceKind == AudioSend.SRC_PLAYBACK) {
                try {
                    s.setProjection(p);
                    s.setSourceKind(AudioSend.SRC_PLAYBACK);
                } catch (Throwable t) {
                    Log.w(TAG, "授权到手后切换到本机声音失败", t);
                }
            }
            // 授权到位后重建一次发送端：之前没授权时它打不开，现在能采了。
            syncSenderTargets();
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
            AudioSend s = sender;
            if (s != null) {
                try {
                    s.setProjection(null);
                } catch (Throwable ignored) {
                }
            }
            // ★ 绝不自动开麦：本机声音这条路断了就【停止】，并由用户自己决定下一步。
            applySourceState(SourcePolicy.afterPlaybackLost(sourceState()), msg);
        }
        Log.w(TAG, "采本机声音的授权没拿到：" + msg);
    }

    /**
     * 系统把授权收回了（用户在状态栏点"停止投放"、或者被别的 App / 系统抢走）。
     *
     * <p>★ 2026-10-19 用户第 1 条（隐私硬约束）：<b>绝不自动退回麦克风</b>。
     * 旧代码在这里 {@code setSourceKind(SRC_MIC)}，于是用户"停止本机声音采集"时麦克风被
     * <b>悄悄打开</b>（而按钮还是灰的，显示与实际不一致）—— 这正是"严重危害用户安全"那条。
     * 现在的处理：本机声音采集一律<b>停止</b>，界面如实显示"已停止 + 原因"。
     */
    public void onProjectionStopped() {
        synchronized (lock) {
            boolean wasGranted = projection != null;
            projection = null;
            if (wasGranted) {
                projectionError = "系统收回了「录制/投射」授权（在状态栏点了停止，或被别的 App 抢走）";
            }
            String reason = projectionError.length() > 0 ? projectionError : "「录制/投射」授权没了";
            AudioSend s = sender;
            if (s != null) {
                try {
                    s.setProjection(null);
                } catch (Throwable t) {
                    Log.w(TAG, "清掉旧授权失败", t);
                }
            }
            // 本机声音还在采 → 停掉；麦克风在采则不受影响（那条路和授权无关）。
            applySourceState(SourcePolicy.afterPlaybackLost(sourceState()), reason);
        }
        Log.w(TAG, "采本机声音的授权被系统收回了");
    }

    /**
     * 没有实际采集需求时释放投屏授权。
     *
     * <p>判据必须看"发送端是否真的在跑"，不能只看用户选的音源：
     * 用户可能选了「本机声音」但所有目标都离线，此时 `sender == null`，
     * 继续持有 MediaProjection 只会让系统那条「屏幕共享中」白白挂着。
     */
    private void releaseProjectionIfIdle() {
        if (projection == null) {
            return;
        }
        if (sourceEnabled && sourceKind == AudioSend.SRC_PLAYBACK && sender == null) {
            releaseProjection();
        }
    }

    /** 释放授权（停引擎时调用）。释放之后要再送本机声音就得重新授权一次。 */
    private void releaseProjection() {
        MediaProjection p;
        synchronized (lock) {
            p = projection;
            projection = null;
            pendingSendId = "";
            // 采集端手里那份引用也要清掉：它继续拿着一个已经 stop 的对象，
            // 下次热切回本机声音只会得到一片静音。
            AudioSend s = sender;
            if (s != null) {
                try {
                    s.setProjection(null);
                } catch (Throwable ignored) {
                }
            }
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

    /**
     * 界面主卡片里那句「现在送的是什么」。
     *
     * <p>★ 说的是<b>引擎当下的真实状态</b>（{@link #sourceState()}，实时问采集端），不是"用户上次选的"。
     * 用户第 1 条报的"实际在采麦克风、按钮却还是灰的"就是两者不一致造成的。
     */
    public String sourceStateText() {
        if (!AudioSend.playbackSupported()) {
            return "你的安卓版本（Android " + Build.VERSION.RELEASE + "）不支持采集本机声音，只能用麦克风。"
                    + "要送手机上正在放的声音，需要 Android 10 及以上。";
        }
        int state = sourceState();
        if (state == SourcePolicy.SRC_OFF) {
            String why = sourceStopReason.length() > 0 ? ("（" + sourceStopReason + "）") : "";
            return "已停止采集" + why + "：两个音源都关着（同一时刻只能采一路）。"
                    + "点「🔊 本机声音」或「🎤 麦克风」才会重新开始采。";
        }
        AudioSend s = sender;
        boolean sending = s != null && s.isRunning();
        if (state == SourcePolicy.SRC_PLAYBACK) {
            if (sending) {
                return "正在送：🔊 本机声音（手机上正在放的音乐/视频/游戏）";
            }
            return "已打开 🔊 本机声音采集；点任意一台设备的「送声音」开始送"
                    + (projection == null ? "（第一次会让你在系统窗口里点一次允许）" : "") + "。";
        }
        // 麦克风：把"另一颗为什么灭了"写清楚（用户第 3 条口径：同一时刻只能采一路）。
        String exclusive = "（同一时刻只能采一路：开了麦克风，「🔊 本机声音」那颗就是关的）";
        if (sending) {
            return "正在送：🎤 麦克风（对讲用的）" + exclusive;
        }
        return "已打开 🎤 麦克风采集；点任意一台设备的「送声音」开始送。" + exclusive;
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
            // ★ 2026-11-04（B 第一阶段）：自回声过滤的基准。对自己 device_id 的哈希；
            //   收到 originId == 自己的帧直接丢并计数（诊断页显示"自回声丢弃 N"）。
            r.setSelfOriginId(selfOriginId());
            // ★ 对端统计的回话入口：对方每 2 秒会说一次"我发了多少、是不是正在发"。
            //   回调在收包线程上，所以只做"解析 + 存起来"；解析失败就当没收到，
            //   绝不能让一份坏包把"收听"整条链路带走。
            r.setStatsListener(new AudioRecv.StatsListener() {
                @Override
                public void onStats(String host, byte[] payload) {
                    onPeerStats(host, payload);
                }
            });
            // ★ 2026-11-02：默认音频端口被别的 App 占时，AudioRecv 会自己换空闲口；
            //   必须在信标里公布实际端口，否则电脑按旧端口发过来全部丢包。
            r.setPortListener(new AudioRecv.PortListener() {
                @Override
                public void onPortChanged(int oldPort, int newPort) {
                    audioPort = newPort;
                    Discovery d = discovery;
                    if (d != null) {
                        try {
                            d.setAudioPort(newPort);
                        } catch (Throwable t) {
                            Log.w(TAG, "同步音频端口到信标失败", t);
                        }
                    }
                    Log.i(TAG, "音频端口已改为 " + newPort + "（原 " + oldPort + "），信标已同步");
                }
            });
            r.start();
            receiver = r;
            // ★ 端口被别的 App 占了时 AudioRecv 会换一个；信标必须跟着改，
            //   否则电脑按信标里的旧端口发过来，手机永远收不到。
            int actual = r.port();
            if (actual > 0 && actual != audioPort) {
                audioPort = actual;
                Discovery d = discovery;
                if (d != null) {
                    d.setAudioPort(actual);
                }
            }
            applyAllowedHosts();
            // ★ 2026-11-02：接收器重建后，把每台设备用户设过的「我听它的」补回去。
            //   原来只补发送侧（restoreGains），重开收听/保存端口后滑杆还写着 0.40，
            //   实际接收增益却回到了 1.00 —— 条子和声音不一致。
            restoreRecvGains(r);
            ensureMediaSession();
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
        releaseMediaSession();
    }

    /**
     * ★ 2026-11-02 用户：手机音量面板里分「手机音量」和「App 音量」，
     * 希望本应用能出现在「App 音量」那一栏，而不是只跟着手机媒体总音量走。
     *
     * <p>Android 的分应用音量只认"正在播放媒体的、有 MediaSession 的 App"。
     * 我们播放用的是 AudioTrack（USAGE_MEDIA），但没有会话 —— 系统音量面板里
     * 就只剩一个总的媒体音量。这里在开始收听时建一个本地播放的 MediaSession
     * （不接管按键、不做播放控制，只为让系统把我们识别成媒体 App），
     * 停止收听时立刻释放；不常驻、不伪造"正在播放"。
     */
    private void ensureMediaSession() {
        if (mediaSession != null || app == null) {
            return;
        }
        synchronized (lock) {
            if (mediaSession != null) {
                return;
            }
            try {
                AudioAttributes attrs = new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build();
                MediaSession s = new MediaSession(app, "AudioMixing");
                s.setCallback(new MediaSession.Callback() {
                    // 不接管播放/暂停：这个会话只为让系统看见"我们在放媒体"。
                });
                s.setPlaybackToLocal(attrs);
                PlaybackState state = new PlaybackState.Builder()
                        .setState(PlaybackState.STATE_PLAYING,
                                PlaybackState.PLAYBACK_POSITION_UNKNOWN, 1.0f)
                        .build();
                s.setPlaybackState(state);
                s.setActive(true);
                mediaSession = s;
            } catch (Throwable t) {
                Log.w(TAG, "创建媒体会话失败（分应用音量可能不显示）", t);
            }
        }
    }

    private void releaseMediaSession() {
        MediaSession s = mediaSession;
        mediaSession = null;
        if (s == null) {
            return;
        }
        try {
            s.setActive(false);
        } catch (Throwable ignored) {
        }
        try {
            s.release();
        } catch (Throwable ignored) {
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
            // ★ 2026-11-01 用户：「电脑端修改音量手机不同步」。
            //   根因在这里：这一层原来把**UDP 来源 IP** 当设备键，而界面 / 收听名单 /
            //   发送目标用的都是**发现层选出的 peer.host**。电脑有双网卡 / VPN / Wi-Fi 与
            //   有线并存时，统计包从哪块网卡发出和信标里报的地址可能不是同一个 ——
            //   音量就被写进了一个界面永远不读的键（手机→电脑那条按 node_id 走，所以正常）。
            //   统计包里的 `node` 才是稳定身份：先用它反查出同一个 peer.host，再往下传，
            //   两处的键从此是同一个字符串。
            String key = resolveHost(ps.node, host);
            ps.host = key;
            ps.receivedAtMs = SystemClock.elapsedRealtime();
            // ★ 心跳也是一条"它还活着"的证据：电脑关掉 App 之后信标和心跳都会停，
            //   这两条一起构成"确认走了"的判据（见 collectSendTargets 的宽限期）。
            onHeartbeat(key);
            if (!key.equals(host)) {
                onHeartbeat(host);      // 发送名单万一还按来源地址记着，也一并刷新
            }
            // ★ 2026-11-08：电脑端可以切换"静音档乘数策略"（同一个设置，两个入口）。
            handleRemoteSilentPolicy(key, ps);
            if (ps.silentPolicyOnly) {
                return;             // 纯设置包：不参与收听/增益/对端统计，处理完就走
            }
            // ★ ① 对端请求我收听它（它正在把声音送给我）→ 自动打开收听（可撤销）
            handleWantListen(key, ps);
            // ★ ① 共享音量：对端报的"它送我的那条链路"的音量 = 我这条接收链路的音量。
            //   第二个地址是**音频包真正的来源**（AudioRecv 按它记增益），两者不一致时一起设。
            handleRemoteGain(key, ps, host);
            synchronized (peerLock) {
                peerStats.remove(key);
                peerStats.put(key, ps);    // 重新插入 = 最新的排在最后，下面按顺序淘汰最老的
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

    // ────────────────── ★ ④ 心跳 / ① 自动收听 / ① 共享音量 ──────────────────

    /** 收到一个统计包 = 对端还活着：把这台设备的"最后确认活着"时间刷新。 */
    private void onHeartbeat(String host) {
        long nowMs = SystemClock.elapsedRealtime();
        for (Map.Entry<String, String> e : sendHosts.entrySet()) {
            if (host.equals(e.getValue())) {
                sendLastAliveMs.put(e.getKey(), nowMs);
            }
        }
    }

    /**
     * ★ 「智连」名单（按设备地址 host 记，2026-10-18 用户第 6 条）。
     *
     * <p>命中的设备：它点「送给它」时，本机<b>自动替它打开收听</b>。手机端<b>只</b>自动开"听"，
     * 绝不自动往外送声音 —— 那是用户点名的安全边界。默认名单是空的 = 一台都不自动。
     *
     * <p>为什么按 host 而不按节点 id：这张名单是**用户点出来**的，地址在发现列表里最直观；
     * 而统计包那条路（{@link #onPeerStats}）会先把 node 反查成同一个 peer.host 再进来，
     * 所以两处用的仍然是同一个键。
     */
    private final Set<String> smartHosts = new LinkedHashSet<String>();
    // ★ 2026-11-04（A）：电脑显式「请开始/停止接收」的请求号与回执。
    //   host → 已处理到的最大 reqId（同一个号只处理一次）；
    //   host → "id|action|reason"（拼进本机统计发回去，电脑端能看到结果）。
    private final Map<String, Long> lastListenReqId = new java.util.HashMap<String, Long>();
    private final Map<String, String> listenAckFor = new java.util.HashMap<String, String>();
    /**
     * ★ 我们替哪几台【自动打开过】收听（按 host 记，2026-10-15 用户反馈第 3 条）。
     *
     * <p>为什么必须单独记一笔：对端说"我不送了"（wantListen=false）时，只有
     * <b>我们替它自动开的</b>那一条该收回；用户自己点开的一律不碰（撤销不是惩罚，
     * 只是把当初替他打开的那一下收回去）。没有这份记录就只能猜，猜错就是动用户的设置。
     */
    private final Set<String> autoListenHosts = new LinkedHashSet<String>();

    /** 这台设备开没开「智连」（host 为空一律算没开；界面拿它给第 4 颗按钮上色）。 */
    public boolean isSmart(String host) {
        if (host == null || host.isEmpty()) {
            return false;
        }
        synchronized (lock) {
            return smartOnLocked(host);
        }
    }

    /**
     * ★ 2026-11-04（第 7 条）：智连名单**不能只按 IP** 判。
     *
     * <p>用户真机：手机上开了智连，电脑一「送声音」手机却不自动收听 —— 电脑有双网卡/
     * VPN 时，统计包从另一块网卡进来（或者信标里报的地址换过一次），`resolveHost`
     * 算出的 host 与当初点开关时的 host 不是同一个字符串，`smartHosts.contains(host)`
     * 就永远 false，代码看起来全对、功能就是不生效。
     *
     * <p>现在同一个设备只要命中任一稳定键就算开了：当前地址、节点 id、设备名。
     * 旧版本存下来的纯 IP 名单照常有效（向后兼容）。
     */
    private boolean smartOnLocked(String host) {
        if (smartHosts.contains(host)) {
            return true;
        }
        Discovery.Peer p = peerByHost(host);
        if (p == null) {
            return false;
        }
        if (smartHosts.contains(idOf(p))) {
            return true;
        }
        String name = p.name == null ? "" : p.name.trim();
        return !name.isEmpty() && smartHosts.contains(name);
    }

    /**
     * 切换这台设备的「智连」开关并落盘，返回<b>新状态</b>（界面据此立刻上色，不必等下一帧）。
     *
     * <p>不用额外做别的：名单一变，下一轮统计包（每 ~2 秒一次）走到 {@link #handleWantListen}
     * 时判据自然就变了 —— 对端还在请求的话，那一轮就会自动把收听打开；对端没在请求，
     * 开了也只是"允许"，不会凭空多出一条收听。
     */
    public boolean toggleSmart(String host) {
        if (host == null || host.isEmpty()) {
            return false;
        }
        boolean nowOn;
        synchronized (lock) {
            boolean on;
            if (smartOnLocked(host)) {
                // 关掉时把该设备的几个键一起摘掉（地址 / 节点 id / 名字），
                // 避免"换了 IP 还显示开、点一下却关不掉"。
                smartHosts.remove(host);
                Discovery.Peer p0 = peerByHost(host);
                if (p0 != null) {
                    smartHosts.remove(idOf(p0));
                    if (p0.name != null) smartHosts.remove(p0.name.trim());
                }
                on = false;
            } else {
                // ★ 2026-11-04（A 硬规则）：广播模式和智连不能组合。广播开着时
                //   只能关、不能开；UI 也会置灰，但引擎必须再挡一次（防旧界面绕过）。
                if (isBroadcasting()) {
                    Log.i(TAG, "广播开着，拒绝打开智连：" + host);
                    return false;
                }
                // 同时记节点 id / 名字：以后它换 IP 了，智连仍然跟着这台设备走。
                smartHosts.add(host);
                Discovery.Peer p1 = peerByHost(host);
                if (p1 != null) {
                    smartHosts.add(idOf(p1));
                    if (p1.name != null && !p1.name.trim().isEmpty()) {
                        smartHosts.add(p1.name.trim());
                    }
                }
                on = true;
            }
            saveSmartHosts();
            nowOn = on;
        }
        // ★ 2026-11-04（第 11 条）：刚打开智连就**立刻**看一次对端缓存的统计，
        //   对方已经在送就马上开始收 —— 不再等下一轮 2 秒统计包，更不该等二十秒。
        if (nowOn) {
            applySmartNow(host);
        }
        return nowOn;
    }

    /** 开智连的瞬间用缓存里的最后一份统计走一遍同一套判据（不伪造发送状态）。 */
    private void applySmartNow(String host) {
        if (host == null || host.isEmpty()) {
            return;
        }
        Diagnosis.PeerStats cached = null;
        synchronized (peerLock) {
            cached = peerStats.get(host);
        }
        if (cached != null && cached.sending) {
            handleWantListen(host, cached);
        }
    }

    // ── 「智连」名单的持久化（键 smart_hosts，逗号分隔；host 里不会有逗号）──

    private void saveSmartHosts() {
        if (app == null) {
            return;
        }
        try {
            StringBuilder sb = new StringBuilder();
            for (String h : smartHosts) {
                if (sb.length() > 0) {
                    sb.append(',');
                }
                sb.append(h);
            }
            app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                    .edit().putString("smart_hosts", sb.toString()).apply();
        } catch (Throwable t) {
            Log.w(TAG, "「智连」名单存不下来", t);
        }
    }

    private void loadSmartHosts() {
        if (app == null) {
            return;
        }
        try {
            String raw = app.getSharedPreferences("audiomixing", Context.MODE_PRIVATE)
                    .getString("smart_hosts", "");
            for (String h : raw.split(",")) {
                String t = h.trim();
                if (!t.isEmpty()) {
                    smartHosts.add(t);
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "「智连」名单读不回来", t);
        }
    }

    /** 对端在请求我收听：**只有这台设备开了「智连」**才自动加进收听名单。 */
    private void handleWantListen(String host, Diagnosis.PeerStats ps) {
        synchronized (lock) {
            boolean already = false;
            for (String id : listenIds) {
                if (host.equals(listenHosts.get(id))) {
                    already = true;
                    break;
                }
            }
            // ★ 2026-10-18 用户第 6 条：判据只剩「这台设备开没开『智连』」。
            //   删掉的两个旧判据（全局总开关 + "关过一次就永不再自动"的黑名单）见 LinkPolicy.listenAction。
            boolean smart = smartOnLocked(host);
            // ★ 这条收听是不是【我们替它自动开的】：只有这种才允许被"我不送了"收回去。
            boolean autoAdded = autoListenHosts.contains(host);
            // ★ 2026-11-01 用户：「智连不好用，实际状态和按键表达状态不匹配」。
            //   根因之一是 `want_listen` 由**发送方**决定发不发（旧电脑只在它自己开了
            //   「智连」时才发）。智连开在**本机**这张卡片上，就不该等对方开口：
            //   对方统计里 `sending=true` = 它确实在往我这儿发（点对点或广播都一样），
            //   本机直接收下。`want_listen` 仍然保留 —— 对方明确请求时也照样自动开。
            //   反向的停止语义不变：`sending=false` 且是我们替它开的，就收回。
            // ★ 2026-11-04（A 硬规则）：广播模式和智连不能组合 —— 广播开着时
            //   智连不再自动开收听（按钮也置灰）；用户手动点的收听不受影响。
            boolean want = !isBroadcasting() && (ps.wantListen || (smart && ps.sending));
            int action = LinkPolicy.listenAction(smart, already, want, autoAdded);
            if (action == LinkPolicy.LISTEN_ADD) {
                // 用节点 id 做键（找不到 Peer 就用地址兜底，和 listenHosts 的约定一致）
                Discovery.Peer p = peerByHost(host);
                String id = (p != null) ? idOf(p) : host;
                listenIds.add(id);
                listenHosts.put(id, host);
                listenPorts.put(id, (p != null && p.audioPort > 0) ? p.audioPort : Proto.DEFAULT_AUDIO_PORT);
                autoListenHosts.add(host);      // 记一笔：这条是我们替它开的
                // ★ 不再往界面上弹"XX 请求收听，已自动打开"这类提示（用户第 6 条点名不要）：
                //   是否需要自动听由「智连」开关说了算，开着就静默生效，不需要每次都说一句。
                Log.i(TAG, "「智连」已自动打开收听：" + ((p != null) ? p.label() : host));
                ensureReceiver();
                applyAllowedHosts();
            } else if (action == LinkPolicy.LISTEN_REMOVE) {
                // ★ 2026-10-15 用户反馈第 3 条「电脑停止送音后手机依旧收音不停止」：
                //   对端明确说"我不送了" → 收回我们替它自动打开的那一条。
                //   边界与电脑端逐字对齐：只收自动开的，用户自己点开的收听不碰。
                java.util.Iterator<String> it = listenIds.iterator();
                while (it.hasNext()) {
                    String id = it.next();
                    if (host.equals(listenHosts.get(id))) {
                        it.remove();
                        listenHosts.remove(id);
                        listenPorts.remove(id);
                    }
                }
                autoListenHosts.remove(host);
                Log.i(TAG, "对端停了送声音，已收回自动打开的收听：" + nameOrHost(host));
                applyAllowedHosts();
                stopReceiverIfIdle();
            }
            // ★ 2026-10-18 用户第 6 条：LISTEN_REVOKED 这个分支已随旧黑名单一起删掉。
            //   旧的「关过一次就永不再自动」会永久卡死自动收听（用户第 1 条真机 bug），
            //   现在"不自动"只是「智连」没开，对端再请求也会正常按开关重新判断。
            // ★ 2026-11-04（A）：显式「请开始/停止接收」必须处理并回 ack —— 不再单向。
            handleExplicitListenRequest(host, ps);
        }
    }

    /**
     * ★ 2026-11-04（A）：处理电脑发来的显式 listen_req，并写下回执。
     *
     * <p>策略和「智连」一致：**接收方的智连说了算**。没开智连 → 明确回 ignored，
     * 电脑能显示"对方没开智连"，而不是傻等；开了智连 → start 自动收听、stop 只收回
     * 我们替它自动开的那条（用户手动点开的收听绝不碰），并回 started/already/stopped/kept。
     */
    private void handleExplicitListenRequest(String host, Diagnosis.PeerStats ps) {
        synchronized (lock) {
            String req = ps.listenReq == null ? "" : ps.listenReq.trim();
            long reqId = ps.listenReqId;
            if (req.isEmpty() || reqId <= 0) {
                return;
            }
            Long before = lastListenReqId.get(host);
            if (before != null && reqId <= before) {
                return;                               // 同一个号只处理一次
            }
            lastListenReqId.put(host, reqId);
            boolean smart = smartOnLocked(host);
            boolean listening = false;
            for (String id : listenIds) {
                if (host.equals(listenHosts.get(id))) {
                    listening = true;
                    break;
                }
            }
            boolean autoAdded = autoListenHosts.contains(host);
            String action;
            String reason = "";
            if (!smart) {
                action = "ignored";
                reason = "本机未开智连";
            } else if (isBroadcasting() && "start".equals(req)) {
                action = "ignored";
                reason = "广播模式不能和智连组合";
            } else if ("start".equals(req)) {
                if (listening) {
                    action = "already";
                } else {
                    // 直接按"自动收听"打开（键=节点 id，地址=host），和 handleWantListen 同一套。
                    Discovery.Peer p = peerByHost(host);
                    String id = (p != null) ? idOf(p) : host;
                    listenIds.add(id);
                    listenHosts.put(id, host);
                    listenPorts.put(id, (p != null && p.audioPort > 0)
                            ? p.audioPort : Proto.DEFAULT_AUDIO_PORT);
                    autoListenHosts.add(host);
                    ensureReceiver();
                    applyAllowedHosts();
                    action = "started";
                }
            } else if ("stop".equals(req)) {
                if (autoAdded) {
                    java.util.Iterator<String> it = listenIds.iterator();
                    while (it.hasNext()) {
                        String id = it.next();
                        if (host.equals(listenHosts.get(id))) {
                            it.remove();
                            listenHosts.remove(id);
                            listenPorts.remove(id);
                        }
                    }
                    autoListenHosts.remove(host);
                    applyAllowedHosts();
                    stopReceiverIfIdle();
                    action = "stopped";
                } else {
                    action = "kept";
                    reason = "用户手动收听，不自动收回";
                }
            } else {
                action = "ignored";
                reason = "未知请求";
            }
            listenAckFor.put(host, reqId + "|" + action + "|" + reason);
            Log.i(TAG, "电脑请求 " + req + "（#" + reqId + "）→ " + action
                    + (reason.isEmpty() ? "" : "（" + reason + "）"));
        }
    }

    private String nameOrHost(String host) {
        Discovery.Peer p = peerByHost(host);
        return (p != null) ? p.label() : host;
    }

    private Discovery.Peer peerByHost(String host) {
        for (Discovery.Peer p : peers()) {
            if (p != null && host != null && host.equals(p.host)) {
                return p;
            }
        }
        return null;
    }

    /** 按节点 id 找这台设备（统计包里的 {@code node} → 发现列表里的同一台）。 */
    private Discovery.Peer peerByNode(String nodeId) {
        if (nodeId == null || nodeId.isEmpty()) {
            return null;
        }
        for (Discovery.Peer p : peers()) {
            if (p != null && nodeId.equals(p.nodeId)) {
                return p;
            }
        }
        return null;
    }

    /**
     * 把"一条消息里说的那台设备"落到发现层的地址上（2026-11-01）。
     *
     * <p>优先按 {@code node} 反查 {@code peer.host}；查不到（信标还没到 / 老对端没报 node）
     * 才退回消息来源地址。界面、收听名单、发送目标全都按 {@code peer.host} 记，
     * 所以统一到这一个键，音量条才会两端同步。
     */
    private String resolveHost(String nodeId, String fallback) {
        if (nodeId != null && !nodeId.isEmpty()) {
            Discovery.Peer p = peerByNode(nodeId);
            if (p != null && p.host != null && !p.host.isEmpty()) {
                return p.host;
            }
        }
        return fallback;
    }

    // ── 共享音量（一个增益，两端同一个值；谁后改以谁为准）──

    /** host → 我"送"它的增益（= 我→它那条链路的共享音量）。 */
    private final Map<String, Float> linkSendGain = new LinkedHashMap<String, Float>();
    /** host → 我"听"它的增益（= 它→我那条链路的共享音量）。 */
    private final Map<String, Float> linkRecvGain = new LinkedHashMap<String, Float>();
    /** 我最后一次修改"我送它的"的墙钟毫秒（发给对端用于判新旧）。 */
    private final Map<String, Long> linkSendGainTs = new LinkedHashMap<String, Long>();
    /** 我最后一次修改"我听它的"的墙钟毫秒（对端会采纳它当它的"送"）。 */
    private final Map<String, Long> linkRecvGainTs = new LinkedHashMap<String, Long>();
    /** 我最后一次修改的时刻（elapsedRealtime）。★ 两个方向必须分开，见字段注释。 */
    private final Map<String, Long> linkSendGainChangedAtMs = new LinkedHashMap<String, Long>();
    private final Map<String, Long> linkRecvGainChangedAtMs = new LinkedHashMap<String, Long>();

    /**
     * ★ 2026-11-08（用户方案）：电脑端下发的"静音档乘数策略"。
     *
     * <p>同一个号只处理一次（{@code silent_vol_policy_req} 单调递增）；电脑重启后号会从 1
     * 重新计，这时手机本地可能已经有一个别的选择 —— 用"策略不同也接受"兜底，保证电脑上的
     * 设置最终一定生效。旧电脑不发这两个键 ⇒ 这里什么都不做（手机本地选择保持不变）。
     */
    private void handleRemoteSilentPolicy(String host, Diagnosis.PeerStats ps) {
        String raw = ps.silentPolicy == null ? "" : ps.silentPolicy.trim().toLowerCase(java.util.Locale.US);
        if (!SendVolume.POLICY_INTERNAL.equals(raw) && !SendVolume.POLICY_LAST.equals(raw)) {
            return;              // 空 / 不认识：老电脑或别的包，保持本地选择
        }
        Long seenObj = silentPolicyReqSeen.get(host);
        long seen = seenObj == null ? 0L : seenObj;
        if (ps.silentPolicyReq > 0 && ps.silentPolicyReq <= seen) {
            return;              // 这个号已经处理过
        }
        silentPolicyReqSeen.put(host, ps.silentPolicyReq);
        if (!raw.equals(silentVolPolicy)) {
            setSilentVolPolicy(raw, true);
            Log.i(TAG, "电脑 " + host + " 切换静音档乘数策略 → " + raw
                    + "（" + SendVolume.policyText(raw) + "）");
        }
    }

    /**
     * 对端报来的增益：采纳"它送我的那条链路"的音量，作为我的"我听它的"。
     *
     * @param host      发现层的 peer.host（界面和发送目标用的键）
     * @param audioHost 这条统计包的来源地址（AudioRecv 按它记接收增益；通常和 host 相同，
     *                  电脑多网卡时会不同 —— 那时两边都要设，声音和滑杆才对得上）
     */
    private void handleRemoteGain(String host, Diagnosis.PeerStats ps, String audioHost) {
        if (!ps.hasGain()) {
            return;      // 老版本没有这个字段：什么都不做（混跑兼容）
        }
        float mine = linkRecvGain.containsKey(host) ? linkRecvGain.get(host) : 1f;
        Long localTsObj = linkRecvGainTs.get(host);
        long localTs = localTsObj == null ? 0L : localTsObj;
        long sinceRecvLocal = LinkPolicy.NEVER;
        Long recvAt = linkRecvGainChangedAtMs.get(host);
        if (recvAt != null) {
            sinceRecvLocal = SystemClock.elapsedRealtime() - recvAt;
        }
        long sinceSendLocal = LinkPolicy.NEVER;
        Long sendAt = linkSendGainChangedAtMs.get(host);
        if (sendAt != null) {
            sinceSendLocal = SystemClock.elapsedRealtime() - sendAt;
        }
        if (LinkPolicy.adoptRemoteGain(ps.gainTs, localTs, sinceRecvLocal, ps.gain, mine)) {
            applyRecvGain(host, ps.gain);
            if (audioHost != null && !audioHost.isEmpty() && !audioHost.equals(host)) {
                // 音频包来自另一个地址（多网卡/VPN）：接收端也要按那个键设一次，
                // 否则滑杆显示对了、真正播出来的声音还是旧音量。
                AudioRecv r = receiver;
                if (r != null) {
                    try {
                        r.setHostGain(audioHost, LinkPolicy.clampGain(ps.gain));
                    } catch (Throwable t) {
                        Log.w(TAG, "设置接收增益失败（音频来源地址）", t);
                    }
                }
            }
            linkRecvGainTs.put(host, ps.gainTs);
            linkRecvGainChangedAtMs.put(host, SystemClock.elapsedRealtime());
            Log.i(TAG, "采纳对端报的共享音量：" + host + " → 我听它的 " + ps.gain);
        }
        // ★ 对端报的"我听它的"（gain_rx）= 它→我这条链路的另一个视角 = 我的"我送它的"
        if (ps.gainRx != Diagnosis.GAIN_UNSET) {
            float mySend = linkSendGain.containsKey(host) ? linkSendGain.get(host) : 1f;
            Long mySendTsObj = linkSendGainTs.get(host);
            long mySendTs = mySendTsObj == null ? 0L : mySendTsObj;
            if (LinkPolicy.adoptRemoteGain(ps.gainRxTs, mySendTs, sinceSendLocal, ps.gainRx, mySend)) {
                applySendGain(host, ps.gainRx);
                linkSendGainTs.put(host, ps.gainRxTs);
                linkSendGainChangedAtMs.put(host, SystemClock.elapsedRealtime());
                Log.i(TAG, "采纳对端报的共享音量：" + host + " → 我送它的 " + ps.gainRx);
            }
        }
    }

    /** 真正把"我听它的"落到接收端（+记住值）。 */
    private void applyRecvGain(String host, float g) {
        float v = LinkPolicy.clampGain(g);
        linkRecvGain.put(host, v);
        AudioRecv r = receiver;
        if (r != null) {
            try {
                r.setHostGain(host, v);
            } catch (Throwable t) {
                Log.w(TAG, "设置接收增益失败", t);
            }
        }
    }

    /** 真正把"我送它的"落到发送端（+记住值）。 */
    private void applySendGain(String host, float g) {
        float v = LinkPolicy.clampGain(g);
        linkSendGain.put(host, v);
        AudioSend s = sender;
        if (s != null) {
            try {
                s.setTargetGain(host, v);
            } catch (Throwable t) {
                Log.w(TAG, "设置发送增益失败", t);
            }
        }
    }

    /** 发送端新建/目标重新上线时，把用户（或对端同步过来）的增益补回去。 */
    private void restoreGains(AudioSend s) {
        if (s == null) {
            return;
        }
        for (Map.Entry<String, Float> e : linkSendGain.entrySet()) {
            try {
                s.setTargetGain(e.getKey(), e.getValue());
            } catch (Throwable ignored) {
                // 补不上就保持默认 1.00
            }
        }
    }

    /** 接收端重建时，把每台设备的「我听它的」补回去（和 restoreGains 对称）。 */
    private void restoreRecvGains(AudioRecv r) {
        if (r == null) {
            return;
        }
        try {
            for (Map.Entry<String, Float> e : new LinkedHashMap<String, Float>(linkRecvGain).entrySet()) {
                r.setHostGain(e.getKey(), e.getValue());
            }
        } catch (Throwable t) {
            Log.w(TAG, "恢复接收增益失败（继续用默认 1.00）", t);
        }
    }

    /** 某一台自己报告的状态；没有就是 null（界面据此显示"等它回话…"）。 */
    public Diagnosis.PeerStats peerStats(String host) {
        if (host == null) {
            return null;
        }
        synchronized (peerLock) {
            Diagnosis.PeerStats direct = peerStats.get(host);
            if (direct != null) {
                return direct;
            }
        }
        // ★ 2026-11-01：统计包可能比信标先到 —— 那时只能按来源 IP 存，而界面拿的是
        //   信标里的地址。用这台设备的 node id 再对一次，免得"明明在回话却显示等它回话"。
        //   （先取 node 再进 peerLock：peerByHost 会去读发现表，别把两把锁套在一起。）
        Discovery.Peer p = peerByHost(host);
        String node = (p != null) ? p.nodeId : "";
        if (node == null || node.isEmpty()) {
            return null;
        }
        synchronized (peerLock) {
            for (Diagnosis.PeerStats ps : peerStats.values()) {
                if (node.equals(ps.node)) {
                    return ps;
                }
            }
        }
        return null;
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

    /** 本应用还在已安装状态吗？（APK 文件消失是比 getPackageInfo 更硬的证据）。 */
    private boolean packageStillInstalled() {
        try {
            String apk = app.getPackageCodePath();
            if (apk != null && !apk.isEmpty() && !new File(apk).exists()) {
                return false;
            }
        } catch (Throwable ignored) {
            // 取不到路径就不能据此判负，继续看包名
        }
        try {
            app.getPackageManager().getPackageInfo(app.getPackageName(), 0);
            return true;
        } catch (android.content.pm.PackageManager.NameNotFoundException e) {
            return false;
        } catch (Throwable t) {
            return true;
        }
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
        pokeStats();
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
     * 本机刚动了音量 / 收发状态 → <b>立刻</b>补发一轮统计，不等那 2 秒一次的周期。
     *
     * <p>用户真机反馈（2026-10-16）：「两边的音量调同步太慢了，要反应很久」。
     * 统计包是唯一携带共享增益（{@code gain / gain_ts / gain_rx}）的消息，而它原来是固定的
     * 2 秒一轮 —— 拖完滑杆，对端最多要等 2 秒才看见新值（再叠上对端自己的处理周期）。
     * 这里只叫醒发送循环，包还是由它那一条线程发出去，界面线程不会为了一个包去等网络。
     */
    public void pokeStats() {
        statsPoke = true;
        synchronized (statsWake) {
            statsWake.notifyAll();
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
     * <p><b>★ 发到哪条路（2026-10-15）</b>：对端信标里有 {@code ctl:1} → 走<b>发现层</b>
     * （{@link Discovery#sendControl}，那层的 socket 一直活着）；老版本对端 → 照旧发
     * <b>对方的音频端口</b>（信标里公布的 {@code audio_port}，由对方的 AudioRecv 收）。
     * 分流的理由见 {@link Discovery} 的类注释：控制消息挂在按需启停的音频接收器上，
     * 对端一停就永远收不到 —— 那就是"电脑送声音开着、手机的收听却不自动打开"的成因。
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
                // ★ 2026-11-02（第二道保险）：卸载时前台服务可能已被系统停掉，
                //   但进程还活着；服务里的 installWatch 就一起没了。引擎自己的常驻线程
                //   每 2 秒再看一眼"包/ apk 还在不在"，不在了立刻 stop() 释放投屏授权。
                if (app != null && !packageStillInstalled()) {
                    Log.w(TAG, "引擎检测到本应用已被卸载，停止并释放投屏授权");
                    stop();
                    return;
                }
                try {
                    sendStatsRound(s);
                } catch (Throwable t) {
                    // 对端还没起来/刚走，send 报错很正常，绝不能因此退出循环。
                    statsLastError = "发统计失败：" + t;
                    Log.w(TAG, statsLastError);
                }
                // 拆成 100ms 小步睡：stop() 时能立刻醒来，不用等满 2 秒；
                // 谁刚动了音量（pokeStats）也能立刻醒来，不必把新值压到下一轮 2 秒。
                for (int i = 0; i < STATS_INTERVAL_MS / 100 && statsRunning && !statsPoke; i++) {
                    synchronized (statsWake) {
                        try {
                            statsWake.wait(100);
                        } catch (InterruptedException ignored) {
                            break;
                        }
                    }
                }
                statsPoke = false;
            }
        } finally {
            try {
                s.close();
            } catch (Throwable ignored) {
            }
        }
    }

    /** 发一轮统计：给每台"有关系的设备"各发一个（内容按方向区分）。 */
    /** ★ 2026-11-04（A）：本机对这台设备的 listen_req 回执，拼成 JSON 片段；没有则空串。 */
    private String listenAckJson(String host) {
        String v;
        synchronized (lock) {
            v = listenAckFor.get(host);
        }
        if (v == null || v.isEmpty()) {
            return "";
        }
        String[] parts = v.split("\\|", 3);
        if (parts.length < 2) {
            return "";
        }
        String reason = parts.length >= 3 ? parts[2] : "";
        // 回执内容是我们自己写死的短词/短句（没有引号、反斜杠），可以安全内联。
        return ",\"listen_ack_id\":" + parts[0]
                + ",\"listen_ack_action\":\"" + parts[1] + "\""
                + ",\"listen_ack_reason\":\"" + reason + "\"";
    }

    private void sendStatsRound(DatagramSocket s) {
        // ★ 每轮先看一眼"对端是不是已经彻底没消息了"（关程序/离开局域网时没人来说
        //   want_listen=false，只能靠这条兜底）—— 必须在下面那个 targets.isEmpty() 提前返回
        //   之前跑，否则"什么都没在发生"的那一轮正好把该收的收听留下了。
        revokeStaleAutoListens();
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
            // ★ ④ 只是"请电脑去连我的蓝牙"的那些，也必须进名单。
            //   2026-10-15 用户真机：手机点了「走蓝牙（电脑去连）」，电脑毫无反应 ——
            //   因为这种时候我既没在收听、也没在发送，targets 是空的，下面那句
            //   `if (targets.isEmpty() && !channelListen) return;` 直接把整轮跳过，
            //   want_bt **一个包都发不出去**（等于死代码）。
            //   记在 btWantPorts 里的就是"我还要请它连蓝牙"的设备，直到用户改选 WiFi。
            targets.putAll(btWantPorts);
            // ★ ④ 「我刚停了」也要送达：停掉的那一台再报 30 秒（want_listen 已经是 false），
            //   否则电脑那边永远收不到"你停了"，它替自动打开的那条收听就一直开着。
            long nowMs = SystemClock.elapsedRealtime();
            for (String host : lastStatsHosts) {
                if (!targets.containsKey(host) && !statsGracePorts.containsKey(host)) {
                    statsGracePorts.put(host, portForHost(host));
                    statsGraceUntil.put(host, nowMs + STATS_STOP_GRACE_MS);
                }
            }
            java.util.Iterator<Map.Entry<String, Integer>> it =
                    statsGracePorts.entrySet().iterator();
            while (it.hasNext()) {
                Map.Entry<String, Integer> e = it.next();
                Long until = statsGraceUntil.get(e.getKey());
                if (until == null || until <= nowMs) {
                    it.remove();
                    statsGraceUntil.remove(e.getKey());
                    continue;
                }
                targets.put(e.getKey(), e.getValue());
            }
            lastStatsHosts.clear();
            lastStatsHosts.addAll(targets.keySet());
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
            // ★ 2026-10-16 用户澄清的语义：广播是**一条独立线路** ——
            //   "我在广播"这件事不再被写成"我在针对性送给你、请你自动打开收听"。
            //   所以 want_listen 只对**用户单独勾过的那几台**为真（点对点路由）；
            //   广播覆盖的那些照旧如实报 sending/sent（对端能看到有声音在来），
            //   但绝不替对端把收听打开 —— 要不要听由**对端自己那台**的「听」决定
            //   （用户原话：「广播是设备只有开关本机"听""送"的权利的」）。
            Discovery.Peer dp = peerByHost(host);
            boolean targeted = dp != null && sendIds.contains(idOf(dp));
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
            Discovery dForId = discovery;
            String myNodeId = (dForId != null) ? dForId.nodeId() : "";
            byte[] rawPayload = Diagnosis.json(myNodeId, nodeName(),
                    iSendToIt ? mySent : 0, iSendToIt ? myBytes : 0, iSendToIt,
                    recv, lost, buf, streams, host,
                    // ★ ① 请求对端自动收听：**只有用户单独点了「送声音」的那几台**才请求
                    //   （广播这条独立线路不请求 —— 见上面 targeted 的说明）
                    iSendToIt && targeted,
                    // ★ ① 我→它这条链路的共享音量（我这边的"我送它的"）
                    gainOf(linkSendGain, host), tsOf(linkSendGainTs, host),
                    // ★ ① 它→我这条链路的共享音量（我这边的"我听它的"）
                    gainOf(linkRecvGain, host), tsOf(linkRecvGainTs, host),
                    // ★ ④ 我在这台上选了「走蓝牙」→ 请**电脑**去连我的蓝牙（幂等）
                    btWantTo(host),
                    // ★ 2026-11-03 Q3：我的媒体/App 音量 → 电脑端按这条路由衰减
                    mediaVolumeRatio(),
                    // ★ 2026-11-04（Q3）：显式声明档位（0/1/2）与"这是广播线路吗"。
                    //   电脑端准入只认这两个键，不再从 want_listen 推断。
                    sendMode(), broadcastOn,
                    // ★ 2026-11-08：音量乘数由档位决定 —— 上报档位/策略，电脑端据此恒不叠乘。
                    volumeSource(), volumeSource(), silentVolPolicy());
            // ★ 2026-11-04（A）：把对"请开始/停止接收"的回执拼进统计 JSON。
            // ★ 2026-11-06（第 2 条取证）：同时把采集层的增益乘数/前后电平拼进去。
            //   两者都只含数字和固定 ASCII，不含引号，老电脑忽略多余键。
            byte[] payload = rawPayload;
            AudioSend diagSender = sender;
            String extra = listenAckJson(host) + (diagSender != null ? diagSender.diagJson() : "");
            if (!extra.isEmpty()) {
                String text = new String(rawPayload, StandardCharsets.UTF_8);
                payload = (text.substring(0, text.length() - 1) + extra + "}")
                        .getBytes(StandardCharsets.UTF_8);
            }
            // ★ 2026-10-15：对端信标里有 ctl:1 → 这条统计/请求改发**发现端口**。
            //   那一层每 2 秒一发一收、一直活着，所以对端（手机）即使没在收听、接收器停着，
            //   也照样收得到"我在把声音送给你，请自动打开收听" —— 这就是 ⑤ 的正解。
            //   老版本对端没有这个键：落到下面照旧发音频端口，一个字都不少（新旧混跑不打哑）。
            //   （dp 就是上面为了算 targeted 已经查过的那一台，别查第二遍。）
            if (dp != null && dp.ctl && discovery != null) {
                int dport = (dp.discPort > 0) ? dp.discPort : Proto.DEFAULT_DISCOVERY_PORT;
                try {
                    if (discovery.sendControl(new String(payload, StandardCharsets.UTF_8),
                            host, dport)) {
                        statsSent++;
                        continue;
                    }
                } catch (Throwable e) {
                    // 发不出去（对端刚走 / Wi-Fi 抖动）：绝不能静默丢掉 —— 落到音频端口再发一份。
                    statsLastError = "控制消息发不到 " + host + ":" + dport + "：" + e;
                    Log.w(TAG, statsLastError);
                }
            }
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

    /**
     * ★ 把"对端已经彻底没消息"的那些【自动打开的收听】收回来（见 AUTO_LISTEN_STALE_MS）。
     *
     * <p>跑在统计线程上（每 2 秒一轮）—— 必须是常驻线程：界面关掉时也要收，
     * 否则用户回到界面看到的就是一条挂着的收听。
     */
    private void revokeStaleAutoListens() {
        java.util.List<String> hosts;
        synchronized (lock) {
            if (autoListenHosts.isEmpty()) {
                return;
            }
            hosts = new ArrayList<String>(autoListenHosts);
        }
        // 先只读快照对端统计（peerLock），再去动收听名单（lock）—— 两把锁不嵌套，避免出现
        // "收包线程等界面线程、界面线程等收包线程"那种谁都不动的局面。
        long now = SystemClock.elapsedRealtime();
        java.util.List<String> stale = new ArrayList<String>();
        // ★ 2026-11-04（A3）：双判据，和电脑端逐字对齐 ——「没声音」不挂断，
        //   音频包静默 **且** 对端统计（发现层心跳）也消失，两个都超过 4 秒才收回。
        //   静音音频仍然有包 ⇒ 音频 age 很小 ⇒ 永远不会被误挂。
        java.util.Map<String, Long> audioAge = new java.util.HashMap<String, Long>();
        AudioRecv r = receiver;
        AudioRecv.Stats rst = (r == null) ? null : r.stats();
        if (rst != null && rst.streams != null) {
            for (AudioRecv.StreamStats ss : rst.streams) {
                if (ss == null || ss.host == null) {
                    continue;
                }
                Long old = audioAge.get(ss.host);
                if (old == null || ss.ageMs < old) {
                    audioAge.put(ss.host, ss.ageMs);
                }
            }
        }
        synchronized (peerLock) {
            for (String host : hosts) {
                Diagnosis.PeerStats ps = peerStats.get(host);
                long age = (ps == null) ? Long.MAX_VALUE : (now - ps.receivedAtMs);
                Long a = audioAge.get(host);
                long audio = (a == null) ? Long.MAX_VALUE : a;
                if (age > AUTO_LISTEN_HANG_MS && audio > AUTO_LISTEN_HANG_MS) {
                    stale.add(host);
                    Log.i(TAG, "自动收听收回判据：心跳 " + age + "ms / 音频静默 " + audio + "ms");
                }
            }
        }
        if (stale.isEmpty()) {
            return;
        }
        synchronized (lock) {
            for (String host : stale) {
                if (!autoListenHosts.remove(host)) {
                    continue;
                }
                java.util.Iterator<String> ids = listenIds.iterator();
                while (ids.hasNext()) {
                    String id = ids.next();
                    if (host.equals(listenHosts.get(id))) {
                        ids.remove();
                        listenHosts.remove(id);
                        listenPorts.remove(id);
                    }
                }
                Log.i(TAG, "对端已经没消息了，已收回自动打开的收听：" + nameOrHost(host));
            }
            applyAllowedHosts();
            stopReceiverIfIdle();
        }
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

    /**
     * 这一路的「信号质量」0~100；没有可测的链路返回 {@code -1}（界面据此写"不可测"）。
     *
     * <p>★ 2026-10-16 用户（图6）：「电脑手机现在都只有"送""收"两个条，现在定义为音量
     * 显示条。并添加一个信号质量条。」—— 分值只用<b>实测计数器</b>算：丢包比例越小分越高，
     * 缓冲越深（说明这一路一直在补迟到/重排的包）再扣一点。
     *
     * <p>为什么没有依据时返回 -1 而不是 0：0 分看起来像"链路很差"，而"根本没测到"和
     * "链路很差"是两件事。界面按 -1 写"不可测"，绝不编一个分数。
     */
    public int peerQualityFor(String host) {
        if (host == null || host.isEmpty()) {
            return -1;
        }
        AudioRecv.Stats st = recvStats();
        // ★ 2026-11-02（2.6）：接收器没跑（没点「收听」/「听广播」）时 recvStats() 返回 null，
        //   旧代码在这里直接 return -1 → 卡片永远写「还没收到它的心跳」，
        //   哪怕对方的信标每 2 秒都在、设备在线。这就是用户说的"反馈不真实"。
        //   现在：没有音频统计就往下走心跳那条兜底（信标新鲜 = 链路正常）。
        if (st != null && st.streams != null) {
            for (AudioRecv.StreamStats ss : st.streams) {
                if (!host.equals(ss.host)) {
                    continue;
                }
                // 算式放在 LinkPolicy 里：那样 PolicyProbe 那道零桩门禁能直接把它测了，
                // 不用起 Android 运行时（这条判据是纯计算，没有理由藏在 Engine 里）。
                return LinkPolicy.qualityScore(ss.received, ss.lost, ss.bufferedMs);
            }
        }
        // ★ 2026-10-16 用户（图1）：「虽然二者没有收发声音，但是设备间信号应当存在」——
        //   没有音频流的时候**不能写"没有依据"**：链路本身一直在（互相看得见信标）。
        //   手机端不回 PING/PONG，所以这条路是唯一的兜底 —— 用心跳新鲜度给分。
        Discovery.Peer peer = peerByHost(host);
        if (peer != null && peer.online()) {
            return LinkPolicy.heartbeatScore(peer.ageMs());
        }
        return -1;
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

    /**
     * ★ 2026-11-03 Q3 / v2.28（用户第 2 条）：当前系统媒体音量归一化到 0..1。
     *
     * <p>★ v2.28 为什么要改成"手机自己乘"：v2.27 假设 {@code AudioPlaybackCapture}
     * 采到的 PCM 已经带上了系统/App 音量，于是让电脑端不要乘（{@code vol_in_capture}）。
     * 真机结果是**音量旋钮完全不影响耳机**——说明采集点在系统音量之前（录屏语义：
     * 录屏不该受扬声器音量影响）。App 自己的音量（{@code AudioTrack.setVolume}）在混音前
     * 就写进轨道了，所以它在 PCM 里；缺的是**系统媒体音量**这一级。
     * 现在由 {@link AudioSend} 在发送前一帧乘上本函数的值，电脑端仍然不乘（单级衰减）。
     *
     * <p>只读，仍然不调用 {@code setStreamVolume} —— 手机自己的音量面板/按键行为不变。
     *
     * <p>用 {@code getStreamMinVolume} 做区间归一化（有些 ROM 的 min 不是 0）。
     * 读不到（极少数 ROM 抛异常）时返回 1.0：**绝不能因为读音量失败而把对端变成静音**。
     */
    public float mediaVolumeRatio() {
        return mediaVolumeRatioFor(app);
    }

    /**
     * 静态版：给发送线程（AudioSend）直接读。
     *
     * <p>第 2 档（"手机不出声、只送声音"）会把系统媒体音量压到 0；那时如果还读当前值，
     * 送出去的 PCM 也会被乘成 0 —— 用户听到的就是"开了第 2 档反而没声音"。所以
     * 静音标记存在时，返回**用户最后一次设定的那一档**；这个值由
     * {@link #applySilentVolume} 在每次归零前按"用户/App 刚调过的值"实时更新，
     * 不再固定为进入静音那一刻的旧值（v2.29 的 0.11 一直不动就是它造成的）。
     */
    public static float mediaVolumeRatioFor(Context ctx) {
        if (ctx == null) {
            return 1f;
        }
        try {
            android.media.AudioManager am =
                    (android.media.AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
            if (am == null) {
                return 1f;
            }
            int cur = am.getStreamVolume(android.media.AudioManager.STREAM_MUSIC);
            android.content.SharedPreferences sp =
                    ctx.getSharedPreferences("audiomixing", Context.MODE_PRIVATE);
            if (sp.getBoolean(PREF_SILENT_ACTIVE, false)) {
                int orig = sp.getInt(PREF_SILENT_ORIG, -1);
                if (orig >= 0) {
                    cur = orig;
                }
            }
            int max = am.getStreamMaxVolume(android.media.AudioManager.STREAM_MUSIC);
            int min = 0;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                try {
                    min = am.getStreamMinVolume(android.media.AudioManager.STREAM_MUSIC);
                } catch (Throwable ignored) {
                    min = 0;
                }
            }
            int span = max - min;
            if (span <= 1) {
                // 真的只有"最小/最大"两档的 ROM：如实报 0/1，让这一级仍然能静音/满音量。
                return cur <= min ? 0f : 1f;
            }
            return Math.max(0f, Math.min(1f, (cur - min) / (float) span));
        } catch (Throwable t) {
            Log.w(TAG, "读媒体音量失败（按 1.0 处理）：" + t);
            return 1f;
        }
    }

    /**
     * ★ 监控档的实时数字面板（一屏读完）。
     *
     * <p>为什么放在 Engine 而不是界面里拼：这四段用的全是本类已有的查询接口
     * （发现层、收端、发端、音量），界面只负责把它画出来 —— 这样"同一份数据三种呈现"
     * 才成立，不会出现"监控档的数字和诊断报告对不上"这种事。
     */
    public String monitorText() {
        StringBuilder sb = new StringBuilder(1024);

        // ── 发现 ──
        sb.append("── 发现 ──\n");
        Discovery d = discovery;
        if (d == null || !d.isRunning()) {
            sb.append("发现没在跑 ✗\n");
        } else {
            sb.append("信标 收 ").append(d.beaconsReceived())
                    .append(" · 发 ").append(d.beaconsSent())
                    .append(" · 自己绕回 ").append(d.beaconsSelfReceived()).append('\n');
            sb.append("组播 ").append(d.multicastIfaces().isEmpty() ? "没加进去 ✗" : d.multicastIfaces())
                    .append(" · 组播锁 ").append(d.multicastLockHeld() ? "✓" : "✗")
                    .append(" · 端口 ").append(d.boundPort()).append('\n');
            sb.append("最近 ").append(d.lastBeaconText()).append('\n');
        }

        // ── 接收 ──
        sb.append("── 接收 ──\n");
        AudioRecv.Stats st = recvStats();
        if (st == null || !receiverRunning()) {
            sb.append("接收器没跑（没开听广播、也没点过收听）\n");
        } else {
            sb.append("收 ").append(st.received).append(" · 丢 ").append(st.lost)
                    .append(" · 迟到 ").append(st.late).append(" · 重复 ").append(st.duplicates)
                    .append(" · 坏包 ").append(st.badPackets).append('\n');
            sb.append("坏包分类 magic ").append(st.badMagic).append(" / 版本 ").append(st.badVersion)
                    .append(" / 太短 ").append(st.badShort).append(" / 编码 ").append(st.badCodec)
                    .append(" / 帧长 ").append(st.badFrames).append('\n');
            if (st.samplesMismatch > 0) {
                // ★ 不是坏包：是对方 samples 字段语义不同（电脑端历史版本填累计值）。
                //   单独一行写出来，免得又被当成"包全坏了"。
                sb.append("samples 字段异常 ").append(st.samplesMismatch)
                        .append(" 次（已按负载长度接收）\n");
            }
            sb.append("缓冲 ").append(st.bufferedMs).append("ms（目标 ").append(st.targetMs)
                    .append("）· 流 ").append(st.streamCount)
                    .append(" · 混音 ").append(pctText(st.peak)).append('\n');
            if (st.streams != null) {
                for (AudioRecv.StreamStats ss : st.streams) {
                    sb.append("  · ").append(ss.host).append(" 收 ").append(ss.received)
                            .append(" · 丢 ").append(ss.lost)
                            .append(" · 缓冲 ").append(ss.bufferedMs).append("ms")
                            .append(" · 电平 ").append(pctText(ss.peak))
                            .append(" · ").append(ss.ageMs).append("ms 前\n");
                }
            }
            if (st.dupRoutePackets > 0) {
                sb.append("  重复流已抑制 ").append(st.dupRoutePackets).append(" 包\n");
            }
        }

        // ── 发送 ──
        sb.append("── 发送 ──\n");
        AudioSend snd = sender;
        if (snd == null || !snd.isRunning()) {
            sb.append("发送器没跑（没点过送声音、也没开发广播）\n");
        } else {
            sb.append("目标 ").append(snd.targetCount()).append(" 台")
                    .append(broadcastOn ? "（📢 广播中）" : "")
                    .append(" · 合计 ").append(snd.packetsSent()).append(" 包 / ")
                    .append(Diagnosis.bytesText(snd.bytesSent()))
                    .append(" · 失败 ").append(snd.sendErrors()).append('\n');
            for (SendFanout.TargetStats ts : snd.targetStats()) {
                sb.append("  · ").append(ts.host).append(':').append(ts.port)
                        .append(" 发 ").append(ts.packetsSent).append(" 包")
                        .append(" · ").append(pctText(ts.peak))
                        .append(" · 音量 ").append(String.format(java.util.Locale.US, "%.2f", ts.gain));
                if (ts.sendErrors > 0) {
                    sb.append(" · 失败 ").append(ts.sendErrors);
                }
                if (ts.lastOkAgeMs < 0) {
                    sb.append(" · 还没成功 ✗");
                } else if (ts.lastOkAgeMs > 2000) {
                    sb.append(" · ").append(ts.lastOkAgeMs / 1000).append(" 秒前 ✗");
                } else {
                    sb.append(" · 正在发 ✓");
                }
                sb.append('\n');
            }
            int n = Math.max(1, snd.targetCount());
            sb.append("带宽 每台约 0.19 MB/s（1.5 Mbit/s）· 合计约 ")
                    .append(String.format(java.util.Locale.US, "%.2f",
                            n * (Proto.SAMPLE_RATE * Proto.CHANNELS * 2) / 1048576.0))
                    .append(" MB/s\n");
        }

        // ── 音量（三层）──
        sb.append("── 音量 ──\n");
        sb.append("我们这一层 ").append(volumeText()).append('\n');
        sb.append("系统 ").append(systemVolumeText()).append('\n');
        return sb.toString();
    }

    /** 0~1 变成 "34%"（监控面板里数字要短）。 */
    private static String pctText(float v) {
        if (Float.isNaN(v) || v < 0f) {
            v = 0f;
        }
        return Math.round(Math.min(1f, v) * 100f) + "%";
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
