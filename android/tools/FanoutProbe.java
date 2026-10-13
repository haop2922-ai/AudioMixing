import android.os.SystemClock;

import com.audiomixing.app.SendFanout;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * ★ 一发多（多目标发送）离线探针。
 *
 * <p>被测的是 {@code SendFanout.java} <b>本体</b>（run_fanout_test.sh 原样编译，只补一个 SystemClock 桩），
 * 而且用的是<b>真实 UDP 回环端口</b>：开 3 个真 socket 当"3 台设备"，看每一包到底有没有到、
 * 内容是不是同一份、目标下线会不会影响别人、目标集合变化会不会打断发送。
 *
 * <p>这就是"手机端 📢 广播我的声音"和"每台设备独立的送电平条"的底层能力 ——
 * 真机上要三台手机才能试，在 JVM 上一条命令就能断言。
 *
 * <p>全部通过返回 0。
 */
public final class FanoutProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) throws Exception {
        InetAddress local = InetAddress.getByName("127.0.0.1");
        // 三个"设备"：真实 UDP socket，绑在回环的临时端口上。
        // 先把接收缓冲开大再 bind —— 默认只有 64KB，连发几包就会在内核里被丢掉
        // （那是 UDP 的正常行为，跟被测代码无关；但测试里得避免它来捣乱）。
        DatagramSocket devA = openReceiver(local);
        DatagramSocket devB = openReceiver(local);
        DatagramSocket devC = openReceiver(local);
        String epA = "127.0.0.1:" + devA.getLocalPort();
        String epB = "127.0.0.1:" + devB.getLocalPort();
        String epC = "127.0.0.1:" + devC.getLocalPort();

        SendFanout fan = new SendFanout();
        byte[] pkt = new byte[1948];              // 和真实一包一样大（28 头 + 1920 负载）
        for (int i = 0; i < pkt.length; i++) {
            pkt[i] = (byte) (i * 7);
        }

        System.out.println("== 1. 一包分发给 3 个目标（一次采集 → 一次编码 → 发 3 份）==");
        fan.setTargets(Arrays.asList(epA, epB, epC));
        check("目标数 = 3（" + fan.targetText() + "）", fan.targetCount() == 3);
        int sent = fan.send(pkt, pkt.length, 0.5f);
        check("一次 send 成功发给 3 个目标（返回 " + sent + "）", sent == 3);
        check("A 收到了整包", recvExact(devA, pkt));
        check("B 收到了整包", recvExact(devB, pkt));
        check("C 收到了整包", recvExact(devC, pkt));

        System.out.println();
        System.out.println("== 2. 连发 20 包：每个目标都收到 20 包，字节数一致 ==");
        long baseA = fan.stats().get(0).packetsSent;
        long baseRounds = fan.packetsSent();
        for (int i = 0; i < 20; i++) {
            pkt[0] = (byte) i;
            fan.send(pkt, pkt.length, 0.5f);
        }
        int gotA = drain(devA);
        int gotB = drain(devB);
        int gotC = drain(devC);
        check("A 收到 " + gotA + " 包（期望 20）", gotA == 20);
        check("B 收到 " + gotB + " 包（期望 20）", gotB == 20);
        check("C 收到 " + gotC + " 包（期望 20）", gotC == 20);
        List<SendFanout.TargetStats> st = fan.stats();
        check("逐目标统计各 +20（"
                        + (st.get(0).packetsSent - baseA) + "/" + (st.get(1).packetsSent - baseA)
                        + "/" + (st.get(2).packetsSent - baseA) + "）",
                st.size() == 3 && st.get(0).packetsSent - baseA == 20
                        && st.get(1).packetsSent - baseA == 20 && st.get(2).packetsSent - baseA == 20);
        check("包数轮次 +20（一次编码发 3 份，所以轮次是 20 而不是 60）",
                fan.packetsSent() - baseRounds == 20);
        check("字节数按份数累计（" + fan.bytesSent() + " 字节）",
                fan.bytesSent() >= 21L * pkt.length * 3);

        System.out.println();
        System.out.println("== 3. 每目标的「送」电平：正常时一样；某台失败后它自己塌下去 ==");
        check("A/B/C 三路电平都等于这一包的电平（0.50）",
                Math.abs(fan.peakFor("127.0.0.1") - 0.5f) < 0.001f);
        check("没送过的地址电平是 0", fan.peakFor("10.9.9.9") == 0f);

