import com.audiomixing.app.LinkPolicy;

/**
 * ★ 链路策略离线探针（task-17：对端消失要停发 + 自动收听 + 共享音量同步）★
 *
 * <p>被测的是 {@code LinkPolicy.java} <b>本体</b>（原样编译，<b>零桩</b> —— 它是纯函数）：
 * <ul>
 *   <li>④ 对端是不是"确认走了"（宽限期 18 秒、两个判据取更保守的那个）；</li>
 *   <li>① 自动收听的四种情形（含"用户撤销过就永不再自动加"）；</li>
 *   <li>① 共享音量谁后改以谁为准（含时钟偏差兜底）；</li>
 *   <li>控制包新字段的解析（{@code Diagnosis.parse} 调的就是这一份，所以线上跑的就是这里测的）。</li>
 * </ul>
 *
 * <p>为什么值得跑：这三件事在真机上都要两台设备配合才试得出来，而边界（宽限期、Wi-Fi 抖帧、
 * 用户撤销、两端同时调音量）恰恰最容易错。
 */
public final class PolicyProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) {
        part1Liveness();
        part2AutoListen();
        part3GainSync();
        part4Protocol();

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    // ── ④ 对端消失要停发（安全项）──

    private static void part1Liveness() {
        System.out.println("== 1. ★ 对端是不是走了（安全：电脑关了就不能再发）==");
        long N = LinkPolicy.NEVER;

        check("刚收到信标（0.5 秒前）+ 心跳（1 秒前）→ 活着",
                LinkPolicy.alive(500, 1000));
        check("只有信标新鲜（心跳从没收到）→ 还算活着（有些设备不发统计包）",
                LinkPolicy.alive(3000, N));
        check("只有心跳新鲜（发现列表里暂时没有它）→ 也算活着",
                LinkPolicy.alive(N, 2000));
        check("信标 14 秒前（<15 秒超时）→ 活着", LinkPolicy.alive(14000, N));
        check("信标 16 秒前 + 心跳 7 秒前 → 两个判据都过期 → 不算活着",
                !LinkPolicy.alive(16000, 7000));
        check("Wi-Fi 抖一下（信标 12 秒 + 心跳 5 秒）→ 不能误判成走了",
                !LinkPolicy.shouldStopSending(12000, 5000));
        check("宽限期内（信标 16 秒 + 心跳 8 秒，静默 8 秒）→ 先不停",
                !LinkPolicy.shouldStopSending(16000, 8000));
        check("★ 确认走了（静默 19 秒 > 宽限期 18 秒）→ 停发",
                LinkPolicy.shouldStopSending(19000, 19000));
        check("★ 电脑关掉 App 的典型现场（信标没了 + 心跳停在 20 秒前）→ 停发",
                LinkPolicy.shouldStopSending(LinkPolicy.NEVER, 20000));
        check("从来没见过它（两个都是 NEVER）→ 不停（那是「还没开始」，不是「走了」）",
                !LinkPolicy.shouldStopSending(N, N));
        check("宽限期就是 18 秒（用户要求 15~20 秒）",
                LinkPolicy.GRACE_MS == 18000L);
        check("静默时长取两个判据里更新鲜的那个（16 秒 / 8 秒 → 8 秒）",
                LinkPolicy.silentForMs(16000, 8000) == 8000);
        String txt = LinkPolicy.goneText("2024TSJ", 18300);
        check("界面那句话写全了：\"" + txt + "\"",
                txt.contains("已停止向 2024TSJ 发送") && txt.contains("18 秒")
                        && txt.contains("关掉 App") && txt.contains("自动继续"));
        check("对端回来之后 shouldStopSending 立刻变 false（= 自动恢复）",
                !LinkPolicy.shouldStopSending(100, 100));
    }

    // ── ① 自动收听 ──

    private static void part2AutoListen() {
        System.out.println();
        System.out.println("== 2. ★ 别人点「送给它」→ 我这边自动打开收听（可撤销）==");
        check("默认开着自动接受 + 对方在请求 + 我没在听 → 自动加进收听",
                LinkPolicy.autoListenAction(true, false, false, true) == LinkPolicy.LISTEN_ADD);
        check("已经在听了 → 什么都不做（不重复弹提示）",
                LinkPolicy.autoListenAction(true, true, false, true) == LinkPolicy.LISTEN_ALREADY);
        check("★ 用户手动关过它 → 永远不再自动加（必须可撤销）",
                LinkPolicy.autoListenAction(true, false, true, true) == LinkPolicy.LISTEN_REVOKED);
        check("全局开关关掉 → 不自动加",
                LinkPolicy.autoListenAction(false, false, false, true) == LinkPolicy.LISTEN_IGNORE);
        check("对方没在请求（它停了）→ 什么都不做（不用额外的取消消息）",
                LinkPolicy.autoListenAction(true, false, false, false) == LinkPolicy.LISTEN_IGNORE);
    }

    // ── ① 共享音量两端同步 ──

