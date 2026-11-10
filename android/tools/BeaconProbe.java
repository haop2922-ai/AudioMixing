import android.content.Context;
import com.audiomixing.app.Discovery;

import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.MulticastSocket;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * 信标 / 节点身份 / 控制面离线探针。
 *
 * <p>被测的是 {@code Discovery.java} 本体（run_beacon_test.sh 原样编译，配 android.* 与
 * org.json 的桩类），断言的是真机上看不见、但一看代码就能定死的事：
 * <ol>
 *   <li><b>节点 ID 持久化</b>：同一个 Context（同一份 SharedPreferences）建两个 Discovery，
 *       拿到的 id 必须完全一样 —— 这是"重启不再多出一台设备"的唯一根据；</li>
 *   <li><b>信标里的 id 不能是空的</b>：把真正要发出去的 JSON 组出来看；</li>
 *   <li><b>★ 控制面（2026-10-15）</b>：控制消息和信标<b>同一条 socket</b>，靠 {@code "ctl"}
 *       是对象还是数字区分。这一条判错就会变成"一边收得到、一边收不到"，
 *       是上一版把控制消息挂在按需启停的音频接收器上时最难查的一类故障。</li>
 * </ol>
 * 全部通过返回 0。
 */
public final class BeaconProbe {

    private static int checks;
    private static int failures;

