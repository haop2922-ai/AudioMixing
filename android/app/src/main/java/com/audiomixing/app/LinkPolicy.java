package com.audiomixing.app;

/**
 * 链路策略：全是<b>纯函数</b>，没有安卓依赖 —— 所以能拿离线门禁一条条断言。
 *
 * <p>为什么要单独一个类（2026-10-14 用户四条反馈）：这三件事都是"什么时候该做什么"的判断，
 * 埋在 Engine 里就只能靠真机试；抽出来之后，边界（宽限期、抖帧、用户撤销、两端同时调音量）
 * 都能在 JVM 上验。
 *
 * <ol>
 *   <li><b>④ 对端是不是走了</b>：用户原话「手机蓝牙向电脑传输音频的情况下，电脑关闭应用，
 *       手机声音还在向电脑发送」—— 这是安全问题（对着一个已经关掉的程序一直发）。</li>
 *   <li><b>① 要不要自动收听</b>：对方点「送给它」时，请求我自动打开收听（可撤销）。</li>
 *   <li><b>① 音量条两端同步</b>：一个共享增益，谁后改以谁为准。</li>
 * </ol>
 */
public final class LinkPolicy {

    private LinkPolicy() {
    }

    // ══════════════════ ④ 对端是否"确认走了" ══════════════════

    /**
     * 宽限期：这么久没有任何消息才认为对端走了。
     *
     * <p>为什么是 18 秒（用户要求 15~20 秒之间）：
     * <ul>
     *   <li>信标 2 秒一个、发现层 15 秒超时 → 只靠信标的话，Wi-Fi 抖几下就会误判；</li>
     *   <li>心跳（T_STATS）2 秒一次、6 秒算过期 → 两个判据合起来，18 秒足够躲开一次
     *       Wi-Fi 抽风（几秒的丢帧），又不至于"电脑关了还在发"太久；</li>
     *   <li>取的是<b>两个判据里更保守的那个</b>（只要有一个还新鲜就算活着）。</li>
     * </ul>
     */
    public static final long GRACE_MS = 18000L;
    /** 信标多久算过期（和 Discovery 的 15 秒一致）。 */
    public static final long BEACON_TIMEOUT_MS = 15000L;
    /** 心跳（T_STATS）多久算过期（和 Diagnosis.PEER_TTL_MS 一致）。 */
    public static final long STATS_TIMEOUT_MS = 6000L;

    /** 没见过 = 用这个值表示（负年龄没法参与比较）。 */
    public static final long NEVER = -1L;

    /**
     * 对端还活着吗（任一判据新鲜即可）。
     *
     * @param beaconAgeMs 最近一次收到它信标距今多少毫秒；{@link #NEVER} = 没见过
     * @param statsAgeMs  最近一次收到它统计包距今多少毫秒；{@link #NEVER} = 没见过
     */
    public static boolean alive(long beaconAgeMs, long statsAgeMs) {
        boolean beaconOk = beaconAgeMs != NEVER && beaconAgeMs <= BEACON_TIMEOUT_MS;
        boolean statsOk = statsAgeMs != NEVER && statsAgeMs <= STATS_TIMEOUT_MS;
        return beaconOk || statsOk;
    }

    /**
     * "已经多久没有它的消息了"——取两个判据里<b>更新鲜</b>的那个（也就是最保守的估计）。
     * 两个都没见过返回 {@link #NEVER}。
     */
    public static long silentForMs(long beaconAgeMs, long statsAgeMs) {
        if (beaconAgeMs == NEVER && statsAgeMs == NEVER) {
            return NEVER;
        }
        if (beaconAgeMs == NEVER) {
            return statsAgeMs;
        }
        if (statsAgeMs == NEVER) {
            return beaconAgeMs;
        }
        return Math.min(beaconAgeMs, statsAgeMs);
    }

    /** 超过宽限期了吗（= 该停发了）。 */
    public static boolean shouldStopSending(long beaconAgeMs, long statsAgeMs) {
        if (alive(beaconAgeMs, statsAgeMs)) {
            return false;
        }
        long silent = silentForMs(beaconAgeMs, statsAgeMs);
        if (silent == NEVER) {
            return false;      // 从来没见过它 —— 那是"还没开始"，不是"走了"
        }
        return silent > GRACE_MS;
    }

    /** 界面上那句人话。 */
    public static String goneText(String name, long silentMs) {
        long sec = Math.max(0, silentMs) / 1000;
        return "已停止向 " + name + " 发送 —— 它已经 " + sec
                + " 秒没有消息了（关掉 App 或离开局域网）。它回来会自动继续，不用你再点。";
    }

    // ══════════════════ ① 自动收听 ══════════════════

