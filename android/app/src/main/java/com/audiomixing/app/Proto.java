package com.audiomixing.app;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.zip.CRC32;

/**
 * 28 字节包头（小端）。字段顺序、偏移、类型与电脑端 audiomixing/proto.py 的
 * HEADER_FIELDS 逐字节一致 —— 手机和电脑要能互通，这一层差一个字节都不行，
 * 所以这里不图省事用对象序列化，老老实实按偏移写。
 *
 * <pre>
 * 偏移  类型      字段
 *  0    uint16   magic         0x574C（'W','L'）
 *  2    uint8    version       1
 *  3    uint8    ptype         1=AUDIO 4=PING 5=PONG 6=BYE ...
 *  4    uint8    flags         0x01 不连续 / 0x02 最后一块 / 0x04 静音
 *  5    uint8    codec         2=PCM16（手机用这个）3=Opus
 *  6    uint16   seq           包序号
 *  8    uint32   ssrc          本流标识
 * 12    uint32   samples       ★见下方说明★（本包帧数；对面历史版本可能填累计值）
 * 16    uint64   ts_ns         发送时刻墙钟纳秒
 * 24    int16    gain_q14      音量，Q14 定点（1.0 == 16384）
 * 26    uint16   payload_len   负载字节数
 * </pre>
 *
 * <p><b>★ samples 字段的语义（2026-10-14 跨端踩过的坑，接收端务必按这段处理）★</b>
 * <ul>
 *   <li><b>规范语义</b>：<b>本包</b>有多少帧（每帧 2 声道）。480 帧 = 10ms = 1920 字节负载。</li>
 *   <li><b>历史事实</b>：电脑端 {@code audiomixing/net/rtpaudio.py} 曾经把它填成
 *       <b>累计帧偏移</b>（用户报告里的 4,924,800 = 480 × 10260）。Python 接收端根本不读这个
 *       字段，所以电脑↔电脑一直没症状；而手机端一旦严格校验它，就会把对方的包全部拒收 ——
 *       现场表现是"电脑在放声音、手机一个包都不收到"。</li>
 *   <li><b>因此</b>：接收端判"这一包是不是标准包"<b>一律以负载长度为准</b>
 *       （1920 字节 = 480 帧；短的补零；只有 &gt; 1920 才拒），
 *       {@code samples} 只作参考、不作拒绝条件（见 {@code AudioRecv.onPacket}）。</li>
 *   <li><b>不要</b>因为这段说明去改包头布局或版本号 —— 这个类是和 Python 端逐字节对拍过的。</li>
 * </ul>
 */
public final class Proto {

    public static final int HEADER_SIZE = 28;
    public static final int MAGIC = 0x574C;
    public static final int VERSION = 1;

    public static final int T_AUDIO = 1, T_HELLO = 2, T_HELLO_ACK = 3, T_PING = 4,
            T_PONG = 5, T_BYE = 6, T_STATS = 7, T_ROUTE = 8, T_KEEPALIVE = 9;

    public static final int CODEC_PCM_F32 = 1, CODEC_PCM_S16 = 2, CODEC_OPUS = 3;

    public static final int FLAG_DISCONTINUITY = 0x01, FLAG_LAST = 0x02, FLAG_SILENCE = 0x04;
    /**
     * ★ 2026-11-04（B 第一阶段）：可选扩展段。**包头仍是 28 字节**，只有置了这一位，
     * 包头与负载之间才多 8 字节：uint32 originId（原始声音主人的稳定哈希，全程不变）
     * + uint8 hop（每转发一跳 +1，超过 {@link #MAX_HOP} 丢）+ 3 字节保留。
     *
     * 为什么用"可选扩展段"而不是新版本号：老端的 decode 只认 VERSION==1，
     * 改版本号会让整条音频链路直接断；而扩展段只在**对端信标声明 ext:1** 时才发
     * （见 Discovery.buildBeacon / AudioSend），老端永远收不到带扩展段的包。
     */
    public static final int FLAG_EXT = 0x08;
    public static final int EXT_SIZE = 8;
    public static final int MAX_HOP = 8;

    /** device_id/node_id → 32 位稳定标识，和电脑端 proto.origin_id_of 完全一致（CRC32）。 */
    public static int originIdOf(String value) {
        String text = value == null ? "" : value.trim();
        CRC32 crc = new CRC32();
        // 电脑端是对 UTF-8 字节做 CRC32（zlib.crc32）；这里必须完全一样。
        byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
        crc.update(bytes, 0, bytes.length);
        long v = crc.getValue() & 0xFFFFFFFFL;
        int id = (int) v;
        return id == 0 ? 1 : id;   // 0 保留 = "没有 origin 信息"
    }

    public static final int SAMPLE_RATE = 48000;
    public static final int CHANNELS = 2;
    /** 10 毫秒一块：48000 * 0.01 = 480 帧 */
    public static final int FRAME_SAMPLES = 480;
    /** 1.0 音量对应的 Q14 值 */
    public static final int GAIN_ONE = 16384;

