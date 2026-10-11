/**
 * 抖动缓冲（AudioRecv.Stream）的离线测试 —— 纯 JVM，不依赖安卓、不需要真机和模拟器。
 *
 * <h3>为什么要有它</h3>
 * 这一小段是整条音频链路里最容易出错、又最难在真机上复现的部分：序号回绕、乱序到达、
 * 丢包补静音、断流恢复、缓冲积压。这些东西在真机上要靠"听"来判断，出了问题也说不清是哪一步；
 * 放到 JVM 上就是纯算术，可以逐帧断言。
 *
 * <h3>它怎么拿到被测代码</h3>
 * <b>不要直接编译这个文件</b> —— 它自己是不完整的：里面没有 Stream 类。
 * {@code run_jitter_test.sh} 会按大括号配平，把 {@code app/src/main/java/com/audiomixing/app/AudioRecv.java}
 * 里的 {@code private final class Stream} <b>原样抽出来</b>插到下面那行 INSERT 标记处，
 * 只做两处机械替换：
 * <pre>
 *   private final class Stream   ->  static final class Stream   （改成静态嵌套，好在本文件里直接用）
 *   SystemClock.elapsedRealtime() ->  JitterProbe.now           （换成可控时钟，才能一步步推进时间）
 * </pre>
 * 逻辑一行都不改，所以这里测的就是仓库里正在跑的那段代码。
 *
 * <h3>怎么跑</h3>
 * <pre>
 *     bash android/tools/run_jitter_test.sh
 * </pre>
 * 全部通过时输出 {@code RESULT: PASS} 并返回 0；任何一项失败返回非 0（可以直接进 CI）。
 *
 * <p>被测代码的约定（改了就要同步改这里）：一帧 480 帧立体声 PCM16 = 1920 字节；
 * 缓冲 32 槽 = 320ms；播放线程每 10ms 调一次 {@code pump}；{@code 0} 表示"这一 tick 这条流不出声"，
 * {@code 1} 表示在播（可能是补的静音）。
 */
import java.util.Arrays;

public class JitterProbe {

    /** 与 AudioRecv 里的同名常量保持一致（抽取出来的代码直接用这些名字）。 */
    static final int SLOTS = 32;
    static final int MASK = SLOTS - 1;
    static final int FRAME_BYTES = 1920;
    static final float PEAK_DECAY = 0.82f;

    /** 可控的单调时钟：对应被测代码里的 SystemClock.elapsedRealtime()，单位毫秒。 */
    static long now = 10_000L;
    /** 对应 AudioRecv.bump(7)：每发生一次"断流恢复"加一。 */
    static int recoveries = 0;

    static void bump(int idx) {
        recoveries++;
    }

    static int fails = 0;
    static int total = 0;

    static void section(String s) {
        System.out.println();
        System.out.println("== " + s + " ==");
    }

    static void check(boolean ok, String what) {
        total++;
        if (!ok) {
            fails++;
        }
        // 只用 ASCII 标记：[OK]/[NG] 在任何控制台编码下都能看清，中文说明可能乱码但结论不会。
        System.out.println("   " + (ok ? "[OK] " : "[NG] ") + what);
    }

    // ---------------------------------------------------------------- 驱动

    /** 用序号编一个唯一且非零的样本值：0 留给"静音补齐"，好区分。 */
    static int sampleOf(int seq) {
        int v = (seq * 7 + 3) & 0x7FFF;
        return v == 0 ? 1 : v;
    }

    /** 模拟收到一包：整帧填成 sampleOf(seq)，和真实链路一样是交错立体声 PCM16。 */
    static void feed(Stream s, int seq) {
        byte[] p = new byte[FRAME_BYTES];
        int v = sampleOf(seq);
        for (int i = 0; i < FRAME_BYTES; i += 2) {
            p[i] = (byte) v;
            p[i + 1] = (byte) (v >> 8);
        }
        s.store(seq, p, FRAME_BYTES, false);
    }

    /** 模拟对方明确标了静音标志的包（空负载 + FLAG_SILENCE）。 */
    static void feedSilent(Stream s, int seq) {
        s.store(seq, new byte[0], 0, true);
    }

    /** 播放线程跑一个 tick：返回 {是否在播, 取到的样本值(0 = 补的静音)}。 */
    static int[] tick(Stream s) {
        byte[] mix = new byte[FRAME_BYTES];
        int r = s.pump(mix, now, 120, 200);
        return new int[]{r, (short) ((mix[0] & 0xFF) | ((mix[1] & 0xFF) << 8))};
    }

    /** 只推进时间、不出数据：模拟对方静默。 */
    static int silenceTicks(Stream s, int n) {
        int silent = 0;
        for (int i = 0; i < n; i++) {
            int[] r = tick(s);
            if (r[0] != 0 && r[1] == 0) {
                silent++;
            }
            now += 10;
        }
        return silent;
    }

    // >>> STREAM_SOURCE_INSERT_HERE <<<

