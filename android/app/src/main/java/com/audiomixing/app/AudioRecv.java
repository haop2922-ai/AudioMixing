package com.audiomixing.app;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioTrack;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 接收 + 抖动缓冲 + 混音播放。
 *
 * <p>一个 UDP socket 绑 {@link Proto#DEFAULT_AUDIO_PORT}，收到的每个包按
 * <b>来源 + ssrc</b> 分流到各自的抖动缓冲；播放线程每 10 毫秒从每个流取一帧，
 * 相加（削顶保护）后写进一个 {@link AudioTrack}。多台手机同时"送声音"就是靠这一步混在一起的。
 *
 * <p><b>为什么要有抖动缓冲</b>：Wi-Fi 上包不会匀速到达，早到晚到几十毫秒是常态。
 * 每收到一包就立刻播，晚到的包只能扔掉（咔哒声）；先攒 {@value #DEFAULT_TARGET_MS} 毫秒再匀速播，
 * 抖动就被吸收掉了。代价是固定多这么点延迟 —— 局域网内这个交换很划算。
 *
 * <p><b>丢包为什么用静音补</b>：播放时钟必须匀速往前走。等丢的那一包，
 * 整条流就会卡一下、后面的包全变成"迟到"；直接补一段静音，听感上只是一个极短的凹陷，
 * 而且是可预期的。包真丢了就补静音，包晚到了就按序号放进缓冲（还没播到就还来得及）。
 *
 * <p><b>为什么不做 PING/PONG 时钟对齐</b>：设计文档里的 PING/PONG 是为了算两台设备的时钟偏差。
 * 但接收端现在是"按到达顺序 + 预缓冲"播放，局域网上时钟偏差远小于 100 毫秒的缓冲目标，
 * 拿它去做播放时刻判断反而会引入新的抖；两端的时钟漂移由缓冲深度自适应吸收。
 * 协议字段 {@code ts_ns} 照收，用来判断"这包在路上走了多久"。
 *
 * <p><b>不用 Opus</b>：手机端只有 PCM16（见设计文档第三节），收到别的 codec 直接丢并计数，
 * 这样整包 APK 不需要一行 NDK。
 *
 * <p><b>线程</b>：一个收包线程（网络）+ 一个播放线程（音频 + 混音）。
 * 界面线程只读 {@link #stats()} / {@link #peak()}，绝不碰 socket 和 AudioTrack。
 */
public final class AudioRecv {

    private static final String TAG = "AM-AudioRecv";

    /** 一帧的字节数（480 帧 × 2 声道 × 2 字节）。 */
    public static final int FRAME_BYTES = Proto.FRAME_SAMPLES * Proto.CHANNELS * 2;
    /** 抖动缓冲默认目标：120 毫秒（文档要求 100~200 之间）。 */
    public static final int DEFAULT_TARGET_MS = 120;
    public static final int MIN_TARGET_MS = 100;
    public static final int MAX_TARGET_MS = 200;
    /** 环形缓冲槽数，必须是 2 的幂（取模只花一次与运算）：32 槽 = 320 毫秒。 */
    private static final int SLOTS = 32;
    private static final int MASK = SLOTS - 1;
    /** 缓冲积到"目标 + 80ms"就主动丢一帧把延迟拉回来（两端音频时钟有 ppm 级漂移，不校正会越积越多）。 */
    private static final int DROP_ABOVE_EXTRA_MS = 80;
    /** 这么久没有新包就把这条流摘掉（对端关掉了/走了）。 */
    private static final int STREAM_IDLE_MS = 3000;
    /** 收包缓冲区：一包 28 + 1920 = 1948 字节，4096 留足余量。 */
    private static final int RECV_BUF = 4096;
    private static final float PEAK_DECAY = 0.82f;

    /**
     * 对端发来的统计包（{@code ptype=7 T_STATS}）回调：两台设备"自己交流"的入口。
     *
     * <p><b>回调跑在收包线程上</b>：实现里只许做"解析 + 存起来"这种微秒级的事
     * （见 {@link Engine} 的实现），绝不能在这里发网络请求或者碰界面。
     * 就算实现里抛了异常也影响不到收包 —— 调用处套了 try/catch。
     */
    public interface StatsListener {
        void onStats(String host, byte[] payload);
    }

    /** 谁想知道对端统计就设进来；为 null 表示不需要（默认）。 */
    private volatile StatsListener statsListener;

    public void setStatsListener(StatsListener l) {
        statsListener = l;
    }

    /** 收包统计，给界面"详情"用。 */
    public static final class Stats {
        public long received;
        public long lost;
        public long late;
        public long duplicates;
        public long badPackets;
        public long filtered;
        public long controls;
        public long recoveries;
        public long trackErrors;
        public int streamCount;
        /** 当前缓冲深度（毫秒，取最大的那条流）。 */
        public int bufferedMs;
        public int targetMs;
        /** 混音输出峰值 0~1（不受本机音量滑杆影响）。 */
        public float peak;
        public List<StreamStats> streams = Collections.emptyList();

        // ── ★ 坏包按原因分类（2026-10-12 真机反馈）──
        // 原来只有一个"坏包 3220"的总数、报告里只解释了其中 49 个，剩下 3171 个只能猜。
        // "有数字没线索"是排查里最难受的状态，所以每一类都单独计数 + 留一个现场样本。
        /** magic 不对：根本不是我们的协议（别人的广播、扫描器、别的 App 的流量）。 */
        public long badMagic;
        /** 版本号不是 1（将来协议升级时能一眼看出来）。 */
        public long badVersion;
        /** 连 28 字节包头都不够（被截断）。 */
        public long badShort;
        /** 是我们的包，但编码不是 PCM16（手机只有 PCM16，Opus/浮点都解不了）。 */
        public long badCodec;
        /** 是我们的 PCM16 包，但帧长不是 480 帧这一档。 */
        public long badFrames;
        /** 各类最近一个坏包的现场（人话，直接抄进诊断报告）。 */
        public String lastMagicSample = "";
        public String lastVersionSample = "";
        public String lastShortSample = "";
        public String lastCodecSample = "";
        public String lastFramesSample = "";
        /** 最近见过的编码值（"codec=3(Opus)"）：判断"对面到底用什么编码发的"最直接的证据。 */
        public String lastCodecSeen = "";
        /**
         * ★ 坏包来源前几名（"192.168.0.109 3171 个"）。
         *
         * <p>这一条把"是某一台设备在按我们不懂的方式发"和"整个网段的噪声都在往这个端口灌"
         * 彻底分开 —— 后者通常说明这个端口号被别的软件占了/被扫了。
         */
        public String badBySource = "";
        /**
         * ★ 坏包长度分布前几名（"1948 字节 3171 个"）。
         *
         * <p>长度是最便宜也最有效的"指纹"：<b>1948 字节</b>正好是"28 字节包头 + 480 帧 PCM16"
         * 的长度 —— 如果坏包全是这个长度，那它几乎肯定是一条<b>音频流</b>（只是头不对/编码不对）；
         * 如果长度是 60/200/1200 这种零碎的，那就是网络里别人的杂流量。
         * 有了这一条，"3171 个坏包到底是什么"就不必再猜。
         */
        public String badByLen = "";
        /** ★ 被抑制掉的"同一台设备第 2 条流"的包数（去重生效的证据）。 */
        public long dupRoutePackets;
        /** 其中"内容指纹确认是同一份声音"的包数（>0 = 确认是同一音源从两条路进来）。 */
        public long dupRouteConfirmed;
        /** 现场描述：谁的哪两条 ssrc 在打架。 */
        public String dupRouteText = "";
        /**
         * ★ 对面报的 samples 和负载长度对不上的次数（包照收，不算坏包）。
         * 电脑端历史版本填的是累计帧偏移，就靠这一条看出来。
         */
        public long samplesMismatch;
        /** 这件事最近一次的现场（人话）。 */
        public String lastSamplesNote = "";

        @Override
        public String toString() {
            return "收 " + received + " 丢 " + lost + " 迟到 " + late + " 坏包 " + badPackets
                    + " 流 " + streamCount + " 缓冲 " + bufferedMs + "ms";
        }
    }

    /** 单条发送方的状态。 */
    public static final class StreamStats {
        public int ssrc;
        public String host = "";
        public long received;
        public long lost;
        public int bufferedMs;
        public float peak;
        public long ageMs;
    }

    // ══════════════════ ★ 同一台设备只让一条流出声（去重） ══════════════════
    //
    // 为什么必须去重（2026-10-12 真机反馈，用户原话「路由模式和广播模式有些许混淆了」）：
    // 电脑同时开着「广播」和「点对点路由」时，**同一份声音会从两条路各来一份**，而两条路的
    // ssrc 不一样（电脑端 ssrc = crc32(node_id|route_id)），在接收端就是两条独立的流 ——
    // 叠加播放的结果是音量翻倍 + 两路的时序差打架（梳状滤波），听感就是"糊、忽大忽小"。
    //
    // 音频包里没有路由 id（电脑端协议注释写得很清楚：An audio packet carries no route id），
    // 所以接收端没法问"你是哪条路由"。能站得住的判据只有一条：**一台设备同一时刻只播一条流**。
    // 它同时也能兜住"同一个 ssrc 被两个 socket 收到"这种更少见的情况。
    //
    // 代价（必须说清楚，不藏着）：如果某一台设备<b>故意</b>同时给你送两路不同的声音
    //   （比如电脑的系统声音 + 电脑的麦克风），这里只会播其中一路 —— 另一路会被抑制，
    //   并在诊断报告里写明"抑制了谁"。宁可少一路，也不要叠着放把音量翻倍。
    /** 每台设备当前允许出声的那条流（ssrc）。只由收包线程读写。 */
    private final Map<String, Integer> hostActiveSsrc = new HashMap<>();
    /** 被抑制掉的重复流包数 / 其中"内容指纹确认是同一份声音"的包数。 */
    private volatile long dupRoutePackets;
    private volatile long dupRouteConfirmed;
    /** 现场描述（诊断报告直接用）。 */
    private volatile String dupRouteText = "";
    /** 指纹历史：只用来"确认"两条流是不是同一份声音，不参与"是否抑制"的判定。 */
    private static final int FP_HISTORY = 64;
    private static final int FP_WINDOW_MS = 120;
    private final int[] fpValue = new int[FP_HISTORY];
    private final int[] fpSsrc = new int[FP_HISTORY];
    private final String[] fpHost = new String[FP_HISTORY];
    private final long[] fpAt = new long[FP_HISTORY];
    private int fpIdx;

    private final Context appCtx;
    /** 端口不是 final：界面层可以"先建对象、启动时再定端口"（要先探测空闲端口就得这么用）。 */
    private volatile int port;
    private final Object lifeLock = new Object();
    private final Object allowLock = new Object();
    /** 只由收包线程添加；播放线程只做移除，所以用写时复制的列表在热路径上零加锁。 */
    private final CopyOnWriteArrayList<Stream> streams = new CopyOnWriteArrayList<>();
    /** 空 = 全收（还没勾任何人时"打开就能听"）；一旦用户动过开关，就只播明确打开的那些。 */
    private final Map<String, Boolean> allowed = new HashMap<>();

    private volatile boolean running;
    private Thread rxThread;
    private Thread mixThread;
    private volatile DatagramSocket socket;
    private volatile AudioTrack track;
    private volatile boolean trackPlaying;
    private WifiManager.WifiLock wifiLock;

    private volatile int targetMs = DEFAULT_TARGET_MS;
    private volatile float gain = 1f;
    private volatile float peak;
    private volatile String lastError = "";

    // received,lost,late,dup,bad,filtered,controls,recoveries,
    // 8..12 = ★ 坏包分类：magic / version / 太短 / 编码不是 PCM16 / 帧长不对
    private final long[] counters = new long[16];
    private volatile long trackErrors;
    private int ssrcSeen;
    /**
     * ★ 坏包现场样本的节流表：<b>每一类各自节流</b>。
     *
     * <p>为什么不能用一个全局节流：真机上往往是好几种坏包混在一起（这次就是）。
     * 全局节流会让"先到的那一类"把后面每一类的样本都吞掉 —— 结果报告里只有一类的现场，
     * 剩下的还是只能猜。分开之后每一类都能立刻留下自己的第一条现场。
     */
    private final long[] lastSampleMs = new long[6];
    // ★ 坏包现场样本（收包线程写、界面线程读，所以是 volatile）
    private volatile String lastMagicSample = "";
    private volatile String lastVersionSample = "";
    private volatile String lastShortSample = "";
    private volatile String lastCodecSample = "";
    private volatile String lastFramesSample = "";
    /** 最近见过的编码（"codec=3（Opus…）"）：判断对面用什么编码发的最直接的证据。 */
    private volatile String lastCodecSeen = "";
    /**
     * ★ 对面报的 samples 和负载长度对不上的次数 + 最近一次的现场。
     *
     * <p>这不是错误计数（包照收照播），而是"对方那个字段语义不同"的证据 ——
     * 电脑端历史版本把 samples 填成累计帧偏移，就是靠这两条看出来的。
     */
    private volatile long samplesMismatch;
    private volatile String lastSamplesNote = "";
    /**
     * ★ 坏包按来源 IP 计数（只由收包线程写，读的时候拿 counters 锁）。
     * 上限 8 个来源：局域网里不会有更多，防的是被人乱刷撑爆内存。
     */
    private final Map<String, Long> badBySource = new java.util.LinkedHashMap<>();
    /** ★ 坏包按"整包长度"计数（同一个上限策略：只留最近 8 种长度）。 */
    private final Map<Integer, Long> badByLen = new java.util.LinkedHashMap<>();

    /** 用默认端口 47831。 */
    public AudioRecv() {
        this(null, Proto.DEFAULT_AUDIO_PORT);
    }

    public AudioRecv(int port) {
        this(null, port);
    }

    /**
     * 带 Context 的版本会拿 {@code WifiLock}：不收包的时候 Wi-Fi 会进省电模式，
     * 包被攒到 DTIM 周期才一起送上来，抖动缓冲会被瞬间冲垮（听感就是周期性的卡顿）。
     */
    public AudioRecv(Context context, int port) {
        this.appCtx = context == null ? null : context.getApplicationContext();
        this.port = port > 0 ? port : Proto.DEFAULT_AUDIO_PORT;
    }

    // ------------------------------------------------------------------ 对外

    /** 开始接收并播放（用构造时定的端口）；重复调用无副作用。 */
    public void start() {
        start(port);
    }

    /**
     * 开始接收并播放，并在这里定下端口。
     *
     * <p>建 socket / 建 AudioTrack 都在工作线程里完成，所以这个方法立刻返回 ——
     * 主线程不会被 bind 或者音频设备初始化卡住。
     */
    public void start(int port) {
        synchronized (lifeLock) {
            if (running) {
                return;
            }
            if (port > 0) {
                this.port = port;
            }
            running = true;
            rxThread = new Thread(this::rxLoop, "am-audio-rx");
            mixThread = new Thread(this::mixLoop, "am-audio-mix");
            rxThread.setDaemon(true);
            mixThread.setDaemon(true);
            rxThread.start();
            mixThread.start();
        }
    }

    public void stop() {
        Thread r;
        Thread m;
        synchronized (lifeLock) {
            if (!running && rxThread == null && mixThread == null) {
                return;
            }
            running = false;
            r = rxThread;
            m = mixThread;
            rxThread = null;
            mixThread = null;
        }
        DatagramSocket s = socket;
        if (s != null) {
            s.close();   // 关掉 socket 才能把阻塞在 receive 的线程叫醒
        }
        AudioTrack t = track;
        if (t != null) {
            try {
                t.pause();
                t.flush();
            } catch (Throwable ignored) {
            }
        }
        if (r != null && r != Thread.currentThread()) {
            try {
                r.join(1000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        if (m != null && m != Thread.currentThread()) {
            try {
                m.join(1500);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        streams.clear();
        releaseTrack();
        releaseWifiLock();
        peak = 0f;
    }

    public boolean isRunning() {
        return running;
    }

    /**
     * 只收听某一台（界面上的「收听」开关）。没勾过任何人时是全收，
     * 这样"打开 App 就出声"；一旦勾过，就只播明确打开的那些，语义清楚不会两头猜。
     */
    public void allow(String host, boolean on) {
        if (host == null || host.isEmpty()) {
            return;
        }
        synchronized (allowLock) {
            allowed.put(host, on);
        }
    }

    /** 清空名单，回到"全收"。 */
    public void clearAllowed() {
        synchronized (allowLock) {
            allowed.clear();
        }
    }

    private boolean isAllowed(String host) {
        synchronized (allowLock) {
            if (allowed.isEmpty()) {
                return true;
            }
            Boolean v = allowed.get(host);
            return v != null && v;
        }
    }

    /**
     * 「我们这一层」的播放增益 0.0~1.0：用 {@code AudioTrack.setVolume}，不动样本（省一次 960 次乘法）。
     *
     * <p>注意它<b>只影响我们这条 AudioTrack</b>，绝不碰系统媒体音量 —— 用户明确要求过：
     * 三层是"系统音量 × 播放器自己的音量 × 我们这一层"，我们只负责最后一层。
     */
    public void setGain(float g) {
        gain = Math.max(0f, Math.min(1f, g));
        AudioTrack t = track;
        if (t != null) {
            try {
                t.setVolume(gain);
            } catch (Throwable ignored) {
            }
        }
    }

    /** {@link #setGain(float)} 的同义词：界面层管这个叫"音量"，名字顺着它更好接。 */
    public void setVolume(float v) {
        setGain(v);
    }

    public float gain() {
        return gain;
    }

    /** 抖动缓冲目标毫秒数（100~200）。 */
    public int targetBufferMs() {
        return targetMs;
    }

    public void setTargetBufferMs(int ms) {
        targetMs = Math.max(MIN_TARGET_MS, Math.min(MAX_TARGET_MS, ms));
    }

    public int port() {
        return port;
    }

    /** 混音输出峰值 0~1，界面画总电平用。 */
    public float peak() {
        return peak;
    }

    /** 当前在收的流数量（用来显示"已连接几台"）。 */
    public int streamCount() {
        return streams.size();
    }

    public String lastError() {
        return lastError;
    }

    /** 收包/丢包/缓冲深度的快照。界面按 10Hz 调也没问题（只在堆上拼一个小对象）。 */
    public Stats stats() {
        Stats st = new Stats();
        synchronized (counters) {
            st.received = counters[0];
            st.lost = counters[1];
            st.late = counters[2];
            st.duplicates = counters[3];
            st.badPackets = counters[4];
            st.filtered = counters[5];
            st.controls = counters[6];
            st.recoveries = counters[7];
            st.badShort = counters[8];
            st.badMagic = counters[9];
            st.badVersion = counters[10];
            st.badCodec = counters[11];
            st.badFrames = counters[12];
            // ★ 坏包来源前 3 名：把"某一台在乱发"和"整个网段的噪声"分开。
            if (!badBySource.isEmpty()) {
                java.util.List<Map.Entry<String, Long>> list = new ArrayList<>(badBySource.entrySet());
                list.sort((a, b) -> Long.compare(b.getValue(), a.getValue()));
                StringBuilder bs = new StringBuilder(64);
                for (int i = 0; i < list.size() && i < 3; i++) {
                    if (bs.length() > 0) {
                        bs.append(" · ");
                    }
                    bs.append(list.get(i).getKey()).append(' ').append(list.get(i).getValue()).append(" 个");
                }
                st.badBySource = bs.toString();
            }
            // ★ 长度分布：1948 字节（28 头 + 480 帧 PCM16）= 一条音频流的形状；
            //   零碎长度 = 网络里别人的杂流量。
            if (!badByLen.isEmpty()) {
                java.util.List<Map.Entry<Integer, Long>> list = new ArrayList<>(badByLen.entrySet());
                list.sort((a, b) -> Long.compare(b.getValue(), a.getValue()));
                StringBuilder bs = new StringBuilder(64);
                for (int i = 0; i < list.size() && i < 3; i++) {
                    if (bs.length() > 0) {
                        bs.append(" · ");
                    }
                    bs.append(list.get(i).getKey()).append(" 字节 ").append(list.get(i).getValue()).append(" 个");
                }
                st.badByLen = bs.toString();
            }
        }
        st.lastMagicSample = lastMagicSample;
        st.lastVersionSample = lastVersionSample;
        st.lastShortSample = lastShortSample;
        st.lastCodecSample = lastCodecSample;
        st.lastFramesSample = lastFramesSample;
        st.lastCodecSeen = lastCodecSeen;
        st.dupRoutePackets = dupRoutePackets;
        st.dupRouteConfirmed = dupRouteConfirmed;
        st.dupRouteText = dupRouteText;
        st.samplesMismatch = samplesMismatch;
        st.lastSamplesNote = lastSamplesNote;
        st.trackErrors = trackErrors;
        st.targetMs = targetMs;
        st.peak = peak;
        long now = SystemClock.elapsedRealtime();
        List<StreamStats> list = new ArrayList<>(streams.size());
        int maxBuf = 0;
        long streamLost = 0;
        for (Stream s : streams) {
            StreamStats ss = new StreamStats();
            ss.ssrc = s.ssrc;
            ss.host = s.host;
            ss.received = s.received;
            ss.lost = s.lost;
            ss.bufferedMs = s.depthMs();
            ss.peak = s.peak;
            ss.ageMs = now - s.lastPacketMs;
            if (ss.bufferedMs > maxBuf) {
                maxBuf = ss.bufferedMs;
            }
            streamLost += s.lost;
            list.add(ss);
        }
        st.streamCount = list.size();
        st.bufferedMs = maxBuf;
        st.streams = list;
        // 顶部这行原来只报 counters[1]，而真正的"播放时跳过了多少帧"记在每条流里
        // （丢包/补静音/压延迟都算），于是出现过"上面写丢 0、下面写丢 5908"的怪相。
        // 真机排查时这个数字是最关键的线索，必须报真实值。
        if (streamLost > st.lost) {
            st.lost = streamLost;
        }
        return st;
    }

    /**
     * 找一个能用的音频端口（47831 被别的进程占了就换）。
     *
     * <p><b>必须在工作线程调用</b>：它会真的 bind 一次 socket，而安卓禁止主线程碰网络。
     * 只是探测，探完立刻关掉 —— 正式收包由 {@link #start()} 在后台重新绑。
     */
    public static int pickFreePort(int preferred) {
        int start = preferred > 0 ? preferred : Proto.DEFAULT_AUDIO_PORT;
        for (int p = start; p < start + 20 && p < 65535; p++) {
            try (DatagramSocket probe = new DatagramSocket(null)) {
                probe.setReuseAddress(false);
                probe.bind(new InetSocketAddress(p));
                return p;
            } catch (IOException e) {
                // 被占了，试下一个
            }
        }
        return start;
    }

    // -------------------------------------------------------------- 收包线程

    private void rxLoop() {
        acquireWifiLock();
        DatagramSocket s;
        try {
            s = new DatagramSocket(null);      // 先建不绑，才能先设 reuse/缓冲再绑
            s.setReuseAddress(true);
            try {
                s.setReceiveBufferSize(256 * 1024);   // 一次 GC/调度停顿可能积压几十个包，内核缓冲要大
            } catch (IOException ignored) {
            }
            s.bind(new InetSocketAddress(port));
            s.setSoTimeout(500);
        } catch (IOException e) {
            note("音频端口 " + port + " 打不开（被占用？）：" + e);
            running = false;
            return;
        }
        socket = s;
        if (running) {
            Log.i(TAG, "开始接收：0.0.0.0:" + port + " 抖动目标 " + targetMs + "ms");
        }
        byte[] buf = new byte[RECV_BUF];
        DatagramPacket p = new DatagramPacket(buf, buf.length);
        try {
            while (running) {
                try {
                    p.setLength(buf.length);
                    s.receive(p);
                    onPacket(buf, p.getLength(), p.getAddress());
                } catch (SocketTimeoutException te) {
                    // 正常：用超时让循环有机会看到 running 变 false
                } catch (IOException e) {
                    if (!running) {
                        break;
                    }
                    // 网卡抖动时 UDP socket 也可能报错，歇一下继续，绝不能退出线程
                    sleepQuiet(50);
                } catch (Throwable t) {
                    note("收包异常：" + t);
                    sleepQuiet(50);
                }
            }
        } finally {
            try {
                s.close();
            } catch (Throwable ignored) {
            }
            if (socket == s) {
                socket = null;
            }
            Log.i(TAG, "接收结束：" + stats());
        }
    }

    private void onPacket(byte[] buf, int len, InetAddress from) {
        // 来源地址先解析出来：下面每个分支（包括坏包分类）都要用它，只算一次。
        String host = normalizeHost(from);
        // ★ 坏包原因分类（2026-10-12 真机反馈）：只有一个"坏包 3220"的总数查不出任何东西，
        //   所以在丢掉之前先分清"为什么丢"，每一类各留一个现场样本。
        //   注意：这里只是只读地看一眼包头，不改动 Proto（那个类和电脑端逐字节对过）。
        if (len < Proto.HEADER_SIZE) {
            bumpBad(8, host, len);
            sampleBad(1, buf, len, from, "长度 " + len + " 字节，连 28 字节包头都不够");
            return;
        }
        int magic = (buf[0] & 0xFF) | ((buf[1] & 0xFF) << 8);
        if (magic != Proto.MAGIC) {
            bumpBad(9, host, len);
            sampleBad(2, buf, len, from, "magic=0x" + Integer.toHexString(magic)
                    + "（我们的是 0x" + Integer.toHexString(Proto.MAGIC) + "）");
            return;
        }
        int ver = buf[2] & 0xFF;
        if (ver != Proto.VERSION) {
            bumpBad(10, host, len);
            sampleBad(3, buf, len, from, "version=" + ver + "（我们的是 " + Proto.VERSION + "）");
            return;
        }
        Proto.Pkt pkt = Proto.decode(buf, 0, len);
        if (pkt == null) {
            // magic 和版本都对、长度也够，decode 不该返回 null；真出现了也不能掩盖。
            bumpBad(0, host, len);
            sampleBad(0, buf, len, from, "包头能认出来但解码失败");
            return;
        }
        if (pkt.type != Proto.T_AUDIO) {
            bump(6);   // PING/PONG/BYE 之类的控制包，先计数
            // ★ 对端统计（T_STATS=7）：两台设备"自己交流"的唯一入口。对方每 2 秒说一次
            //   "我发了多少、是不是正在发、我收了你多少"，界面据此直接显示是哪一端的问题。
            //
            //   必须放在下面 isAllowed(host) 白名单检查【之前】：统计是双向的 ——
            //   我可能在给它送声音、但并没有勾「收听」它，这种时候同样要收得到它的回话。
            //   （音频包仍然严格走白名单，这里只是控制包的分发。）
            if (pkt.type == Proto.T_STATS) {
                dispatchStats(host, pkt.payload);
            }
            return;
        }
        int plen0 = pkt.payload == null ? 0 : pkt.payload.length;
        // 记下"对面到底用什么编码发的"：这一条比任何猜测都直接（电脑端广播/路由用的是
        // config.codec，默认 pcm_s16，但用户可能改成 opus 或 pcm_f32）。
        if (pkt.codec != Proto.CODEC_PCM_S16) {
            bumpBad(11, host, len);
            lastCodecSeen = "codec=" + pkt.codec + "（" + codecName(pkt.codec) + "）";
            sampleBad(4, buf, len, from, "编码 " + lastCodecSeen + "，负载 " + plen0
                    + " 字节，samples=" + pkt.samples);
            return;
        }
        lastCodecSeen = "codec=2（PCM 16位）";
        if (!isAllowed(host)) {
            bump(5);   // 用户没勾「收听」它（也没开「听广播」）
            return;
        }
        int plen = plen0;
        // ★★ 准入判据 = 【负载长度】，不再看 samples ★★
        //
        // 为什么（2026-10-14 跨端字段语义错误，用户报告里的真实数字）：
        //   电脑端 audiomixing/net/rtpaudio.py 把 samples 填成了"累计帧偏移"
        //   （4,924,800 = 480 × 10260），而 Python 接收端根本不读这个字段 ——
        //   所以电脑↔电脑一直没症状；手机端以前严格校验它，于是那些包全部被拒，
        //   表现就是"电脑在放声音、手机一个包都不收"。
        //
        // 新判据（只看负载；PCM16 立体声一包 1920 字节 = 480 帧 = 10ms）：
        //   · plen == 1920 → 收下（标准包）
        //   · plen <  1920 → 收下，尾巴补零（短读早就有这个能力，几毫秒静音听不出来）
        //   · plen >  1920 → 拒绝（这才是真的"一包里塞了不止 480 帧"，我们不做重采样）
        // samples 只作参考：和负载对不上就记一笔、在诊断报告里写人话，绝不因此拒收。
        if (plen > FRAME_BYTES) {
            bumpBad(12, host, len);
            sampleBad(5, buf, len, from, "负载 " + plen + " 字节 > 一包 " + FRAME_BYTES
                    + " 字节（samples=" + pkt.samples + "）");
            return;
        }
        String smpNote = samplesNote(pkt.samples, plen);
        if (smpNote.length() > 0) {
            samplesMismatch++;
            lastSamplesNote = smpNote;
        }
        // ★ 同一台设备只让一条流出声（见上面那一大段说明）
        Integer busy = acceptStreamFor(host, pkt.ssrc);
        if (busy != null) {
            dupRoutePackets++;
            if (fingerprintSeen(host, pkt.ssrc, pkt.payload, plen)) {
                dupRouteConfirmed++;
            }
            dupRouteText = host + "：ssrc " + Integer.toHexString(busy) + " 正在放，"
                    + "ssrc " + Integer.toHexString(pkt.ssrc) + " 已抑制（同一台设备的第 2 条流，不叠加）";
            return;                      // 不建流、不混音 —— 只播一条
        }
        rememberFingerprint(host, pkt.ssrc, pkt.payload, plen);
        // received 的口径 = 真正收下并会播出去的包；被抑制掉的重复流不算在里面
        // （它们单独记在 dupRoutePackets，报告里分开写，免得"收包很多却没声音"这种假象）。
        bump(0);
        Stream stream = streamFor(host, pkt.ssrc);
        stream.store(pkt.seq, pkt.payload, plen, (pkt.flags & Proto.FLAG_SILENCE) != 0);
    }

    /**
     * 这台设备现在允许哪条流出声。
     *
     * @return null = 放行（这台还没有流，或者就是它自己在发）；否则返回<b>正在放</b>的那条流的 ssrc
     *         （调用方据此抑制这一包）。
     */
    private Integer acceptStreamFor(String host, int ssrc) {
        Integer cur = hostActiveSsrc.get(host);
        if (cur == null) {
            if (hostActiveSsrc.size() < 32) {      // 上限：防被人乱刷撑爆
                hostActiveSsrc.put(host, ssrc);
            }
            return null;
        }
        if (cur == ssrc) {
            return null;
        }
        // 已经有一条在放：它还在发吗？走了/断了就把位置让给新的这条。
        Stream act = findStream(host, cur);
        if (act == null || SystemClock.elapsedRealtime() - act.lastPacketMs > STREAM_IDLE_MS) {
            hostActiveSsrc.put(host, ssrc);
            return null;
        }
        return cur;
    }

    private Stream findStream(String host, int ssrc) {
        for (Stream s : streams) {
            if (s.ssrc == ssrc && host.equals(s.host)) {
                return s;
            }
        }
        return null;
    }

    /** 某台设备现在有没有在出声（界面用：显示"实际生效状态"）。 */
    public boolean receivingFrom(String host) {
        if (host == null || host.isEmpty()) {
            return false;
        }
        long now = SystemClock.elapsedRealtime();
        for (Stream s : streams) {
            if (host.equals(s.host) && now - s.lastPacketMs <= 1000) {
                return true;
            }
        }
        return false;
    }

    /**
     * 一小段内容指纹；<b>0 = 静音/太短，不参与比对</b>。
     *
     * <p>为什么静音要排除：静音包的字节几乎一模一样，全都会被判成"同一份声音"，
     * 那这个指标就永远是"确认"，一点信息量都没有。
     */
    private static int fingerprintOf(byte[] buf, int len) {
        if (buf == null || len < 16) {
            return 0;
        }
        int h = 0;
        int peak = 0;
        // 每 24 个样本取一个：够区分内容，又几乎不吃 CPU（一包 40 次循环，100 包/秒）
        for (int i = 0; i + 1 < len; i += 48) {
            int v = (short) ((buf[i] & 0xFF) | ((buf[i + 1] & 0xFF) << 8));
            int a = v < 0 ? -v : v;
            if (a > peak) {
                peak = a;
            }
            h = h * 31 + v;
        }
        return peak < 64 ? 0 : h;
    }

    /** 这个包的内容，是不是"同一台设备另一条流最近发过的同一份声音"。 */
    private boolean fingerprintSeen(String host, int ssrc, byte[] payload, int len) {
        int fp = fingerprintOf(payload, len);
        if (fp == 0) {
            return false;
        }
        long now = SystemClock.elapsedRealtime();
        for (int i = 0; i < FP_HISTORY; i++) {
            if (fpValue[i] == fp && fpSsrc[i] != ssrc && host.equals(fpHost[i])
                    && now - fpAt[i] <= FP_WINDOW_MS) {
                return true;
            }
        }
        return false;
    }

    private void rememberFingerprint(String host, int ssrc, byte[] payload, int len) {
        int fp = fingerprintOf(payload, len);
        if (fp == 0) {
            return;
        }
        fpValue[fpIdx] = fp;
        fpSsrc[fpIdx] = ssrc;
        fpHost[fpIdx] = host;
        fpAt[fpIdx] = SystemClock.elapsedRealtime();
        fpIdx = (fpIdx + 1) % FP_HISTORY;
    }

    /**
     * 描述"对面报的 samples 和负载长度对不上"这件事（人话）；对得上就返回空串。
     *
     * <p>为什么值得单独写一句话：电脑端历史版本把 samples 填成<b>累计帧偏移</b>
     * （用户报告里 4,924,800 = 480 × 10260）。这在真机上看起来像"包全坏了"，
     * 其实只是对方一个字段的语义不同 —— 报告里必须把这件事说清楚，
     * 否则下一个人又会去查"是不是丢包/是不是编码不对"。
     */
    private static String samplesNote(int samples, int plen) {
        int expect = plen / 4;                       // PCM16 立体声：4 字节一帧
        if (samples == expect) {
            return "";
        }
        if (plen == FRAME_BYTES && samples > Proto.FRAME_SAMPLES) {
            return "对面把 samples 填成了累计值（" + samples + "），不影响播放 —— 已按负载长度接收（"
                    + Proto.FRAME_SAMPLES + " 帧/" + FRAME_BYTES + " 字节）";
        }
        return "对面报的 samples=" + samples + " 和负载 " + plen
                + " 字节对不上（按负载算应是 " + expect + " 帧），已按负载长度接收";
    }

    /** 编码的人话名字（和电脑端 proto.CODEC_NAMES 一致，报告里两边对得上）。 */
    private static String codecName(int c) {
        switch (c) {
            case Proto.CODEC_PCM_F32:
                return "PCM 浮点32，手机解不了";
            case Proto.CODEC_PCM_S16:
                return "PCM 16位";
            case Proto.CODEC_OPUS:
                return "Opus，手机解不了";
            default:
                return "未知编码";
        }
    }

    /**
     * 记一个坏包现场。{@code kind}：0=其它 1=太短 2=magic 3=版本 4=编码 5=帧长。
     *
     * <p>为什么要带"开头 16 个字节的十六进制"：3220 个坏包到底是"别人的流量"还是
     * "我们自己的包但参数不对"，看一眼开头就能定性 —— 这是从一个数字变成一条线索的关键。
     * （我们的包头开头一定是 {@code 4c 57 01}；是纯文本的话还能直接认出协议名，比如
     * SSDP 的 "M-SEARCH"、DNS、mDNS 之类。）
     * 另外带来源 IP 和总长度：如果都来自同一个地址，那基本就是某台设备在按我们不懂的方式发。
     */
    private void sampleBad(int kind, byte[] buf, int len, InetAddress from, String why) {
        long now = SystemClock.elapsedRealtime();
        int slot = (kind >= 0 && kind < lastSampleMs.length) ? kind : 0;
        if (now - lastSampleMs[slot] < 500) {
            return;                 // 节流：同一类坏包成灾时不必每个都拼字符串
        }
        lastSampleMs[slot] = now;
        StringBuilder sb = new StringBuilder(128);
        sb.append(why).append("；共 ").append(len).append(" 字节，来自 ")
                .append(normalizeHost(from)).append("，开头 ");
        int n = Math.min(16, len);
        for (int i = 0; i < n; i++) {
            int v = buf[i] & 0xFF;
            if (v < 16) {
                sb.append('0');
            }
            sb.append(Integer.toHexString(v)).append(' ');
        }
        String s = sb.toString();
        switch (kind) {
            case 1:
                lastShortSample = s;
                break;
            case 2:
                lastMagicSample = s;
                break;
            case 3:
                lastVersionSample = s;
                break;
            case 4:
                lastCodecSample = s;
                break;
            case 5:
                lastFramesSample = s;
                break;
            default:
                if (lastMagicSample.length() == 0) {
                    lastMagicSample = s;
                }
        }
    }

    /**
     * 把统计包交给监听器。
     *
     * <p><b>为什么整段都套 try/catch</b>：这段跑在收包线程上，而监听器是 {@link Engine} 的代码
     * （里面要解析 JSON）。真机上 Wi-Fi 里什么怪包都有，解析失败一次就可能让"收听"整条链路停摆 ——
     * 外层 rxLoop 虽然也有兜底，但那里 catch 之后会 sleep 50ms，抖动缓冲会被冲垮。
     * 所以宁可在这里丢掉一份统计，也绝不让收包线程受一点影响。
     */
    private void dispatchStats(String host, byte[] payload) {
        StatsListener l = statsListener;
        if (l == null || payload == null || payload.length == 0) {
            return;
        }
        try {
            l.onStats(host, payload);
        } catch (Throwable t) {
            note("对端统计回调异常（已忽略）：" + t);
        }
    }

    /** 只由收包线程调用：找不到就新建一条流。 */
    private Stream streamFor(String host, int ssrc) {
        String key = host + "/" + (ssrc & 0xFFFFFFFFL);
        for (Stream s : streams) {
            if (s.key.equals(key)) {
                return s;
            }
        }
        Stream s = new Stream(key, host, ssrc);
        s.gain = gainFor(host);      // 用户给这台设过的增益，新流也要带上
        streams.add(s);
        ssrcSeen++;
        Log.i(TAG, "新音频流：" + key);
        return s;
    }

    // -------------------------------------------------------- 播放/混音线程

    private void mixLoop() {
        // 复用同一个混音缓冲：每 10ms 一趟的循环里绝不分配大对象，否则 GC 一停顿就是爆音。
        byte[] mix = new byte[FRAME_BYTES];
        int idleTicks = 0;
        while (running) {
            try {
                Arrays.fill(mix, (byte) 0);
                boolean produced = mixOnce(mix);
                if (!produced) {
                    // 没有任何流在播：不写 AudioTrack，省电也省 CPU。
                    idleTicks++;
                    if (idleTicks == 30 && trackPlaying) {
                        pauseTrack();
                    }
                    sleepQuiet(10);
                    continue;
                }
                idleTicks = 0;
                if (!ensurePlaying()) {
                    sleepQuiet(10);
                    continue;
                }
                AudioTrack t = track;
                if (t == null) {
                    sleepQuiet(10);
                    continue;
                }
                int w = t.write(mix, 0, FRAME_BYTES);
                if (w < 0) {
                    trackErrors++;
                    if (trackErrors % 20 == 1) {
                        note("AudioTrack.write 失败：" + w + "，尝试重建");
                    }
                    // 后台 30 秒回来、音频设备被别的应用抢走等情况都会走到这里：
                    // 重建一次 AudioTrack 是最省事也最有效的自愈手段。
                    pauseTrack();
                    releaseTrack();
                }
            } catch (Throwable t) {
                note("混音线程异常：" + t);
                sleepQuiet(20);
            }
        }
        releaseTrack();
    }

    /**
     * 让每条流往前走一帧，并把结果相加到混音缓冲里。
     * 具体每一步都在 {@link Stream#pump} 里（那里加了锁），这里只负责汇总。
     *
     * @return 是否有流在播放（没有的话调用方可以干脆不写 AudioTrack）
     */
    private boolean mixOnce(byte[] mix) {
        long now = SystemClock.elapsedRealtime();
        float outPeak = 0f;
        boolean produced = false;
        int dropAbove = targetMs + DROP_ABOVE_EXTRA_MS;

        for (Stream s : streams) {
            // 对端走了/断了：摘掉，别留着一个永远不出声的流占混音位置。
            if (now - s.lastPacketMs > STREAM_IDLE_MS) {
                streams.remove(s);
                Log.i(TAG, "音频流超时移除：" + s.key);
                continue;
            }
            if (s.pump(mix, now, targetMs, dropAbove) != 0) {
                produced = true;
            }
            if (s.peak > outPeak) {
                outPeak = s.peak;
            }
        }
        peak = outPeak;
        return produced;
    }

    private boolean ensurePlaying() {
        AudioTrack t = track;
        if (t == null) {
            t = openTrack();
            if (t == null) {
                return false;
            }
            track = t;
        }
        if (!trackPlaying) {
            try {
                t.setVolume(gain);
                t.play();
                trackPlaying = true;
            } catch (Throwable e) {
                note("AudioTrack.play 失败：" + e);
                releaseTrack();
                return false;
            }
        }
        return true;
    }

    private void pauseTrack() {
        AudioTrack t = track;
        if (t != null) {
            try {
                t.pause();
                t.flush();   // 把停下来这段时间的旧数据丢掉，恢复时才不会放出一段"过去的声音"
            } catch (Throwable ignored) {
            }
        }
        trackPlaying = false;
    }

    private AudioTrack openTrack() {
        try {
            int minBuf = AudioTrack.getMinBufferSize(Proto.SAMPLE_RATE,
                    AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT);
            if (minBuf <= 0) {
                minBuf = FRAME_BYTES * 4;
            }
            // 缓冲只留 2 帧（20ms）再加系统最小值：抖动已经由我们自己的缓冲吸收了，
            // AudioTrack 的缓冲纯粹是给线程调度留余量 —— 留太大等于白送延迟。
            int bufBytes = Math.max(minBuf, FRAME_BYTES * 2);
            AudioFormat fmt = new AudioFormat.Builder()
                    .setSampleRate(Proto.SAMPLE_RATE)
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                    .build();
            AudioAttributes attrs = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                    .build();
            AudioTrack.Builder b = new AudioTrack.Builder()
                    .setAudioAttributes(attrs)
                    .setAudioFormat(fmt)
                    .setBufferSizeInBytes(bufBytes)
                    .setTransferMode(AudioTrack.MODE_STREAM);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                // 能走快速混音通道就走；厂商不给也无所谓，只是延迟略大。
                b.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY);
            }
            AudioTrack t = b.build();
            if (t.getState() != AudioTrack.STATE_INITIALIZED) {
                note("AudioTrack 初始化失败（没有可用输出设备？）");
                try {
                    t.release();
                } catch (Throwable ignored) {
                }
                return null;
            }
            Log.i(TAG, "AudioTrack 就绪：缓冲 " + bufBytes + " 字节");
            return t;
        } catch (Throwable e) {
            note("AudioTrack 创建失败：" + e);
            return null;
        }
    }

    private void releaseTrack() {
        AudioTrack t = track;
        track = null;
        trackPlaying = false;
        if (t != null) {
            try {
                t.pause();
            } catch (Throwable ignored) {
            }
            try {
                t.flush();
            } catch (Throwable ignored) {
            }
            try {
                t.release();
            } catch (Throwable ignored) {
            }
        }
    }

    // ------------------------------------------------------------------ 内部

    /**
     * 一条发送方的抖动缓冲。
     *
     * <p>结构是一个 32 槽的环形数组，槽位下标就是 {@code seq & 31}：
     * {@code nextSeq} 是"下一个要播的序号"，收到的包按它到 {@code nextSeq} 的距离落槽；
     * 落在过去（已播过）的直接丢并计入迟到，超前的放进槽里等播放线程来取。
     * 这样写比维护一个有序 map 快得多，而且播放路径上完全不分配内存。
     *
     * <p><b>锁</b>：收包线程和播放线程都要碰 {@code nextSeq/filled}，所以这几个字段统一用
     * {@code synchronized(this)} 保护（每 10 毫秒抢一次、临界区只有几百纳秒，代价可以忽略）。
     * 统计字段用 volatile：界面线程会无锁读它们画电平条。
     */
    private final class Stream {
        final String key;
        final String host;
        final int ssrc;
        final byte[][] slots = new byte[SLOTS][FRAME_BYTES];
        final boolean[] filled = new boolean[SLOTS];

        int nextSeq = -1;
        int highestSeq = 0;
        boolean playing;
        long prebufStartMs;
        volatile long lastPacketMs = SystemClock.elapsedRealtime();
        volatile long received;
        volatile long lost;
        volatile long late;
        volatile long dup;
        volatile float peak;
        /** ★ 这一路自己的增益（"我听它的"滑杆，1.0 = 原样）。由界面线程改、收包线程读。 */
        volatile float gain = 1f;

        Stream(String key, String host, int ssrc) {
            this.key = key;
            this.host = host;
            this.ssrc = ssrc;
        }

        /** 收包线程调用：把这一包放进对应槽位。 */
        void store(int seq, byte[] payload, int len, boolean silent) {
            long now = SystemClock.elapsedRealtime();
            lastPacketMs = now;
            received++;
            synchronized (this) {
                if (nextSeq < 0) {
                    // 这条流的第一包：它就是播放的起点，预缓冲计时也从这一刻开始。
                    nextSeq = seq;
                    highestSeq = seq;
                    prebufStartMs = now;
                } else {
                    int d = (seq - nextSeq) & 0xFFFF;
                    if (d >= 0x8000) {
                        late++;       // 已经播过这个序号了（迟到/重复的老包），不能倒回去播
                        return;
                    }
                    if (d >= SLOTS) {
                        late++;       // 太超前，缓冲装不下：说明我们这边卡了很久，丢比塞进去更安全
                        return;
                    }
                    if (((seq - nextSeq) & 0xFFFF) > ((highestSeq - nextSeq) & 0xFFFF)) {
                        highestSeq = seq;
                    }
                }
                int idx = seq & MASK;
                if (filled[idx]) {
                    dup++;
                    return;
                }
                byte[] slot = slots[idx];
                if (silent || len <= 0) {
                    // 对端明确标了"静音"：占住槽位但不拷数据，播放时就不会把它算成丢包。
                    Arrays.fill(slot, (byte) 0);
                } else if (gain == 1f) {
                    // 绝大多数情况走这条：原样拷贝，一次乘法都不做。
                    System.arraycopy(payload, 0, slot, 0, len);
                    if (len < FRAME_BYTES) {
                        Arrays.fill(slot, len, FRAME_BYTES, (byte) 0);   // 短包尾巴补零
                    }
                } else {
                    // ★ 这一台自己设了增益（"我听它的"滑杆）：只乘这一路的样本，别的流不受影响。
                    scalePcm16(payload, slot, len, gain);
                    if (len < FRAME_BYTES) {
                        Arrays.fill(slot, len, FRAME_BYTES, (byte) 0);
                    }
                }
                filled[idx] = true;
            }
        }

        /**
         * 把 PCM16 样本乘上增益后拷进目标缓冲（带削顶）。
         *
         * <p><b>为什么放在 Stream 里面</b>：它只服务这一路。放在外层会让
         * {@code tools/extract_stream.awk} 抽出来的 Stream 类编译不过（抖动测试就废了），
         * 所以凡是 Stream 用到的东西都留在 Stream 内部。
         *
         * <p>超过 32767 必须削顶 —— 回绕会变成刺耳的爆音。
         */
        private void scalePcm16(byte[] src, byte[] dst, int len, float g) {
            for (int i = 0; i + 1 < len; i += 2) {
                int v = (short) ((src[i] & 0xFF) | ((src[i + 1] & 0xFF) << 8));
                int out = Math.round(v * g);
                if (out > 32767) {
                    out = 32767;
                } else if (out < -32768) {
                    out = -32768;
                }
                dst[i] = (byte) out;
                dst[i + 1] = (byte) (out >> 8);
            }
        }

        /**
         * 播放线程调用：让这条流往前走一帧（必要时先预缓冲、或主动丢一帧压延迟），
         * 有数据就相加进 {@code mix}。
         *
         * @return 0 = 这一 tick 这条流不出声（还在预缓冲）；1 = 在播（可能是补的静音）
         */
        synchronized int pump(byte[] mix, long now, int targetMs, int dropAboveMs) {
            if (nextSeq < 0) {
                return 0;   // 还没收到第一包
            }
            if (!playing) {
                int bufferedMs = depthMs();
                if (now - prebufStartMs < targetMs && bufferedMs < (SLOTS - 4) * 10) {
                    return 0;   // 先攒够再播：这一段不出声，换来后面不卡
                }
                playing = true;
            }
            // 缓冲积太多说明我们播得比对方发得慢（两端音频时钟有 ppm 级漂移），
            // 或者刚开始播时手里已经攒了一大段。要主动丢掉多余的，把延迟压回去。
            //
            // ★ 2026-10-12 真机反馈的头号问题（"连上了、数字都在动、就是没声音"）：
            //   这里原来是"每 tick 只丢一帧，然后 return"。对端每 10ms 也正好来一帧，
            //   于是变成收一帧丢一帧，缓冲深度永远降不下来 —— 结果每个 tick 都在丢，
            //   实测 5946 个包里 5908 个被这样丢掉，等于全程静音。
            //   修法：一次把多出来的全丢到目标深度，然后【继续往下正常播放】，不要 return。
            if (depthMs() > dropAboveMs) {
                int guard = 0;
                while (depthMs() > targetMs && guard++ < SLOTS) {
                    filled[nextSeq & MASK] = false;
                    nextSeq = (nextSeq + 1) & 0xFFFF;
                    lost++;
                    peak *= PEAK_DECAY;
                }
                // 注意：这里不 return，让这一 tick 照常出声，否则永远播不出第一帧。
            }
            int idx = nextSeq & MASK;
            if (filled[idx]) {
                byte[] src = slots[idx];
                filled[idx] = false;
                float p = 0f;
                for (int i = 0; i < FRAME_BYTES; i += 2) {
                    int a = (mix[i] & 0xFF) | (mix[i + 1] << 8);
                    int b = (src[i] & 0xFF) | (src[i + 1] << 8);
                    int m = a + b;
                    if (m > 32767) {
                        m = 32767;          // 多台设备同时出声必须削顶，回绕会变成刺耳的噪声
                    } else if (m < -32768) {
                        m = -32768;
                    }
                    mix[i] = (byte) m;
                    mix[i + 1] = (byte) (m >> 8);
                    int abs = b < 0 ? -b : b;
                    if (abs > p) {
                        p = abs;
                    }
                }
                float pf = p / 32768f;
                // 快起慢落：数字每秒跳 100 次，不衰减的话界面根本看不清。
                peak = pf >= peak ? pf : peak * PEAK_DECAY;
            } else {
                // 这一包没到：补静音（什么都不混就是静音）。不补的话整条流会往后错位。
                lost++;
                int ahead = (highestSeq - nextSeq) & 0xFFFF;
                if (ahead == 0 || ahead >= 0x8000) {
                    // 手上没有任何更新的包 —— 这不是丢包，是流断了（对端切后台 / Wi-Fi 打个嗝）。
                    // 回到预缓冲状态，等它恢复时重新攒一段再播；继续傻等会让序号错位到最后全变"迟到"。
                    playing = false;
                    nextSeq = -1;
                    prebufStartMs = now;
                    Arrays.fill(filled, false);
                    bump(7);   // recoveries
                    // ★ 这里必须直接返回。原来会一路走到函数末尾那句无条件的
                    //   nextSeq = (nextSeq + 1) & 0xFFFF，把刚设好的 -1 推成 0：
                    //   播放头于是在一个空缓冲上空转计数，等对端恢复时新包全被判成"太超前"丢掉，
                    //   流再也回不来（离线抖动缓冲测试抓到："断流 2.5s 后恢复：重新出声 0 帧"）。
                    return 0;
                }
            }
            // 双保险：恢复分支把 nextSeq 置成 -1 之后，绝不能再自增（-1 是"等第一包"的哨兵值）。
            if (nextSeq < 0) {
                return 0;
            }
            nextSeq = (nextSeq + 1) & 0xFFFF;
            return 1;
        }

        /** 缓冲里还有多少帧可用，折算成毫秒（槽位只有 32 个，数一遍比维护计数器更难写错）。 */
        synchronized int depthMs() {
            if (nextSeq < 0) {
                return 0;
            }
            int n = 0;
            for (int i = 0; i < SLOTS; i++) {
                if (filled[(nextSeq + i) & MASK]) {
                    n++;
                }
            }
            return n * 10;
        }
    }

    /**
     * ★ 每台设备各自的"我听它的"增益（界面滑杆）。
     *
     * <p>为什么不直接改总增益：用户要的是<b>每一台自己一条可调音量条</b>
     * （原话：「我要求的是各设备可调节的音量条」）。做法是把增益乘在<b>这一路</b>的样本上
     * （{@link Stream#store} 拷进槽位时乘），所以各台互不影响；总增益（AudioTrack 音量）照旧叠在上面。
     *
     * <p>写的是 copy-on-write 的不可变 map：收包线程每包要读一次，不能让它去抢锁。
     */
    private volatile Map<String, Float> hostGain = Collections.emptyMap();

    /** 设定某一台的接收增益（0~1.5，1.0 = 原样）。立刻生效。 */
    public void setHostGain(String host, float g) {
        if (host == null || host.isEmpty()) {
            return;
        }
        float v = Math.max(0f, Math.min(1.5f, g));
        Map<String, Float> next = new HashMap<>(hostGain);
        next.put(host, v);
        hostGain = Collections.unmodifiableMap(next);   // 一次替换，收包线程永远读到完整的一份
        for (Stream s : streams) {
            if (host.equals(s.host)) {
                s.gain = v;
            }
        }
    }

    /** 某一台现在的接收增益。 */
    public float hostGain(String host) {
        Float v = hostGain.get(host);
        return v == null ? 1f : v;
    }

    /** 新建流时把用户设过的增益带上（不然新流的滑杆会"跳回 1.00"）。 */
    private float gainFor(String host) {
        Float v = hostGain.get(host);
        return v == null ? 1f : v;
    }

    private void bump(int idx) {
        synchronized (counters) {
            counters[idx]++;
        }
    }

    /**
     * ★ 记一个坏包：总数 + 分类 + 来源 IP + 整包长度。
     *
     * @param idx 分类下标（0 = 其它/解码失败，8..12 见 counters 的注释）
     * @param host 来源地址（记不住就算了，不影响计数）
     * @param len 整包字节数（长度是最有效的"指纹"，见 {@link Stats#badByLen}）
     */
    private void bumpBad(int idx, String host, int len) {
        synchronized (counters) {
            counters[4]++;
            if (idx > 0) {
                counters[idx]++;
            }
            if (host != null && !host.isEmpty()) {
                Long c = badBySource.get(host);
                badBySource.put(host, (c == null ? 0L : c) + 1L);
                // 只有收包线程写；上限 8 个来源，超了就丢最老的那个。
                while (badBySource.size() > 8) {
                    java.util.Iterator<String> it = badBySource.keySet().iterator();
                    if (!it.hasNext()) {
                        break;
                    }
                    it.next();
                    it.remove();
                }
            }
            if (len > 0) {
                Integer key = len;
                Long c = badByLen.get(key);
                badByLen.put(key, (c == null ? 0L : c) + 1L);
                while (badByLen.size() > 8) {
                    java.util.Iterator<Integer> it = badByLen.keySet().iterator();
                    if (!it.hasNext()) {
                        break;
                    }
                    it.next();
                    it.remove();
                }
            }
        }
    }

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

    @SuppressWarnings("deprecation")
    private void acquireWifiLock() {
        if (appCtx == null || wifiLock != null) {
            return;
        }
        try {
            WifiManager wm = (WifiManager) appCtx.getSystemService(Context.WIFI_SERVICE);
            if (wm == null) {
                return;
            }
            // 29 起有 LOW_LATENCY 模式（明确告诉系统"这是实时音频"），老系统只有 HIGH_PERF。
            int mode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                    ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                    : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
            WifiManager.WifiLock l = wm.createWifiLock(mode, "audiomixing-recv");
            l.setReferenceCounted(false);
            l.acquire();
            wifiLock = l;
        } catch (Exception e) {
            note("WifiLock 获取失败（继续接收，只是息屏后可能变抖）：" + e);
        }
    }

    private void releaseWifiLock() {
        WifiManager.WifiLock l = wifiLock;
        wifiLock = null;
        if (l != null) {
            try {
                if (l.isHeld()) {
                    l.release();
                }
            } catch (Throwable ignored) {
            }
        }
    }

    private void note(String msg) {
        lastError = msg;
        Log.w(TAG, msg);
    }

    private static void sleepQuiet(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
