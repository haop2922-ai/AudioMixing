package com.audiomixing.app;

import java.util.HashMap;
import java.util.Iterator;
import java.util.Map;

/**
 * 跨接收器共享的「一台设备同一时刻只播一条流」闸门。
 *
 * <p>★ 2.31.2：2.42.2 起广播和点对点拆成了两个端口，手机也对应开了两个
 * {@link AudioRecv}（{@code receiver} + {@code channelReceiver}）。但原来
 * “同一台设备只播一条流”的 {@code hostActiveSsrc} 是每个 AudioRecv 各自的实例字段，
 * 两个接收器互相看不见：同一台电脑的广播流和点对点流会各自由一个 AudioTrack 播放，
 * 叠在一起就是削顶/梳状滤波，听感“沙沙/发毛”。
 *
 * <p>所以把这张表提到两个接收器外面，由 Engine 建一份、两边共用。谁先到谁占位；
 * 占位的那条流 3 秒没有新包，才允许另一条接管。
 *
 * <p>纯 Java，不依赖 Android，可直接用 javac/java 跑 {@code HostGateTest}。
 */
public final class HostGate {

    public static final class Entry {
        public final int ssrc;
        public final Object owner;
        public long lastMs;

        Entry(int ssrc, Object owner, long lastMs) {
            this.ssrc = ssrc;
            this.owner = owner;
            this.lastMs = lastMs;
        }
    }

    /** 上限：防被人乱刷撑爆内存（局域网里不会有这么多设备）。 */
    private static final int MAX_HOSTS = 64;

    private final long idleMs;
    private final Map<String, Entry> map = new HashMap<>();

    public HostGate() {
        this(3000L);
    }

    public HostGate(long idleMs) {
        this.idleMs = idleMs > 0 ? idleMs : 3000L;
    }

    /**
     * 这台设备现在允许哪条流出声。
     *
     * @return null = 放行（这台还没有流，或者就是它自己在放）；
     *         否则返回<b>正在放</b>的那条流的 ssrc（调用方据此抑制这一包）。
     */
    public synchronized Integer allow(String host, int ssrc, Object owner, long now) {
        if (host == null || host.isEmpty()) {
            return null;
        }
        Entry cur = map.get(host);
        if (cur == null) {
            if (map.size() < MAX_HOSTS) {
                map.put(host, new Entry(ssrc, owner, now));
            }
            return null;
        }
        if (cur.ssrc == ssrc) {
            cur.lastMs = now;
            return null;
        }
        if (now - cur.lastMs > idleMs) {
            map.put(host, new Entry(ssrc, owner, now));
            return null;
        }
        return cur.ssrc;
    }

    /** 当前占位的接收器（诊断用；可能为 null）。 */
    public synchronized Object ownerOf(String host) {
        Entry cur = map.get(host);
        return cur == null ? null : cur.owner;
    }

    /** 当前占位的流（诊断用；没有返回 -1）。 */
    public synchronized int ssrcOf(String host) {
        Entry cur = map.get(host);
        return cur == null ? -1 : cur.ssrc;
    }

    /** 一个接收器停了：只释放它自己占的位，不碰另一个接收器。 */
    public synchronized void releaseAll(Object owner) {
        Iterator<Map.Entry<String, Entry>> it = map.entrySet().iterator();
        while (it.hasNext()) {
            if (it.next().getValue().owner == owner) {
                it.remove();
            }
        }
    }

    public synchronized void clear() {
        map.clear();
    }

    public synchronized int size() {
        return map.size();
    }
}
