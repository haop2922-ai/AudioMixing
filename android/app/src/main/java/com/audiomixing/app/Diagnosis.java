package com.audiomixing.app;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.os.Build;
import android.os.SystemClock;

import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 诊断报告 + 「对端统计」的数据模型与解析。
 *
 * <p><b>为什么要有这个类</b>：真机排查时最痛苦的不是没声音，而是<b>不知道是哪一端的问题</b> ——
 * 是对方没在发，还是我这边没在收？以前只能靠截图里的几个数字猜。所以这里做两件事：
 * <ol>
 *   <li>{@link #build(Context)}：把这一刻的全部状态拼成一段纯文本，用户点一下「复制」就能发出来；</li>
 *   <li>{@link PeerStats}：两台设备互相交换的那份统计（对方自己说"我发了多少、是不是正在发"），
 *       界面据此直接显示「对方：没在发 ✗（对方那台要点「送声音」）」。</li>
 * </ol>
 *
 * <p><b>为什么 JSON 自己手写而不引第三方库</b>：这个工程是零依赖的（没有 appcompat，也没有
 * Gson/org.json 的习惯用法），而且 {@link #parse} 要能在<b>纯 JVM 里跑单测</b> ——
 * 所以整个类里 Android 的调用只出现在 {@link #build(Context)} 这条链上，
 * 数据的生成与解析只用 {@code java.*}。
 *
 * <p><b>解析的底线</b>：Wi-Fi 上什么怪包都有（截断的、乱码的、别人扫端口塞的）。
 * {@link #parse} 对任何输入都<b>只返回 null，绝不抛异常</b> —— 它跑在收包线程上，
 * 抛一次就可能让"收听"整条链路停摆。调用方（Engine）另外再兜一层 try/catch。
 */
public final class Diagnosis {

    private Diagnosis() {
    }

    /** 统计包的标识，对应负载里的 {@code "t"} 字段。 */
    public static final String TAG_STATS = "stats";

    /**
     * 对端统计多久算过期：对方每 2 秒发一次，6 秒没更新就当它"没在回话"。
     * 这个值不能太短（Wi-Fi 抖动丢一两个包很正常，太短会闪），也不能太长（对方真的走了要看出来）。
     */
    public static final long PEER_TTL_MS = 6000L;

    /** 统计负载的长度上限：正常只有两三百字节，超过这个数的一律当坏包（防止有人塞大包）。 */
    private static final int MAX_PAYLOAD = 4096;

    // ══════════════════════════════ 对端统计的数据模型 ══════════════════════════════

    /**
     * 对端在它那份统计包里报告的、它那一侧的状态。
     *
     * <p>只看它自己的字段：{@code sent/bytes/sending} 是"它发给我多少"，
     * {@code recv/lost/bufMs} 是"它从我这里收到多少"。两边各存一份，
     * 于是"谁没在发、谁没收着"一眼就能看出来 —— 这正是用户要的"让俩设备自己交流"。
     *
     * <p>对象一旦发布就不再修改（Engine 每次收到新包都换一个新对象），
     * 所以界面线程读到一半也不会看到"半新半旧"的字段。
     */
    public static final class PeerStats {
        /** 报告方的地址（Engine 按来源填，负载里的 target 只是它自己以为的对端）。 */
        public String host = "";
        /** 报告方的名字。 */
        public String name = "";
        /** 它发出去的音频包数 / 字节数。 */
        public long sent;
        public long bytes;
        /** 它现在是不是正在送声音（false = 它那台没点「送声音」）。 */
        public boolean sending;
        /** 它从我这里收到的包数 / 丢包数。 */
        public long recv;
        public long lost;
        /** 它那条流的缓冲毫秒数。 */
        public int bufMs;
        /** 它那边的音频流条数。 */
        public int streams;
        /** 它以为的对端地址（我）。 */
        public String target = "";
        /** 它发包时的墙钟毫秒（两台手机的时钟不一定一致，只作参考）。 */
        public long reportTsMs;
        /** 本机收到这份统计的时刻（elapsedRealtime，用于判断新鲜度；由 Engine 填）。 */
        public long receivedAtMs;

        /** 这份统计还新鲜吗（{@code nowMs} 用 elapsedRealtime）。 */
        public boolean fresh(long nowMs) {
            return receivedAtMs > 0 && (nowMs - receivedAtMs) <= PEER_TTL_MS;
        }

        /** 距今多少毫秒（给报告里写"1.2 秒前"）。 */
        public long ageMs(long nowMs) {
            return receivedAtMs <= 0 ? -1 : (nowMs - receivedAtMs);
        }
    }

    // ══════════════════════════════ 生成统计负载 ══════════════════════════════

    /**
     * 拼一个统计包负载。字段名严格按 task 约定，末尾多带一个 {@code v}（协议版本）和
     * {@code ts}（发送时刻），这样以后加字段不会把老版本解析弄崩。
     *
     * <p>为什么用 UTF-8 手写：解析方就是本类的 {@link #parse}，两边同一份代码、
     * 同一套转义规则，比引一个 JSON 库可控得多（也能在纯 JVM 里对拍）。
     */
    public static byte[] json(String name, long sent, long bytes, boolean sending,
                              long recv, long lost, int bufMs, int streams, String target) {
        StringBuilder sb = new StringBuilder(192);
        sb.append("{\"t\":\"").append(TAG_STATS).append("\",\"v\":1");
        sb.append(",\"name\":\"").append(escape(name)).append('"');
        sb.append(",\"sent\":").append(sent);
        sb.append(",\"bytes\":").append(bytes);
        sb.append(",\"sending\":").append(sending);
        sb.append(",\"recv\":").append(recv);
        sb.append(",\"lost\":").append(lost);
        sb.append(",\"buf_ms\":").append(bufMs);
        sb.append(",\"streams\":").append(streams);
        sb.append(",\"target\":\"").append(escape(target)).append('"');
        sb.append(",\"ts\":").append(System.currentTimeMillis());
        sb.append('}');
        return sb.toString().getBytes(StandardCharsets.UTF_8);
    }

    // ══════════════════════════════ 解析统计负载 ══════════════════════════════

    /**
     * 解析对端发来的统计负载。
     *
     * <p><b>任何异常输入都返回 null，绝不抛</b>：这个方法跑在收包线程上（Engine 的监听器里），
     * 真机上会收到截断包、乱码、别人扫描端口的垃圾，抛一次就可能把"收听"整条链路带走。
     *
     * <p>故意不用 org.json：一是能在纯 JVM 单测里跑，二是这里的解析要"能读多少读多少"，
     * 库的严格校验在局域网里反而是负担。
     */
    public static PeerStats parse(byte[] raw, int len) {
        try {
            if (raw == null || len <= 0 || len > MAX_PAYLOAD) {
                return null;
            }
            if (len > raw.length) {
                len = raw.length;
            }
            String s = new String(raw, 0, len, StandardCharsets.UTF_8);
            if (s.indexOf('{') < 0 || s.indexOf('}') < 0) {
                return null;                     // 不是对象形状，直接当垃圾
            }
            String t = str(s, "t");
            if (t != null && !TAG_STATS.equals(t)) {
                return null;                     // 自称是别的类型（以后可能有别的控制包）
            }
            PeerStats ps = new PeerStats();
            ps.name = nz(str(s, "name"));
            ps.sent = num(s, "sent", 0);
            ps.bytes = num(s, "bytes", 0);
            ps.sending = bool(s, "sending", false);
            ps.recv = num(s, "recv", 0);
            ps.lost = num(s, "lost", 0);
            ps.bufMs = (int) clamp(num(s, "buf_ms", 0), 0, 60000);
            ps.streams = (int) clamp(num(s, "streams", 0), 0, 10000);
            ps.target = nz(str(s, "target"));
            ps.reportTsMs = num(s, "ts", 0);
            // 什么都不像：不是我们的统计包（比如一段纯文本里正好有个 {}）。
            if (t == null && ps.name.isEmpty() && ps.sent == 0 && ps.recv == 0 && !ps.sending) {
                return null;
            }
            return ps;
        } catch (Throwable t) {
            return null;                          // 手写解析也可能有边界情况，兜底不留隐患
        }
    }

    public static PeerStats parse(byte[] raw) {
        return raw == null ? null : parse(raw, raw.length);
    }

    // ══════════════════════════════ 界面那一行文字 ══════════════════════════════

    /**
     * 设备卡片里的「对方」那一行。用户要的原话就是这三种状态：
     * 「已发 5938 包 / 11.0 MB · 正在发 ✓」/「没在发 ✗」/「等它回话」。
     *
     * @param nowMs 用 {@code SystemClock.elapsedRealtime()} 取的当前时刻
     */
    public static String cardLine(PeerStats ps, long nowMs) {
        if (ps == null) {
            return "对方：等它回话…（它每 2 秒会回一次）";
        }
        if (!ps.fresh(nowMs)) {
            long age = ps.ageMs(nowMs);
            return "对方：没在回话 ✗（最后一句是 " + (age < 0 ? "?" : (age / 1000) + " 秒前")
                    + "，那台的 App 还在跑吗？）";
        }
        StringBuilder sb = new StringBuilder(96);
        sb.append("对方：");
        if (ps.sending) {
            sb.append("已发 ").append(ps.sent).append(" 包 / ").append(bytesText(ps.bytes))
                    .append(" · 正在发 ✓");
        } else {
            sb.append("没在发 ✗（对方那台要点「送声音」）");
            if (ps.sent > 0) {
                sb.append("（之前发过 ").append(ps.sent).append(" 包 / ")
                        .append(bytesText(ps.bytes)).append("）");
            }
        }
        // 对方报告的"它收了我多少"：这是判断"我发出去的声音到底到没到"的唯一依据。
        sb.append(" · 它收 ").append(ps.recv).append(" 包 丢 ").append(ps.lost)
                .append(" 缓冲 ").append(ps.bufMs).append("ms");
        return sb.toString();
    }

    /** 详情面板里的「对端回话」多行文字。 */
    public static String peersSummary(List<PeerStats> list, long nowMs) {
        if (list == null || list.isEmpty()) {
            return "还没收到任何对端的统计回话";
        }
        StringBuilder sb = new StringBuilder(256);
        for (PeerStats ps : list) {
            sb.append(ps.host);
            if (ps.name != null && ps.name.length() > 0) {
                sb.append("（").append(ps.name).append("）");
            }
            sb.append(' ').append(cardLine(ps, nowMs)).append('\n');
        }
        if (sb.length() > 0) {
            sb.setLength(sb.length() - 1);
        }
        return sb.toString();
    }

    // ══════════════════════════════ 诊断报告 ══════════════════════════════

    /**
     * 生成完整报告。纯函数：只读状态、只返回字符串，不启线程、不改任何开关
     * （所以以后加单测可以直接对着它断言）。
     */
    public static String build(Context ctx) {
        // peek() 而不是 get()：诊断不该顺手把引擎建起来（那会真的去开组播端口）。
        return build(ctx, Engine.peek());
    }

    /** 报告主体。{@code e} 允许为 null（引擎还没起来时也要能出报告）。 */
    public static String build(Context ctx, Engine e) {
        long nowUp = SystemClock.elapsedRealtime();
        StringBuilder sb = new StringBuilder(2048);
        sb.append("========== Audio Mixing 诊断报告 ==========\n");
        sb.append("生成时间：").append(timeText(System.currentTimeMillis())).append('\n');
        sb.append("App     ：").append(appVersion(ctx)).append('\n');
        sb.append("系统    ：").append(androidVersion()).append('\n');
        sb.append("已运行  ：").append(durationText(nowUp)).append('\n');

        if (e == null) {
            sb.append("\n【引擎没在运行】\n")
                    .append("App 还没启动过，或者进程刚被系统回收。\n")
                    .append("（打开 App 主界面即可让引擎跑起来，设备列表要它在才看得到。）\n");
            return sb.toString();
        }

        // ── 本机 ──
        sb.append("\n【本机】\n");
        sb.append("名称    ：").append(e.nodeName()).append('\n');
        sb.append("平台    ：").append(e.platform()).append('\n');
        sb.append("本机 IP ：").append(e.localIpsText()).append('\n');
        sb.append("音频端口：").append(e.audioPort()).append("（UDP，收发都用它）\n");
        sb.append("收听名单：").append(e.listeningCount()).append(" 台 → ")
                .append(e.listenTargetsText()).append('\n');
        // ★ 听广播（频道）：这是"电脑在广播、手机要能听见"的唯一入口，报告里必须有。
        sb.append("听广播  ：").append(e.isChannelListening() ? "已打开 ✓（谁来都听）" : "没开 ✗").append('\n');
        sb.append("送声目标：").append(e.sendTargetText()).append('\n');
        sb.append("运行状态：发现 ").append(onOff(e.discoveryRunning()))
                .append(" / 接收器 ").append(onOff(e.receiverRunning()))
                .append(" / 发送器 ").append(onOff(e.senderRunning())).append('\n');
        // ★ 音量：把"三层"写清楚。用户明确要求过：我们只负责我们自己这一层，
        //   绝不能顺手改系统音量（以前的滑杆就是错的）。
        sb.append("我们这一层：").append(e.volumeText()).append('\n');
        sb.append("系统音量：").append(e.systemVolumeText()).append('\n');
        sb.append("音量关系：").append(e.volumeExplainText()).append('\n');
        // ★ 节点 ID：持久化的，重启不变 —— 电脑端才不会把同一台手机记成好几台设备。
        sb.append("节点 ID ：").append(e.nodeIdText()).append('\n');
        sb.append("统计包  ：已发 ").append(e.statsPacketsSent())
                .append(" 个（每 2 秒一次，收发双方互报）\n");

        // ── ★ 声音来源（送什么）──
        // 用户被"手机只采了麦克风"误导过一次（那是对讲机，不是他要的桌面音频），
        // 所以这一段把"选了什么、实际采的是什么、为什么不一致、支不支持、是不是一直静音"
        // 全部写出来 —— 采不到声音时，这几行就是定性用的证据。
        sb.append("\n【声音来源（送什么）】\n");
        sb.append("当前选择：").append(e.sourceNameText()).append('\n');
        sb.append("实际采集：").append(e.activeSourceText()).append('\n');
        sb.append("系统支持：").append(e.playbackSupportText()).append('\n');
        sb.append("授权状态：").append(e.projectionStateText()).append('\n');
        sb.append("采集格式：").append(e.captureFormatText()).append('\n');
        sb.append("静音情况：").append(e.silenceText()).append('\n');
        String srcHint = e.silenceHintText();
        if (srcHint != null && srcHint.length() > 0) {
            sb.append("提醒    ：").append(srcHint).append('\n');
        }
        String pending = e.pendingSendId();
        if (pending != null && pending.length() > 0) {
            sb.append("等待中  ：正在等系统的投屏授权回来（授权后就送给 ").append(pending).append("）\n");
        }
        sb.append("（采本机声音的限制：安卓 10 以下没有这个能力；DRM 保护的内容和\n")
                .append("  主动禁止被采集的 App 采不到，会是静音；少数厂商 ROM 阉割了该接口。）\n");

        // ── 发现 ──
        sb.append("\n【发现（信标）】\n");
        sb.append(indent(e.discoveryStatsText())).append('\n');
        // ★ 把我们真正发出去的信标贴一行：里面有 id / audio_port / 总字节数，
        //   "电脑那边看到我的 id 是空的"这类怀疑一次就能说清。
        sb.append("我发的信标：").append(e.beaconPreviewText()).append('\n');
        List<Discovery.Peer> peers = safePeers(e);
        sb.append("当前看到 ").append(peers.size()).append(" 台设备：\n");
        if (peers.isEmpty()) {
            sb.append("  （一台都没有。两台设备必须连同一个 Wi-Fi；\n")
                    .append("    有些路由器开了「AP 隔离/客户端隔离」，那样互相永远看不到。）\n");
        }
        for (Discovery.Peer p : peers) {
            sb.append("  · ").append(p.label())
                    .append("  ").append(p.host).append(':').append(p.audioPort)
                    .append("  ").append(nz(p.platform));
            if (p.busy) {
                sb.append("  [对端忙]");
            }
            sb.append('\n');
        }

        // ── 接收 ──
        sb.append("\n【接收（我听别人）】\n");
        AudioRecv.Stats st = e.recvStats();
        if (st == null) {
            sb.append("接收器没在运行（没点过任何一台的「🎧 收听」，也没开「📻 听广播」）。\n");
        } else {
            sb.append("听广播  ：").append(e.isChannelListening()
                    ? "开着（接收端不按名单过滤，谁来都听）" : "没开（只收点过「收听」的那几台）").append('\n');
            // ★ 「开关是意图，这一行才是事实」——用户说的"路由/广播混淆"就靠它对质。
            sb.append("实际收到：").append(e.recvStreamsText()).append('\n');
            String dup = e.dupRouteStatsText();
            if (dup != null && dup.length() > 0) {
                sb.append("重复流  ：").append(dup).append('\n');
            }
            sb.append("已收包  ：").append(st.received).append(" 包\n");
            sb.append("丢包    ：").append(st.lost).append("（迟到 ").append(st.late)
                    .append(" · 重复 ").append(st.duplicates).append("）\n");
            sb.append("坏包    ：").append(st.badPackets).append("（控制包 ").append(st.controls)
                    .append(" · 按名单过滤 ").append(st.filtered)
                    .append(" · 自愈 ").append(st.recoveries).append("）\n");
            // ★ 坏包按原因分开报（2026-10-12 真机反馈）：原来只有一个"坏包 3220"的总数，
            //   报告里只解释了其中 49 个，剩下 3171 个只能猜 —— 有数字没线索是最难受的排查状态。
            sb.append("坏包分类：magic 不对 ").append(st.badMagic)
                    .append(" · 版本不对 ").append(st.badVersion)
                    .append(" · 包太短 ").append(st.badShort)
                    .append(" · 编码不是 PCM16 ").append(st.badCodec)
                    .append(" · 帧长不是 480 ").append(st.badFrames).append('\n');
            if (st.lastCodecSeen != null && st.lastCodecSeen.length() > 0) {
                sb.append("对面编码：").append(st.lastCodecSeen).append('\n');
            }
            if (st.badBySource != null && st.badBySource.length() > 0) {
                sb.append("坏包来源：").append(st.badBySource)
                        .append("（全来自同一台 = 那台在按我们不懂的方式发；"
                                + "来自很多台 = 多半是这个端口被别的软件/扫描器盯上了）\n");
            }
            if (st.badByLen != null && st.badByLen.length() > 0) {
                sb.append("坏包长度：").append(st.badByLen)
                        .append("（1948 字节 = 28 字节包头 + 480 帧 PCM16，正是我们一条音频流的长度；"
                                + "零碎长度多半是别人的杂流量）\n");
            }
            appendSample(sb, "最近坏包", st.lastMagicSample);
            appendSample(sb, "最近编码包", st.lastCodecSample);
            appendSample(sb, "最近帧长包", st.lastFramesSample);
            appendSample(sb, "最近短包", st.lastShortSample);
            appendSample(sb, "最近版本包", st.lastVersionSample);
            sb.append("缓冲    ：").append(st.bufferedMs).append("ms（目标 ")
                    .append(st.targetMs).append("ms）\n");
            sb.append("流数    ：").append(st.streamCount).append('\n');
            sb.append("混音电平：").append(percent(st.peak)).append('\n');
            if (st.trackErrors > 0) {
                sb.append("音频轨错误：").append(st.trackErrors).append(" 次\n");
            }
            if (st.streams != null && !st.streams.isEmpty()) {
                sb.append("每条流：\n");
                for (AudioRecv.StreamStats ss : st.streams) {
                    sb.append("  · ").append(ss.host)
                            .append(" 收 ").append(ss.received).append(" 包")
                            .append(" · 丢 ").append(ss.lost)
                            .append(" · 缓冲 ").append(ss.bufferedMs).append("ms")
                            .append(" · 电平 ").append(percent(ss.peak))
                            .append(" · 最后一包 ").append(ss.ageMs).append("ms 前\n");
                }
            }
        }

        // ── 发送 ──
        sb.append("\n【发送（别人听我）】\n");
        if (!e.senderRunning()) {
            sb.append("发送器没在运行（没点过任何一台的「送声音」）。\n");
        } else {
            sb.append("目标    ：").append(e.sendTargetText()).append('\n');
            // ★ 送出去的是本机声音还是麦克风，必须在报告里一眼看到（别再让人猜）
            sb.append("音源    ：").append(e.activeSourceText()).append('\n');
            sb.append("已发包  ：").append(e.sentPackets()).append(" 包 / ")
                    .append(bytesText(e.sentBytes())).append('\n');
            sb.append("发送失败：").append(e.sendErrors()).append(" 次\n");
        }

        // ── 对端回话（这就是"让俩设备自己交流"的结果） ──
        sb.append("\n【对端自己报告的统计】\n");
        List<PeerStats> psList = e.peerStatsList();
        if (psList.isEmpty()) {
            sb.append("还没收到任何对端的统计回话。\n")
                    .append("（对方那台要是也在跑这个 App，并且我们之间有「收听/送声音」关系，\n")
                    .append("  它每 2 秒会主动报一次；一直收不到通常说明两台不在同一个 Wi-Fi。）\n");
        } else {
            for (PeerStats ps : psList) {
                long age = ps.ageMs(nowUp);
                sb.append("· ").append(ps.host);
                if (ps.name.length() > 0) {
                    sb.append("（").append(ps.name).append("）");
                }
                sb.append('\n');
                sb.append("    ").append(ps.sending
                                ? "它说：正在送声音 ✓"
                                : "它说：没在送声音 ✗（要它那台点「送声音」）")
                        .append('\n');
                sb.append("    它已发：").append(ps.sent).append(" 包 / ").append(bytesText(ps.bytes))
                        .append('\n');
                sb.append("    它收我：").append(ps.recv).append(" 包 · 丢 ").append(ps.lost)
                        .append(" · 缓冲 ").append(ps.bufMs).append("ms · 它那边 ")
                        .append(ps.streams).append(" 条流\n");
                sb.append("    这份统计：").append(age < 0 ? "?" : (age + "ms") + " 前收到")
                        .append(ps.fresh(nowUp) ? "（新鲜 ✓）" : "（已过期 ✗）").append('\n');
            }
        }

        // ── 一眼判断：把"到底是哪一端的问题"直接写出来 ──
        conclude(sb, e, nowUp, st, psList);

        // ── 最近错误 ──
        sb.append("\n【最近一次错误】\n");
        sb.append("接收：").append(orNone(e.recvLastError())).append('\n');
        sb.append("发送：").append(orNone(e.sendLastError())).append('\n');
        sb.append("发现：").append(orNone(e.discLastError())).append('\n');

        sb.append("\n========== 报告结束（把整段复制给对方即可） ==========\n");
        return sb.toString();
    }

    /**
     * 「一眼判断」：报告里最值钱的一段。
     *
     * <p>用户的原话是"不知道是哪一端没在发、哪一端没收"。所以这里不只是罗列数字，
     * 而是把两边的话摆在一起下结论：
     * <ul>
     *   <li>对方说它在发、而我一个包都没收到 → 九成是两台不在同一个 Wi-Fi（或 AP 隔离）；</li>
     *   <li>对方说它没在发 → 去对方那台点「送声音」，跟本机没关系；</li>
     *   <li>本机没在发 → 在本机点「送声音」；</li>
     *   <li>对方在发、我也在收，但丢包/缓冲很糟 → 是 Wi-Fi 质量问题，不是配置问题。</li>
     * </ul>
     */
    private static void conclude(StringBuilder sb, Engine e, long nowUp,
                                 AudioRecv.Stats st, List<PeerStats> psList) {
        sb.append("\n【一眼判断】\n");
        boolean sending = e.senderRunning();
        long recvAll = st == null ? 0 : st.received;

        if (sending) {
            sb.append("· 本机：正在送声音 ✓（已发 ").append(e.sentPackets()).append(" 包，音源 ")
                    .append(AudioSend.sourceName(e.activeSourceKind())).append("）\n");
            if (e.sourceMismatch()) {
                // 最容易让人误判的一种状态：以为是本机声音，其实退回了麦克风（或反过来）。
                sb.append("· ★ 本机：你想送 ").append(AudioSend.sourceName(e.sourceKind()))
                        .append("，实际送的是 ").append(AudioSend.sourceName(e.activeSourceKind()))
                        .append(" → 详情看上面「声音来源」那一段；对面听到的是 ")
                        .append(AudioSend.sourceShortName(e.activeSourceKind()))
                        .append("，不是你以为的那个。\n");
            }
        } else {
            sb.append("· 本机：没在送声音 ✗ → 想让对方听见，在本机任意一台设备上点「送声音」\n");
        }
        if (e.receiverRunning()) {
            sb.append("· 本机：接收器在跑，已收到 ").append(recvAll).append(" 个音频包")
                    .append(recvAll > 0 ? " ✓\n" : "（一个都没有）\n");
            if (e.isChannelListening() && recvAll == 0) {
                // ★ 用户现在最常见的现场：电脑在广播、手机开着听广播却没声。这里把两种可能
                //   按"先查哪个"的顺序写清楚，而不是丢一句"没收到包"。
                sb.append("· ★ 本机：开着「听广播」却一个包都没收到 → 按顺序查两件事：")
                        .append("① 对面那台到底在不在广播（电脑上「广播我的声音」要开着，")
                        .append("而且它的采集设备得真的在出声音）；")
                        .append("② 对面的发现列表里有没有本机 —— 电脑端的广播**只发给\"它看得见的在线节点\"**，")
                        .append("看不见就等于发给 0 个人。看上面【发现（信标）】的自检那几行：")
                        .append("如果写着\"从没收到过自己的信标\"，那是本机/AP 的组播问题，跟对面无关。\n");
            } else if (e.isChannelListening()) {
                sb.append("· 本机：听广播已经收到声音 ✓（谁在广播就听谁，不用点设备）\n");
            }
        } else {
            sb.append("· 本机：接收器没在跑 → 想听某台就点它的「🎧 收听」；")
                    .append("想听电脑正在广播的声音就开「📻 听广播」\n");
        }
        try {
            if (e.peers().isEmpty()) {
                sb.append("· 本机：设备列表是空的（0 台）。上面【发现（信标）】的自检会说明卡在哪一步：")
                        .append("「自己绕回来 0」= 本机收不到组播/广播；「不是我们的」很大 = 收到的都是别人的流量；")
                        .append("「最近收到：一个都没有」= 对面根本没发到你这台。\n");
            }
        } catch (Throwable ignored) {
            // 设备列表读不到不影响其余判断
        }

        // ★ 坏包的说人话版本：一个"坏包 3220"的数字没用，这里直接给结论和下一步动作。
        //   （必须放在下面"对端没回话就 return"之前，否则最需要这段的时候反而看不到。）
        if (st != null && st.badPackets > 0) {
            if (st.badCodec > 0) {
                sb.append("· 本机：有 ").append(st.badCodec)
                        .append(" 个音频包的编码不是 PCM16（").append(nz(st.lastCodecSeen))
                        .append("）→ 手机只有 PCM16 解码器，去对面那台把编码改成 pcm_s16 就能听见。\n");
            }
            if (st.badFrames > 0) {
                sb.append("· 本机：有 ").append(st.badFrames)
                        .append(" 个包不是 480 帧/10ms 一包 → 手机只认这一档（不做重采样），")
                        .append("要对面把每包帧数改回 480。\n");
            }
            if (st.badMagic > 0) {
                sb.append("· 本机：有 ").append(st.badMagic)
                        .append(" 个包的 magic 不对（根本不是我们这套协议）→ 通常不是对方发的，")
                        .append("而是网络里别人的广播/扫描流量，或者是另一套协议（网页端/别的 App）。")
                        .append("看上面「最近坏包」那一行开头的十六进制就能定性。\n");
            }
            if (st.badShort > 0) {
                sb.append("· 本机：有 ").append(st.badShort)
                        .append(" 个包太短（连 28 字节包头都不够）→ 是别的流量或者被截断的包，不影响收听。\n");
            }
            if (st.badVersion > 0) {
                sb.append("· 本机：有 ").append(st.badVersion)
                        .append(" 个包版本号不是 1 → 对面那台的协议版本和本机不一致（一边升级了一边没升）。\n");
            }
            long classified = st.badMagic + st.badVersion + st.badShort + st.badCodec + st.badFrames;
            if (st.badPackets > classified) {
                sb.append("· 本机：另有 ").append(st.badPackets - classified)
                        .append(" 个坏包没归到已知类别里（多半是更早版本记下的旧数字）。\n");
            }
        }

        if (psList.isEmpty()) {
            sb.append("· 对端：一句话都没回。要么对方那台没打开 App，要么两台不在同一个 Wi-Fi")
                    .append("（路由器开了客户端隔离也会这样）。\n");
            return;
        }
        for (PeerStats ps : psList) {
            if (!ps.fresh(nowUp)) {
                sb.append("· 对端 ").append(ps.host)
                        .append("：超过 ").append(PEER_TTL_MS / 1000)
                        .append(" 秒没回话 ✗ → 那台的 App 可能被系统杀了/切后台了。\n");
                continue;
            }
            if (!ps.sending) {
                sb.append("· 对端 ").append(ps.host)
                        .append("：它自己说没在发 ✗ → 去它那台点「送声音」，本机这边没问题。\n");
                continue;
            }
            // 对方说它在发：那就只看"我到底收到没有"——这才是真正的分界线。
            long got = recvFrom(st, ps.host);
            if (got > 0) {
                sb.append("· 对端 ").append(ps.host).append("：它在发、本机也收到了 ")
                        .append(got).append(" 包 ✓");
                if (ps.lost > 0 || (st != null && st.lost > 0)) {
                    sb.append("，但有丢包（本机 ").append(st == null ? 0 : st.lost)
                            .append(" / 它 ").append(ps.lost).append("）")
                            .append(" → 属于 Wi-Fi 质量问题，靠近路由器或改成 5G 频段试试");
                }
                sb.append('\n');
            } else {
                sb.append("· 对端 ").append(ps.host)
                        .append("：它说它在发，但本机一个包都没收到 ✗ ← ")
                        .append("最可能的原因：两台不在同一个 Wi-Fi、路由器开了「客户端隔离/AP 隔离」，")
                        .append("或者本机接收器没起来（上面「运行状态」里看接收器）。\n");
            }
            if (ps.recv == 0 && sending) {
                sb.append("· 对端 ").append(ps.host)
                        .append("：本机在发，但它报的收包数是 0 → 它那边没在收（它没点「🎧 收听」，")
                        .append("或者它那边的接收器没起来）。\n");
            }
        }
    }

    /** 某一路（某台设备）收到的包数；没有这条流就是 0。 */
    private static long recvFrom(AudioRecv.Stats st, String host) {
        if (st == null || st.streams == null || host == null) {
            return 0;
        }
        for (AudioRecv.StreamStats ss : st.streams) {
            if (host.equals(ss.host)) {
                return ss.received;
            }
        }
        return 0;
    }

    // ══════════════════════════════ 小工具 ══════════════════════════════

    /** 字节数转人话（"11.0 MB"），和界面上「已发 … MB」的写法一致。 */
    public static String bytesText(long bytes) {
        if (bytes < 0) {
            bytes = 0;
        }
        if (bytes < 1024) {
            return bytes + " B";
        }
        if (bytes < 1048576L) {
            return String.format(Locale.US, "%.1f KB", bytes / 1024.0);
        }
        if (bytes < 1073741824L) {
            return String.format(Locale.US, "%.1f MB", bytes / 1048576.0);
        }
        return String.format(Locale.US, "%.2f GB", bytes / 1073741824.0);
    }

    private static String percent(float v) {
        if (Float.isNaN(v) || v < 0f) {
            v = 0f;
        }
        return String.format(Locale.US, "%.0f%%", Math.min(1f, v) * 100f);
    }

    private static String onOff(boolean b) {
        return b ? "运行中 ✓" : "已停止 ✗";
    }

    private static String orNone(String s) {
        return (s == null || s.length() == 0) ? "（无）" : s;
    }

    /** 坏包现场样本：有才写一行（缩进），没有就不占篇幅。 */
    private static void appendSample(StringBuilder sb, String label, String sample) {
        if (sample != null && sample.length() > 0) {
            sb.append("  ").append(label).append("：").append(sample).append('\n');
        }
    }

    private static String indent(String s) {
        if (s == null) {
            return "  （无）";
        }
        return "  " + s.replace("\n", "\n  ");
    }

    private static List<Discovery.Peer> safePeers(Engine e) {
        try {
            List<Discovery.Peer> l = e.peers();
            return l == null ? java.util.Collections.<Discovery.Peer>emptyList() : l;
        } catch (Throwable t) {
            return java.util.Collections.emptyList();
        }
    }

    private static String timeText(long ms) {
        try {
            return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date(ms));
        } catch (Throwable t) {
            return String.valueOf(ms);
        }
    }

    /** 进程跑了多久（诊断里很有用：刚重启过和跑了一小时的现场完全不一样）。 */
    private static String durationText(long upMs) {
        long sec = upMs / 1000;
        if (sec < 60) {
            return sec + " 秒";
        }
        long min = sec / 60;
        if (min < 60) {
            return min + " 分 " + (sec % 60) + " 秒";
        }
        return (min / 60) + " 小时 " + (min % 60) + " 分";
    }

    /** 版本号：API 28 起 {@code versionCode} 被标了过时（要换成 getLongVersionCode），
     *  但我们 minSdk 24，两个都得留着，所以这里显式压掉那条警告。 */
    @SuppressWarnings("deprecation")
    private static String appVersion(Context ctx) {
        if (ctx == null) {
            return "未知";
        }
        try {
            PackageInfo pi = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return pi.versionName + "（versionCode " + pi.versionCode + "）";
        } catch (Throwable t) {
            return "读取失败：" + t;
        }
    }

    private static String androidVersion() {
        try {
            return "Android " + Build.VERSION.RELEASE + "（API " + Build.VERSION.SDK_INT + "）· "
                    + Build.MANUFACTURER + " " + Build.MODEL;
        } catch (Throwable t) {
            return "未知";
        }
    }

    // ── 极简 JSON 读写（只支持我们自己的字段类型：字符串/整数/bool） ──

    private static String escape(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                default:
                    // 控制字符一律丢掉：设备名里混进它们会让对端的解析变复杂，不值当。
                    if (c >= 0x20) {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }

    /** 找到 {@code "key"} 后面那个值的起始下标；找不到返回 -1。 */
    private static int valueStart(String s, String key) {
        String pat = "\"" + key + "\"";
        int i = s.indexOf(pat);
        if (i < 0) {
            return -1;
        }
        int colon = s.indexOf(':', i + pat.length());
        if (colon < 0) {
            return -1;
        }
        int v = colon + 1;
        while (v < s.length() && Character.isWhitespace(s.charAt(v))) {
            v++;
        }
        return v < s.length() ? v : -1;
    }

    private static String str(String s, String key) {
        int v = valueStart(s, key);
        if (v < 0 || s.charAt(v) != '"') {
            return null;
        }
        StringBuilder out = new StringBuilder(32);
        for (int i = v + 1; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"') {
                return out.toString();
            }
            if (c == '\\' && i + 1 < s.length()) {
                char n = s.charAt(++i);
                switch (n) {
                    case 'n':
                        out.append('\n');
                        break;
                    case 'r':
                        out.append('\r');
                        break;
                    case 't':
                        out.append('\t');
                        break;
                    case 'u':
                        // 「反斜杠 + u + 四位十六进制」的转义：解析失败就原样跳过，
                        // 不因为一个坏转义把整包作废。
                        // 注意：这里必须避开真正的那种转义写法 —— javac 在词法分析之前
                        // 就会处理它，注释里写了不合法的形式会直接编译不过。
                        if (i + 4 < s.length()) {
                            try {
                                out.append((char) Integer.parseInt(s.substring(i + 1, i + 5), 16));
                                i += 4;
                            } catch (Throwable ignored) {
                                // 保持原样
                            }
                        }
                        break;
                    default:
                        out.append(n);
                }
                continue;
            }
            out.append(c);
        }
        return out.toString();   // 没有收尾引号（被截断）：返回已读到的部分
    }

    private static long num(String s, String key, long def) {
        int v = valueStart(s, key);
        if (v < 0) {
            return def;
        }
        boolean neg = s.charAt(v) == '-';
        int i = neg ? v + 1 : v;
        long r = 0;
        int digits = 0;
        while (i < s.length() && digits < 18) {
            char c = s.charAt(i);
            if (c < '0' || c > '9') {
                break;
            }
            r = r * 10 + (c - '0');
            i++;
            digits++;
        }
        if (digits == 0) {
            return def;
        }
        return neg ? -r : r;
    }

    private static boolean bool(String s, String key, boolean def) {
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

    private static long clamp(long v, long lo, long hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    private static String nz(String s) {
        return s == null ? "" : s;
    }
}
