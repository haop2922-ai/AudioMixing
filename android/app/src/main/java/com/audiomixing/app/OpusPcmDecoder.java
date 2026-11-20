package com.audiomixing.app;

import io.github.jaredmdobson.concentus.OpusDecoder;

/**
 * ★ 2.31.1（#3）：PC → 手机的 Opus 解码。
 *
 * <p>只做一件小事：把一包 Opus 解成 48kHz 立体声 PCM16（小端交错），
 * 交给 {@link AudioRecv.Stream} 按 480 帧切块后走原来的抖动缓冲/混音。
 * 不碰发送方向（手机→PC 仍是 PCM16），也不引入 NDK。
 */
public final class OpusPcmDecoder {

    private final OpusDecoder decoder;

    public OpusPcmDecoder() throws Exception {
        decoder = new OpusDecoder(48000, 2);
    }

    /**
     * @param data   Opus 包
     * @param len    包长
     * @param frames 包里的帧数（协议里的 samples）；0/异常时按 960 处理
     * @return PCM16 小端交错字节；解码失败返回空数组
     */
    public byte[] decode(byte[] data, int len, int frames) {
        int hint = frames > 0 ? frames : 960;
        // Opus 合法帧长最大 120ms（5760 帧）；超出的按 5760 截断，防越界。
        hint = Math.max(120, Math.min(5760, hint));
        short[] pcm = new short[hint * 2];
        int got;
        try {
            got = decoder.decode(data, 0, len, pcm, 0, hint, false);
        } catch (Throwable t) {
            return new byte[0];
        }
        if (got <= 0) {
            return new byte[0];
        }
        byte[] out = new byte[got * 2 * 2];
        for (int i = 0; i < got * 2; i++) {
            int v = pcm[i];
            out[i * 2] = (byte) v;
            out[i * 2 + 1] = (byte) (v >> 8);
        }
        return out;
    }
}
