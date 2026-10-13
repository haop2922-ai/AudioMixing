package com.audiomixing.app;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioPlaybackCaptureConfiguration;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.projection.MediaProjection;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.util.Arrays;

/**
 * 采集 + 打包 + UDP 单播：把「本机的声音」变成一条 UDP 音频流。
 *
 * <p><b>★ 两种音源，用户可选 —— 这是这个 App 的定位问题，不是小功能。</b>
 * 早期版本只有麦克风，用户的原话是「你这手机都是采集的麦克风的声音，那不成对讲机了吗？
 * 我的需求一直都是桌面音频啊」。所以：
 * <ul>
 *   <li>{@link #SRC_PLAYBACK} 🔊 <b>本机正在播放的声音</b>（音乐 / 视频 / 游戏 / 网页）——
 *       {@link MediaProjection} 拿授权 + {@link AudioPlaybackCaptureConfiguration} 建 AudioRecord，
 *       Android 10（API 29）起才有这个能力；</li>
 *   <li>{@link #SRC_MIC} 🎤 <b>麦克风</b> —— 对讲场景仍然有用，所以保留。</li>
 * </ul>
 * <b>采不到就如实退回麦克风，并把原因记在 {@link #lastError()} 里</b>，
 * 让界面和诊断报告能写清"实际在采的是哪一个"。绝不假装成功 —— 用户已经被误导过一次了。
 *
 * <p><b>参数固定</b> 48000Hz / 立体声 / PCM16，一次读 480 帧（10 毫秒）打一个包。
 * <b>为什么不用 Opus</b>：PCM16 立体声 48k 是 1536 KB/s，Wi-Fi 上几台设备完全吃得下；
 * 换来的是不用 NDK、不用 libopus、装机包小、也没了编解码的额外延迟。
 *
 * <p><b>线程</b>：{@link #start()} 只负责起线程并立刻返回。采集和发 socket 都在工作线程里，
 * 因为 {@code AudioRecord.read()} 会阻塞 10ms、{@code send()} 也可能被 Wi-Fi 拖住，
 * 界面线程一秒都不能卡。
 *
 * <p><b>换音源</b>：{@link #setSourceKind(int)} 只挂一个"待换"标记，真正重建 AudioRecord
 * 是在采集线程里做的 —— 同一个线程里先关旧的再开新的，所以<b>永远不会有两份采集同时往
 * socket 里灌</b>（那会让对端听到叠在一起的两路声音）。
 *
 * <p><b>时钟</b>：{@code ts_ns} 用墙钟（{@code System.currentTimeMillis() * 1e6}），
 * 和电脑端 {@code time.time_ns()} 同源；接收端主要靠"到达顺序 + 预缓冲"播放，
 * 这个时间戳用于统计和对齐参考。
 */
public final class AudioSend {

    private static final String TAG = "AM-AudioSend";

    /** 一包的负载字节数：480 帧 × 2 声道 × 2 字节 = 1920。 */
    public static final int FRAME_BYTES = Proto.FRAME_SAMPLES * Proto.CHANNELS * 2;

    // ══════════════════════════ ★ 送什么声音（音源） ══════════════════════════

    /** 音源：麦克风（对讲用，最早的实现）。 */
    public static final int SRC_MIC = 0;
    /** 音源：本机正在播放的声音（音乐/视频/游戏），Android 10+ 才有。 */
    public static final int SRC_PLAYBACK = 1;

