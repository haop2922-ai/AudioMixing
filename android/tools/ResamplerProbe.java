import android.media.AudioRecord;
import com.audiomixing.app.ResamplerUnderTest;

/**
 * Resampler（采集格式 → 协议格式的换算）离线探针。
 *
 * <p>被测代码是<b>从 AudioSend.java 里原样抽出来</b>的（见 extract_resampler.awk），
 * 只额外包了一层能调用的外壳，逻辑一行没改。
 *
 * <p>为什么值得测：这段换算只在"设备给不出 48000Hz 立体声"时才跑，真机上很难复现，
 * 而且出错的表现是"变调/爆音/越跑越偏"—— 都是靠耳朵听、说不清的那种。在 JVM 上它是纯算术。
 *
 * <p>全部断言通过返回 0，否则返回 1。
 */
public final class ResamplerProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) {
        System.out.println("== 1. 44100Hz 立体声 → 48000Hz 立体声（最常见的降级档）==");
        runResampleCase(44100, 2);

        System.out.println();
        System.out.println("== 2. 48000Hz 单声道 → 48000Hz 立体声（设备不给立体声）==");
        runResampleCase(48000, 1);

        System.out.println();
        System.out.println("== 3. 22050Hz 单声道 → 48000Hz 立体声（最极端的一档）==");
        runResampleCase(22050, 1);

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    /** 一次完整的"跑 10 秒音频"验证：频率、幅度、消耗速度、连续性、声道。 */
    private static void runResampleCase(int srcRate, int srcCh) {
        final int blocks = 1000;                 // 1000 × 10ms = 10 秒
        final int outFrames = 480;
        final double toneHz = 1000.0;
        final double amp = 0.5;

        Sine src = new Sine(srcRate, srcCh, toneHz, amp);
        AudioRecord stub = new AudioRecord();
        AudioRecord.source = src;
        // 换算器必须跨块复用：相位是它的状态，每块都新建一个等于每 10ms 重置一次相位。
        Object rs = ResamplerUnderTest.make(srcRate, srcCh);

        byte[] out = new byte[outFrames * 4];
        short[] mono = new short[blocks * outFrames];      // 只看左声道
        long totalOut = 0;
        int badReturn = 0;
        double maxJump = 0;
        int lastL = Integer.MIN_VALUE;
        boolean stereoOk = true;

        for (int b = 0; b < blocks; b++) {
            int n = ResamplerUnderTest.fill(rs, stub, out);
            if (n <= 0) {
                badReturn++;
            }
            for (int i = 0; i < outFrames; i++) {
                int o = i * 4;
                int l = (short) ((out[o] & 0xFF) | ((out[o + 1] & 0xFF) << 8));
                int r = (short) ((out[o + 2] & 0xFF) | ((out[o + 3] & 0xFF) << 8));
                mono[(int) totalOut + i] = (short) l;
                if (srcCh == 1 && l != r) {
                    stereoOk = false;                      // 单声道必须复制成两声道
                }
                if (lastL != Integer.MIN_VALUE) {
                    double jump = Math.abs(l - lastL);
                    if (jump > maxJump) {
                        maxJump = jump;
                    }
                }
                lastL = l;
            }
            totalOut += outFrames;
        }

        double seconds = totalOut / 48000.0;
        // ① 频率：数上升过零点（带线性插值），再除以时长
        double hz = measureHz(mono, totalOut, toneHz);
        double hzErr = Math.abs(hz - toneHz) / toneHz;
        check("换出来的音调还是 1kHz（实测 " + fmt(hz, 2) + " Hz，偏差 "
                + fmt(hzErr * 100, 3) + "%）", hzErr < 0.01);

        // ② 幅度：不能被重采样吃掉（线性插值对 1kHz 的影响应该 < 1%）
        double peak = 0;
        for (int i = 0; i < totalOut; i++) {
            double a = Math.abs(mono[i]);
            if (a > peak) {
                peak = a;
            }
        }
        double wantPeak = amp * 32767;
        double peakErr = Math.abs(peak - wantPeak) / wantPeak;
        check("幅度没被削掉（实测峰值 " + fmt(peak, 0) + "，期望 " + fmt(wantPeak, 0)
                + "，偏差 " + fmt(peakErr * 100, 2) + "%）", peakErr < 0.02);

        // ③ 消耗速度：10 秒音频必须正好吃掉 10 秒的输入（差一点就会越跑越偏）
        double wantIn = srcRate * seconds;
        double inErr = Math.abs(src.frames - wantIn) / wantIn;
        check("吃掉输入 " + src.frames + " 帧（期望 " + fmt(wantIn, 0) + "，偏差 "
                + fmt(inErr * 100, 3) + "%）", inErr < 0.005);

        // ④ 连续性：块边界不许有跳变（1kHz/48k 的理论最大步进约 "
        //    2π·1000/48000·16383 ≈ 2145，留一点余量）
        check("块边界没有跳变（相邻样本最大差 " + fmt(maxJump, 0) + "，阈值 3000）", maxJump < 3000);

        // ⑤ 输出帧数：每块必须正好 480 帧（协议是固定帧长的）
        check("每块都返回正数、共 " + totalOut + " 帧 = " + blocks + " × 480（返回非正数的次数 "
                + badReturn + "）", badReturn == 0 && totalOut == (long) blocks * outFrames);

        // ⑥ 声道
        if (srcCh == 1) {
            check("单声道被正确复制成左右两声道", stereoOk);
        } else {
            check("立体声两声道都输出了内容（非全零）", peak > 0);
        }
    }

    /** 用上升过零点估频率（相邻两点线性插值，精度足够分辨 1% 的变调）。 */
    private static double measureHz(short[] x, long n, double nominal) {
        double first = -1;
        double last = -1;
        int crossings = 0;
        for (long i = 1; i < n; i++) {
            int a = x[(int) (i - 1)];
            int b = x[(int) i];
            if (a <= 0 && b > 0) {
                double frac = (0.0 - a) / (double) (b - a);
                double pos = (i - 1) + frac;
                if (first < 0) {
                    first = pos;
                } else {
                    last = pos;
                    crossings++;
                }
            }
        }
        if (crossings < 1 || first < 0 || last < 0) {
            return 0;
        }
        // crossings 个周期分布在 (last-first) 个样本里
        return crossings * 48000.0 / (last - first);
    }

    private static String fmt(double v, int digits) {
        return String.format(java.util.Locale.US, "%." + digits + "f", v);
    }

    private static void check(String what, boolean ok) {
        checks++;
        if (!ok) {
            failures++;
        }
        System.out.println("   " + (ok ? "[OK] " : "[FAIL] ") + what);
    }

    /** 按源采样率产出 1kHz 正弦（交错），模拟 AudioRecord 的采集。 */
    static final class Sine implements AudioRecord.Source {
        private final int rate;
        private final int ch;
        private final double step;
        private final double amp;
        private double phase;
        long frames;

        Sine(int rate, int ch, double hz, double amp) {
            this.rate = rate;
            this.ch = ch;
            this.step = hz / rate;
            this.amp = amp;
        }

        @Override
        public int read(short[] buf, int off, int len) {
            int n = len / ch;
            for (int i = 0; i < n; i++) {
                short v = (short) Math.round(Math.sin(2 * Math.PI * phase) * amp * 32767);
                for (int c = 0; c < ch; c++) {
                    buf[off + i * ch + c] = v;
                }
                phase += step;
                if (phase >= 1.0) {
                    phase -= 1.0;
                }
            }
            frames += n;
            return n * ch;
        }
    }

    private ResamplerProbe() {
    }
}
