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

    private final long[] counters = new long[8];   // received,lost,late,dup,bad,filtered,controls,recoveries
    private volatile long trackErrors;
    private int ssrcSeen;

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

    /** 本机音量 0.0~1.0，用 AudioTrack.setVolume，不动样本（省一次 960 次乘法）。 */
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
        }
        st.trackErrors = trackErrors;
        st.targetMs = targetMs;
        st.peak = peak;
        long now = SystemClock.elapsedRealtime();
        List<StreamStats> list = new ArrayList<>(streams.size());
        int maxBuf = 0;
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
            list.add(ss);
        }
        st.streamCount = list.size();
        st.bufferedMs = maxBuf;
        st.streams = list;
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
        Proto.Pkt pkt = Proto.decode(buf, 0, len);
        if (pkt == null) {
            bump(4);   // badPackets：magic/版本不对，不是我们的包
            return;
        }
        if (pkt.type != Proto.T_AUDIO) {
            bump(6);   // PING/PONG/BYE 之类的控制包，现在只计数
            return;
        }
        if (pkt.codec != Proto.CODEC_PCM_S16) {
            bump(4);   // 手机没有 Opus 解码器（见类注释），直接丢
            return;
        }
        String host = normalizeHost(from);
        if (!isAllowed(host)) {
            bump(5);   // 用户没勾「收听」它
            return;
        }
        int plen = pkt.payload == null ? 0 : pkt.payload.length;
        if (plen > FRAME_BYTES || pkt.samples > Proto.FRAME_SAMPLES) {
            // 只认 480 帧一包（10ms）。别的帧长我们不做重采样 —— 宁可不要，也不要变速变调。
            bump(4);
            return;
        }
        bump(0);   // received
        Stream stream = streamFor(host, pkt.ssrc);
        stream.store(pkt.seq, pkt.payload, plen, (pkt.flags & Proto.FLAG_SILENCE) != 0);
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
                } else {
                    System.arraycopy(payload, 0, slot, 0, len);
                    if (len < FRAME_BYTES) {
                        Arrays.fill(slot, len, FRAME_BYTES, (byte) 0);   // 短包尾巴补零
                    }
                }
                filled[idx] = true;
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
            // 缓冲积太多说明我们播得比对方发得慢（两端音频时钟有 ppm 级漂移）。
            // 主动丢一帧把延迟压回去，否则延迟会越攒越大，最后积满一缓冲只能整段跳。
            if (depthMs() > dropAboveMs) {
                filled[nextSeq & MASK] = false;
                nextSeq = (nextSeq + 1) & 0xFFFF;
                lost++;
                peak *= PEAK_DECAY;
                return 1;
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
                    Arrays.fill(filled, false);
                    bump(7);   // recoveries
                }
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

    private void bump(int idx) {
        synchronized (counters) {
            counters[idx]++;
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