    /**
     * 这台机器支不支持采"本机正在播放的声音"。
     *
     * <p>只有 Android 10（API 29）才有 {@code AudioPlaybackCapture}。低版本<b>没有</b>这个能力，
     * 界面上必须直说"你的安卓版本不支持，只能用麦克风"，不能装作选上了。
     */
    public static boolean playbackSupported() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q;
    }

    /** 音源的人话名字（界面、通知、诊断报告共用一处定义，免得两种说法把用户又绕晕）。 */
    public static String sourceName(int kind) {
        return kind == SRC_PLAYBACK ? "🔊 本机声音" : "🎤 麦克风";
    }

    /** 音源的短名字（通知栏那种一行放不下的地方用）。 */
    public static String sourceShortName(int kind) {
        return kind == SRC_PLAYBACK ? "本机声音" : "麦克风";
    }

    /**
     * 麦克风优先级：通话音源自带回声消除/自动增益，手机外放时最不容易啸叫，
     * 所以设计文档选它；有些机器不认这个音源（或没给权限），逐级退回。
     */
    private static final int[] MIC_SOURCE_CHAIN = {
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.DEFAULT,
    };

    /** 本机声音采集要匹配的用法：音乐/视频、游戏、以及没标用途的 App。 */
    private static final int[] PLAYBACK_USAGES = {
            AudioAttributes.USAGE_MEDIA,
            AudioAttributes.USAGE_GAME,
            AudioAttributes.USAGE_UNKNOWN,
    };

    /** 低于这个电平就当这一帧是静音（16 位满量程 32768，约 -72dB）。 */
    private static final float SILENCE_LEVEL = 0.0008f;

    private final Context appCtx;          // 可以为 null：拿不到就不锁 Wi-Fi，功能照旧
    private volatile String host;
    private volatile int port;

    private final Object lifeLock = new Object();
    private volatile boolean running;
    private Thread worker;
    private volatile AudioRecord record;
    private volatile Resampler resampler;   // 非空 = 采集格式要换算（绝大多数设备是 null）
    // 发送用的 UDP socket 现在归 SendFanout 管（它要同时发给 N 个目标，还负责逐目标统计）
    private WifiManager.WifiLock wifiLock;

    private volatile int gainQ14 = Proto.GAIN_ONE;
    private volatile float peak;
    private volatile long packetsSent;
    private volatile long bytesSent;
    private volatile int sendErrors;
    private volatile String lastError = "";
    private volatile int audioSourceUsed = -1;

    // ── ★ 音源状态：wantSource = 用户要的，activeSource = 真正在采的（可能不一样，界面上要如实说） ──
    private volatile int wantSource = SRC_MIC;
    private volatile int activeSource = -1;
    private volatile boolean switchPending;
    private volatile MediaProjection projection;    // 采本机声音必须有它（由 Engine 授权后交过来）
    private volatile int captureRate = Proto.SAMPLE_RATE;
    private volatile int captureChannels = Proto.CHANNELS;
    /** 打开采集失败的原因（本机声音采不到时，界面/诊断报告直接引用这句话）。 */
    private volatile String captureProblem = "";

    // ── 静音观察：采到了但一直是静音是"DRM/被禁止采集"最典型的表现，报告里要能看出来 ──
    private volatile long lastLoudMs;
    private volatile boolean everLoud;

    private int ssrc;

    // ★ 一发多：一次采集、一包编码 → 分发给 N 个目标（见 SendFanout 的类注释）。
    //   单独拎出来还有一个好处：它是纯 java.net 逻辑，能拿真实 UDP 回环做离线门禁。
    private final SendFanout fanout = new SendFanout();

    public AudioSend(String host, int port) {
        this(null, host, port);
    }

    /**
     * ★ 不带目标的版本：先建好采集器，目标后面用 {@link #setTargets} 给。
     *
     * <p>「📢 广播我的声音」和"同时送给好几台"都走这个 —— 采集只有一份，
     * 送给谁是一个可以随时变的集合。
     */
    public AudioSend(Context context) {
        this(context, null, 0);
        fanout.setTargets(java.util.Collections.<String>emptyList());
    }

    /**
     * 带 Context 的版本会顺手拿一个 {@code WifiLock}：息屏后 Wi-Fi 进省电模式时，
     * 发包会被攒到 DTIM 周期才出去，听感就是"一顿一顿的"。前台服务能保住进程，
     * 但保不住 Wi-Fi 射频，所以这里要单独锁一下。
     *
     * <p>Context 还有一个用途：查本机音频通路的原生采样率（本机声音采集降级时要用）。
     */
    public AudioSend(Context context, String host, int port) {
        this.appCtx = context == null ? null : context.getApplicationContext();
        this.host = host;
        this.port = port;
        this.ssrc = new java.util.Random().nextInt();
        if (this.ssrc == 0) {
            this.ssrc = 1;
        }
        if (host != null && !host.isEmpty() && port > 0) {
            fanout.setTargets(java.util.Collections.singletonList(host + ":" + port));
        }
    }

    // ------------------------------------------------------------------ 对外

    /** 开始采集并发送；重复调用无副作用。 */
    public void start() {
        synchronized (lifeLock) {
            if (running) {
                return;
            }
            running = true;
            worker = new Thread(this::loop, "am-audio-tx");
            worker.setDaemon(true);
            worker.start();
        }
    }

    /** 停止发送并释放采集。采集线程最多阻塞 10ms，所以这里 join 很快。 */
    public void stop() {
        Thread t;
        synchronized (lifeLock) {
            if (!running && worker == null) {
                return;
            }
            running = false;
            t = worker;
            worker = null;
        }
        if (t != null && t != Thread.currentThread()) {
            try {
                t.join(1500);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        releaseAll();   // 万一线程没退干净，这里兜底释放，采集不能一直被占着
    }

    public boolean isRunning() {
        return running;
    }

    // ------------------------------------------------------------ ★ 音源切换

    /**
     * 请求换音源。真正的重建在采集线程里做（先关旧的再开新的，不会有两路叠着发）。
     *
     * <p>如果请求的是本机声音但采不到（没授权 / 低于 Android 10 / ROM 阉割），
     * 采集线程会<b>退回麦克风</b>并把原因写进 {@link #lastError()}，
     * 此时 {@link #activeSourceKind()} 会如实返回 {@link #SRC_MIC}。
     */
    public void setSourceKind(int kind) {
        if (kind == SRC_PLAYBACK && !playbackSupported()) {
            kind = SRC_MIC;         // 低版本没有这个能力，不装作能选
        }
        if (kind == wantSource && activeSource == kind) {
            return;
        }
        wantSource = kind;
        if (running) {
            switchPending = true;   // 线程没在跑就不用挂标记，start() 时会直接按 wantSource 打开
        }
    }

    /** 用户/引擎选的音源（可能因为不支持或采不到而和实际不一致）。 */
    public int requestedSourceKind() {
        return wantSource;
    }

    /** <b>实际</b>在采的音源；-1 = 还没打开。界面显示"现在送的是什么"必须用这个。 */
    public int activeSourceKind() {
        return activeSource;
    }

    /** 把授权拿到的 MediaProjection 交给采集端（采本机声音必须）。 */
    public void setProjection(MediaProjection p) {
        projection = p;
        // 换了（或丢了）授权就要重建 AudioRecord：老的那个绑在旧 projection 上，
        // 继续用只会得到一片静音，而且用户完全看不出来。
        if (running && wantSource == SRC_PLAYBACK) {
            switchPending = true;
        }
    }

    public boolean hasProjection() {
        return projection != null;
    }

    /** 实际采集用的采样率/声道数（正常就是 48000/2，降级过就写实际值）。 */
    public int captureRate() {
        return captureRate;
    }

    public int captureChannels() {
        return captureChannels;
    }

    /**
     * 已经连续多少毫秒没有采到非静音内容（elapsedRealtime 口径，0 = 没在采）。
     *
     * <p>为什么值得看：本机声音采到了、包也在发，但全是零 —— 这就是 DRM 保护内容
     * 或"主动禁止被采集的 App"最典型的样子。不写出来的话用户只会以为"这个功能坏了"。
     */
    public long silentMs() {
        if (!running || activeSource < 0) {
            return 0;
        }
        long t = lastLoudMs;
        return t <= 0 ? 0 : Math.max(0, SystemClock.elapsedRealtime() - t);
    }

    /** 这次采集里到底出过声音没有（配合 {@link #silentMs()} 区分"一直静音"和"刚刚静了"）。 */
    public boolean everLoud() {
        return everLoud;
    }

    /** 这次采集用的格式说明（诊断报告里用）。 */
    public String captureFormatText() {
        if (activeSource < 0) {
            return "还没打开采集";
        }
        StringBuilder sb = new StringBuilder();
        sb.append(captureRate).append("Hz ");
        sb.append(captureChannels == 1 ? "单声道" : "立体声");
        sb.append(" PCM16");
        if (resampler != null) {
            sb.append("（设备只给得出这个格式，已在发送前换算成 48000Hz 立体声）");
        }
        return sb.toString();
    }

    /**
     * 实时电平 0.0~1.0（峰值，快起慢落），界面画电平条用。
     * 取的是<b>加过音量之后</b>的值，也就是真正发出去的电平。
     */
    public float peak() {
        return peak;
    }

    /** 发送增益，0.0~2.0（1.0 = 原样）。界面「我们这一层的音量」滑杆接这里（只乘我们自己的 PCM）。 */
    public void setGain(float g) {
        setGainQ14(Math.round(Math.max(0f, Math.min(2f, g)) * Proto.GAIN_ONE));
    }

    public float gain() {
        return gainQ14 / (float) Proto.GAIN_ONE;
    }

    /** 直接写协议里的 Q14 定点增益。 */
    public void setGainQ14(int q14) {
        gainQ14 = Math.max(0, Math.min(Proto.GAIN_ONE * 2, q14));
    }

    public int gainQ14() {
        return gainQ14;
    }

    /** 运行中也能改目标（对端换 IP、用户改勾选都用得上）。单目标版本的便捷写法。 */
    public void setTarget(String host, int port) {
        this.host = host;
        this.port = port;
        fanout.setTargets(java.util.Collections.singletonList(host + ":" + port));
    }

    /**
     * ★ 设定"送给谁"（可以多台）。目标集合变化<b>不会重启采集</b> —— 麦克风/MediaProjection
     * 一直开着，只是每包多发给几个人或几个人不发而已。
     *
     * @param endpoints "host:port" 列表（Engine 按发现列表 + 用户勾选算出来）
     */
    public void setTargets(java.util.List<String> endpoints) {
        fanout.setTargets(endpoints);
        if (endpoints != null && !endpoints.isEmpty()) {
            String first = endpoints.get(0);
            int colon = first.lastIndexOf(':');
            if (colon > 0) {
                this.host = first.substring(0, colon);
                try {
                    this.port = Integer.parseInt(first.substring(colon + 1).trim());
                } catch (Throwable ignored) {
                    // 端口解析不了就保持原值
                }
            }
        }
    }

    /** 当前目标数（界面/诊断用）。 */
    public int targetCount() {
        return fanout.targetCount();
    }

    /** 目标列表的字符串（"192.168.0.109:47831 / …"，诊断报告用）。 */
    public String targetText() {
        return fanout.targetText();
    }

    /** 逐目标的发送统计（界面"送"电平条 + 诊断报告）。 */
    public java.util.List<SendFanout.TargetStats> targetStats() {
        return fanout.stats();
    }

    /** 发给某一台的电平 0~1（没送给它就是 0）。 */
    public float targetPeak(String host) {
        return fanout.peakFor(host);
    }

    public String host() {
        return host;
    }

    public int port() {
        return port;
    }

    /** 本次会话的流标识（接收端按它分流）。 */
    public int ssrc() {
        return ssrc;
    }

    public long packetsSent() {
        return packetsSent;
    }

    public long bytesSent() {
        return bytesSent;
    }

    /** 发送失败的次数（对端关了、Wi-Fi 断了都算），界面"详情"里可以显示。 */
    public int sendErrors() {
        return sendErrors;
    }

    /** 实际打开的麦克风音源（MediaRecorder.AudioSource.*）；采本机声音时是 -1（没有麦克风音源这回事）。 */
    public int audioSource() {
        return audioSourceUsed;
    }

    public String lastError() {
        return lastError;
    }

    // ------------------------------------------------------------- 工作线程

    private void loop() {
        acquireWifiLock();
        if (!openCapture()) {
            running = false;
            releaseAll();
            return;
        }
        // 开机这一次已经按最新的 wantSource 打开了，把可能残留的换音源标记清掉。
        switchPending = false;
        Log.i(TAG, "开始发送：目标 " + fanout.targetCount() + " 个（" + fanout.targetText()
                + "）音源=" + sourceName(activeSource) + " ssrc=" + ssrc);

        // 复用同一份缓冲和包头对象：每 10ms 跑一趟的循环里尽量不产生垃圾，
        // 否则一次 GC 停顿就是一次可听见的爆音。
        byte[] frame = new byte[FRAME_BYTES];
        Proto.Pkt pkt = new Proto.Pkt(Proto.T_AUDIO);
        pkt.codec = Proto.CODEC_PCM_S16;
        pkt.samples = Proto.FRAME_SAMPLES;
        pkt.ssrc = ssrc;
        pkt.payload = frame;
        int seq = 0;
        int consecutiveErrors = 0;
        int reopenCount = 0;

        try {
            while (running) {
                // ★ 换音源：只在这个线程里重建，先关旧的再开新的，绝不会两路声音叠着发。
                if (switchPending) {
                    switchPending = false;
                    if (!rebuildCapture()) {
                        note("换音源后采集打不开了，停止发送");
                        break;
                    }
                }

                int n = readFrame(frame);
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_BAD_VALUE
                            || n == AudioRecord.ERROR_DEAD_OBJECT) {
                        if (consecutiveErrors++ > 20) {
                            // 音频服务重启（ERROR_DEAD_OBJECT）、或者投屏授权被系统收回，
                            // 都是会被用户感觉到的常见故障：重建一次采集就能继续，
                            // 而不是让"送声音"这个开关悄悄失效。
                            if (reopenCount++ > 5 || !rebuildCapture()) {
                                note(sourceShortName(wantSource) + "反复打开失败，停止发送：" + n);
                                break;
                            }
                            consecutiveErrors = 0;
                            continue;
                        }
                        sleepQuiet(10);
                        continue;
                    }
                    continue;   // 偶发负数，跳过这一块
                }
                consecutiveErrors = 0;
                reopenCount = 0;

                // 静音观察用的是【加音量之前】的电平：用户把"我们这一层的音量"拖到 0 不该被当成
                // "采不到声音"（那会指向完全错误的排查方向）。
                float rawPeak = rawPeak(frame);
                float p = applyGain(frame, gainQ14);
                if (rawPeak > SILENCE_LEVEL) {
                    lastLoudMs = SystemClock.elapsedRealtime();
                    everLoud = true;
                }
                // 快起慢落，跟真实电平表的观感一致（否则数字每秒跳 100 次，根本看不清）。
                peak = p >= peak ? p : peak * 0.82f;

                pkt.seq = seq++ & 0xFFFF;                       // uint16 回绕
                pkt.tsNs = System.currentTimeMillis() * 1_000_000L;
                pkt.gainQ14 = gainQ14;
                // ★ 一包只编码一次，然后分发给所有目标（所有人收到的字节完全一样）。
                byte[] raw = Proto.encode(pkt);
                int sent = fanout.send(raw, raw.length, p);
                if (sent > 0) {
                    packetsSent += sent;
                    bytesSent += (long) raw.length * sent;
                } else {
                    sendErrors++;
                    // 一个目标都没有 / 全都发不出去：很常见（对端刚走），不要因此退出线程。
                    if (sendErrors % 100 == 1) {
                        note("这一包没有发出去（目标 " + fanout.targetCount() + " 个）");
                    }
                    sleepQuiet(5);
                }
            }
        } catch (Throwable t) {
            note("采集线程异常退出：" + t);
        } finally {
            running = false;
            releaseAll();
            Log.i(TAG, "发送结束，共 " + packetsSent + " 包 / " + bytesSent + " 字节");
        }
    }

    /** 取一帧（480 帧立体声 PCM16）。直接采集或换算，都会把 {@code frame} 填满。 */
    private int readFrame(byte[] frame) {
        AudioRecord rec = record;
        if (rec == null) {
            return AudioRecord.ERROR_INVALID_OPERATION;
        }
        Resampler rs = resampler;
        if (rs == null) {
            int n = rec.read(frame, 0, FRAME_BYTES);
            if (n > 0 && n < FRAME_BYTES) {
                // 短读：补齐成固定 480 帧，接收端就不用处理变长包（丢的是几毫秒静音，听不出来）。
                Arrays.fill(frame, n, FRAME_BYTES, (byte) 0);
            }
            return n;
        }
        return rs.fill(rec, frame, Proto.FRAME_SAMPLES);
    }

    /**
     * 按 wantSource 打开采集。打不开就返回 false（调用方负责收尾）。
     *
     * <p>本机声音打不开时会<b>退回麦克风</b>：用户至少还能对讲，而界面上会如实写
     * "你要的是本机声音，但采不到，现在实际送的是麦克风 + 原因"。
     */
    private boolean openCapture() {
        closeRecord();                  // 保险：任何情况下都不允许两份采集并存
        captureProblem = "";            // 清空上一次的原因，免得报出一条早就过时的解释
        Capture c = null;
        if (wantSource == SRC_PLAYBACK) {
            if (!playbackSupported()) {
                captureProblem = "你的安卓版本低于 Android 10，系统没有「采集本机声音」这个能力";
            } else if (projection == null) {
                captureProblem = "还没拿到「录制/投射」授权（点送声音时会弹系统授权框）";
            } else {
                c = openPlaybackCapture(projection);
            }
            if (c == null) {
                String why = captureProblem.length() > 0 ? captureProblem : "未知原因";
                note("本机声音采不到：" + why + " → 已改用麦克风，界面会如实显示");
                captureProblem = why;
            }
        } else {
            c = openMicCapture();
        }
        if (c == null) {
            if (wantSource == SRC_MIC) {
                note("没有可用的麦克风音源（RECORD_AUDIO 权限给了吗？）");
            }
            return false;
        }
        record = c.rec;
        resampler = c.rs;
        captureRate = c.rate;
        captureChannels = c.channels;
        activeSource = c.kind;
        audioSourceUsed = c.kind == SRC_MIC ? c.micSource : -1;
        peak = 0f;
        lastLoudMs = SystemClock.elapsedRealtime();
        everLoud = false;
        try {
            c.rec.startRecording();
        } catch (Throwable t) {
            note("startRecording 抛异常：" + t);
            closeRecord();
            return false;
        }
        if (c.rec.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
            note(c.kind == SRC_PLAYBACK ? "本机声音 startRecording 失败（授权被系统收回了？）"
                    : "麦克风 startRecording 失败（权限被拒？）");
            closeRecord();
            return false;
        }
        if (c.kind != wantSource) {
            // 退回麦克风了：activeSource 和 wantSource 不一致，界面据此显示警告。
            activeSource = c.kind;
        }
        return true;
    }

    /** 采集线程里换音源：先关旧的，再按当前 wantSource 开新的（同一个线程，绝不并发）。 */
    private boolean rebuildCapture() {
        Log.i(TAG, "换音源 → " + sourceName(wantSource));
        return openCapture();
    }

    /** 关掉当前的 AudioRecord（stop + release），并把状态清干净。 */
    private void closeRecord() {
        AudioRecord rec = record;
        record = null;
        resampler = null;
        activeSource = -1;
        if (rec != null) {
            try {
                if (rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    rec.stop();
                }
            } catch (Throwable ignored) {
                // 已经被系统收掉时会抛，忽略
            }
            try {
                rec.release();
            } catch (Throwable ignored) {
                // 同上
            }
        }
        peak = 0f;
    }

    /**
     * 打开「本机正在播放的声音」。
     *
     * <p>关键点：{@code AudioRecord.Builder} 上<b>只能设</b> playback capture 配置，
     * <b>不能同时设音源</b>（设了会抛 UnsupportedOperationException）——
     * 这也是"采本机声音"和"采麦克风"在代码上必须分成两条路的原因。
     *
     * <p>格式按 48000/立体声 起试：绝大多数设备一次就成，走的是零拷贝那条路。
     * 少数机型只给得出 44100 或单声道，就降级并用 {@link Resampler} 换算成协议格式 ——
     * 不然对端听到的会整体变调。
     */
    private Capture openPlaybackCapture(MediaProjection p) {
        AudioPlaybackCaptureConfiguration cfg;
        try {
            AudioPlaybackCaptureConfiguration.Builder b =
                    new AudioPlaybackCaptureConfiguration.Builder(p);
            for (int usage : PLAYBACK_USAGES) {
                b.addMatchingUsage(usage);
            }
            cfg = b.build();
        } catch (Throwable t) {
            // 授权被收回、或者 ROM 把这条 API 砍了：如实记下来，退回麦克风。
            captureProblem = "系统拒绝了本机声音采集（" + t + "）";
            return null;
        }
        for (int[] fmt : playbackFormats()) {
            int rate = fmt[0];
            int ch = fmt[1];
            AudioRecord rec = null;
            try {
                int mask = (ch == 1) ? AudioFormat.CHANNEL_IN_MONO : AudioFormat.CHANNEL_IN_STEREO;
                int minBuf = AudioRecord.getMinBufferSize(rate, mask, AudioFormat.ENCODING_PCM_16BIT);
                if (minBuf <= 0) {
                    minBuf = FRAME_BYTES * 4;
                }
                // 硬件缓冲至少留 4 块（40ms），防止线程被调度走时采集溢出丢样本。
                int bufBytes = Math.max(minBuf, FRAME_BYTES * 4);
                AudioFormat af = new AudioFormat.Builder()
                        .setSampleRate(rate)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setChannelMask(mask)
                        .build();
                rec = new AudioRecord.Builder()
                        .setAudioFormat(af)
                        .setBufferSizeInBytes(bufBytes)
                        .setAudioPlaybackCaptureConfig(cfg)   // ★ 这一行就是"采本机声音"
                        .build();
                if (rec.getState() == AudioRecord.STATE_INITIALIZED) {
                    Resampler rs = (rate == Proto.SAMPLE_RATE && ch == Proto.CHANNELS)
                            ? null : new Resampler(rate, ch, Proto.FRAME_SAMPLES);
                    Log.i(TAG, "本机声音采集已打开：" + rate + "Hz/" + ch + "ch"
                            + (rs == null ? "（零拷贝）" : "（发送前换算）"));
                    return new Capture(rec, rs, SRC_PLAYBACK, rate, ch, -1);
                }
                rec.release();
                rec = null;
                Log.w(TAG, "本机声音采集 " + rate + "Hz/" + ch + "ch 打不开，试下一个格式");
            } catch (Throwable t) {
                if (rec != null) {
                    try {
                        rec.release();
                    } catch (Throwable ignored) {
                    }
                }
                captureProblem = "本机声音采集打不开（" + t + "）";
                Log.w(TAG, "本机声音采集失败 " + rate + "Hz/" + ch + "ch", t);
            }
        }
        if (captureProblem.length() == 0) {
            captureProblem = "这台设备的系统声音采集通路打不开（有些厂商 ROM 阉割了 AudioPlaybackCapture）";
        }
        return null;
    }

    /** 本机声音的候选格式：48000/立体声 → 48000/单声道 → 设备原生采样率的立体声/单声道。 */
    private int[][] playbackFormats() {
        int devRate = 0;                    // 设备音频通路的原生采样率（变量名不能叫 native，那是关键字）
        try {
            AudioManager am = (AudioManager) appCtx.getSystemService(Context.AUDIO_SERVICE);
            if (am != null) {
                String s = am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE);
                if (s != null && s.trim().length() > 0) {
                    devRate = Integer.parseInt(s.trim());
                }
            }
        } catch (Throwable ignored) {
            // 拿不到就用默认，不影响主路径
        }
        if (devRate <= 0 || devRate == Proto.SAMPLE_RATE) {
            return new int[][]{{Proto.SAMPLE_RATE, 2}, {Proto.SAMPLE_RATE, 1}};
        }
        return new int[][]{
                {Proto.SAMPLE_RATE, 2},
                {Proto.SAMPLE_RATE, 1},
                {devRate, 2},
                {devRate, 1},
        };
    }

    /** 打开麦克风（原来的那条路，一行逻辑都没改）。 */
    private Capture openMicCapture() {
        int minBuf;
        try {
            minBuf = AudioRecord.getMinBufferSize(Proto.SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_STEREO, AudioFormat.ENCODING_PCM_16BIT);
        } catch (Throwable t) {
            minBuf = AudioRecord.ERROR;
        }
        if (minBuf <= 0) {
            // 极少见：有些 ROM 在 specific 组合下返回 ERROR。给个保守值试一把。
            minBuf = FRAME_BYTES * 4;
        }
        // 硬件缓冲至少留 4 块（40ms），防止线程被调度走时采集溢出丢样本。
        int bufBytes = Math.max(minBuf, FRAME_BYTES * 4);
        AudioFormat fmt = new AudioFormat.Builder()
                .setSampleRate(Proto.SAMPLE_RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_IN_STEREO)
                .build();
        // 刻意不设 AudioAttributes：AudioRecord.Builder 会按音源推导属性，
        // 两个都设反而在部分版本上把音源（也就是回声消除）丢掉，甚至直接抛异常。
        for (int src : MIC_SOURCE_CHAIN) {
            AudioRecord rec = null;
            try {
                rec = new AudioRecord.Builder()
                        .setAudioSource(src)
                        .setAudioFormat(fmt)
                        .setBufferSizeInBytes(bufBytes)
                        .build();
                if (rec.getState() == AudioRecord.STATE_INITIALIZED) {
                    return new Capture(rec, null, SRC_MIC, Proto.SAMPLE_RATE, Proto.CHANNELS, src);
                }
                rec.release();
            } catch (Throwable t) {
                // 权限没给会抛 SecurityException；某些 ROM 对特定音源直接抛。
                if (rec != null) {
                    try {
                        rec.release();
                    } catch (Throwable ignored) {
                    }
                }
                note("音源 " + src + " 打不开：" + t);
            }
        }
        captureProblem = "没有可用的麦克风音源（RECORD_AUDIO 权限给了吗？）";
        return null;
    }

    /**
     * 就地把 PCM16 乘上 Q14 增益（1.0 = 16384 不动样本）。
     * 在发送端做音量有个好处：接收端不需要知道发送端的滑杆位置，
     * 协议里的 gain_q14 仍然是"这条流现在多大音量"的如实描述。
     *
     * @return 加完增益后的峰值 0.0~1.0
     */
    private static float applyGain(byte[] buf, int gainQ14) {
        int peakAbs = 0;
        for (int i = 0; i + 1 < buf.length; i += 2) {
            int v = (buf[i] & 0xFF) | ((buf[i + 1] & 0xFF) << 8);
            v = (short) v;                                  // 还原成有符号 16 位
            int g = (v * gainQ14) >> 14;
            if (g > 32767) {
                g = 32767;                                  // 必须削顶，回绕会变成刺耳的爆音
            } else if (g < -32768) {
                g = -32768;
            }
            buf[i] = (byte) g;
            buf[i + 1] = (byte) (g >> 8);
            int a = g < 0 ? -g : g;
            if (a > peakAbs) {
                peakAbs = a;
            }
        }
        return peakAbs / 32768f;
    }

    /** 加增益之前的峰值（判断"是不是真的没声音"必须用它，不能用被音量滑杆影响的那个值）。 */
    private static float rawPeak(byte[] buf) {
        int peakAbs = 0;
        for (int i = 0; i + 1 < buf.length; i += 2) {
            int v = (short) ((buf[i] & 0xFF) | ((buf[i + 1] & 0xFF) << 8));
            int a = v < 0 ? -v : v;
            if (a > peakAbs) {
                peakAbs = a;
            }
        }
        return peakAbs / 32768f;
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
            int mode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                    ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                    : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
            WifiManager.WifiLock l = wm.createWifiLock(mode, "audiomixing-send");
            l.setReferenceCounted(false);   // 引用计数自己管，避免被别的组件顺手 release
            l.acquire();
            wifiLock = l;
        } catch (Exception e) {
            note("WifiLock 获取失败（继续发送，只是息屏后可能变抖）：" + e);
        }
    }

    private void releaseAll() {
        closeRecord();
        // 发送 socket 归 SendFanout 管；这里一起收掉（目标集合保留，下次 start 会重开 socket）
        fanout.close();
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
        peak = 0f;
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

    // ══════════════════════════ 内部小结构 ══════════════════════════

    /** 打开成功的一份采集：AudioRecord + 是否需要换算 + 实际格式。 */
    private static final class Capture {
        final AudioRecord rec;
        final Resampler rs;      // null = 采集格式就是协议格式（零拷贝）
        final int kind;          // SRC_MIC / SRC_PLAYBACK
        final int rate;
        final int channels;
        final int micSource;     // 麦克风音源（MediaRecorder.AudioSource.*），本机声音时是 -1

        Capture(AudioRecord rec, Resampler rs, int kind, int rate, int channels, int micSource) {
            this.rec = rec;
            this.rs = rs;
            this.kind = kind;
            this.rate = rate;
            this.channels = channels;
            this.micSource = micSource;
        }
    }

    /**
     * 采集格式 → 协议格式（48000Hz 立体声 PCM16）的换算。
     *
     * <p><b>为什么需要它</b>：协议里每包固定 480 帧 / 48000Hz，而个别设备（尤其是被厂商改过的
     * 系统声音采集通路）只给得出 44100Hz 或单声道。直接把 44100 的样本当 48000 发出去，
     * 对端听到的会整体变调、包的时间也对不上 —— 所以这里做线性插值重采样 + 单声道复制。
     *
     * <p><b>绝大多数设备根本走不到这里</b>：48000/立体声能直接打开时用的是零拷贝那条路，
     * 这个类连对象都不会建。
     */
    private static final class Resampler {
        private final int srcCh;        // 1 或 2
        private final double ratio;     // 输入帧数 / 输出帧数（<1 = 升采样）
        private final short[] in;       // 交错的输入样本
        private final int capFrames;    // in 最多装多少帧
        private int frames;             // in 里的有效帧数
        private double phase;           // 下一个输出帧对应的输入位置（单位：帧，可含小数）

        Resampler(int srcRate, int srcCh, int outFrames) {
            this.srcCh = (srcCh == 1) ? 1 : 2;
            this.ratio = srcRate / (double) Proto.SAMPLE_RATE;
            // 每轮最多用到「相位 + (outFrames-1)*ratio + 插值要的下一帧」，再留几个富余。
            this.capFrames = (int) Math.ceil(outFrames * ratio) + 8;
            this.in = new short[capFrames * this.srcCh];
        }

        /**
         * 从 {@code rec} 取够数据，往 {@code out} 里写满 {@code outFrames} 帧 48000Hz 立体声。
         *
         * @return 本轮读到的输入帧数（>0 正常）；AudioRecord 的错误码（<0）原样返回，
         *         让调用方的"重建采集"逻辑照旧生效。
         */
        int fill(AudioRecord rec, byte[] out, int outFrames) {
            int need = (int) Math.floor(phase + (outFrames - 1) * ratio) + 2;   // 插值要用到 i0 和 i0+1
            if (need > capFrames) {
                need = capFrames;
            }
            int got = 0;
            int lastErr = 0;
            for (int attempt = 0; frames < need && attempt < 8; attempt++) {
                int room = (capFrames - frames) * srcCh;
                if (room <= 0) {
                    break;
                }
                int n;
                try {
                    // 注意：这个重载返回的是 short 的个数，不是帧数。
                    n = rec.read(in, frames * srcCh, room);
                } catch (Throwable t) {
                    lastErr = AudioRecord.ERROR_INVALID_OPERATION;
                    break;
                }
                if (n < 0) {
                    lastErr = n;
                    break;
                }
                int f = n / srcCh;
                if (f <= 0) {
                    break;
                }
                frames += f;
                got += f;
            }
            if (got == 0 && lastErr < 0 && frames < need) {
                return lastErr;                 // 真的出错了，交给上层按错误码处理
            }
            if (frames <= 0) {
                Arrays.fill(out, 0, outFrames * 4, (byte) 0);
                return 0;
            }

            final int maxIdx = frames - 1;
            for (int i = 0, o = 0; i < outFrames; i++, o += 4) {
                double p = phase + i * ratio;
                int i0 = (int) p;                       // p >= 0，直接截断就是 floor
                if (i0 < 0) {
                    i0 = 0;
                } else if (i0 > maxIdx) {
                    i0 = maxIdx;
                }
                int i1 = (i0 + 1 > maxIdx) ? maxIdx : i0 + 1;
                double f = p - i0;
                if (f < 0) {
                    f = 0;
                } else if (f > 1) {
                    f = 1;                              // 数据没跟上时保持最后一帧，不产生爆音
                }
                int l;
                int r;
                if (srcCh == 1) {
                    l = r = lerp(in[i0], in[i1], f);    // 单声道复制成两声道，和电脑端一致
                } else {
                    l = lerp(in[i0 * 2], in[i1 * 2], f);
                    r = lerp(in[i0 * 2 + 1], in[i1 * 2 + 1], f);
                }
                out[o] = (byte) l;
                out[o + 1] = (byte) (l >> 8);
                out[o + 2] = (byte) r;
                out[o + 3] = (byte) (r >> 8);
            }

            // 收尾：把用过的整帧丢掉，小数相位留到下一轮，保证长时间跑不会漂。
            double consumed = phase + outFrames * ratio;
            int drop = (int) Math.floor(consumed);
            if (drop > frames) {
                drop = frames;
            }
            if (drop > 0) {
                System.arraycopy(in, drop * srcCh, in, 0, (frames - drop) * srcCh);
                frames -= drop;
                phase = consumed - drop;
            } else {
                phase = consumed;
            }
            return got;
        }

        private static int lerp(short a, short b, double f) {
            return (int) Math.round(a + (b - a) * f);
        }
    }
}