    public static final int LISTEN_IGNORE = 0;      // 什么都不做
    public static final int LISTEN_ADD = 1;         // 自动加入收听
    public static final int LISTEN_ALREADY = 2;     // 已经在听了
    public static final int LISTEN_REVOKED = 3;     // 用户手动关过它，不再自动加

    /**
     * 收到"请求收听"时该怎么办。
     *
     * @param autoAccept        全局开关「自动接受别人的收听请求」是否打开
     * @param alreadyListening  这台现在已经在收听名单里
     * @param userRevoked       用户曾经手动把这台的收听关掉过（撤销过就不能再自动加）
     * @param wantListen        对端这次是不是在请求
     */
    public static int autoListenAction(boolean autoAccept, boolean alreadyListening,
                                       boolean userRevoked, boolean wantListen) {
        if (!wantListen) {
            return LISTEN_IGNORE;
        }
        if (alreadyListening) {
            return LISTEN_ALREADY;       // 已经在听：别重复弹提示
        }
        if (userRevoked) {
            return LISTEN_REVOKED;       // ★ 必须可撤销：用户关过的，永远不再自动加
        }
        if (!autoAccept) {
            return LISTEN_IGNORE;        // 全局关掉了自动接受
        }
        return LISTEN_ADD;
    }

    // ══════════════════ ① 音量条两端同步 ══════════════════

    /**
     * 收到对端的增益值时，要不要采纳（一个共享增益，谁后改以谁为准）。
     *
     * <p>规则（简单、可解释、能收敛）：
     * <ol>
     *   <li>对端的时间戳更新 → 采纳（后改的赢）；</li>
     *   <li>时间戳不新，但<b>我们本地已经 5 秒没动过</b>且值确实不一样 → 也采纳。
     *       这一条是为了兜住两台设备时钟有偏差的情况（否则一台慢了几秒的设备怎么调都同步不过去）；</li>
     *   <li>其余情况忽略（我们刚改的更新，别被对面那条旧广播顶回去）。</li>
     * </ol>
     *
     * @param remoteTs            对端报的"最后修改时刻"（墙钟毫秒，0 = 它没报）
     * @param localTs             我本地最后修改时刻（0 = 我从没改过）
     * @param sinceLocalChangeMs  距我上次修改过了多少毫秒（{@link #NEVER} = 从没改过）
     */
    public static boolean adoptRemoteGain(long remoteTs, long localTs, long sinceLocalChangeMs,
                                          float remoteGain, float localGain) {
        if (Math.abs(remoteGain - localGain) < 0.005f) {
            return false;                                  // 值一样：不用动（也不会来回弹）
        }
        if (remoteTs > localTs) {
            return true;                                   // 后改的赢
        }
        return sinceLocalChangeMs == NEVER || sinceLocalChangeMs > 5000L;   // 时钟偏差兜底
    }

    /** 增益夹到 0.00 ~ 1.50（界面滑杆的范围）。 */
    public static float clampGain(float g) {
        if (Float.isNaN(g)) {
            return 1f;
        }
        return Math.max(0f, Math.min(1.5f, g));
    }

    // ══════════════════ 控制包：复用 T_STATS 的三个新字段 ══════════════════
    //
    // 协议约定（2026-10-14，和电脑端 task-16 对齐）：**不新增包类型、不改包头**，
    // 就在现有 T_STATS(ptype=7) 的 JSON 负载里多加这几个键 ——
    // 老版本收到不认识它们会原样忽略，所以新旧可以混跑。
    //
    //   "want_listen": true|false   我在把声音送给对端，请求对端自动打开收听（幂等；停了自然变 false）
    //   "gain": 0.00~1.50          我→对端这条链路的共享音量（= 我的"我送它的"）
    //   "gain_ts": <墙钟毫秒>       上面那个值最后一次被修改的时刻
    //   "gain_rx": 0.00~1.50       对端→我这条链路的共享音量（= 我的"我听它的"）
    //   "gain_rx_ts": <墙钟毫秒>
    //
    // 为什么把这段解析放在这个类里：它是**纯逻辑**，放这儿就能被离线门禁逐条断言
    // （Diagnosis 那份报告生成代码依赖一大堆安卓类，拉进来测试要造一堆桩，不值当）。
    // Diagnosis.parse 会调它，所以线上跑的就是这里测的这一份。

    public static final String KEY_WANT_LISTEN = "want_listen";
    public static final String KEY_GAIN = "gain";
    public static final String KEY_GAIN_TS = "gain_ts";
    public static final String KEY_GAIN_RX = "gain_rx";
    public static final String KEY_GAIN_RX_TS = "gain_rx_ts";

