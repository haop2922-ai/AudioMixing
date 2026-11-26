package com.audiomixing.app;

/**
 * HostGate 的纯 JVM 自检：不依赖安卓，javac/java 直接跑。
 *
 * <pre>
 *   javac -d out HostGate.java HostGateTest.java
 *   java -cp out com.audiomixing.app.HostGateTest
 * </pre>
 */
public final class HostGateTest {

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

    public static void main(String[] args) {
        HostGate gate = new HostGate(3000);
        Object p2p = new Object();
        Object broadcast = new Object();
        String host = "192.0.2.10";

        check(gate.allow(host, 11, p2p, 1000) == null,
                "第一条流占位");
        check(Integer.valueOf(11).equals(gate.allow(host, 22, broadcast, 1001)),
                "另一路接收器被抑制（跨接收器去重）");
        check(gate.allow(host, 11, p2p, 1002) == null,
                "占位流继续放行");
        check(Integer.valueOf(11).equals(gate.allow(host, 22, broadcast, 3500)),
                "3 秒内仍被抑制");
        check(gate.allow(host, 22, broadcast, 4100) == null,
                "占位流 3 秒没包后让位");
        check(Integer.valueOf(22).equals(gate.allow(host, 11, p2p, 4101)),
                "新占位流反过来抑制旧接收器");

        gate.releaseAll(broadcast);
        check(gate.allow(host, 33, p2p, 4102) == null,
                "停掉一个接收器只释放它自己的占位");

        String other = "192.0.2.11";
        check(gate.allow(other, 44, broadcast, 4103) == null,
                "不同设备互不影响");

        System.out.println("----------------------------------------");
        System.out.println("共 " + (passed + failed) + " 项，通过 " + passed + "，失败 " + failed);
        if (failed > 0) {
            System.exit(1);
        }
    }
}
