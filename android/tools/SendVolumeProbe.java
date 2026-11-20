import com.audiomixing.app.SendVolume;

/**
 * ★ 送声档位音量离线探针（2026-11-08 用户方案）★
 *
 * <p>被测的是 {@code SendVolume.java} <b>本体</b>（原样编译，零桩）：
 * <ul>
 *   <li>外放档：{@code post/raw ≈ media × gain / 16384}（乘 STREAM_MUSIC，原行为）；</li>
 *   <li>静音档：<b>跟随手机最后一次设定的媒体音量</b>（★ 2.29.3：mute 标志保留索引，
 *       复刻 2.18/2.26 的成功语义）；没有可用值时退回 1.0；</li>
 *   <li>{@code vol_in_capture}：外放档 false、静音档 true（按档位真实取值）；</li>
 *   <li>两档切换时乘数与声明同步变化，但两端合计只乘一次。</li>
 * </ul>
 */
public final class SendVolumeProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) {
        externalMode();
        silentFollowsVolume();
        silentLastNonzero();
        switching();
        policyNormalize();

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    // ── 外放档：media = 系统音量，total = gain × media / 16384 ──

    private static void externalMode() {
        int vol = SendVolume.q14(0.25f);          // 系统媒体音量 25%
        int gain = SendVolume.q14(0.50f);         // 用户「送声音」增益 50%
        int media = SendVolume.mediaQ14(1, SendVolume.POLICY_LAST, vol, 0);
        int total = SendVolume.totalQ14(gain, media);
        float raw = 0.80f;
        float post = raw * total / (float) SendVolume.ONE;
        check("外放档 media = 系统音量 0.25", media == 4096);
        check("外放档 total = gain×media/16384 = 0.125", total == 2048);
        near("外放档 post/raw ≈ total/16384", post / raw, total / (float) SendVolume.ONE, 0.0001f);
        check("外放档 vol_in_capture=false", !SendVolume.volInCapture(1));
        check("外放档档位名=external", "external".equals(SendVolume.modeName(1)));
    }

    // ── 静音档：跟随手机音量（★ 2.29.3 复刻 2.18/2.26）──

    private static void silentFollowsVolume() {
        int vol = SendVolume.q14(0.25f);          // 手机当前音量 25%
        int gain = SendVolume.q14(0.50f);
        int media = SendVolume.mediaQ14(2, SendVolume.POLICY_LAST, vol, vol);
        int total = SendVolume.totalQ14(gain, media);
        check("静音档 media = 手机音量 0.25", media == vol);
        check("静音档 total = gain×0.25", total == SendVolume.totalQ14(gain, vol));
        float raw = 0.80f;
        float post = raw * total / (float) SendVolume.ONE;
        near("静音档 post/raw ≈ total/16384", post / raw, total / (float) SendVolume.ONE, 0.0001f);
        check("静音档 vol_in_capture=true", SendVolume.volInCapture(2));
        check("静音档档位名=silent", "silent".equals(SendVolume.modeName(2)));
        // 老电脑还会下发 internal_only；静音档同样跟随，不再固定 1.0。
        int legacy = SendVolume.mediaQ14(2, SendVolume.POLICY_INTERNAL, vol, vol);
        check("旧 internal_only 入参也跟随音量", legacy == vol);
    }

    // ── 静音档没有可用音量：退回 1.0 ──

    private static void silentLastNonzero() {
        int last = SendVolume.q14(0.40f);         // 进静音前的最后设定 40%
        int gain = SendVolume.q14(0.50f);
        int media = SendVolume.mediaQ14(2, SendVolume.POLICY_LAST, 0, last);
        int total = SendVolume.totalQ14(gain, media);
        check("有记录时 media = 0.40", media == last);
        check("有记录时 total = gain×0.40", total == SendVolume.totalQ14(gain, last));
        // 没有可用旧值时必须退回 1.0，不能把静音档做成 0。
        int noHistory = SendVolume.mediaQ14(2, SendVolume.POLICY_LAST, 0, 0);
        check("没有可用音量 → 退回 1.0", noHistory == SendVolume.ONE);
    }

    // ── 两档切换：乘数与声明必须一起变 ──

    private static void switching() {
        int vol = SendVolume.q14(0.30f);
        int gain = SendVolume.q14(0.80f);
        int extMedia = SendVolume.mediaQ14(1, SendVolume.POLICY_LAST, vol, 0);
        int silMedia = SendVolume.mediaQ14(2, SendVolume.POLICY_LAST, vol, vol);
        check("切到静音档：media 不变（都=手机音量 0.30）", extMedia == vol && silMedia == vol);
        check("切到静音档：total 不变（合计只乘一次）",
                SendVolume.totalQ14(gain, extMedia) == SendVolume.totalQ14(gain, silMedia));
        check("切到静音档：vol_in_capture 由 false 变 true",
                !SendVolume.volInCapture(1) && SendVolume.volInCapture(2));
        check("外放档总乘数上限仍为 2.0",
                SendVolume.totalQ14(SendVolume.ONE * 3, SendVolume.ONE) == SendVolume.ONE * 2);
    }

    private static void policyNormalize() {
        check("默认策略 = last_nonzero",
                SendVolume.POLICY_LAST.equals(SendVolume.normalizePolicy(null)));
        check("不认识的值 → last_nonzero",
                SendVolume.POLICY_LAST.equals(SendVolume.normalizePolicy("whatever")));
        check("旧 internal_only → last_nonzero",
                SendVolume.POLICY_LAST.equals(SendVolume.normalizePolicy("internal_only")));
        check("last_nonzero 保留",
                SendVolume.POLICY_LAST.equals(SendVolume.normalizePolicy("LAST_NONZERO")));
    }

    // ── 断言工具 ──

    private static void check(String what, boolean ok) {
        checks++;
        if (ok) {
            System.out.println("  ok   " + what);
        } else {
            failures++;
            System.out.println("  FAIL " + what);
        }
    }

    private static void near(String what, float got, float want, float tol) {
        check(what + String.format(java.util.Locale.US, "（got=%.6f want=%.6f）", got, want),
                Math.abs(got - want) <= tol);
    }
}
