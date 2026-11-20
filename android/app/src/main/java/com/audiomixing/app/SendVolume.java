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
 *       {@code STREAM_MUSIC} 锁在 0，但<b>乘数仍然跟随手机当前音量</b>：
 *       {@code mediaQ14 = 进静音前最后一次非零系统音量}（音量键/App 滑杆每次调整都会被
 *       记成新值，再钉回 0）。没有可用值时退回 1.0，避免静音。
 *       ★ 2.42.3 修复（真机第 3 条）：旧默认 {@code internal_only} 固定乘 1.0，
 *       真机症状就是「静音档传音，手机调音量电脑那头不变」——已废弃。</li>
 * </ul>
 *
 * <p><b>不变式</b>：两端合计只乘一次。外放档由手机乘系统音量
 * （{@code vol_in_capture=false}），静音档手机同样乘一次
 * （{@code vol_in_capture=true}）；电脑端看到档位声明后系数恒为 1.0。
 *
 * <p><b>一条限制（界面/文档必须写清）</b>：静音档下系统媒体音量被我们钉在 0，
 * 滑杆本身改 {@code STREAM_MUSIC} 的 App（B 站/抖音这类）仍能带动电脑那头音量
 * （每次调整都会被记成新值），但手机自己不会响。
 * 内部增益类 App（YouTube 播放器这类）不受影响。
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

    /** 静音档乘数策略：跟随手机最近一次设定的音量（唯一策略，见类注释）。 */
    public static final String POLICY_LAST = "last_nonzero";
    /** 旧策略（固定乘 1.0）。2.42.3 起已废弃，仅保留常量避免旧配置/旧协议解析失败。 */
    public static final String POLICY_INTERNAL = "internal_only";

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

    /** 策略归一化：静音档现在只有"跟随手机音量"一种语义，旧值一律按它处理。 */
    public static String normalizePolicy(String policy) {
        return POLICY_LAST;
    }

    /** 界面里的中文策略名。 */
    public static String policyText(String policy) {
        return "跟随手机音量（静音档用最后一次设定）";
    }

    /**
     * 采集层这一级要乘的媒体音量（Q14）。
     *
     * <p>★ 2.42.3 修复（真机第 3 条）：静音档从"固定 1.0"改成"跟随手机音量"。
     * {@code mediaVolumeRatioFor} 在静音档里返回的就是"进静音前最后一次设定值"，
     * 用户按音量键或拖 App 滑杆都会刷新它；没有可用值时才退回 1.0（绝不把声音乘成 0）。
     *
     * @param systemVolQ14   当前系统媒体音量（静音档里是"最后一次设定值"）
     * @param lastNonzeroQ14 进入静音前最后一次非零音量（0 = 没有可用值 → 按 1.0）
     */
    public static int mediaQ14(int sendMode, String policy, int systemVolQ14, int lastNonzeroQ14) {
        if (isSilent(sendMode)) {
            // ★ 2.29.3：静音档用 mute 标志让手机不响，STREAM_MUSIC 索引保留；
            //   用户/App 调多少这里就乘多少（2.18/2.26 的成功语义）。
            int tracked = lastNonzeroQ14 > 0 ? lastNonzeroQ14 : systemVolQ14;
            return tracked > 0 ? clamp(tracked) : ONE;
        }
        return clamp(systemVolQ14);                // 外放档：乘系统媒体音量（原行为）
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