        System.out.println();
        System.out.println("== 4. 目标中途下线：不影响其它目标（一个坏不能拖垮全部）==");
        devB.close();                              // B 关机了
        int sent2 = fan.send(pkt, pkt.length, 0.4f);
        check("B 关了之后，一次 send 仍然成功发给留下的目标（返回 " + sent2 + "）", sent2 >= 1);
        check("目标集合还是 3 个（用户没取消，只是它暂时收不到）", fan.targetCount() == 3);

        System.out.println();
        System.out.println("== 5. 目标集合变化不重启采集：socket 与统计都保持连续 ==");
        long before = fan.packetsSent();
        drain(devA);                                // 先把 A 的积压读干净，下面才好数
        fan.setTargets(Arrays.asList(epA, epC));    // 用户把 B 取消了
        check("目标数变成 2", fan.targetCount() == 2);
        int sent3 = fan.send(pkt, pkt.length, 0.3f);
        check("改成 2 个目标后 send 返回 2", sent3 == 2);
        check("包数轮次继续 +1、没有清零（" + before + " → " + fan.packetsSent() + "）",
                fan.packetsSent() == before + 1);
        st = fan.stats();
        check("剩下的两个目标统计还在（各 " + st.get(0).packetsSent + "/" + st.get(1).packetsSent + " 包）",
                st.size() == 2 && st.get(0).packetsSent > 20 && st.get(1).packetsSent > 20);
        check("A 仍然收得到（集合变化没有打断它）", drain(devA) == 1);

        System.out.println();
        System.out.println("== 6. 空目标：不崩、不发、不误报成功 ==");
        fan.setTargets(new ArrayList<String>());
        check("目标清空后 send 返回 0", fan.send(pkt, pkt.length, 0.1f) == 0);
        fan.setTargets(null);
        check("传 null 也不崩，目标数为 0", fan.targetCount() == 0);
        check("没有目标时 peek 电平是 0", fan.peakFor("127.0.0.1") == 0f);

        System.out.println();
        System.out.println("== 7. 坏地址只影响它自己 ==");
        devA.close();
        devA = openReceiver(local);                 // 重新开一个，验证别人没事
        String epA2 = "127.0.0.1:" + devA.getLocalPort();
        fan.setTargets(Arrays.asList(epA2, "这不是个地址:47831", epC));
        int sent4 = fan.send(pkt, pkt.length, 0.2f);
        check("坏地址在场时，好目标照样收到（返回 " + sent4 + "）", sent4 >= 2);
        check("重新开的 A 收到了", drain(devA) == 1);

        SystemClock.now += 5000;
        fan.close();

        System.out.println();
        System.out.println("== 8. ★ 逐目标增益（「我送它的」滑杆）：只改那一路，别人收原包 ==");
        // 注意：增益是"按设备"（host）记的 —— 真实局域网里一台设备就是一个 IP，
        // 所以这里必须用两个不同的回环地址当两台设备（127.0.0.1 / 127.0.0.2），
        // 否则"两个目标同一个 host"会被同一条滑杆一起改掉（那其实是正确行为）。
        InetAddress local2 = InetAddress.getByName("127.0.0.2");
        SendFanout fan2 = new SendFanout();
        DatagramSocket gA = openReceiver(local);
        DatagramSocket gB = openReceiver(local2);
        String gpA = "127.0.0.1:" + gA.getLocalPort();
        String gpB = "127.0.0.2:" + gB.getLocalPort();
        fan2.setTargets(Arrays.asList(gpA, gpB));
        byte[] flat = flatPcmPacket(1000);          // 每个样本都是 1000：缩放结果一眼能算出来
        check("初始增益都是 1.00", fan2.targetGain("127.0.0.1") == 1f);
        fan2.setTargetGain("127.0.0.1", 0.5f);
        check("设完是 0.50", Math.abs(fan2.targetGain("127.0.0.1") - 0.5f) < 0.001f);
        int n8 = fan2.send(flat, flat.length, 0.5f);
        check("两个目标都发出去了（返回 " + n8 + "）", n8 == 2);
        int aSample = firstSample(gA);
        int bSample = firstSample(gB);
        check("A（127.0.0.1，设了 0.50）收到缩放后的 500，实测 " + aSample, aSample == 500);
        check("B（127.0.0.2，没设）收到原样本 1000，实测 " + bSample, bSample == 1000);
        boolean sawHalf = false;
        for (SendFanout.TargetStats t : fan2.stats()) {
            if (Math.abs(t.gain - 0.5f) < 0.001f) {
                sawHalf = true;
            }
        }
        check("逐目标统计里带着各自的增益值", sawHalf);

