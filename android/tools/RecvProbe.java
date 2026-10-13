import android.content.Context;
import android.media.AudioTrack;
import android.net.wifi.WifiManager;
import android.os.SystemClock;

import com.audiomixing.app.AudioRecv;
import com.audiomixing.app.Proto;

import java.lang.reflect.Method;
import java.net.InetAddress;

/**
 * 收包准入逻辑离线探针（收端"为什么没声音"的那一半）。
 *
 * <p>被测的是 {@code AudioRecv.onPacket} 本体 —— 用 run_recv_admission_test.sh 把整个
 * AudioRecv.java <b>原样</b>拿过来，配一组 android.* 桩类编译（源码一字未改），
 * 再拿<b>电脑端 proto.py 真编出来的字节</b>喂进去。
 *
 * <p>验三件事：
 * <ol>
 *   <li>电脑端发的标准包（PCM16/480 帧/1920 字节负载）手机到底收不收 —— 这是跨语言互通的核心；</li>
 *   <li>各种坏包是不是被分到正确的类别里（这次排查最卡人的就是"坏包 3220"只有一个总数）；</li>
 *   <li>「听广播」= 名单留空 = 谁来都听 这条语义到底成不成立 —— 手机能不能听见电脑的广播就靠它。</li>
 * </ol>
 * 全部通过返回 0。
 */
public final class RecvProbe {

    private static int checks;
    private static int failures;
    private static AudioRecv recv;
    private static Method onPacket;

    /** 电脑端 proto.py 真编出来的包：PCM16 / 480 帧 / 1920 字节负载 / 1948 字节整包。 */
    private static final String PC_PCM16_HEX =
            "4c570101000207000df0ad0be001000000002a36fe9c971700408007004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0004000c0"
            + "004000c0004000c0004000c0004000c0004000c0004000c0004000c0";

    public static void main(String[] args) throws Exception {
        recv = new AudioRecv((Context) null, 47831);
        onPacket = AudioRecv.class.getDeclaredMethod(
                "onPacket", byte[].class, int.class, InetAddress.class);
        onPacket.setAccessible(true);

        InetAddress pc = InetAddress.getByName("192.168.0.109");       // "电脑"
        InetAddress other = InetAddress.getByName("192.168.0.5");      // "别人"
        byte[] pcPkt = hex(PC_PCM16_HEX);
        check("电脑端真编出来的包是 1948 字节（28 头 + 1920 负载）", pcPkt.length == 1948);

        System.out.println("== 1. 电脑端标准包（PCM16 / 480 帧）手机收不收 ==");
        feed(pcPkt, pc);
        AudioRecv.Stats s = recv.stats();
        check("已收包 = 1（收下了 ✓）", s.received == 1);
        check("坏包 = 0（没被误判成坏包）", s.badPackets == 0);
        check("建起了 1 条流", s.streamCount == 1);

        System.out.println();
        System.out.println("== 2. 坏包分类：每一类都要落到正确的格子里 ==");
        byte[] opus = Proto.encode(pkt(Proto.CODEC_OPUS, 480, 160));       // 电脑端 codec=opus 的样子
        feed(opus, pc);
        s = recv.stats();
        check("codec=3(Opus) → 编码不是 PCM16（badCodec=" + s.badCodec + "）", s.badCodec == 1);
        check("  最近编码记到了：\"" + s.lastCodecSeen + "\"", s.lastCodecSeen.contains("codec=3"));

        byte[] big = Proto.encode(pkt(Proto.CODEC_PCM_S16, 960, 3840));    // 每包 960 帧
        feed(big, pc);
        s = recv.stats();
        check("samples=960 → 帧长不是 480（badFrames=" + s.badFrames + "）", s.badFrames == 1);

        byte[] badMagic = pcPkt.clone();
        badMagic[0] = 0x00;
        badMagic[1] = 0x00;
        feed(badMagic, pc);
        s = recv.stats();
        check("magic 不对 → badMagic（" + s.badMagic + "）", s.badMagic == 1);
        check("  现场样本里有来源和 hex：\"" + shorten(s.lastMagicSample) + "\"",
                s.lastMagicSample.contains("192.168.0.109") && s.lastMagicSample.contains("00 00"));

        byte[] shortPkt = new byte[10];
        feed(shortPkt, pc);
        s = recv.stats();
        check("10 字节 → 包太短（badShort=" + s.badShort + "）", s.badShort == 1);

        byte[] badVer = pcPkt.clone();
        badVer[2] = 9;
        feed(badVer, pc);
        s = recv.stats();
        check("version=9 → 版本不对（badVersion=" + s.badVersion + "）", s.badVersion == 1);
        check("坏包总数 = 5（5 个坏包全被分类，一个不漏）", s.badPackets == 5);
        check("坏包来源按 IP 统计：\"" + s.badBySource + "\"", s.badBySource.contains("192.168.0.109"));
        check("坏包长度分布（1948 字节 = 一条音频流的形状）：\"" + s.badByLen + "\"",
                s.badByLen.contains("1948 字节"));
        check("仍然只有 1 个包被真正收下（坏包没有污染接收计数）", s.received == 1);

        System.out.println();
        System.out.println("== 3. 「听广播」= 名单留空 = 谁来都听（手机能听电脑广播就靠这条）==");
        recv.allow("192.168.0.5", true);                 // 只收听 192.168.0.5
        feed(pcPkt, pc);                                 // 电脑发来的点对点包
        s = recv.stats();
        check("名单里只有别人时，电脑的包被按名单过滤（filtered=" + s.filtered + "）", s.filtered == 1);
        check("  这条不算坏包（filtered 和 badPackets 是两回事）", s.badPackets == 5);

        recv.clearAllowed();                             // ★ 这就是 Engine 打开「听广播」时做的事
        feed(pcPkt, pc);
        s = recv.stats();
        check("名单清空后（听广播模式），电脑的包被收下（received=" + s.received + "）", s.received == 2);
        check("  流数还是 1（同一台同一个 ssrc 不会重复建流）", s.streamCount == 1);

        byte[] stats = Proto.encode(ctl(Proto.T_STATS));
        feed(stats, other);
        s = recv.stats();
        check("控制包（对端统计）即使不在名单里也照收（controls=" + s.controls + "）", s.controls == 1);

        System.out.println();
        System.out.println("== 4. 同一台设备的第 2 条流绝不叠加播放（用户说的「路由/广播混淆」）==");
        InetAddress dupHost = InetAddress.getByName("192.168.0.77");
        InetAddress otherHost = InetAddress.getByName("192.168.0.88");
        int before = recv.stats().streamCount;
        // 同一台设备、两个 ssrc（电脑同时开广播 + 点对点路由时就是这样），而且负载完全相同
        byte[] routeA = Proto.encode(tonePkt(0x1111));
        byte[] routeB = Proto.encode(tonePkt(0x2222));
        feed(routeA, dupHost);
        s = recv.stats();
        check("第 1 条流正常建流（流数 " + before + " → " + s.streamCount + "）",
                s.streamCount == before + 1);
        for (int i = 0; i < 10; i++) {
            feed(routeB, dupHost);
        }
        s = recv.stats();
        check("第 2 条流被抑制、没有建流（流数仍是 " + s.streamCount + "）", s.streamCount == before + 1);
        check("抑制计数 = 10（dupRoutePackets=" + s.dupRoutePackets + "）", s.dupRoutePackets == 10);
        check("指纹确认是同一份声音（dupRouteConfirmed=" + s.dupRouteConfirmed + "）",
                s.dupRouteConfirmed == 10);
        check("现场描述点出了两条 ssrc：\"" + s.dupRouteText + "\"",
                s.dupRouteText.contains("1111") && s.dupRouteText.contains("2222"));

        byte[] otherRoute = Proto.encode(tonePkt(0x3333));
        feed(otherRoute, otherHost);
        s = recv.stats();
        check("另一台设备不受影响、照常建流（流数 " + s.streamCount + "）", s.streamCount == before + 2);

        // 第 1 条流走了（超过 3 秒没有包）之后，第 2 条流必须能接手，不能被无限抑制
        SystemClock.now += 4000;
        feed(routeB, dupHost);
        s = recv.stats();
        check("第 1 条流超时后第 2 条流接手（抑制计数不再增长，" + s.dupRoutePackets + "）",
                s.dupRoutePackets == 10);

        System.out.println();
        System.out.println("RESULT: " + (failures == 0 ? "PASS" : "FAIL")
                + "  （" + checks + " 项断言"
                + (failures == 0 ? "全部通过）" : "，失败 " + failures + " 项）"));
        System.exit(failures == 0 ? 0 : 1);
    }

