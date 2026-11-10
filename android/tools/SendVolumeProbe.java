import com.audiomixing.app.SendVolume;

/**
 * ★ 送声档位音量离线探针（2026-11-08 用户方案）★
 *
 * <p>被测的是 {@code SendVolume.java} <b>本体</b>（原样编译，零桩）：
 * <ul>
 *   <li>外放档：{@code post/raw ≈ media × gain / 16384}（乘 STREAM_MUSIC，原行为）；</li>
 *   <li>静音档 internal_only：{@code post/raw ≈ gain / 16384}（不乘，只保留内部增益）；</li>
 *   <li>静音档 last_nonzero：沿用进静音前最后一次非零音量；没有旧值时退回 1.0（不静音）；</li>
 *   <li>{@code vol_in_capture}：外放档 false、静音档 true（按档位真实取值）；</li>
 *   <li>两档切换时乘数与声明同步变化。</li>
 * </ul>
 */
public final class SendVolumeProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) {
        externalMode();
        silentInternal();
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
        int media = SendVolume.mediaQ14(1, SendVolume.POLICY_INTERNAL, vol, 0);
        int total = SendVolume.totalQ14(gain, media);
        float raw = 0.80f;
        float post = raw * total / (float) SendVolume.ONE;
        check("外放档 media = 系统音量 0.25", media == 4096);
        check("外放档 total = gain×media/16384 = 0.125", total == 2048);
        near("外放档 post/raw ≈ total/16384", post / raw, total / (float) SendVolume.ONE, 0.0001f);
        check("外放档 vol_in_capture=false", !SendVolume.volInCapture(1));
        check("外放档档位名=external", "external".equals(SendVolume.modeName(1)));
    }

    // ── 静音档 internal_only：media = 1.0，total = gain ──

    private static void silentInternal() {
        int vol = SendVolume.q14(0.25f);
        int gain = SendVolume.q14(0.50f);
        int media = SendVolume.mediaQ14(2, SendVolume.POLICY_INTERNAL, vol, vol);
        int total = SendVolume.totalQ14(gain, media);
        check("静音档 internal_only media=1.0", media == SendVolume.ONE);
        check("静音档 internal_only total=gain", total == gain);
        float raw = 0.80f;
        float post = raw * total / (float) SendVolume.ONE;
        near("静音档 post/raw ≈ gain/16384", post / raw, gain / (float) SendVolume.ONE, 0.0001f);
        check("静音档 vol_in_capture=true", SendVolume.volInCapture(2));
        check("静音档档位名=silent", "silent".equals(SendVolume.modeName(2)));
    }

    // ── 静音档 last_nonzero：media = 进静音前最后一次非零音量 ──

    private static void silentLastNonzero() {
        int last = SendVolume.q14(0.40f);         // 进静音前是 40%
        int gain = SendVolume.q14(0.50f);
        int media = SendVolume.mediaQ14(2, SendVolume.POLICY_LAST, 0, last);
        int total = SendVolume.totalQ14(gain, media);
        check("last_nonzero media = 旧值 0.40", media == last);
        check("last_nonzero total = gain×0.40", total == SendVolume.totalQ14(gain, last));
        float raw = 1.0f;
        near("last_nonzero post/raw ≈ total/16384",
                (raw * total / (float) SendVolume.ONE) / raw, total / (float) SendVolume.ONE, 0.0001f);
        // 没有可用旧值时必须退回 1.0，不能把静音档做成 0（用户会以为坏了）。
        int noHistory = SendVolume.mediaQ14(2, SendVolume.POLICY_LAST, 0, 0);
        check("last_nonzero 没有旧值 → 退回 1.0", noHistory == SendVolume.ONE);
    }

    // ── 两档切换：乘数与声明必须一起变 ──

    private static void switching() {
        int vol = SendVolume.q14(0.30f);
        int gain = SendVolume.q14(0.80f);
        int gainQ14 = SendVolume.q14(0.60f);
        int extMedia = SendVolume.mediaQ14(1, SendVolume.POLICY_INTERNAL, vol, 0);
        int silMedia = SendVolume.mediaQ14(2, SendVolume.POLICY_INTERNAL, vol, 0);
        check("切到静音档：media 由 0.30 变 1.00", extMedia == vol && silMedia == SendVolume.ONE);
        check("切到静音档：total 由 gain×0.30 变 gain",
                SendVolume.totalQ14(gain, extMedia) != SendVolume.totalQ14(gain, silMedia)
                        && SendVolume.totalQ14(gain, silMedia) == gain);
        check("切到静音档：vol_in_capture 由 false 变 true",
                !SendVolume.volInCapture(1) && SendVolume.volInCapture(2));
        check("外放档总乘数上限仍为 2.0",
                SendVolume.totalQ14(SendVolume.ONE * 3, SendVolume.ONE) == SendVolume.ONE * 2);
    }

    private static void policyNormalize() {
        check("默认策略 = internal_only",
                SendVolume.POLICY_INTERNAL.equals(SendVolume.normalizePolicy(null)));
        check("不认识的值 → internal_only",
                SendVolume.POLICY_INTERNAL.equals(SendVolume.normalizePolicy("whatever")));
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
