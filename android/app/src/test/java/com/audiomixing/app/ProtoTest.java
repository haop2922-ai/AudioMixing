package com.audiomixing.app;

import com.audiomixing.app.Proto.Pkt;

/**
 * Proto 的自检：**纯 JVM**，不依赖安卓，javac/java 直接就能跑。
 *
 * 它构造和电脑端 tools/check_android_proto.py 完全相同的 5 个样例包，
 * 并把编出来的字节打成 hex 打印出来。两边的 hex 逐字符相同，
 * 就证明手机和电脑说的是同一种"话"—— 这比看代码可靠得多。
 *
 *     javac -d out Proto.java ProtoTest.java && java -cp out com.audiomixing.app.ProtoTest
 */
public final class ProtoTest {

    private static int passed = 0;
    private static int failed = 0;

    private static void check(boolean ok, String label) {
        if (ok) {
            passed++;
            System.out.println("  [PASS] " + label);
        } else {
            failed++;
            System.out.println("  [FAIL] " + label);
        }
    }

    private static byte[] ramp(int n) {
        byte[] out = new byte[n];
        for (int i = 0; i < n; i++) {
            out[i] = (byte) (i & 0xFF);
        }
        return out;
    }

    /** 与 tools/check_android_proto.py 的 samples() 一一对应，顺序也一样。 */
    private static Pkt[] samples() {
        Pkt audio = new Pkt(Proto.T_AUDIO);
        audio.codec = Proto.CODEC_PCM_S16;
        audio.seq = 1;
        audio.ssrc = 0x11223344;
        audio.samples = 480;
        audio.tsNs = 1_700_000_000_000_000_000L;
        audio.gainQ14 = Proto.GAIN_ONE;
        audio.payload = ramp(128);

        Pkt empty = new Pkt(Proto.T_AUDIO);
        empty.codec = Proto.CODEC_PCM_S16;
        empty.seq = 2;
        empty.ssrc = 1;
        empty.samples = 0;
        empty.tsNs = 1234567890L;
        empty.gainQ14 = Proto.GAIN_ONE;
        empty.payload = new byte[0];

        Pkt wrap = new Pkt(Proto.T_AUDIO);
        wrap.codec = Proto.CODEC_PCM_S16;
        wrap.seq = 65535;
        wrap.ssrc = 2;
        wrap.samples = 480;
        wrap.tsNs = 1L;
        wrap.gainQ14 = 8192;
        wrap.payload = new byte[]{0x01, 0x02};

        Pkt ping = new Pkt(Proto.T_PING);
        ping.codec = Proto.CODEC_PCM_S16;
        ping.seq = 0;
        ping.ssrc = 3;
        ping.samples = 0;
        ping.tsNs = 99L;
        ping.gainQ14 = Proto.GAIN_ONE;
        ping.payload = new byte[]{0x7B, 0x22, 0x74, 0x22, 0x3A, 0x31, 0x7D};   // {"t":1}

        Pkt bye = new Pkt(Proto.T_BYE);
        bye.codec = Proto.CODEC_PCM_S16;
        bye.seq = 0;
        bye.ssrc = 4;
        bye.samples = 0;
        bye.tsNs = 0L;
        bye.gainQ14 = 0;
        bye.payload = new byte[0];

        return new Pkt[]{audio, empty, wrap, ping, bye};
    }

    private static final String[] LABELS = {
            "普通音频包 480 帧", "空负载", "seq 回绕 65535", "PING 控制包", "BYE 控制包"
    };

    private static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte x : b) {
            sb.append(String.format("%02x", x));
        }
        return sb.toString();
    }

    public static void main(String[] args) {
        System.out.println("== 包头 ==");
        check(Proto.HEADER_SIZE == 28, "包头长度是 28 字节");
        check(Proto.MAGIC == 0x574C, "magic 是 'WL'");
        check(Proto.SAMPLE_RATE == 48000 && Proto.CHANNELS == 2, "格式是 48000Hz 立体声");
        check(Proto.FRAME_SAMPLES == 480, "10ms 一块 = 480 帧");

        System.out.println("== 编出来的字节（拿去和 Python 端比对）==");
        Pkt[] ps = samples();
        for (int i = 0; i < ps.length; i++) {
            byte[] raw = Proto.encode(ps[i]);
            System.out.println("   " + LABELS[i] + "\t" + raw.length + " 字节\t" + hex(raw));
        }

        System.out.println("== 编解码往返 ==");
        for (int i = 0; i < ps.length; i++) {
            byte[] raw = Proto.encode(ps[i]);
            Pkt back = Proto.decode(raw);
            boolean ok = back != null
                    && back.type == ps[i].type
                    && back.codec == ps[i].codec
                    && back.seq == ps[i].seq
                    && back.ssrc == ps[i].ssrc
                    && back.samples == ps[i].samples
                    && back.tsNs == ps[i].tsNs
                    && back.gainQ14 == ps[i].gainQ14
                    && back.payload.length == ps[i].payload.length;
            check(ok, "往返一致: " + LABELS[i]);
        }

        System.out.println("== 边界 ==");
        check(Proto.decode(new byte[10]) == null, "太短的包返回 null");
        byte[] bad = Proto.encode(new Pkt(Proto.T_AUDIO));
        bad[0] = 0x00;
        check(Proto.decode(bad) == null, "magic 不对返回 null");
        Pkt big = new Pkt(Proto.T_AUDIO);
        big.ssrc = 0xFFFFFFFF;                    // uint32 的最大值，Java 里是负数
        big.seq = 65535;
        Pkt bigBack = Proto.decode(Proto.encode(big));
        check(bigBack != null && bigBack.ssrc == 0xFFFFFFFF && bigBack.seq == 65535,
                "ssrc 用满 32 位、seq 到 65535 都不失真");

        System.out.println("== 声道/帧数换算 ==");
        check(Proto.FRAME_SAMPLES * Proto.CHANNELS * 2 == 1920,
                "480 帧立体声 PCM16 = 1920 字节负载（和电脑端一致）");

        System.out.println("============================================");
        System.out.println("共 " + (passed + failed) + " 项，通过 " + passed
                + (failed > 0 ? "，失败 " + failed : ""));
        System.out.println("RESULT: " + (failed == 0 ? "PASS" : "FAIL"));
        System.exit(failed == 0 ? 0 : 1);
    }
}