        System.out.println();
        System.out.println("== 9. 削顶保护：增益 1.5 时大声样本不会回绕成爆音 ==");
        fan2.setTargetGain("127.0.0.1", 1.5f);
        byte[] loud = flatPcmPacket(30000);          // 30000 × 1.5 = 45000 > 32767 → 必须削成 32767
        fan2.send(loud, loud.length, 1f);
        int clipped = firstSample(gA);
        check("30000 × 1.50 → 削顶成 32767（不是回绕的负数），实测 " + clipped, clipped == 32767);
        fan2.setTargetGain("127.0.0.1", 9f);         // 越界值必须被夹住
        check("越界的增益被夹到 1.50（实测 " + fan2.targetGain("127.0.0.1") + "）",
                Math.abs(fan2.targetGain("127.0.0.1") - 1.5f) < 0.001f);

        System.out.println();
        System.out.println("== 10. 增益会被记住：目标下线再上线，滑杆不回跳 ==");
        fan2.setTargetGain("127.0.0.1", 0.4f);
        fan2.setTargets(Arrays.asList(gpB));         // A 下线
        fan2.setTargets(Arrays.asList(gpA, gpB));    // A 又上线
        check("重新上线后增益还是 0.40（实测 " + fan2.targetGain("127.0.0.1") + "）",
                Math.abs(fan2.targetGain("127.0.0.1") - 0.4f) < 0.001f);
        fan2.close();
        gA.close();
        gB.close();

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    /** 造一个"每个样本都是 v"的 PCM16 包（1948 字节），缩放结果一眼能算出来。 */
    private static byte[] flatPcmPacket(int v) {
        byte[] pkt = new byte[1948];
        // 28 字节包头留空（本探针直接调 SendFanout，不经过解码，所以头内容无关）
        for (int i = 28; i + 1 < pkt.length; i += 2) {
            pkt[i] = (byte) v;
            pkt[i + 1] = (byte) (v >> 8);
        }
        return pkt;
    }

    /** 收一包、返回负载第一个样本（超时返回 Integer.MIN_VALUE）。 */
    private static int firstSample(DatagramSocket s) throws Exception {
        byte[] buf = new byte[4096];
        DatagramPacket p = new DatagramPacket(buf, buf.length);
        try {
            s.receive(p);
        } catch (Exception e) {
            return Integer.MIN_VALUE;
        }
        if (p.getLength() < 30) {
            return Integer.MIN_VALUE;
        }
        return (short) ((buf[28] & 0xFF) | ((buf[29] & 0xFF) << 8));
    }

    /** 开一个"设备端"接收 socket：先把接收缓冲开大再 bind（默认 64KB 太小，连发会被内核丢）。 */
    private static DatagramSocket openReceiver(InetAddress local) throws Exception {
        DatagramSocket s = new DatagramSocket(null);
        s.setReuseAddress(true);
        try {
            s.setReceiveBufferSize(1 << 20);
        } catch (Exception ignored) {
            // 系统不给那么大也无所谓，测试里少发几包就是
        }
        s.bind(new java.net.InetSocketAddress(local, 0));
        s.setSoTimeout(400);
        return s;
    }

    /** 收一包并逐字节比对（超时返回 false）。 */
    private static boolean recvExact(DatagramSocket s, byte[] expect) throws Exception {
        byte[] buf = new byte[4096];
        DatagramPacket p = new DatagramPacket(buf, buf.length);
        try {
            s.receive(p);
        } catch (Exception e) {
            return false;
        }
        if (p.getLength() != expect.length) {
            return false;
        }
        for (int i = 0; i < expect.length; i++) {
            if (buf[i] != expect[i]) {
                return false;
            }
        }
        return true;
    }

    /** 把已经到达的包全部读掉，返回包数。 */
    private static int drain(DatagramSocket s) {
        int n = 0;
        byte[] buf = new byte[4096];
        DatagramPacket p = new DatagramPacket(buf, buf.length);
        while (true) {
            try {
                s.receive(p);
                n++;
            } catch (Exception e) {
                return n;
            }
        }
    }

    private static void check(String what, boolean ok) {
        checks++;
        if (!ok) {
            failures++;
        }
        System.out.println("   " + (ok ? "[OK] " : "[FAIL] ") + what);
    }

    private FanoutProbe() {
    }
}