    public static void main(String[] args) {
        System.out.println("抖动缓冲离线测试（被测代码从 AudioRecv.java 原样抽取）");

        // ---- 1. 匀速到达：先预缓冲，然后一帧不差地按序号播 ----
        section("1. 匀速到达（预缓冲 + 顺序播放）");
        {
            Stream s = new Stream("a", "10.0.0.2", 1);
            int seq = 1000, expect = 1000, plays = 0, mismatch = 0, silence = 0, prebufTicks = 0;
            for (int i = 0; i < 200; i++) {
                feed(s, seq++);
                int[] r = tick(s);
                if (r[0] == 0) {
                    prebufTicks++;
                } else {
                    if (r[1] == 0) {
                        silence++;
                    } else if (r[1] == sampleOf(expect)) {
                        plays++;
                    } else {
                        mismatch++;
                    }
                    expect++;
                }
                now += 10;
            }
            check(prebufTicks >= 10 && prebufTicks <= 14,
                    "预缓冲 " + prebufTicks + " tick（约 " + prebufTicks * 10 + "ms，目标 120ms）");
            check(plays == 200 - prebufTicks, "顺序播出 " + plays + " 帧，期望 " + (200 - prebufTicks));
            check(mismatch == 0, "乱序/错位 " + mismatch + " 帧");
            check(silence == 0, "非丢包情况下补静音 " + silence + " 次（应为 0）");
            check(s.lost == 0, "lost=" + s.lost + "（应为 0）");
        }

        // ---- 2. 中途丢 3 包：补静音，不卡住，不把后面的包判成迟到 ----
        section("2. 丢 3 包（静音补齐）");
        {
            Stream s = new Stream("b", "10.0.0.3", 2);
            int seq = 1000;
            for (int i = 0; i < 60; i++) {
                feed(s, seq++);
                tick(s);
                now += 10;
            }
            long lostBefore = s.lost;
            long lateBefore = s.late;
            int silenceTicks = 0;
            for (int i = 0; i < 60; i++) {
                if (!(i >= 20 && i < 23)) {   // i=20..22 这三包"丢了"：跳过不发
                    feed(s, seq);
                }
                seq++;
                int[] r = tick(s);
                if (r[0] != 0 && r[1] == 0) {
                    silenceTicks++;
                }
                now += 10;
            }
            check(silenceTicks == 3, "补静音 " + silenceTicks + " 次（应为 3；播放头滞后缓冲深度，"
                    + "洞要过 ~120ms 才轮到播放头，所以不是「丢的那一刻」就静音）");
            check(s.lost - lostBefore == 3, "lost 增加 " + (s.lost - lostBefore) + "（应为 3）");
            check(s.late - lateBefore == 0, "后续包被判迟到 " + (s.late - lateBefore) + " 个（应为 0）");
            int tailSilence = 0;
            for (int i = 0; i < 40; i++) {
                feed(s, seq++);
                int[] r = tick(s);
                if (r[0] != 0 && r[1] == 0) {
                    tailSilence++;
                }
                now += 10;
            }
            check(tailSilence == 0, "洞过去之后补静音 " + tailSilence + " 次（应为 0，丢包只影响那 30ms）");
            check(s.depthMs() >= 100 && s.depthMs() <= 140,
                    "稳态缓冲深度 " + s.depthMs() + "ms（应停在 ~120ms 目标附近）");
        }

        // ---- 3. seq 从 65535 回绕到 0 ----
        section("3. uint16 序号回绕 65535 -> 0");
        {
            Stream s = new Stream("c", "10.0.0.4", 3);
            int seq = 65490, expect = 65490, plays = 0, mismatch = 0, silence = 0;
            for (int i = 0; i < 200; i++) {
                feed(s, seq);
                int[] r = tick(s);
                if (r[0] != 0) {
                    if (r[1] == 0) {
                        silence++;
                    } else {
                        if (r[1] == sampleOf(expect)) {
                            plays++;
                        } else {
                            mismatch++;
                        }
                        expect = (expect + 1) & 0xFFFF;
                    }
                }
                seq = (seq + 1) & 0xFFFF;
                now += 10;
            }
            check(plays > 180, "回绕后仍顺序播出 " + plays + " 帧");
            check(mismatch == 0, "回绕处错位 " + mismatch + " 帧（回绕算错这里会炸）");
            check(silence == 0 && s.lost == 0, "回绕被误判成丢包 " + s.lost + " 次（应为 0）");
            check(seq == ((65490 + 200) & 0xFFFF), "测试自身序号回绕正常（跑完 seq=" + seq + "）");
        }

        // ---- 4. 乱序到达：按序号归位，不额外丢 ----
        section("4. 乱序到达（2000,2002,2001,2003...）");
        {
            Stream s = new Stream("d", "10.0.0.5", 4);
            int[] order = {2000, 2002, 2001, 2003, 2005, 2004, 2006, 2008, 2007, 2009};
            int expect = 2000, plays = 0, mismatch = 0;
            for (int k = 0; k < 10; k++) {
                feed(s, order[k]);
                int[] r = tick(s);
                if (r[0] != 0 && r[1] != 0) {
                    if (r[1] == sampleOf(expect)) {
                        plays++;
                    } else {
                        mismatch++;
                    }
                    expect++;
                }
                now += 10;
            }
            for (int i = 2010; i < 2060; i++) {
                feed(s, i);
                int[] r = tick(s);
                if (r[0] != 0 && r[1] != 0) {
                    if (r[1] == sampleOf(expect)) {
                        plays++;
                    } else {
                        mismatch++;
                    }
                    expect++;
                }
                now += 10;
            }
            check(mismatch == 0, "乱序到达后仍然按序播出，错位 " + mismatch + " 帧");
            check(s.lost == 0, "因为乱序而误判丢包 " + s.lost + " 次（应为 0）");
            check(plays > 40, "播出 " + plays + " 帧");
        }

        // ---- 5. ★ 断流 2.5 秒再恢复：回到预缓冲，不自暴自弃 ----
        //      这一段是"断网/切后台 30 秒回来要能自动恢复"那条验收标准的离线对应物。
        section("5. 断流 2.5s 再恢复（曾经的真 bug 就在这里）");
        {
            Stream s = new Stream("e", "10.0.0.6", 5);
            int seq = 3000;
            for (int i = 0; i < 40; i++) {
                feed(s, seq++);
                tick(s);
                now += 10;
            }
            int recBefore = recoveries;
            silenceTicks(s, 250);          // 2.5 秒没有任何包
            check(!s.playing, "断流后停止播放（playing=false）");
            check(s.nextSeq == -1, "回到预缓冲状态（nextSeq=-1，哨兵值不许被自增破坏）");
            check(recoveries - recBefore >= 1, "计到 1 次恢复（recoveries=" + (recoveries - recBefore) + "）");

            int firstAfter = seq;
            int expect = firstAfter, plays = 0, mismatch = 0, silence = 0;
            for (int i = 0; i < 60; i++) {
                feed(s, seq);
                int[] r = tick(s);
                if (r[0] != 0) {
                    if (r[1] == 0) {
                        silence++;
                    } else if (r[1] == sampleOf(expect)) {
                        plays++;
                        expect++;
                    } else {
                        mismatch++;
                        expect++;
                    }
                }
                seq++;
                now += 10;
            }
            check(plays > 40, "恢复后重新出声 " + plays + " 帧（修复前是 0 帧 = 一路静音回不来）");
            check(mismatch == 0, "恢复后错位 " + mismatch + " 帧");
            check(silence == 0, "恢复后补静音 " + silence + " 次（应为 0）");
            check(s.depthMs() >= 100 && s.depthMs() <= 140, "恢复后缓冲深度回到 " + s.depthMs() + "ms");
        }

        // ---- 6. 对方发得比我们播得快：主动丢帧，缓冲不许无限涨 ----
        section("6. 持续过载（每 tick 3 包）");
        {
            Stream s = new Stream("f", "10.0.0.7", 6);
            int seq = 4000, maxDepth = 0, maxDepthPlaying = 0;
            for (int i = 0; i < 200; i++) {
                for (int k = 0; k < 3; k++) {
                    feed(s, seq++);
                }
                tick(s);
                int d = s.depthMs();
                maxDepth = Math.max(maxDepth, d);
                if (s.playing) {
                    // 预缓冲阶段故意攒一大段（还没开始消费），那不是"延迟失控"；
                    // 真正要盯的是"开始播之后延迟有没有被封住"。
                    maxDepthPlaying = Math.max(maxDepthPlaying, d);
                }
                now += 10;
            }
            check(maxDepthPlaying <= 260, "开播后缓冲深度封顶 " + maxDepthPlaying + "ms（丢弃阈值 200ms）");
            check(maxDepth <= 320, "预缓冲阶段最深 " + maxDepth + "ms（不许超过 32 槽 = 320ms 的容量）");
            check(s.lost > 0, "过载时主动丢了 " + s.lost + " 帧把延迟压回来");
            for (int i = 0; i < 40; i++) {
                feed(s, seq++);
                tick(s);
                now += 10;
            }
            check(s.depthMs() <= 140, "过载结束后 400ms 内回落到 " + s.depthMs() + "ms（目标 120ms）");
            check(s.received == 640, "收包计数 " + s.received + "（600 包过载 + 40 包回落 = 640）");
        }

        // ---- 7. 对端标了静音标志的包不算丢包 ----
        section("7. FLAG_SILENCE 包");
        {
            Stream s = new Stream("g", "10.0.0.8", 7);
            int seq = 5000;
            for (int i = 0; i < 40; i++) {
                if (i >= 20 && i < 25) {
                    feedSilent(s, seq++);
                } else {
                    feed(s, seq++);
                }
                tick(s);
                now += 10;
            }
            check(s.lost == 0, "静音包被算成丢包 " + s.lost + " 次（应为 0）");
            check(s.dup == 0 && s.late == 0, "静音包重复/迟到计数 " + s.dup + "/" + s.late + "（应为 0）");
        }

        System.out.println();
        if (fails == 0) {
            System.out.println("RESULT: PASS  （" + total + " 项断言全部通过）");
        } else {
            System.out.println("RESULT: FAIL  （" + fails + "/" + total + " 项断言失败）");
            System.exit(1);
        }
    }
}
