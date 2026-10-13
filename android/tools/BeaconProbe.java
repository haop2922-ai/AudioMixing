import android.content.Context;
import com.audiomixing.app.Discovery;

/**
 * 信标 / 节点身份离线探针（task-11 第 ① 条：手机重启后电脑多出一台重名设备）。
 *
 * <p>被测的是 {@code Discovery.java} 本体（run_beacon_test.sh 原样编译，配 android.* 与
 * org.json 的桩类），断言的是两件真机上看不见、但一看代码就能定死的事：
 * <ol>
 *   <li><b>节点 ID 持久化</b>：同一个 Context（同一份 SharedPreferences）建两个 Discovery，
 *       拿到的 id 必须完全一样 —— 这是"重启不再多出一台设备"的唯一根据；</li>
 *   <li><b>信标里的 id 不能是空的</b>：把真正要发出去的 JSON 组出来看。</li>
 * </ol>
 * 全部通过返回 0。
 */
public final class BeaconProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) {
        System.out.println("== 1. 节点 ID 持久化（重启不变 —— 电脑端才不会记成好几台）==");
        Context ctx = new Context();                       // 模拟"同一台手机、同一份存储"
        Discovery d1 = new Discovery(ctx);
        String id1 = d1.nodeId();
        check("id 非空：\"" + id1 + "\"", id1 != null && !id1.isEmpty());
        check("长得像 UUID（36 字符、4 个连字符）",
                id1.length() == 36 && id1.chars().filter(c -> c == '-').count() == 4);

        Discovery d2 = new Discovery(ctx);                 // 相当于"App 重启一次"
        check("重启后 id 完全一样：" + d1.nodeId().equals(d2.nodeId()), id1.equals(d2.nodeId()));
        Discovery d3 = new Discovery(ctx);
        check("再重启一次还是一样（三次都相同）", id1.equals(d3.nodeId()));

        Context other = new Context();                     // 另一台手机（另一份存储）
        Discovery d4 = new Discovery(other);
        check("换一台设备会得到不同的 id（不是写死的常量）", !id1.equals(d4.nodeId()));

        System.out.println();
        System.out.println("== 2. 信标里必须有非空的 id（电脑端就是靠它去重的）==");
        String beacon = d1.beaconPreview();
        System.out.println("   本机信标：" + beacon);
        check("信标里带 id 且非空", beacon.contains("\"id\":\"" + id1 + "\""));
        check("信标里有 wl=1（协议版本）", beacon.contains("\"wl\":1"));
        check("信标里有 audio_port=47831", beacon.contains("\"audio_port\":47831"));
        check("信标里有名字和平台", beacon.contains("\"name\":\"") && beacon.contains("\"platform\":\""));
        check("信标里有 sources / sinks（电脑端据此判断对端能力）",
                beacon.contains("\"sources\":[") && beacon.contains("\"sinks\":["));
        int bytes = reportedBytes(beacon);
        check("信标不超过协议上限 1200 字节（实测 " + bytes + "）", bytes > 0 && bytes < 1200);

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    /** 从"…（共 N 字节）"里把 N 抠出来。 */
    private static int reportedBytes(String s) {
        try {
            int a = s.lastIndexOf("共 ");
            int b = s.indexOf(" 字节", a);
            return Integer.parseInt(s.substring(a + 2, b).trim());
        } catch (Throwable t) {
            return -1;
        }
    }

    private static void check(String what, boolean ok) {
        checks++;
        if (!ok) {
            failures++;
        }
        System.out.println("   " + (ok ? "[OK] " : "[FAIL] ") + what);
    }

    private BeaconProbe() {
    }
}
