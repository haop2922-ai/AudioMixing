package com.audiomixing.app;

/**
 * 送声音档位 → 采集层媒体音量乘数。
 *
 * <p><b>2026-11-08 用户方案（替代"三档音量模式"）</b>：音量算法直接绑在送声档位上，
 * 不再向用户暴露 system/capture 这类概念。
 *
 * <ul>
 *   <li><b>外放档（{@link #MODE_EXTERNAL} / sendMode=1）</b>：采集到的 PCM 已含 App 内部增益，
 *       再乘 {@code STREAM_MUSIC} 归一值 —— {@code mediaQ14 = 系统音量}，
 *       {@code totalQ14 = gain × media / 16384}。这是原有行为。</li>
 *   <li><b>静音档（{@link #MODE_SILENT} / sendMode=2）</b>：我们为了关喇叭把
 *       {@code STREAM_MUSIC} 锁在 0，这个 0 没有信息量，<b>不乘</b>：
 *       默认 {@code mediaQ14 = 16384}，只保留 PCM 里的 App 内部增益；
 *       可选策略 {@link #POLICY_LAST} 沿用"进入静音前最后一次非零系统音量"。</li>
 * </ul>
 *
 * <p><b>不变式</b>：两端合计只乘一次。外放档由手机乘系统音量
 * （{@code vol_in_capture=false}），静音档手机不再叠加
 * （{@code vol_in_capture=true}）；电脑端看到档位声明后系数恒为 1.0。
 *
 * <p><b>两条限制（界面/文档必须写清）</b>：
 * <ol>
 *   <li>静音档乘数为 1.0 时耳机侧相当于满格放，从外放档切到静音档可能明显变响
 *       （外放档压到 25% 的信息在静音档不存在）。</li>
 *   <li>滑杆本身改 {@code STREAM_MUSIC} 的 App（B 站/抖音这类）在静音档里调音量无效 ——
 *       系统音量被钉死 0，既改不动也读不到；这类 App 要用滑杆请切外放档。
 *       内部增益类 App（YouTube 播放器这类）不受影响。</li>
 * </ol>
 *
 * <p>本类零安卓依赖，供 {@code android/tools/run_volume_mode_test.sh} 直接编译断言。
 */
public final class SendVolume {

    private SendVolume() {
    }

    /** 外放档（sendMode=1）。 */
    public static final String MODE_EXTERNAL = "external";
    /** 静音档（sendMode=2）。 */
    public static final String MODE_SILENT = "silent";

    /** 静音档乘数策略：只保留 App 内部音量（默认，推荐）。 */
    public static final String POLICY_INTERNAL = "internal_only";
    /** 静音档乘数策略：沿用进入静音前最后一次非零系统音量。 */
    public static final String POLICY_LAST = "last_nonzero";

    /** Q14 的 1.00（和 Proto.GAIN_ONE 同值；不引用 Proto 以保持零依赖）。 */
    public static final int ONE = 16384;

    public static boolean isSilent(int sendMode) {
        return sendMode >= 2;
    }

    /** 协议里的档位名（external/silent）。 */
    public static String modeName(int sendMode) {
        return isSilent(sendMode) ? MODE_SILENT : MODE_EXTERNAL;
    }

    /** 界面/诊断里的中文档位名。 */
    public static String modeText(int sendMode) {
        return isSilent(sendMode) ? "静音档（手机不响，只送声）" : "外放档（手机也响）";
    }

    /** 策略归一化；不认识的一律按默认 internal_only。 */
    public static String normalizePolicy(String policy) {
        String p = policy == null ? "" : policy.trim().toLowerCase(java.util.Locale.US);
        return POLICY_LAST.equals(p) ? POLICY_LAST : POLICY_INTERNAL;
    }

    /** 界面里的中文策略名。 */
    public static String policyText(String policy) {
        return POLICY_LAST.equals(normalizePolicy(policy))
                ? "沿用进静音前的音量" : "只用 App 内部音量（推荐）";
    }

    /**
     * 采集层这一级要乘的媒体音量（Q14）。
     *
     * @param systemVolQ14   当前系统媒体音量（静音档里 {@code mediaVolumeRatioFor} 会返回
     *                       "进静音前最后一次设定值"，正好就是 last_nonzero 语义）
     * @param lastNonzeroQ14 进入静音前最后一次非零音量（0 = 没有可用值 → 按 1.0）
     */
    public static int mediaQ14(int sendMode, String policy, int systemVolQ14, int lastNonzeroQ14) {
        if (!isSilent(sendMode)) {
            return clamp(systemVolQ14);            // 外放档：乘系统媒体音量（原行为）
        }
        if (POLICY_LAST.equals(normalizePolicy(policy))) {
            int v = lastNonzeroQ14 > 0 ? lastNonzeroQ14 : systemVolQ14;
            return v > 0 ? clamp(v) : ONE;         // 没有可用旧值就退回 1.0，避免静音
        }
        return ONE;                                // internal_only：不乘（静音的 0 无信息量）
    }

    /** 真正套到 PCM 上的合成乘数：gain × media / 16384，上限 2.0。 */
    public static int totalQ14(int gainQ14, int mediaQ14) {
        long v = ((long) gainQ14 * (long) mediaQ14) / (long) ONE;
        return (int) Math.min((long) ONE * 2L, v);
    }

    /** 统计包里的 {@code vol_in_capture}：外放档 false、静音档 true（按档位真实取值）。 */
    public static boolean volInCapture(int sendMode) {
        return isSilent(sendMode);
    }

    /** 0..1 的浮点音量 → Q14。 */
    public static int q14(float vol) {
        float v = Math.max(0f, Math.min(1f, vol));
        return Math.round(v * ONE);
    }

    /** 诊断一行：`静音档（手机不响，只送声）· 只用 App 内部音量 → media=1.00`。 */
    public static String text(int sendMode, String policy, int mediaQ14) {
        return modeText(sendMode) + " · " + policyText(policy)
                + " → media=" + String.format(java.util.Locale.US, "%.2f", mediaQ14 / (float) ONE);
    }

    private static int clamp(int v) {
        return Math.max(0, Math.min(ONE, v));
    }
}
