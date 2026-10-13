package com.audiomixing.app;

import android.os.SystemClock;
import android.util.Log;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * ★ 一发多：<b>一次采集、一包编码 → 分发给 N 个目标</b>。
 *
 * <p><b>为什么单独一个类</b>（2026-10-14 用户反馈「没有发广播」「每台设备都要有独立的收/送电平条」）：
 * 这两条其实是同一个底层能力 —— 手机端要能"把我的声音同时送给好几台"。原来的 AudioSend 是
 * <b>一个目标 + 一个 socket</b> 写死的，做不到；而把分发逻辑单独拎出来之后：
 * <ul>
 *   <li>它<b>只依赖 java.net</b>（外加一个 SystemClock 取时间），所以可以拿真实 UDP 回环端口
 *       做离线门禁 —— 一包分给 3 个目标、目标中途下线、目标集合变化不重启，全都能断言；</li>
 *   <li>AudioSend 只管"采集 + 编码"，不再关心有几个人在听。</li>
 * </ul>
 *
 * <p><b>三件绝不允许发生的事</b>：
 * <ol>
 *   <li><b>不重复采集</b>：采集线程永远只有一个，目标数变化不碰麦克风/MediaProjection；</li>
 *   <li><b>不重复编码</b>：一包只编码一次，所有目标发的是<b>同一个字节数组</b>；</li>
 *   <li><b>一个目标坏掉不能拖垮别人</b>：单个目标发送失败只记它自己的错误，其余照发。</li>
 * </ol>
 *
 * <p><b>带宽的实话</b>：PCM16 立体声 48kHz = 48000×2×2 = 192000 字节/秒 ≈ <b>0.19 MB/s（1.5 Mbit/s）
 * 每个目标</b>。N 个目标就是 N 份 —— 这个数目本身不大（4 台 ≈ 0.77 MB/s），真正先吃紧的是
 * 另外两件事：① 每个 1948 字节的包会被 IP 层拆成 2 个分片（MTU 1500），目标越多空口包数越多；
 * ② 手机 CPU 要为每个目标做一次系统调用。所以界面上给的是真实数字 + 一句"目标多时可能卡"，
 * 而不是编一个更吓人的带宽。
 */
public final class SendFanout {

    private static final String TAG = "AM-SendFanout";

    /** 单个目标的实时状态（给界面"送"电平条和诊断报告用）。 */
    public static final class TargetStats {
        public String host = "";
        public int port;
        /** 发给它多少包 / 多少字节 / 失败多少次。 */
        public long packetsSent;
        public long bytesSent;
        public int sendErrors;
        /**
         * 这一路"我送给它"的电平 0~1。
         *
         * <p>注意：所有目标收到的音频内容是<b>同一份</b>（就是"一次编码发多份"的意义），
         * 所以正常情况下各路的电平理应一样。这里之所以按目标分别衰减：某个目标
         * 超过 1 秒没发成功，它的电平就归 0 —— 这一路的条子会塌下去，一眼看出"这台没在收"。
         */
        public float peak;
        /** 最近一次发送成功距今多少毫秒；-1 = 从来没成功过。 */
        public long lastOkAgeMs = -1;
        public String lastError = "";
        /** ★ 这一路的增益（"我送它的"滑杆），1.00 = 原样。 */
        public float gain = 1f;
    }

    /** 一个目标。字段只由发送线程改；volatile 的部分给界面线程读。 */
    private static final class Target {
        final String key;          // "host:port"，也是去重的键
        final String host;
        final int port;
        InetAddress addr;          // 解析后缓存（解析失败会置空，下一包重试）
        DatagramPacket out;        // 复用的包头对象（10ms 一趟的循环里不产生垃圾）
        volatile long packetsSent;
        volatile long bytesSent;
        volatile int sendErrors;
        volatile long lastSendOkMs;
        volatile float peak;
        volatile String lastError = "";
        /**
         * ★ 这一路自己的增益（"我送它的"滑杆，1.0 = 原样）。
         *
         * <p>各目标可以不一样，所以增益不同的目标需要**各自一份缩放后的包**
         * （见 {@link #send}）。全都是 1.0 时走的是老路：一包编码一次、所有目标发同一份。
         */
        volatile float gain = 1f;

        Target(String host, int port) {
            this.host = host;
            this.port = port;
            this.key = host + ":" + port;
        }
    }

    private final Object lock = new Object();
    private final Map<String, Target> targets = new LinkedHashMap<String, Target>();
    private volatile DatagramSocket socket;
    private volatile long packetsSent;
    private volatile long bytesSent;
    private volatile int sendErrors;
    private volatile String lastError = "";

