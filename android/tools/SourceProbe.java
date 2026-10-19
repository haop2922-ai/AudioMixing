import com.audiomixing.app.SourcePolicy;

/**
 * ★ 音源开关离线探针（2026-10-19 用户真机三条反馈）★
 *
 * <p>被测的是 {@code SourcePolicy.java} <b>本体</b>（原样编译，<b>零桩</b> —— 它是纯函数）：
 * <ul>
 *   <li>① <b>绝不自动开麦</b>：本机声音采集不可用（停止采集 / 系统收回授权 / 采不到）之后，
 *       状态一律是"已停止"而<b>绝不是麦克风</b>（用户原话："严重危害用户安全"）；</li>
 *   <li>② <b>两颗按钮是真 toggle</b>：点击未激活 = 激活、点击已激活 = 关闭，
 *       而且"点一下状态一定变"（不许出现"点了按钮但状态没变化"）；</li>
 *   <li>③ <b>同一时刻只能采一路</b>：本机声音亮着时麦克风必须灭、反之亦然
 *       （"两颗都亮着骗人"是用户明确禁止的）。</li>
 * </ul>
 *
 * <p>为什么值得跑：这三件事只有在真机上点按钮才试得出来，而边界（跨音源互斥、
 * 授权丢失后的落点、点了必变）恰恰最容易错。
 */
public final class SourceProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) {
        part1NeverAutoMic();
        part2RealToggle();
        part3MutualExclusive();

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    // ── ① 绝不自动开麦（隐私硬约束）──

    private static void part1NeverAutoMic() {
        System.out.println("== 1. ★ 本机声音不可用后绝不自动开麦（停止/授权丢失/采不到）==");
        check("停止本机声音采集（PLAYBACK 丢失）→ 落到「已停止」，不是麦克风",
                SourcePolicy.afterPlaybackLost(SourcePolicy.SRC_PLAYBACK) == SourcePolicy.SRC_OFF);
        check("★ 关键：落点绝不是麦克风（自动开麦是用户点名的安全事故）",
                SourcePolicy.afterPlaybackLost(SourcePolicy.SRC_PLAYBACK) != SourcePolicy.SRC_MIC);
        check("本来就是已停止 → 还是已停止（不因为授权丢失把麦克风打开）",
                SourcePolicy.afterPlaybackLost(SourcePolicy.SRC_OFF) == SourcePolicy.SRC_OFF);
        check("正在采麦克风 → 不受影响（麦克风那条路和授权无关，不该被顺手关掉）",
                SourcePolicy.afterPlaybackLost(SourcePolicy.SRC_MIC) == SourcePolicy.SRC_MIC);

        boolean anyAutoMic = false;
        int[] all = {SourcePolicy.SRC_OFF, SourcePolicy.SRC_MIC, SourcePolicy.SRC_PLAYBACK};
        for (int s : all) {
            if (SourcePolicy.afterPlaybackLost(s) == SourcePolicy.SRC_MIC
                    && s != SourcePolicy.SRC_MIC) {
                anyAutoMic = true;
            }
        }
        check("★ 遍历所有状态：只有「本来是麦克风」才可能还是麦克风，绝不会有别的状态跳进麦克风",
                !anyAutoMic);
    }

    // ── ② 真 toggle（未激活→激活、已激活→关闭）──

