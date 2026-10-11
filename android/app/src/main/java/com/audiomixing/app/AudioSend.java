package com.audiomixing.app;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.util.Log;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.util.Arrays;

/**
 * 采集 + 打包 + UDP 单播：把本机麦克风变成一条 UDP 音频流。
 *
 * <p>参数固定 48000Hz / 立体声 / PCM16，一次读 480 帧（10 毫秒）打一个包。
 * <b>为什么不用 Opus</b>：PCM16 立体声 48k 是 1536 KB/s，Wi-Fi 上几台设备完全吃得下；
 * 换来的是不用 NDK、不用 libopus、装机包小、也没了编解码的额外延迟。
 * 手机之间用 PCM16，和电脑端 {@code codec=3(Opus)} 那条路是分开的。
 *
 * <p><b>线程</b>：{@link #start()} 只负责起线程并立刻返回。采集和发socket都在工作线程里，
 * 因为 {@code AudioRecord.read()} 会阻塞 10ms、{@code send()} 也可能被 Wi-Fi 拖住，
 * 界面线程一秒都不能卡。
 *
 * <p><b>时钟</b>：{@code ts_ns} 用墙钟（{@code System.currentTimeMillis() * 1e6}），
 * 和电脑端 {@code time.time_ns()} 同源；接收端主要靠"到达顺序 + 预缓冲"播放，
 * 这个时间戳用于统计和对齐参考。
 */
public final class AudioSend {

    private static final String TAG = "AM-AudioSend";

    /** 一包的负载字节数：480 帧 × 2 声道 × 2 字节 = 1920。 */
    public static final int FRAME_BYTES = Proto.FRAME_SAMPLES * Proto.CHANNELS * 2;

    /**
     * 麦克风优先级：通话音源自带回声消除/自动增益，手机外放时最不容易啸叫，
     * 所以设计文档选它；有些机器不认这个音源（或没给权限），逐级退回。
     */
    private static final int[] SOURCE_CHAIN = {
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.DEFAULT,
    };

    private final Context appCtx;          // 可以为 null：拿不到就不锁 Wi-Fi，功能照旧
    private volatile String host;
    private volatile int port;

    private final Object lifeLock = new Object();
    private volatile boolean running;
    private Thread worker;
    private volatile AudioRecord record;
    private volatile DatagramSocket socket;
    private WifiManager.WifiLock wifiLock;

    private volatile int gainQ14 = Proto.GAIN_ONE;
    private volatile float peak;
    private volatile long packetsSent;
    private volatile long bytesSent;
    private volatile int sendErrors;
    private volatile String lastError = "";
    private volatile int audioSourceUsed = -1;
    private int ssrc;

    public AudioSend(String host, int port) {
        this(null, host, port);
    }

