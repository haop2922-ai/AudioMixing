package com.audiomixing.app;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

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
 * 12    uint32   samples       本包帧数（每帧 2 声道）
 * 16    uint64   ts_ns         发送时刻墙钟纳秒
 * 24    int16    gain_q14      音量，Q14 定点（1.0 == 16384）
 * 26    uint16   payload_len   负载字节数
 * </pre>
 */
public final class Proto {

    public static final int HEADER_SIZE = 28;
    public static final int MAGIC = 0x574C;
    public static final int VERSION = 1;

    public static final int T_AUDIO = 1, T_HELLO = 2, T_HELLO_ACK = 3, T_PING = 4,
            T_PONG = 5, T_BYE = 6, T_STATS = 7, T_ROUTE = 8, T_KEEPALIVE = 9;

    public static final int CODEC_PCM_F32 = 1, CODEC_PCM_S16 = 2, CODEC_OPUS = 3;

    public static final int FLAG_DISCONTINUITY = 0x01, FLAG_LAST = 0x02, FLAG_SILENCE = 0x04;

    public static final int SAMPLE_RATE = 48000;
    public static final int CHANNELS = 2;
    /** 10 毫秒一块：48000 * 0.01 = 480 帧 */
    public static final int FRAME_SAMPLES = 480;
    /** 1.0 音量对应的 Q14 值 */
    public static final int GAIN_ONE = 16384;

    public static final int DEFAULT_AUDIO_PORT = 47831;
    public static final int DEFAULT_DISCOVERY_PORT = 47830;
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

        public Pkt(int type) {
            this.type = type;
        }
    }

    /** 编成一个可以直接 send 的字节数组。 */
    public static byte[] encode(Pkt p) {
        byte[] payload = p.payload != null ? p.payload : new byte[0];
        ByteBuffer b = ByteBuffer.allocate(HEADER_SIZE + payload.length);
        // 必须显式指定小端：Java 默认是大端，而我们的协议是网络里跑的小端布局。
        b.order(ByteOrder.LITTLE_ENDIAN);
        b.putShort((short) MAGIC);
        b.put((byte) VERSION);
        b.put((byte) (p.type & 0xFF));
        b.put((byte) (p.flags & 0xFF));
        b.put((byte) (p.codec & 0xFF));
        b.putShort((short) (p.seq & 0xFFFF));
        b.putInt(p.ssrc);
        b.putInt(p.samples);
        b.putLong(p.tsNs);
        b.putShort((short) (p.gainQ14 & 0xFFFF));
        b.putShort((short) (payload.length & 0xFFFF));
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
        int available = len - HEADER_SIZE;
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
