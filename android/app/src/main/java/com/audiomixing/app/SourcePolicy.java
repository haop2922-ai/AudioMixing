package com.audiomixing.app;

/**
 * 音源开关策略：全是<b>纯函数</b>，没有安卓依赖 —— 所以能拿离线门禁一条条断言。
 *
 * <p>为什么要单独一个类（2026-10-19 用户真机三条反馈）：
 * <ol>
 *   <li><b>绝不自动开麦</b>：用户原话「我停止本机声音采集的时候，居然自动切换到了麦克风采集。
 *       严重危害用户安全，且此时麦克风按钮还是灰色的」—— 采集源不可用时只能<b>如实显示"已停止"+原因</b>，
 *       麦克风必须只由用户主动点击才开；</li>
 *   <li><b>两颗按钮是真 toggle</b>：用户原话「点击未激活按键即为激活，再次点击即为关闭」，
 *       而之前"再点一次不是关闭、而是又激活一遍"；</li>
 *   <li><b>同一时刻只能采一路</b>：AudioSend 里只有一份采集（先关旧的再开新的，绝不两路叠着发），
 *       所以"开了麦时「本机声音」那颗必须显示为关"，而不是两颗都亮着骗人。</li>
 * </ol>
 *
 * <p>状态只有一个值：{@link #SRC_OFF}（没在采）/ {@link #SRC_MIC} / {@link #SRC_PLAYBACK}。
 * 值必须和 {@link AudioSend} 的两个音源常量一致 —— 那边现在是<b>直接引用</b>这里的常量，
 * 所以不可能漂。
 */
public final class SourcePolicy {

    private SourcePolicy() {
    }

    /** 音源：麦克风（对讲用，最早的实现）。 */
    public static final int SRC_MIC = 0;
    /** 音源：本机正在播放的声音（音乐/视频/游戏），Android 10+ 才有。 */
    public static final int SRC_PLAYBACK = 1;
    /** 没在采：两颗音源按钮都不亮。 */
    public static final int SRC_OFF = -1;

    /**
     * 把（开关，实际在采的音源）折成一个状态值。
     *
     * <p>界面/诊断只认这一个值，而且<b>每次刷新都重新算</b>（见 Engine.sourceState）——
     * 绝不用缓存的标志位，否则又会出现"显示的和实际不一致"。
     */
    public static int state(boolean enabled, int activeKind) {
        return enabled ? activeKind : SRC_OFF;
    }

    /** 这一颗音源按钮该不该点亮：只有"当前开着的那个音源"那颗亮，另一颗必暗（互斥）。 */
    public static boolean lit(int state, int kind) {
        return state == kind;
    }

    /**
     * 点一下某颗音源按钮之后的新状态：点在<b>已亮</b>的那颗 = 关闭；点在<b>没亮</b>的那颗 = 打开它。
     *
     * <p>返回值<b>一定和原状态不同</b> —— 用户明令禁止"点了按钮但状态没变化"。
     */
    public static int toggle(int state, int kind) {
        return state == kind ? SRC_OFF : kind;
    }

    /**
     * 本机声音采集不可用了（没授权 / 系统收回授权 / 采不到 / ROM 阉割）之后该变成什么状态。
     *
     * <p><b>隐私硬约束：绝不自动打开麦克风。</b>本机声音断了就一律变成"已停止"，
     * 由用户自己点「🎤 麦克风」才开。原本在采麦克风时不受影响（那条路和 MediaProjection 无关）。
     */
    public static int afterPlaybackLost(int state) {
        return state == SRC_PLAYBACK ? SRC_OFF : state;
    }

    /** 这一路现在能不能选：本机声音要 Android 10+；麦克风任何时候都能选。 */
    public static boolean selectable(int kind, boolean playbackSupported) {
        return kind != SRC_PLAYBACK || playbackSupported;
    }
}