    /**
     * 设定目标集合（可以随时调，采集线程不会因此重启）。
     *
     * <p>已存在的目标<b>保持原对象</b>（统计不清零、socket 也不重开）；新目标加进来、
     * 消失的目标移出去。这就是"对方上线/下线不打断正在送的声音"。
     *
     * @param endpoints "host:port" 列表；传空 = 谁都不发
     */
    public void setTargets(Collection<String> endpoints) {
        synchronized (lock) {
            if (endpoints == null || endpoints.isEmpty()) {
                targets.clear();
                return;
            }
            Map<String, Target> next = new LinkedHashMap<String, Target>();
            for (String ep : endpoints) {
                if (ep == null || ep.isEmpty()) {
                    continue;
                }
                String host = ep;
                int port = Proto.DEFAULT_AUDIO_PORT;
                int colon = ep.lastIndexOf(':');
                if (colon > 0) {
                    host = ep.substring(0, colon);
                    try {
                        port = Integer.parseInt(ep.substring(colon + 1).trim());
                    } catch (Throwable ignored) {
                        port = Proto.DEFAULT_AUDIO_PORT;
                    }
                }
                if (host.isEmpty()) {
                    continue;
                }
                String key = host + ":" + port;
                Target t = targets.get(key);
                if (t == null) {
                    t = new Target(host, port);
                    // 用户给这台设过的增益要带上（它下线又上线，滑杆不该跳回 1.00）
                    Float dg = desiredGain.get(host);
                    if (dg != null) {
                        t.gain = dg;
                    }
                }
                next.put(key, t);
            }
            targets.clear();
            targets.putAll(next);
        }
    }

    /** 目标集合的字符串（诊断报告用）。 */
    public String targetText() {
        synchronized (lock) {
            if (targets.isEmpty()) {
                return "（无）";
            }
            StringBuilder sb = new StringBuilder(96);
            for (Target t : targets.values()) {
                if (sb.length() > 0) {
                    sb.append(" / ");
                }
                sb.append(t.key);
            }
            return sb.toString();
        }
    }

    public int targetCount() {
        synchronized (lock) {
            return targets.size();
        }
    }

    // ────────────── ★ 逐目标增益（"我送它的"滑杆） ──────────────

    /**
     * 设定某一台的发送增益（0~1.5，1.0 = 原样）。立刻生效。
     *
     * <p>找不到目标也没关系：值会被记下来，那台设备下次上线时自动带上（用户设过就不该丢）。
     */
    public void setTargetGain(String host, float g) {
        if (host == null || host.isEmpty()) {
            return;
        }
        float v = Math.max(0f, Math.min(1.5f, g));
        synchronized (lock) {
            for (Target t : targets.values()) {
                if (host.equals(t.host)) {
                    t.gain = v;
                }
            }
            desiredGain.put(host, v);
        }
    }

    /** 某一台现在的发送增益。 */
    public float targetGain(String host) {
        if (host == null) {
            return 1f;
        }
        synchronized (lock) {
            for (Target t : targets.values()) {
                if (host.equals(t.host)) {
                    return t.gain;
                }
            }
            Float v = desiredGain.get(host);
            return v == null ? 1f : v;
        }
    }

    /** 用户设过的增益（目标还没上线也要记住）。 */
    private final Map<String, Float> desiredGain = new java.util.HashMap<String, Float>();
    /** 按增益缓存"缩放后的包"（跨包复用，避免每 10ms 分配）。 */
    private java.util.Map<Float, byte[]> scaledBufs;

