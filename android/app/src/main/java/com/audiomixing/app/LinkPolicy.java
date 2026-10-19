package com.audiomixing.app;

import java.util.List;

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

    /**
     * ★ 断联多久就直接把这张设备卡片（以及它撑着的陈旧条目 / 提示）从界面上清掉。
     *
     * <p>用户原话：「这种已经信号断联两分钟的就直接删掉就好了。不然全堆积在程序里」。
     * 上面的 {@link #GRACE_MS}（18 秒）只是"先别发"；这条是"直接删掉"—— 超过它，
     * 这个对端不会再回来，留着一张写着"还没收到它的心跳"的卡片只会越堆越多。
     *
     * <p>为什么是纯函数：判据抽出来，{@code android/tools/PolicyProbe.java} 这条零桩门禁
     * 才能直接钉住它（"断联 2 分钟后卡片被清掉"）——不给真机上凑数的机会。
     */
    public static final long CARD_STALE_MS = 120000L;

    /** 这张卡片是不是该清掉了（断联超过 {@link #CARD_STALE_MS}；从没见过 = 不算，那是"还没出现"）。 */
    public static boolean cardStale(long ageMs) {
        return ageMs != NEVER && ageMs >= CARD_STALE_MS;
    }

    // ══════════════════ ① 自动收听 ══════════════════

    public static final int LISTEN_IGNORE = 0;      // 什么都不做
    public static final int LISTEN_ADD = 1;         // 自动加入收听
    public static final int LISTEN_ALREADY = 2;     // 已经在听了
    /** ★ 对端明确说"我不送了" → 收回<b>我们替它自动打开</b>的那一条（2026-10-15）。 */
    public static final int LISTEN_REMOVE = 4;

    /**
     * 收到"请求收听"时该怎么办。
     *
     * <p>★ 2026-10-18 用户第 6 条：判据只剩一条 —— **这台设备有没有开「智连」**。
     * <pre>
     *   · smart —— 这台设备在「智连」名单里（默认名单是空的 = 一台都不自动）
     *   · alreadyListening —— 现在已经在收听名单里了
     *   · wantListen —— 对端这一轮还在请求我收听（它还在送声音）
     *   · autoAdded —— 这条收听是【我们替它自动开的】（只有这种才允许被"我不送了"收回去）
     * </pre>
     *
     * <p>被这一版**删掉**的两个旧判据，都是为了修用户真机反馈：
     * <ul>
     *   <li>{@code autoAccept}（全局总开关）—— 用户要的是"每台设备一个开关"；</li>
     *   <li>{@code userRevoked}（"关过一次就永不再自动"的黑名单）—— 它造成了用户第 1 条那个
     *       「电脑主动发送声音手机不会主动接收」：用户手动关过一次收听，那台电脑就<b>永久</b>
     *       被记进黑名单，之后电脑再怎么点「送给它」，这边都永远不再自动打开
     *       （旧代码里那份记忆只有在"对端不再请求"时才过期，而电脑一直在请求，所以永远不过期）。
     *       现在"要不要自动"完全由「智连」那个开关注定，不存在会卡死的中间态。</li>
     * </ul>
     */
    public static int listenAction(boolean smart, boolean alreadyListening,
                                   boolean wantListen, boolean autoAdded) {
        if (!wantListen) {
            // 它不送了：只有"我们替它自动开的"这条该收回；用户自己开的一律不碰。
            return (alreadyListening && autoAdded) ? LISTEN_REMOVE : LISTEN_IGNORE;
        }
        if (alreadyListening) {
            return LISTEN_ALREADY;       // 已经在听：别重复加，也别弹提示
        }
        return smart ? LISTEN_ADD : LISTEN_IGNORE;
    }

    /**
     * 「信号质量」0~100，只用<b>实测计数器</b>算 —— 用户（图6）要的第三条
     * （「现在定义为音量显示条。并添加一个信号质量条」）。
     *
     * <p>算法：丢包比例越大分越低（丢 5% 约扣 20 分）；缓冲比 60ms 深得越多再扣一点
     * （缓冲深说明这一路一直在补迟到/重排的包）。
     *
     * @param received   收到的包数
     * @param lost       判为丢掉的包数
     * @param bufferedMs 当前缓冲深度（毫秒）
     * @return 0~100 的分数；<b>-1 = 一个包都没统计到</b>，即"没有依据" ——
     *         界面按 -1 写"不可测"。0 分和"不可测"是两件事，不能混。
     */
    public static int qualityScore(long received, long lost, int bufferedMs) {
        long total = received + lost;
        if (total <= 0) {
            return -1;
        }
        double penalty = (lost / (double) total) * 400.0;
        if (bufferedMs > 60) {
            penalty += Math.min(30.0, (bufferedMs - 60) * 0.5);
        }
        return (int) Math.max(0L, Math.min(100L, Math.round(100.0 - penalty)));
    }

    /**
     * 「信号质量」的**兜底**：完全没有音频流、也没有往返时延时用的分。
     *
     * <p>★ 2026-10-16 用户（图1）：「虽然二者没有收发声音，但是设备间信号应当存在」——
     * 原来只在"正在收它的音频流"时才有分，两台设备明明互相看得见，卡片上却写着
     * "还没有可测的链路"。手机端尤其明显：手机<b>不回 PING/PONG</b>，所以没有音频流时
     * 就一点依据都没有了 —— 可链路本身一直在（每 ~2 秒一个信标）。
     *
     * <p>口径与电脑端 {@code engine._heartbeat_score} 逐字对齐：刚听到（≤2500ms）算满，
     * 越久没听到扣得越多（5 秒 → 80 分，10 秒 → 40 分，15 秒 → 0 分）。
     */
    public static int heartbeatScore(long ageMs) {
        double seconds = Math.max(0L, ageMs) / 1000.0;
        double penalty = Math.max(0.0, seconds - 2.5) * 8.0;
        long score = Math.round(100.0 - Math.min(100.0, penalty));
        return (int) Math.max(0L, Math.min(100L, score));
    }

    // ══════════════════ ① 音量条两端同步 ══════════════════

    /**
     * 对端报来的共享音量要不要采纳？—— <b>不拿时序/时钟判新旧</b>。
     *
     * <p>★ 2026-10-16 真机反馈改的（用户原话：「拖动音量调大约 5 秒另一个设备同步。然后有时候
     * 会出现听送两条音量条都自己刷回 1」）。原来这里第二条是 {@code remoteTs > localTs}
     * （比墙钟时间戳）、第三条是"本机超过 5 秒没动过就采纳"——两台机器的墙钟本来就对不齐，
     * 于是：本地一改，对端的值 5 秒内一律不采纳（**要等 5 秒才同步**）；
     * 5 秒一到，对端那个值（在老电脑上可能是"没有这条路由"时的默认 1.0）
     * 又被采纳进来（**音量条自己刷回 1**）。延迟和回弹是同一个原因。
     *
     * <p>现在只剩两条规则，都好预测：
     * <ol>
     *   <li>值一样 → 不动（这也就不会来回弹）；</li>
     *   <li>本机 1.2 秒内刚动过 → 不抢（用户还在拖的时候别被对端拉走）；
     *       其余情况<b>最后到达的为准</b>。</li>
     * </ol>
     * 时间戳仍然收下并写回去（报给对端看"我这个值是什么时候改的"），但不参与判断。
     */
    public static boolean adoptRemoteGain(long remoteTs, long localTs, long sinceLocalChangeMs,
                                          float remoteGain, float localGain) {
        if (Math.abs(remoteGain - localGain) < 0.005f) {
            return false;                                  // 值一样：不用动（也不会来回弹）
        }
        return sinceLocalChangeMs == NEVER || sinceLocalChangeMs >= LOCAL_HOLD_MS;
    }

    /** 本机刚动过之后，这段时间内不让对端的值抢走（用户还在拖）。 */
    public static final long LOCAL_HOLD_MS = 1200L;

    /** 增益夹到 0.00 ~ 2.00（和电脑端同一个口径：两端上限一致）。 */
    public static float clampGain(float g) {
        if (Float.isNaN(g)) {
            return 1f;
        }
        return Math.max(0f, Math.min(2f, g));
    }

    // ══════════════════ ④ 我在不在对端的蓝牙名单里 ══════════════════

    /**
     * 对端（电脑）报的蓝牙名单里有没有我这台 —— 决定"送声音"前要不要先问一句走哪条路。
     *
     * <p>用户原话：「手机界面点击送声音后，应当判断手机是否在电脑的蓝牙设备列表里。
     * 如果是就让用户判断到底用蓝牙还是WiFi。如果否就直接走WiFi路径。」
     * 注意这个判断只决定<b>要不要多问一句</b>，判错不改变 WiFi 这条路本身是否可用。
     *
     * <p>规则（简单、可预测，故意不做模糊匹配）：
     * <ol>
     *   <li>{@code roster} 为 null / 空 → false：老版本电脑没报 {@code bt} 键，直接走 WiFi；</li>
     *   <li>{@code mine} 为空（或去掉空白后不足 2 字符）→ false；</li>
     *   <li>两边都 {@code trim} + 小写后<b>任一边包含另一边</b>就算匹配 ——
     *       不能用全等：系统会给同型号设备的名字加短后缀（{@code 小红的手机} vs {@code 小红的手机_2}），
     *       电脑看到的和手机自己报的常常就差这一截；</li>
     *   <li>长度小于 2 的条目直接跳过，免得 {@code "a"} 这种把什么都匹配上。</li>
     * </ol>
     */
    public static boolean btNameMatches(String mine, List<String> roster) {
        if (roster == null || roster.isEmpty()) {
            return false;
        }
        if (mine == null) {
            return false;
        }
        String a = mine.trim().toLowerCase(java.util.Locale.ROOT);
        if (a.length() < 2) {
            return false;                 // 空串 / 单字符不参与匹配
        }
        for (String raw : roster) {
            if (raw == null) {
                continue;
            }
            String x = raw.trim().toLowerCase(java.util.Locale.ROOT);
            if (x.length() < 2) {
                continue;                 // 单字符条目不参与匹配（"a" 会匹配上一切）
            }
            if (a.contains(x) || x.contains(a)) {
                return true;
            }
        }
        return false;
    }

    // ══════════════════ 控制包：复用 T_STATS 的三个新字段 ══════════════════
    //
    // 协议约定（2026-10-14，和电脑端 task-16 对齐）：**不新增包类型、不改包头**，
    // 就在现有 T_STATS(ptype=7) 的 JSON 负载里多加这几个键 ——
    // 老版本收到不认识它们会原样忽略，所以新旧可以混跑。
    //
    //   "want_listen": true|false   我在把声音送给对端，请求对端自动打开收听（幂等；停了自然变 false）
    //   "gain": 0.00~2.00          我→对端这条链路的共享音量（= 我的"我送它的"）
    //   "gain_ts": <墙钟毫秒>       上面那个值最后一次被修改的时刻
    //   "gain_rx": 0.00~2.00       对端→我这条链路的共享音量（= 我的"我听它的"）
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

        /** @param gain 已夹到 0.00~2.00；{@code unset=true} 表示对面根本没报这个字段 */
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