    private static void part2RealToggle() {
        System.out.println();
        System.out.println("== 2. ★ 两颗按钮是真 toggle：未激活→激活、已激活→关闭 ==");
        check("点未激活的「本机声音」（当前已停止）→ 打开本机声音",
                SourcePolicy.toggle(SourcePolicy.SRC_OFF, SourcePolicy.SRC_PLAYBACK)
                        == SourcePolicy.SRC_PLAYBACK);
        check("再次点已激活的「本机声音」→ 关闭（不是又激活一遍）",
                SourcePolicy.toggle(SourcePolicy.SRC_PLAYBACK, SourcePolicy.SRC_PLAYBACK)
                        == SourcePolicy.SRC_OFF);
        check("点未激活的「麦克风」（当前已停止）→ 打开麦克风",
                SourcePolicy.toggle(SourcePolicy.SRC_OFF, SourcePolicy.SRC_MIC)
                        == SourcePolicy.SRC_MIC);
        check("再次点已激活的「麦克风」→ 关闭（用户第 3 条那个 bug 的回归）",
                SourcePolicy.toggle(SourcePolicy.SRC_MIC, SourcePolicy.SRC_MIC)
                        == SourcePolicy.SRC_OFF);

        // "点了按钮但状态没变化"是用户明令禁止的：任意状态下点任意一颗，新状态必须不同。
        boolean noop = false;
        int[] states = {SourcePolicy.SRC_OFF, SourcePolicy.SRC_MIC, SourcePolicy.SRC_PLAYBACK};
        int[] kinds = {SourcePolicy.SRC_MIC, SourcePolicy.SRC_PLAYBACK};
        for (int s : states) {
            for (int k : kinds) {
                if (SourcePolicy.toggle(s, k) == s) {
                    noop = true;
                }
            }
        }
        check("★ 遍历所有状态×两颗按钮：点一下状态必定改变（不会有「点了没反应」）", !noop);

        // 视觉状态来自"引擎真实状态"：把（开关，实际在采的音源）折成一个状态值。
        check("开关开 + 实际采本机声音 → 状态=本机声音",
                SourcePolicy.state(true, SourcePolicy.SRC_PLAYBACK) == SourcePolicy.SRC_PLAYBACK);
        check("开关开 + 实际采麦克风 → 状态=麦克风",
                SourcePolicy.state(true, SourcePolicy.SRC_MIC) == SourcePolicy.SRC_MIC);
        check("开关关 → 状态=已停止（不管上次选的是哪一路）",
                SourcePolicy.state(false, SourcePolicy.SRC_PLAYBACK) == SourcePolicy.SRC_OFF
                        && SourcePolicy.state(false, SourcePolicy.SRC_MIC) == SourcePolicy.SRC_OFF);
        check("状态=本机声音 → 「本机声音」亮、且「麦克风」灭",
                SourcePolicy.lit(SourcePolicy.SRC_PLAYBACK, SourcePolicy.SRC_PLAYBACK)
                        && !SourcePolicy.lit(SourcePolicy.SRC_PLAYBACK, SourcePolicy.SRC_MIC));
        check("状态=已停止 → 两颗都不亮（不会留着上次的缓存标志位）",
                !SourcePolicy.lit(SourcePolicy.SRC_OFF, SourcePolicy.SRC_PLAYBACK)
                        && !SourcePolicy.lit(SourcePolicy.SRC_OFF, SourcePolicy.SRC_MIC));
    }

    // ── ③ 第 3 条口径：同一时刻只能采一路（互斥）──

    private static void part3MutualExclusive() {
        System.out.println();
        System.out.println("== 3. ★ 口径：同一时刻只能采一路（本机声音 与 麦克风 互斥）==");
        check("麦克风亮着时点「本机声音」→ 换成采本机声音（麦克风那颗自动灭）",
                SourcePolicy.toggle(SourcePolicy.SRC_MIC, SourcePolicy.SRC_PLAYBACK)
                        == SourcePolicy.SRC_PLAYBACK);
        check("本机声音亮着时点「麦克风」→ 换成采麦克风（本机声音那颗自动灭）",
                SourcePolicy.toggle(SourcePolicy.SRC_PLAYBACK, SourcePolicy.SRC_MIC)
                        == SourcePolicy.SRC_MIC);

        // 任意状态（含各自 toggle 之后）两颗按钮最多亮一颗 —— "两颗都亮着骗人"直接判死。
        boolean bothLit = false;
        int[] states = {SourcePolicy.SRC_OFF, SourcePolicy.SRC_MIC, SourcePolicy.SRC_PLAYBACK};
        for (int s : states) {
            if (SourcePolicy.lit(s, SourcePolicy.SRC_MIC)
                    && SourcePolicy.lit(s, SourcePolicy.SRC_PLAYBACK)) {
                bothLit = true;
            }
            for (int k : new int[]{SourcePolicy.SRC_MIC, SourcePolicy.SRC_PLAYBACK}) {
                int after = SourcePolicy.toggle(s, k);
                if (SourcePolicy.lit(after, SourcePolicy.SRC_MIC)
                        && SourcePolicy.lit(after, SourcePolicy.SRC_PLAYBACK)) {
                    bothLit = true;
                }
            }
        }
        check("★ 遍历所有状态及其 toggle 结果：两颗按钮最多亮一颗（绝不「两颗都亮」）", !bothLit);

        // 常量必须和 AudioSend 同值（那边现在是直接引用这里，钉住不让它悄悄漂）。
        check("常量：SRC_MIC == 0（AudioSend.SRC_MIC 同值）", SourcePolicy.SRC_MIC == 0);
        check("常量：SRC_PLAYBACK == 1（AudioSend.SRC_PLAYBACK 同值）", SourcePolicy.SRC_PLAYBACK == 1);
        check("常量：SRC_OFF == -1（不在 AudioSend 里，是界面/引擎内部用的「没在采」）",
                SourcePolicy.SRC_OFF == -1);
        check("本机声音能不能选看系统版本（<Android 10 不可选）；麦克风永远可选",
                !SourcePolicy.selectable(SourcePolicy.SRC_PLAYBACK, false)
                        && SourcePolicy.selectable(SourcePolicy.SRC_PLAYBACK, true)
                        && SourcePolicy.selectable(SourcePolicy.SRC_MIC, false));
    }

    private static void check(String what, boolean ok) {
        checks++;
        if (!ok) {
            failures++;
        }
        System.out.println("   " + (ok ? "[OK] " : "[FAIL] ") + what);
    }

    private SourceProbe() {
    }
}
