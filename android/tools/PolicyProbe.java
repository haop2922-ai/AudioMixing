import com.audiomixing.app.LinkPolicy;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * ★ 链路策略离线探针（task-17：对端消失要停发 + 自动收听 + 共享音量同步）★
 *
 * <p>被测的是 {@code LinkPolicy.java} <b>本体</b>（原样编译，<b>零桩</b> —— 它是纯函数）：
 * <ul>
 *   <li>④ 对端是不是"确认走了"（宽限期 18 秒、两个判据取更保守的那个）；</li>
 *   <li>① 自动收听（2026-10-18 用户第 6 条：判据只剩每台设备各自的「智连」开关，
 *       默认关；含用户第 1 条那个"关过一次就永不再自动"卡死 bug 的回归）；</li>
 *   <li>① 共享音量谁后改以谁为准（含时钟偏差兜底）；</li>
 *   <li>控制包新字段的解析（{@code Diagnosis.parse} 调的就是这一份，所以线上跑的就是这里测的）；</li>
 *   <li>④ 我在不在对端的蓝牙名单里（决定"送声音"前要不要先问走蓝牙还是 WiFi）。</li>
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
        part5BtRoster();
        part6QualityAndRevoke();

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

        // ★ 断联 2 分钟后卡片直接清掉（用户口径：「这种已经信号断联两分钟的就直接删掉就好了，
        //   不然全堆积在程序里」）。判据在 LinkPolicy.cardStale，Engine.peers() 就按它过滤列表。
        check("★ 刚断联（119.9 秒）→ 卡片还在（不到 2 分钟不清）",
                !LinkPolicy.cardStale(LinkPolicy.CARD_STALE_MS - 1));
        check("★ 断联满 2 分钟（120 秒）→ 卡片被清掉",
                LinkPolicy.cardStale(LinkPolicy.CARD_STALE_MS));
        check("★ 真机那张截图的情形（断联 128 秒）→ 卡片被清掉",
                LinkPolicy.cardStale(128000L));
        check("刚刚还在发信标（2 秒前）→ 卡片当然不清", !LinkPolicy.cardStale(2000));
        check("从来没见过它（NEVER）→ 不算陈旧（那是「还没出现」，不是「走了」）",
                !LinkPolicy.cardStale(LinkPolicy.NEVER));
        check("清卡片的阈值就是 2 分钟（用户口径）", LinkPolicy.CARD_STALE_MS == 120000L);
    }

    // ── ① 自动收听 ──

    private static void part2AutoListen() {
        System.out.println();
        System.out.println("== 2. ★ 每台设备一个「智连」开关 → 我这边自动打开收听（可收回）==");
        check("这台开着「智连」+ 对端在请求 + 我没在听 → 自动加进收听",
                LinkPolicy.listenAction(true, false, true, false) == LinkPolicy.LISTEN_ADD);
        check("已经在听了 → 什么都不做（不重复加）",
                LinkPolicy.listenAction(true, true, true, false) == LinkPolicy.LISTEN_ALREADY);
        check("这台「智连」没开 → 不自动加（默认关，要自己点「🎧 收听」）",
                LinkPolicy.listenAction(false, false, true, false) == LinkPolicy.LISTEN_IGNORE);
        check("对端没在请求、我也没在听 → 什么都不做",
                LinkPolicy.listenAction(true, false, false, false) == LinkPolicy.LISTEN_IGNORE);
        // ★ 2026-10-18 用户第 1 条真机 bug 回归：
        //   旧代码用户手动关过一次收听就把那台设备永久记进 listenRevoked，之后对端再怎么点
        //   「送给它」，这边都永远返回 LISTEN_REVOKED —— 电脑一直在请求，那份记忆就永不过期，
        //   于是"电脑主动发送声音手机不会主动接收"。现在判据只剩「智连」开关，只要它开着，
        //   对端一直请求就该【每一轮都返回 ADD】，不存在会被"关过一次"卡住的中间态。
        boolean stuck = false;
        for (int i = 0; i < 5; i++) {
            if (LinkPolicy.listenAction(true, false, true, false) != LinkPolicy.LISTEN_ADD) {
                stuck = true;
            }
        }
        check("★ 回归（用户第 1 条）：「智连」开着 + 对端一直请求 → 连续 5 轮都是 ADD，不会卡死", !stuck);
        // ★ 2026-10-15 用户反馈第 3 条「电脑停止送音后手机依旧收音不停止」：
        //   对端明确说"我不送了"（want_listen=false），我们替它自动开的那一条必须收回去 ——
        //   否则它会一直收下去（电脑那边是有状态的、自己关掉了；我们这边只会保持上次的状态）。
        check("★ 它说不送了 + 这条是我们替它自动开的 → 收回",
                LinkPolicy.listenAction(true, true, false, true) == LinkPolicy.LISTEN_REMOVE);
        check("★ 它说不送了 + 但这条是用户自己点开的 → 一律不碰（撤销不是惩罚）",
                LinkPolicy.listenAction(true, true, false, false) == LinkPolicy.LISTEN_IGNORE);
        check("它说不送了 + 我本来就没在听 → 什么都不做",
                LinkPolicy.listenAction(true, false, false, true) == LinkPolicy.LISTEN_IGNORE);
    }

    // ── ① 共享音量两端同步 ──

    private static void part3GainSync() {
        System.out.println();
        System.out.println("== 3. ★ 共享音量：一个增益两端一个值，按「最后到达的为准」（不碰时钟） ==");
        check("对端报了个不同的值、我们最近没动过 → 采纳它",
                LinkPolicy.adoptRemoteGain(1000, 500, 3000, 0.8f, 1.0f));
        check("本地刚改过（0.3 秒前）→ 不采纳（用户还在拖，别被对端拉走）",
                !LinkPolicy.adoptRemoteGain(500, 1000, 300, 0.8f, 1.0f));
        check("值一样 → 不采纳（避免两端来回弹）",
                !LinkPolicy.adoptRemoteGain(9999, 1, 9999, 0.8f, 0.8f));
        check("★ 对端时钟慢（时间戳更旧）、我们 2 秒没动过 → 也要立刻采纳："
                        + "以前这条要等满 5 秒，正是用户报的「拖完音量要 5 秒才同步」",
                LinkPolicy.adoptRemoteGain(1, 999999, 2000, 0.5f, 1.0f));
        check("本地 1 秒前刚改过、对端又报了个旧时间戳 → 先不采纳",
                !LinkPolicy.adoptRemoteGain(1, 999999, 1000, 0.5f, 1.0f));
        check("从没本地改过（NEVER）→ 对端说多少就多少",
                LinkPolicy.adoptRemoteGain(0, 0, LinkPolicy.NEVER, 0.3f, 1.0f));
        check("增益夹在 0.00~2.00（和电脑端同一个上限）",
                LinkPolicy.clampGain(9f) == 2f
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
        check("越界的 gain=9.99 被夹到 2.00（实测 " + f4.gain + "）",
                !f4.gainUnset && Math.abs(f4.gain - 2f) < 0.001f);
        Fields f5 = new Fields();
        LinkPolicy.readLinkFields(null, f5);
        LinkPolicy.readLinkFields("完全不是 JSON 的一段中文", f5);
        check("null / 纯文本都不抛异常", f5.gainUnset && !f5.wantListen);
    }

    // ── ④ "送声音"前先判断我在不在电脑的蓝牙名单里 ──

    private static void part5BtRoster() {
        System.out.println();
        System.out.println("== 5. ★ 我在不在对端的蓝牙名单里（决定送声音前要不要先问走蓝牙还是 WiFi）==");
        List<String> none = Collections.emptyList();

        check("电脑没给名单（老版本电脑没有 bt 键 / 空表）→ 不匹配（直接走 WiFi）",
                !LinkPolicy.btNameMatches("小红的手机", none));
        check("名单是 null → 不匹配（当空表处理，不能是 null 崩了）",
                !LinkPolicy.btNameMatches("小红的手机", null));

        check("完全同名 → 匹配（会先问一句走蓝牙还是 WiFi）",
                LinkPolicy.btNameMatches("小红的手机", Arrays.asList("小红的手机")));
        check("大小写 + 首尾空格不同也算同名（两边都 trim + 小写）",
                LinkPolicy.btNameMatches("  XiaoHong ", Arrays.asList("xiaohong")));

        // 系统会给同型号设备的名字加短后缀：电脑看到的和手机自报的常常就差这一截。
        check("★ 名单里带后缀（小红的手机_2）→ 包含关系也算匹配",
                LinkPolicy.btNameMatches("小红的手机", Arrays.asList("小红的手机_2")));
        check("★ 反过来（我自报的更长，名单里是它的一段）→ 也算匹配",
                LinkPolicy.btNameMatches("小红的手机（客厅）", Arrays.asList("小红的手机")));

        check("名单里只有别的设备（平板）→ 不匹配（直接走 WiFi）",
                !LinkPolicy.btNameMatches("小红的手机", Arrays.asList("平板", "客厅音响")));
        check("名字为空 → 不匹配",
                !LinkPolicy.btNameMatches("", Arrays.asList("小红的手机")));
        check("名字只有空白 → 不匹配",
                !LinkPolicy.btNameMatches("   ", Arrays.asList("小红的手机")));

        // 单字符参与匹配会"匹配一切"（"a" 会命中 Apple/Air），所以两边都要挡掉。
        check("★ 名单里是单字符（a）→ 不参与匹配（防止匹配一切）",
                !LinkPolicy.btNameMatches("小红的手机", Arrays.asList("a")));
        check("★ 我这边是单字符（a）→ 也不匹配（防止匹配一切）",
                !LinkPolicy.btNameMatches("a", Arrays.asList("Apple")));
    }

    // ── ★ 2026-10-16（用户第 7/8 条）：信号质量条 + 撤销只撤这一次 ──

    private static void part6QualityAndRevoke() {
        System.out.println();
        System.out.println("== 6. ★ 信号质量条（只用实测计数）+ 智连开关的回归 ==");
        // 信号质量：0~100 全靠实测计数器算；没有依据必须是 -1，不能拿 0 冒充"链路很差"。
        check("一个包都没统计到 → -1（「不可测」，不是 0 分）",
                LinkPolicy.qualityScore(0, 0, 0) == -1);
        check("收到 1000 包、一个没丢、缓冲正常 → 满分",
                LinkPolicy.qualityScore(1000, 0, 40) == 100);
        check("丢 2.5%（25/1000）→ 扣约 10 分",
                LinkPolicy.qualityScore(975, 25, 40) == 90);
        check("丢 25%（250/1000）→ 扣满 100 分 → 0 分（不会变负数）",
                LinkPolicy.qualityScore(750, 250, 40) == 0);
        check("缓冲很深（160ms）再多扣 30 分",
                LinkPolicy.qualityScore(1000, 0, 160) == 70);
        check("缓冲不算深（60ms 以内）不额外扣",
                LinkPolicy.qualityScore(1000, 0, 60) == 100);
        // ★ 2026-10-18 用户第 1 条真机 bug 回归：「撤销过就永不再自动」的黑名单已随旧判据一起删掉。
        //   判据只剩「智连」开关，所以只要它开着，对端持续请求就【一直】返回 ADD ——
        //   不存在"关过一次就永久卡死"的中间态（旧代码正是这里让手机再也不自动接收电脑的声音）。
        check("★ 「智连」开着 + 对端每一轮都在请求 → 每一轮都是 ADD（不会再被「关过一次」卡死）",
                LinkPolicy.listenAction(true, false, true, false) == LinkPolicy.LISTEN_ADD
                        && LinkPolicy.listenAction(true, false, true, false) == LinkPolicy.LISTEN_ADD
                        && LinkPolicy.listenAction(true, false, true, false) == LinkPolicy.LISTEN_ADD);
        check("★「智连」默认关 → 对端请求也不自动加（安全默认：必须用户自己开）",
                LinkPolicy.listenAction(false, false, true, false) == LinkPolicy.LISTEN_IGNORE);
        check("「智连」开着 + 已经在听 → ALREADY（不重复加，也不会来回弹）",
                LinkPolicy.listenAction(true, true, true, true) == LinkPolicy.LISTEN_ALREADY);
        // ★ 2026-10-16 用户（图1）：「虽然二者没有收发声音，但是设备间信号应当存在」——
        //   没有音频流时退回心跳新鲜度：链路本身一直在，不能写"没有依据"。
        check("★ 刚收到心跳（1 秒前）→ 满分（没有音频流时也看得到信号）",
                LinkPolicy.heartbeatScore(1000) == 100);
        check("心跳 2.5 秒前 → 仍是满分（心跳本来就该 2 秒一次）",
                LinkPolicy.heartbeatScore(2500) == 100);
        check("心跳 5 秒前 → 80 分（迟到了）", LinkPolicy.heartbeatScore(5000) == 80);
        check("心跳 10 秒前 → 40 分", LinkPolicy.heartbeatScore(10000) == 40);
        check("心跳 15 秒前 → 0 分（差一步就判离线）", LinkPolicy.heartbeatScore(15000) == 0);
        check("负的年龄（时钟回拨）不当成负数分", LinkPolicy.heartbeatScore(-5000) == 100);
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