    /**
     * 把 PCM16 样本乘上增益；超过 32767 削顶（回绕会变成刺耳的爆音）。
     *
     * <p>和接收侧那套（{@code AudioRecv.Stream#scalePcm16}）是同一个数学，
     * 但两边各自独立 —— 一处是"我听它的"，一处是"我送它的"。
     */
    private static void scalePcm16(byte[] src, byte[] dst, int len, float g) {
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
     * 把同一包发给所有目标。
     *
     * <p><b>快路径（绝大多数情况）</b>：所有目标的增益都是 1.00 → 一包编码一次，
     * 所有目标发的是<b>同一个数组</b>，零额外拷贝。
     * <b>慢路径</b>：某个目标设了自己的增益（"我送它的"滑杆），那就为那一路准备一份
     * 缩放后的包（同一个增益值的目标共用一份），其余目标照旧用原包。
     *
     * @param pkt  已经编码好的整包
     * @param len  有效长度
     * @param peak 这一包的电平（1.00 的目标用它；设了增益的目标按增益折算）
     * @return 成功发出去的目标数
     */
    public int send(byte[] pkt, int len, float peak) {
        if (pkt == null || len <= 0) {
            return 0;
        }
        DatagramSocket s = ensureSocket();
        if (s == null) {
            sendErrors++;
            return 0;
        }
        List<Target> list;
        synchronized (lock) {
            if (targets.isEmpty()) {
                return 0;
            }
            list = new ArrayList<Target>(targets.values());
        }
        long now = SystemClock.elapsedRealtime();
        int ok = 0;
        // 这一包用到的"按增益缩放过的副本"（同一个增益只做一份，缓冲跨包复用，不产生垃圾）
        if (scaledBufs == null) {
            scaledBufs = new java.util.HashMap<Float, byte[]>(4);
        }

        for (Target t : list) {
            float g = t.gain;
            byte[] data = pkt;
            float p = peak;
            if (g != 1f) {
                data = scaledBufs.get(g);
                if (data == null || data.length < len) {
                    data = new byte[len];
                    scaledBufs.put(g, data);
                    if (scaledBufs.size() > 8) {
                        // 增益种类太多（不可能）：清掉重来，防止无限长
                        scaledBufs.clear();
                        scaledBufs.put(g, data);
                    }
                }
                scalePcm16(pkt, data, len, g);
                p = Math.min(1f, peak * g);
            }
            try {
                if (t.addr == null) {
                    t.addr = InetAddress.getByName(t.host);
                    t.out = null;
                }
                if (t.out == null || t.out.getLength() != len) {
                    // 地址变了或长度变了才重建包头；否则只换负载（10ms 一趟的循环里别产生垃圾）
                    t.out = new DatagramPacket(data, len, t.addr, t.port);
                } else {
                    t.out.setData(data, 0, len);
                }
                s.send(t.out);
                t.packetsSent++;
                t.bytesSent += len;
                t.lastSendOkMs = now;
                t.peak = p;
                ok++;
            } catch (Throwable e) {
                // 单个目标失败（对端走了、地址解析不了、网卡抽风）绝不能影响别的目标。
                t.sendErrors++;
                t.lastError = String.valueOf(e);
                t.addr = null;                 // 下一包重新解析一次
                t.out = null;
                if (t.sendErrors % 100 == 1) {
                    Log.w(TAG, "发给 " + t.key + " 失败：" + e);
                }
            }
        }
        if (ok > 0) {
            packetsSent++;
            bytesSent += (long) len * ok;
        } else {
            sendErrors++;
            lastError = "这一包一个目标都没发出去（" + list.size() + " 个目标）";
        }
        return ok;
    }

    /** 逐目标的快照（界面每 150ms 读一次，几台设备而已，直接新建小对象）。 */
    public List<TargetStats> stats() {
        long now = SystemClock.elapsedRealtime();
        List<TargetStats> out = new ArrayList<TargetStats>();
        synchronized (lock) {
            for (Target t : targets.values()) {
                TargetStats st = new TargetStats();
                st.host = t.host;
                st.port = t.port;
                st.packetsSent = t.packetsSent;
                st.bytesSent = t.bytesSent;
                st.sendErrors = t.sendErrors;
                st.lastError = t.lastError;
                st.gain = t.gain;
                long edad = t.lastSendOkMs <= 0 ? -1 : (now - t.lastSendOkMs);
                st.lastOkAgeMs = edad;
                // 超过 1 秒没成功发出去 → 这一路的电平归零（"这台没在收"一眼可见）
                st.peak = (edad >= 0 && edad <= 1000) ? t.peak : 0f;
                out.add(st);
            }
        }
        return out;
    }

    /** 某台设备这一路的发送电平（界面"送"电平条用；没在送它就是 0）。 */
    public float peakFor(String host) {
        if (host == null || host.isEmpty()) {
            return 0f;
        }
        long now = SystemClock.elapsedRealtime();
        synchronized (lock) {
            for (Target t : targets.values()) {
                if (host.equals(t.host)) {
                    long edad = t.lastSendOkMs <= 0 ? -1 : (now - t.lastSendOkMs);
                    return (edad >= 0 && edad <= 1000) ? t.peak : 0f;
                }
            }
        }
        return 0f;
    }

    public long packetsSent() {
        return packetsSent;
    }

    public long bytesSent() {
        return bytesSent;
    }

    public int sendErrors() {
        return sendErrors;
    }

    public String lastError() {
        return lastError;
    }

    /** 正在用的 socket（重复调用返回同一个；关掉之后会重开一个）。 */
    private DatagramSocket ensureSocket() {
        DatagramSocket s = socket;
        if (s != null && !s.isClosed()) {
            return s;
        }
        synchronized (lock) {
            if (socket != null && !socket.isClosed()) {
                return socket;
            }
            try {
                // 不绑端口：发送用临时端口就够。注意这个调用发生在采集线程里
                // （安卓禁止主线程碰网络，所以绝不在这里之外的线程建它）。
                DatagramSocket fresh = new DatagramSocket();
                fresh.setSendBufferSize(64 * 1024);   // 减少 sendto 排队阻塞
                socket = fresh;
                Log.i(TAG, "发送 socket 就绪，目标 " + targets.size() + " 个");
                return fresh;
            } catch (Throwable e) {
                lastError = "UDP socket 打不开：" + e;
                Log.w(TAG, lastError);
                return null;
            }
        }
    }

    /** 关掉 socket（采集线程退出时调）。目标集合保留，重新 start 时还能用。 */
    public void close() {
        DatagramSocket s;
        synchronized (lock) {
            s = socket;
            socket = null;
        }
        if (s != null) {
            try {
                s.close();
            } catch (Throwable ignored) {
            }
        }
    }
}