    /**
     * 带 Context 的版本会顺手拿一个 {@code WifiLock}：息屏后 Wi-Fi 进省电模式时，
     * 发包会被攒到 DTIM 周期才出去，听感就是"一顿一顿的"。前台服务能保住进程，
     * 但保不住 Wi-Fi 射频，所以这里要单独锁一下。
     */
    public AudioSend(Context context, String host, int port) {
        this.appCtx = context == null ? null : context.getApplicationContext();
        this.host = host;
        this.port = port;
        this.ssrc = new java.util.Random().nextInt();
        if (this.ssrc == 0) {
            this.ssrc = 1;
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

    /** 停止发送并释放麦克风。采集线程最多阻塞 10ms，所以这里 join 很快。 */
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
        releaseAll();   // 万一线程没退干净，这里兜底释放，麦克风不能一直被占着
    }

    public boolean isRunning() {
        return running;
    }

    /**
     * 实时电平 0.0~1.0（峰值，快起慢落），界面画电平条用。
     * 取的是<b>加过音量之后</b>的值，也就是真正发出去的电平。
     */
    public float peak() {
        return peak;
    }

    /** 发送增益，0.0~2.0（1.0 = 原样）。界面"本机音量"滑杆接这里。 */
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

    /** 运行中也能改目标（对端换 IP、用户改勾选都用得上）。 */
    public void setTarget(String host, int port) {
        this.host = host;
        this.port = port;
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

    /** 实际打开的麦克风音源（MediaRecorder.AudioSource.*），-1 表示没打开。 */
    public int audioSource() {
        return audioSourceUsed;
    }

    public String lastError() {
        return lastError;
    }

    // ------------------------------------------------------------- 工作线程

    private void loop() {
        acquireWifiLock();
        try {
            DatagramSocket s = new DatagramSocket();   // 不绑端口：发送用临时端口就够
            s.setSendBufferSize(64 * 1024);            // 减少 sendto 排队阻塞
            socket = s;
        } catch (IOException e) {
            note("UDP socket 打不开：" + e);
            running = false;
            releaseAll();
            return;
        }

        AudioRecord rec = openRecord();
        if (rec == null) {
            running = false;
            releaseAll();
            return;
        }
        record = rec;
        rec.startRecording();
        if (rec.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
            note("麦克风 startRecording 失败（权限被拒？）");
            running = false;
            releaseAll();
            return;
        }
        Log.i(TAG, "开始发送到 " + host + ":" + port + " 音源=" + audioSourceUsed + " ssrc=" + ssrc);

        // 复用同一份缓冲和包头对象：每 10ms 跑一趟的循环里尽量不产生垃圾，
        // 否则一次 GC 停顿就是一次可听见的爆音。
        byte[] frame = new byte[FRAME_BYTES];
        Proto.Pkt pkt = new Proto.Pkt(Proto.T_AUDIO);
        pkt.codec = Proto.CODEC_PCM_S16;
        pkt.samples = Proto.FRAME_SAMPLES;
        pkt.ssrc = ssrc;
        pkt.payload = frame;
        int seq = 0;
        DatagramPacket out = null;
        String outHost = null;
        int outPort = -1;
        int consecutiveErrors = 0;
        int reopenCount = 0;

        try {
            while (running) {
                int n = rec.read(frame, 0, FRAME_BYTES);
                if (n <= 0) {
                    if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_BAD_VALUE
                            || n == AudioRecord.ERROR_DEAD_OBJECT) {
                        if (consecutiveErrors++ > 20) {
                            // 音频服务重启（ERROR_DEAD_OBJECT）是会被用户感觉到的常见故障：
                            // 重建一次麦克风就能继续，而不是让"送声音"这个开关悄悄失效。
                            if (reopenCount++ > 5 || !reopenRecord()) {
                                note("麦克风反复打开失败，停止发送：" + n);
                                break;
                            }
                            rec = record;
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
                if (n < FRAME_BYTES) {
                    // 短读：补齐成固定 480 帧，接收端就不用处理变长包（丢的是几毫秒静音，听不出来）。
                    Arrays.fill(frame, n, FRAME_BYTES, (byte) 0);
                }
                float p = applyGain(frame, gainQ14);
                // 快起慢落，跟真实电平表的观感一致（否则数字每秒跳 100 次，根本看不清）。
                peak = p >= peak ? p : peak * 0.82f;

                pkt.seq = seq++ & 0xFFFF;                       // uint16 回绕
                pkt.tsNs = System.currentTimeMillis() * 1_000_000L;
                pkt.gainQ14 = gainQ14;
                byte[] raw = Proto.encode(pkt);
                String h = host;
                int pt = port;
                try {
                    if (out == null || out.getLength() != raw.length || outPort != pt
                            || outHost == null || !outHost.equals(h)) {
                        // 目标变了（用户改勾选）就重新解析地址，不能继续往老对端发。
                        out = new DatagramPacket(raw, raw.length, InetAddress.getByName(h), pt);
                        outHost = h;
                        outPort = pt;
                    } else {
                        out.setData(raw, 0, raw.length);        // 地址不变，只换负载
                    }
                    socket.send(out);
                    packetsSent++;
                    bytesSent += raw.length;
                } catch (IOException | RuntimeException e) {
                    sendErrors++;
                    // 对端还没起来/刚走，这里报错很正常，不要因此退出线程。
                    if (sendErrors % 100 == 1) {
                        note("发送失败：" + e);
                    }
                    out = null;                                  // 目标地址重新解析一次
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

    /**
     * 音频服务重启/设备被抢占之后重新打开麦克风。
     * 只在采集线程里调用：先把旧对象放掉，再按同一套音源优先级重开一个。
     */
    private boolean reopenRecord() {
        AudioRecord old = record;
        record = null;
        if (old != null) {
            try {
                old.stop();
            } catch (Throwable ignored) {
            }
            try {
                old.release();
            } catch (Throwable ignored) {
            }
        }
        AudioRecord fresh = openRecord();
        if (fresh == null) {
            return false;
        }
        fresh.startRecording();
        if (fresh.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
            try {
                fresh.release();
            } catch (Throwable ignored) {
            }
            note("重开的麦克风启动失败");
            return false;
        }
        record = fresh;
        Log.i(TAG, "麦克风已重建，继续发送");
        return true;
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

    /** 依次尝试音源，返回第一个真正初始化成功的 AudioRecord。 */
    private AudioRecord openRecord() {        int minBuf;
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
        for (int src : SOURCE_CHAIN) {
            AudioRecord rec = null;
            try {
                rec = new AudioRecord.Builder()
                        .setAudioSource(src)
                        .setAudioFormat(fmt)
                        .setBufferSizeInBytes(bufBytes)
                        .build();
                if (rec.getState() == AudioRecord.STATE_INITIALIZED) {
                    audioSourceUsed = src;
                    return rec;
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
        note("没有可用的麦克风音源（RECORD_AUDIO 权限给了吗？）");
        return null;
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
        AudioRecord rec = record;
        record = null;
        if (rec != null) {
            try {
                if (rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    rec.stop();
                }
            } catch (Throwable ignored) {
            }
            try {
                rec.release();
            } catch (Throwable ignored) {
            }
        }
        DatagramSocket s = socket;
        socket = null;
        if (s != null) {
            try {
                s.close();
            } catch (Throwable ignored) {
            }
        }
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
}