    public static final int DEFAULT_AUDIO_PORT = 47831;
    /**
     * ★ 2026-11-01 用户：两端默认发现端口统一为 17830。
     * 这个值必须和电脑端 {@code audiomixing/proto.py} 的 {@code PORT_DISCOVERY} 一致 —
     * 它是两端**唯一**需要手填的端口；音频端口由信标协商，不需要管。
     */
    public static final int DEFAULT_DISCOVERY_PORT = 17830;
    /**
     * 旧默认发现端口。升级时把"从没改过"的 47830 自动搬到 17830
     * （见 {@link Engine#configuredDiscoveryPort()}）；用户自己填过的其它值一律不动。
     */
    public static final int LEGACY_DISCOVERY_PORT = 47830;
    public static final String DISCOVERY_GROUP = "239.255.77.120";

    private Proto() {
    }

    /** 一个包头 + 负载。字段用 int 表示无符号值（Java 没有 uint，位模式一致就行）。 */
    public static final class Pkt {
        public int type;
        public int flags;
        public int codec = CODEC_PCM_S16;
        public int seq;
        public int ssrc;
        public int samples;
        public long tsNs;
        public int gainQ14 = GAIN_ONE;
        public byte[] payload = new byte[0];
        /** 扩展段字段（仅 FLAG_EXT 时存在；0 = 老端/未协商）。 */
        public int originId = 0;
        public int hop = 0;

        public Pkt(int type) {
            this.type = type;
        }
    }

    /** 编成一个可以直接 send 的字节数组。 */
    public static byte[] encode(Pkt p) {
        byte[] payload = p.payload != null ? p.payload : new byte[0];
        int flags = p.flags & 0xFF;
        boolean ext = p.originId != 0;
        if (ext) {
            flags |= FLAG_EXT;
        }
        ByteBuffer b = ByteBuffer.allocate(HEADER_SIZE + (ext ? EXT_SIZE : 0) + payload.length);
        // 必须显式指定小端：Java 默认是大端，而我们的协议是网络里跑的小端布局。
        b.order(ByteOrder.LITTLE_ENDIAN);
        b.putShort((short) MAGIC);
        b.put((byte) VERSION);
        b.put((byte) (p.type & 0xFF));
        b.put((byte) flags);
        b.put((byte) (p.codec & 0xFF));
        b.putShort((short) (p.seq & 0xFFFF));
        b.putInt(p.ssrc);
        b.putInt(p.samples);
        b.putLong(p.tsNs);
        b.putShort((short) (p.gainQ14 & 0xFFFF));
        b.putShort((short) (payload.length & 0xFFFF));
        if (ext) {
            b.putInt(p.originId);
            b.put((byte) (p.hop & 0xFF));
            b.put((byte) 0);
            b.put((byte) 0);
            b.put((byte) 0);
        }
        b.put(payload);
        return b.array();
    }

    /** 解一段字节；长度不够或 magic 不对返回 null（调用方丢掉这一包即可）。 */
    public static Pkt decode(byte[] raw, int off, int len) {
        if (raw == null || len < HEADER_SIZE || off < 0 || off + len > raw.length) {
            return null;
        }
        ByteBuffer b = ByteBuffer.wrap(raw, off, len).order(ByteOrder.LITTLE_ENDIAN);
        int magic = b.getShort() & 0xFFFF;
        if (magic != MAGIC) {
            return null;
        }
        int version = b.get() & 0xFF;
        if (version != VERSION) {
            return null;
        }
        Pkt p = new Pkt(b.get() & 0xFF);
        p.flags = b.get() & 0xFF;
        p.codec = b.get() & 0xFF;
        p.seq = b.getShort() & 0xFFFF;
        p.ssrc = b.getInt();
        p.samples = b.getInt();
        p.tsNs = b.getLong();
        p.gainQ14 = b.getShort();          // 有符号：音量可以是负的
        int plen = b.getShort() & 0xFFFF;
        int offset = HEADER_SIZE;
        if ((p.flags & FLAG_EXT) != 0) {
            if (len < HEADER_SIZE + EXT_SIZE) {
                return null;               // 扩展段被截断：这包不可信，丢掉
            }
            p.originId = b.getInt();
            p.hop = b.get() & 0xFF;
            b.get(); b.get(); b.get();     // 3 字节保留
            offset += EXT_SIZE;
        }
        int available = len - offset;
        if (plen > available) {
            plen = available;              // 包被截断也不要崩，取能拿到的部分
        }
        p.payload = new byte[plen];
        if (plen > 0) {
            b.get(p.payload, 0, plen);
        }
        return p;
    }

    public static Pkt decode(byte[] raw) {
        return raw == null ? null : decode(raw, 0, raw.length);
    }

    /** 短标识，和电脑端 proto.short_id 一样，用于日志里认人。 */
    public static String shortId(String nodeId) {
        if (nodeId == null) {
            return "?";
        }
        String s = nodeId.replace("-", "");
        return s.length() > 6 ? s.substring(0, 6) : s;
    }
}