    private static Proto.Pkt pkt(int codec, int samples, int payloadLen) {
        Proto.Pkt p = new Proto.Pkt(Proto.T_AUDIO);
        p.codec = codec;
        p.samples = samples;
        p.ssrc = 0x1234;
        p.payload = new byte[payloadLen];
        return p;
    }

    /**
     * 一条"有声音"的流。注意负载<b>不掺 ssrc</b>：现实里同一份声音从两条路进来时，
     * 两条流的负载是同一份音频（这正是"指纹相同"要认出来的东西）。
     * 负载也不能是全零 —— 静音不参与去重比对。
     */
    private static Proto.Pkt tonePkt(int ssrc) {
        Proto.Pkt p = new Proto.Pkt(Proto.T_AUDIO);
        p.codec = Proto.CODEC_PCM_S16;
        p.samples = 480;
        p.ssrc = ssrc;
        byte[] payload = new byte[1920];
        for (int i = 0; i < payload.length; i++) {
            payload[i] = (byte) ((i * 37) & 0xFF);
        }
        p.payload = payload;
        return p;
    }

    private static Proto.Pkt ctl(int type) {
        Proto.Pkt p = new Proto.Pkt(type);
        p.payload = "{\"t\":\"stats\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        return p;
    }

    private static void feed(byte[] raw, InetAddress from) throws Exception {
        SystemClock.now += 10;      // 让"最近样本"的节流（500ms）每次都放行
        onPacket.invoke(recv, raw, raw.length, from);
    }

    private static byte[] hex(String s) {
        byte[] out = new byte[s.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(s.substring(i * 2, i * 2 + 2), 16);
        }
        return out;
    }

    private static String shorten(String s) {
        if (s == null) {
            return "";
        }
        return s.length() > 70 ? s.substring(0, 70) + "…" : s;
    }

    private static void check(String what, boolean ok) {
        checks++;
        if (!ok) {
            failures++;
        }
        System.out.println("   " + (ok ? "[OK] " : "[FAIL] ") + what);
    }

    private RecvProbe() {
    }
}