    /** 新字段的落点（由 {@code Diagnosis.PeerStats} 实现；接口在这儿是为了能单独测解析）。 */
    public interface LinkFields {
        void setWantListen(boolean v);

        /** @param gain 已夹到 0.00~1.50；{@code unset=true} 表示对面根本没报这个字段 */
        void setGain(float gain, long ts, boolean unset);

        void setGainRx(float gain, long ts, boolean unset);
    }

    /**
     * 从统计包的 JSON 文本里读这几个新字段。
     *
     * <p>容错原则和老版本一致：<b>读不到就当没有</b>，绝不抛 —— 这段跑在收包线程上。
     * 老版本发的包一个键都没有，读完什么都不变（这就是"混跑不崩"的根据）。
     */
    public static void readLinkFields(String json, LinkFields out) {
        if (json == null || out == null) {
            return;
        }
        try {
            if (json.indexOf(KEY_WANT_LISTEN) < 0 && json.indexOf(KEY_GAIN) < 0) {
                return;      // 老版本的包：快速返回
            }
            if (json.indexOf(KEY_WANT_LISTEN) >= 0) {
                out.setWantListen(readBool(json, KEY_WANT_LISTEN, false));
            }
            float g = readFloat(json, KEY_GAIN, Float.NaN);
            if (!Float.isNaN(g)) {
                out.setGain(clampGain(g), readLong(json, KEY_GAIN_TS, 0L), false);
            }
            float rx = readFloat(json, KEY_GAIN_RX, Float.NaN);
            if (!Float.isNaN(rx)) {
                out.setGainRx(clampGain(rx), readLong(json, KEY_GAIN_RX_TS, 0L), false);
            }
        } catch (Throwable ignored) {
            // 半截包/乱码：什么都不改，绝不影响收包
        }
    }

    /** 找到 {@code "key"} 后面那个值的位置；找不到返回 -1。 */
    private static int valueStart(String s, String key) {
        int i = s.indexOf('"' + key + '"');
        if (i < 0) {
            return -1;
        }
        int colon = s.indexOf(':', i + key.length() + 2);
        if (colon < 0) {
            return -1;
        }
        int v = colon + 1;
        while (v < s.length() && Character.isWhitespace(s.charAt(v))) {
            v++;
        }
        return v < s.length() ? v : -1;
    }

    private static boolean readBool(String s, String key, boolean def) {
        int v = valueStart(s, key);
        if (v < 0) {
            return def;
        }
        if (s.startsWith("true", v)) {
            return true;
        }
        if (s.startsWith("false", v)) {
            return false;
        }
        return def;
    }

    /** 读一个数字（可能带小数、可能带负号）；读不到返回 def。 */
    private static float readFloat(String s, String key, float def) {
        int v = valueStart(s, key);
        if (v < 0) {
            return def;
        }
        int i = v;
        boolean neg = s.charAt(i) == '-';
        if (neg) {
            i++;
        }
        long whole = 0;
        int digits = 0;
        while (i < s.length() && digits < 9) {
            char c = s.charAt(i);
            if (c < '0' || c > '9') {
                break;
            }
            whole = whole * 10 + (c - '0');
            i++;
            digits++;
        }
        if (digits == 0) {
            return def;
        }
        int frac = 0;
        int scale = 1;
        if (i < s.length() && s.charAt(i) == '.') {
            i++;
            for (int k = 0; k < 2 && i < s.length(); k++, i++) {
                char c = s.charAt(i);
                if (c < '0' || c > '9') {
                    break;
                }
                frac = frac * 10 + (c - '0');
                scale *= 10;
            }
        }
        float out = whole + frac / (float) scale;
        return neg ? -out : out;
    }

    /**
     * 读一个整数（可能很大：{@code gain_ts} 是 13 位的墙钟毫秒）。
     *
     * <p><b>为什么不能借道 float</b>：float 只有 24 位尾数（约 7 位十进制），
     * 13 位的时间戳过一遍就会丢精度 —— 那样"谁后改以谁为准"直接失效。
     * 之前的实现就是先读成 float 再转 long，门禁当场抓到了（1700000000000 → 170000000）。
     */
    private static long readLong(String s, String key, long def) {
        int v = valueStart(s, key);
        if (v < 0) {
            return def;
        }
        int i = v;
        boolean neg = s.charAt(i) == '-';
        if (neg) {
            i++;
        }
        long out = 0;
        int digits = 0;
        while (i < s.length() && digits < 18) {
            char c = s.charAt(i);
            if (c < '0' || c > '9') {
                break;
            }
            out = out * 10 + (c - '0');
            i++;
            digits++;
        }
        if (digits == 0) {
            return def;
        }
        return neg ? -out : out;
    }
}