    private static void part3GainSync() {
        System.out.println();
        System.out.println("== 3. ★ 共享音量：一个增益两端一个值，谁后改以谁为准 ==");
        check("对端的时间戳更新 → 采纳它（1000 > 500）",
                LinkPolicy.adoptRemoteGain(1000, 500, 3000, 0.8f, 1.0f));
        check("对端的时间戳更旧 → 不采纳（我们刚改的更新）",
                !LinkPolicy.adoptRemoteGain(500, 1000, 300, 0.8f, 1.0f));
        check("值一样 → 不采纳（避免两端来回弹）",
                !LinkPolicy.adoptRemoteGain(9999, 1, 9999, 0.8f, 0.8f));
        check("★ 时钟偏差兜底：时间戳更旧，但我们本地 6 秒没动过 → 还是采纳",
                LinkPolicy.adoptRemoteGain(1, 999999, 6000, 0.5f, 1.0f));
        check("本地刚改过（1 秒前）→ 不采纳对端那条旧广播",
                !LinkPolicy.adoptRemoteGain(1, 999999, 1000, 0.5f, 1.0f));
        check("从没本地改过（NEVER）→ 对端说多少就多少",
                LinkPolicy.adoptRemoteGain(0, 0, LinkPolicy.NEVER, 0.3f, 1.0f));
        check("增益夹在 0.00~1.50", LinkPolicy.clampGain(9f) == 1.5f
                && LinkPolicy.clampGain(-2f) == 0f && LinkPolicy.clampGain(Float.NaN) == 1f);
    }

    // ── 协议：复用 T_STATS 加字段，新旧混跑 ──

    /** 收集 LinkPolicy 读出来的新字段，用来断言解析结果。 */
    private static final class Fields implements LinkPolicy.LinkFields {
        boolean wantListen;
        float gain = -2f;
        long gainTs;
        float gainRx = -2f;
        long gainRxTs;
        boolean gainUnset = true;
        boolean gainRxUnset = true;

        @Override
        public void setWantListen(boolean v) {
            wantListen = v;
        }

        @Override
        public void setGain(float g, long ts, boolean unset) {
            gain = g;
            gainTs = ts;
            gainUnset = unset;
        }

        @Override
        public void setGainRx(float g, long ts, boolean unset) {
            gainRx = g;
            gainRxTs = ts;
            gainRxUnset = unset;
        }
    }

    private static void part4Protocol() {
        System.out.println();
        System.out.println("== 4. ★ 控制包协议：复用 T_STATS(ptype=7) 的 JSON 加字段，新旧混跑 ==");
        System.out.println("   字段名：" + LinkPolicy.KEY_WANT_LISTEN + " / " + LinkPolicy.KEY_GAIN
                + " / " + LinkPolicy.KEY_GAIN_TS + " / " + LinkPolicy.KEY_GAIN_RX
                + " / " + LinkPolicy.KEY_GAIN_RX_TS);

        // 新版本发出来的包（在原有字段末尾追加）
        String modern = "{\"t\":\"stats\",\"v\":1,\"name\":\"2024TSJ\",\"sent\":100,\"bytes\":2000,"
                + "\"sending\":true,\"recv\":50,\"lost\":1,\"buf_ms\":120,\"streams\":2,"
                + "\"target\":\"192.168.0.101\",\"want_listen\":true,\"gain\":0.77,"
                + "\"gain_ts\":1700000000000,\"gain_rx\":1.25,\"gain_rx_ts\":1700000000500,"
                + "\"ts\":1700000000600}";
        Fields f = new Fields();
        LinkPolicy.readLinkFields(modern, f);
        check("want_listen 读成 true", f.wantListen);
        check("gain 读成 0.77（两位小数不丢精度，实测 " + f.gain + "）",
                !f.gainUnset && Math.abs(f.gain - 0.77f) < 0.001f);
        check("gain_ts 读成 1700000000000", f.gainTs == 1700000000000L);
        check("gain_rx 读成 1.25（实测 " + f.gainRx + "）",
                !f.gainRxUnset && Math.abs(f.gainRx - 1.25f) < 0.001f);
        check("gain_rx_ts 读成 1700000000500", f.gainRxTs == 1700000000500L);

        // 老版本发出来的包（一个键都没有）—— 必须什么都不改
        String legacy = "{\"t\":\"stats\",\"v\":1,\"name\":\"2024TSJ\",\"sent\":100,\"bytes\":2000,"
                + "\"sending\":true,\"recv\":50,\"lost\":1,\"buf_ms\":120,\"streams\":2,"
                + "\"target\":\"192.168.0.101\",\"ts\":1700000000600}";
        Fields f2 = new Fields();
        LinkPolicy.readLinkFields(legacy, f2);
        check("★ 老版本（没有新字段）→ want_listen 保持 false（不会误触发自动收听）",
                !f2.wantListen);
        check("★ 老版本 → gain 保持未设置（两端各自保留本地音量）", f2.gainUnset && f2.gainRxUnset);
        check("★ 老版本不抛异常、也不改动任何字段", f2.gainTs == 0 && f2.gainRxTs == 0);

        // 半截包 / 乱码 / 越界值
        Fields f3 = new Fields();
        LinkPolicy.readLinkFields("{\"t\":\"stats\",\"want_listen\":tr", f3);
        check("半截的 true（\"tr\"）→ 不当成 true（保守处理）", !f3.wantListen);
        Fields f4 = new Fields();
        LinkPolicy.readLinkFields("{\"t\":\"stats\",\"gain\":9.99,\"gain_ts\":1}", f4);
        check("越界的 gain=9.99 被夹到 1.50（实测 " + f4.gain + "）",
                !f4.gainUnset && Math.abs(f4.gain - 1.5f) < 0.001f);
        Fields f5 = new Fields();
        LinkPolicy.readLinkFields(null, f5);
        LinkPolicy.readLinkFields("完全不是 JSON 的一段中文", f5);
        check("null / 纯文本都不抛异常", f5.gainUnset && !f5.wantListen);
    }

    private static void check(String what, boolean ok) {
        checks++;
        if (!ok) {
            failures++;
        }
        System.out.println("   " + (ok ? "[OK] " : "[FAIL] ") + what);
    }

    private PolicyProbe() {
    }
}