    public static void main(String[] args) throws Exception {
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
        // ★ 2026-11-04：信标新增 device_id / instance_id / ext 后超过 320 字的预览窗口，
        //   后半段（ctl / disc_port）被截断。断言必须对着**完整 JSON** 做，不能靠预览。
        String full = beaconJson(d1);
        String dev1 = strField(full, "device_id");
        String inst1 = strField(full, "instance_id");
        System.out.println("   本机信标：" + beacon);
        System.out.println("   完整信标：" + full);
        check("信标里带 id 且非空", beacon.contains("\"id\":\"" + id1 + "\""));
        check("信标里有 wl=1（协议版本）", beacon.contains("\"wl\":1"));
        check("信标里有 audio_port=47831", beacon.contains("\"audio_port\":47831"));
        check("信标里有名字和平台", beacon.contains("\"name\":\"") && beacon.contains("\"platform\":\""));
        // ★ 用户反馈：手机装的是新版，电脑端卡片上却写着 1.0.0 —— 因为 "version" 原来写死成协议版本。
        //   现在它读 BuildConfig.VERSION_NAME（本测试的桩把它定成 "2.12"，对齐 build.gradle）。
        check("★ 信标里的 version 报的是 App 版本，不再是写死的 1.0.0",
                beacon.contains("\"version\":\"2.12\"") && !beacon.contains("\"version\":\"1.0.0\""));
        check("信标里有 sources / sinks（电脑端据此判断对端能力）",
                beacon.contains("\"sources\":[") && beacon.contains("\"sinks\":["));
        check("信标里有 ctl=1（对端据此把控制消息发到这一层）", hasNum(full, "ctl", 1));
        check("信标里有 disc_port=17830（对端按它发控制消息）",
                hasNum(full, "disc_port", 17830));
        check("★ 信标里有 device_id（同一台手机的 WiFi/蓝牙合并主键）", !dev1.isEmpty());
        check("device_id 长得像 UUID（36 字符、4 个连字符）",
                dev1.length() == 36 && dev1.chars().filter(c -> c == '-').count() == 4);
        check("★ 信标里有 instance_id（换机搬数据时识破「两台真机共用一个 device_id」）",
                !inst1.isEmpty());
        check("重启后 device_id 不变（合并主键必须稳定）",
                dev1.equals(strField(beaconJson(d2), "device_id")));
        check("重启后 instance_id 不变（安装级标识）",
                inst1.equals(strField(beaconJson(d2), "instance_id")));
        check("换一台设备 device_id 不同", !dev1.equals(strField(beaconJson(d4), "device_id")));
        check("换一台设备 instance_id 不同", !inst1.equals(strField(beaconJson(d4), "instance_id")));
        check("★ 信标里有 ext=1（声明能解析音频扩展段的 originId/hop）", hasNum(full, "ext", 1));
        int bytes = reportedBytes(beacon);
        check("信标不超过协议上限 1200 字节（实测 " + bytes + "）", bytes > 0 && bytes < 1200);

        System.out.println();
        System.out.println("== 3. ★ 控制面：控制消息和信标同一条 socket（2026-10-15）==");
        System.out.println("   背景：这一层每 2 秒一发一收、一直活着；音频接收端是按需启停的。");
        System.out.println("   控制消息（请自动收听 / 共享音量 / 请连蓝牙）原来挂音频端口，"
                + "对端接收器一停就收不到。");
        controlPlane();

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    /**
     * ★ 控制面（2026-10-15）：同一个 socket 上两种包 —— 信标和"请对端自动收听我"这类控制消息。
     *
     * <p>验四件事：① 信标（{@code ctl} 是数字）不会被误判成控制消息；② 外衣里抠出来的正文
     * 一字不差；③ 真起一个 UDP socket 跑一遍：发出去 → 对端收到 → 对端抠出来；
     * ④ 收到的控制消息走 {@code onControl}，而且【绝不会】变成设备列表里的一台假设备。
     */
    private static void controlPlane() throws Exception {
        String beacon = "{\"wl\":1,\"id\":\"abc\",\"name\":\"手机\",\"ctl\":1,\"disc_port\":17830}";
        check("信标（ctl 是数字）不被判成控制消息", Discovery.controlBodyOf(beacon) == null);

        String body = "{\"t\":\"stats\",\"name\":\"电脑\",\"want_listen\":true,\"gain\":0.75}";
        String envelope = "{\"wl\":1,\"ctl\":" + body + "}";
        check("外衣里抠出来的就是 ctl 正文，一字不多不少",
                body.equals(Discovery.controlBodyOf(envelope)));

        // 设备名里可能有花括号：抠取必须认得"字符串里的括号不算括号"。
        String tricky = "{\"wl\":1,\"ctl\":{\"t\":\"stats\",\"name\":\"客厅{电脑}\"}}";
        check("正文里字符串内的花括号不会把抠取截断",
                "{\"t\":\"stats\",\"name\":\"客厅{电脑}\"}".equals(
                        Discovery.controlBodyOf(tricky)));

        check("没有 ctl 键的包不是控制消息",
                Discovery.controlBodyOf("{\"wl\":1,\"id\":\"a\"}") == null);
        check("被截断的外衣（括号不配对）当垃圾丢掉，绝不猜",
                Discovery.controlBodyOf("{\"wl\":1,\"ctl\":{\"t\":\"stats\"") == null);

        // ── 真跑一遍：发出去 → 对端收到 → 对端抠出来 ──
        DatagramSocket lis = new DatagramSocket(0);
        lis.setSoTimeout(2000);
        Discovery sender = new Discovery(new Context());
        MulticastSocket ms = new MulticastSocket();
        setField(sender, "sock", ms);
        String received = null;
        try {
            boolean ok = sender.sendControl(body, "127.0.0.1", lis.getLocalPort());
            check("sendControl 报告发送成功（对端不支持时靠这个返回值决定退回音频端口）", ok);
            check("控制消息计数 +1", sender.controlsSent() == 1);
            byte[] buf = new byte[4096];
            DatagramPacket p = new DatagramPacket(buf, buf.length);
            lis.receive(p);
            received = new String(buf, 0, p.getLength(), StandardCharsets.UTF_8);
        } catch (Throwable t) {
            received = null;
        } finally {
            lis.close();
            ms.close();
        }
        check("对端真的收到了这条 UDP 包", received != null);
        check("外衣形状是 {\"wl\":1,\"ctl\":{…}}",
                received != null && received.startsWith("{\"wl\":1,\"ctl\":{"));
        check("对端抠出来的正文和发出去的完全一样",
                body.equals(Discovery.controlBodyOf(received)));

        // ── 收到控制消息：走 onControl，绝不进设备列表 ──
        Discovery recv = new Discovery(new Context());
        final String[] seen = new String[]{null, null};
        recv.setListener(new Discovery.Listener() {
            @Override
            public void onPeersChanged(List<Discovery.Peer> p) {
            }

            @Override
            public void onControl(String host, String json) {
                seen[0] = host;
                seen[1] = json;
            }
        });
        Method hb = Discovery.class.getDeclaredMethod("handleBeacon", byte[].class, int.class,
                InetAddress.class);
        hb.setAccessible(true);
        byte[] raw = envelope.getBytes(StandardCharsets.UTF_8);
        hb.invoke(recv, raw, raw.length,
                InetAddress.getByAddress(new byte[]{(byte) 192, (byte) 168, 0, 9}));
        check("onControl 被叫到，来源就是发信那台", "192.168.0.9".equals(seen[0]));
        check("正文原样传下来（和音频层那份走同一段解析）", body.equals(seen[1]));
        check("控制消息计数 +1", recv.controlsReceived() == 1);
        check("控制消息【绝不能】变成一台设备", recv.peers().isEmpty());
    }

    private static void setField(Discovery d, String name, Object value) throws Exception {
        Field f = Discovery.class.getDeclaredField(name);
        f.setAccessible(true);
        f.set(d, value);
    }

    /** 直接调私有的 buildBeacon()，拿到**完整** JSON（beaconPreview 只给前 320 字）。 */
    private static String beaconJson(Discovery d) throws Exception {
        Method m = Discovery.class.getDeclaredMethod("buildBeacon");
        m.setAccessible(true);
        byte[] raw = (byte[]) m.invoke(d);
        return new String(raw, StandardCharsets.UTF_8);
    }

    /** 从完整信标里取字符串字段（值里没有引号，UUID/名字都安全）。 */
    private static String strField(String json, String key) {
        String needle = "\"" + key + "\":\"";
        int a = json.indexOf(needle);
        if (a < 0) {
            return "";
        }
        int b = json.indexOf('"', a + needle.length());
        return b < 0 ? "" : json.substring(a + needle.length(), b);
    }

    private static boolean hasNum(String json, String key, int value) {
        return json.contains("\"" + key + "\":" + value);
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
