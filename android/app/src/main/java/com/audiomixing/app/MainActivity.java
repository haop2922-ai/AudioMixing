package com.audiomixing.app;

import android.Manifest;
import android.app.Activity;
import android.app.Dialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.media.AudioManager;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.EditText;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 界面：打开就能看见局域网里的设备，点一下就能用。
 *
 * <p><b>没有配对、没有房间号、没有二维码</b> —— 用户原话是"一个局域网下的没什么意义"。
 * 设备列表完全来自 {@link Discovery} 的自主发现：谁在发信标，谁的卡片就出现；
 * 15 秒没信标就自己消失。手机之间直连，电脑关掉照常工作。
 *
 * <p>每一行两个开关：
 * <ul>
 *   <li><b>🎧 收听</b>：收这台发来的声音（可以同时收听多台，声音会混在一起）。</li>
 *   <li><b>🔊 送声音</b>：把<b>本机正在播放的声音</b>送给这台（音乐/视频/游戏）——
 *       这是这个 App 本来的用法；切成 🎤 麦克风就是对讲。</li>
 * </ul>
 *
 * <p><b>★ 为什么"送什么声音"要做成显式可选的</b>：早期版本只采麦克风，用户的原话是
 * 「你这手机都是采集的麦克风的声音，那不成对讲机了吗？我的需求一直都是桌面音频啊」。
 * 所以这个页面上：①音源选择放在最显眼的位置；②下面那行<b>永远写"实际在送的是哪一个"</b>，
 * 采不到本机声音就如实写原因并说明已退回麦克风；③限制（安卓 10 以下没有这个能力、
 * DRM/主动禁止采集的内容采不到、部分 ROM 阉割）都写在界面上，不让用户再被误导一次。
 *
 * <p>视觉对齐 {@code audiomixing/ui/phone.html}：深色底 + 圆角卡片 + 大状态 + 电平条。
 * 工程零依赖（没有 appcompat），所以界面全部用代码搭、圆角用 {@link GradientDrawable} 画。
 */
public class MainActivity extends Activity {

    // ── 配色：和手机网页版 phone.html 同一套 ──
    private static final int BG = 0xFF070B12;
    private static final int FG = 0xFFEAF1FB;
    private static final int DIM = 0xFF8EA0B8;
    private static final int ACC = 0xFF38BDF8;
    private static final int ACC2 = 0xFF6366F1;
    private static final int OK = 0xFF34D399;
    private static final int WARN = 0xFFFBBF24;
    private static final int CARD_BG = 0x0BFFFFFF;   // rgba(255,255,255,.045)
    private static final int LINE = 0x17FFFFFF;      // rgba(255,255,255,.09)
    private static final int TRACK = 0x1AFFFFFF;     // 电平条底槽
    private static final int ON_TEXT = 0xFF04121F;   // 亮色按钮上的深色字

    private static final int MATCH = ViewGroup.LayoutParams.MATCH_PARENT;
    private static final int WRAP = ViewGroup.LayoutParams.WRAP_CONTENT;

    private static final int REQ_MIC = 1001;
    private static final int REQ_NOTIF = 1002;
    /** ★ 「采本机声音」的系统授权窗口（MediaProjection 的一次性许可）。 */
    private static final int REQ_PROJ = 1003;
    /** 界面刷新间隔：150ms 足够跟手，又不会一直在重画。 */
    private static final long TICK_MS = 150L;

    private Engine engine;
    private Handler ui;
    private AudioManager audioManager;
    private int sysVolMax;

    private LinearLayout listBox;
    private TextView pill;
    private TextView detailToggle;
    private LinearLayout detailWrap;
    private TextView volumeVal;
    private SeekBar volumeBar;
    /** ★ 系统媒体音量那一行（只显示，不修改）。 */
    private TextView sysVolText;
    /** 头部那行"本机名字 · 平台"（点它就能改名）。 */
    private TextView subLine;
    // ★ 「送什么声音」那一块
    private TextView playChip;
    private TextView micChip;
    private TextView sourceNow;
    private TextView sourceWarn;
    private String lastSrcSig;
    // ★ 「听广播」（频道模式）
    private TextView channelChip;
    private TextView channelNow;
    private boolean lastChannelOn;
    // ★ 「📢 广播我的声音」（发出去）
    private TextView broadcastChip;
    private TextView broadcastNow;
    private boolean lastBroadcastOn;
    /** 「自动接受收听请求」开关上一次的选中态（只在变了才重建背景）。 */
    private boolean lastAutoAccept;
    /** 「❔ 展开说明」/「收起说明」（简洁档里把长文字收起来用）。 */
    private TextView helpChip;
    // ★ ①④ 自动收听 + 安全停发的界面元素
    private TextView autoListenLine;
    private TextView suspendedLine;
    private TextView autoAcceptChip;
    private TextView autoAcceptHint;

    private final Map<String, PeerRow> rows = new LinkedHashMap<String, PeerRow>();
    private final Map<String, TextView> kvValues = new LinkedHashMap<String, TextView>();

    /** null 表示还没建过列表，这样第一次刷新一定会把"搜索中"的空状态画出来。 */
    private String lastSig;
    private boolean detailsOpen;
    private boolean ticking;
    private int tickCount;
    /** 等权限 / 等投屏授权回来之后再开始送的那台设备。 */
    private Discovery.Peer pendingSend;

    // ══════════════════ ★ 三档浏览模式：简洁 / 详细 / 监控 ══════════════════
    //
    // 用户原话：「补充：手机app也应当设置监控/简洁/详细，三个页面。」
    // 三档的定位（和电脑端、和用户统一的口径，名字不再改）：
    //   · 简洁：只留最关键的东西 —— 打开就知道点哪里（长说明收起来）
    //   · 详细：把"详情"里的东西变成一档常驻显示（本机信息 + 每台的完整信息）
    //   · 监控：只读的实时数字面板（收发/丢包/坏包/缓冲/带宽/逐流/发现层自检），一屏读完
    // ★ 同一份数据三种呈现：三档全部复用 Engine 的查询接口，没有第二套取数逻辑。
    private static final int MODE_SIMPLE = 0;
    private static final int MODE_DETAIL = 1;
    private static final int MODE_MONITOR = 2;
    private static final String PREFS_NAME = "audiomixing";
    private static final String KEY_VIEW_MODE = "view_mode";

    private int viewMode = MODE_SIMPLE;
    private TextView modeSimple;
    private TextView modeDetail;
    private TextView modeMonitor;
    private TextView monitorText;
    private View monitorCard;
    private View channelCard;
    private View controlCard;
    private View hintCard;
    /** 「简洁」档里被收起来的长说明（点「❔ 展开说明」还能看）。 */
    private final List<View> longTextViews = new ArrayList<View>();
    private boolean helpExpanded;

    /** 三档切换（分段控件，和「送什么声音」同一套视觉）。 */
    private void setViewMode(int mode) {
        viewMode = mode;
        saveViewMode();
        if (mode == MODE_DETAIL) {
            detailsOpen = true;      // 「详细」档 = 把「详情」里的东西常驻展开
        }
        applyMode();
        if (mode == MODE_MONITOR && monitorText != null) {
            setTextIfChanged(monitorText, engine.monitorText());   // 切过去立刻有内容，不等 300ms
        }
        refreshNow();
    }

    private void saveViewMode() {
        try {
            getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                    .edit().putInt(KEY_VIEW_MODE, viewMode).apply();
        } catch (Throwable ignored) {
            // 存不了就只是"下次打开回到默认档"，不影响使用
        }
    }

    private int loadViewMode() {
        try {
            int m = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                    .getInt(KEY_VIEW_MODE, MODE_SIMPLE);
            return (m == MODE_DETAIL || m == MODE_MONITOR) ? m : MODE_SIMPLE;
        } catch (Throwable t) {
            return MODE_SIMPLE;      // 默认「简洁」：第一次用的人最需要"点哪里"
        }
    }

    /** 按当前档位切换"显示什么"—— 数据一份，呈现三样。 */
    private void applyMode() {
        boolean monitor = viewMode == MODE_MONITOR;
        // 「简洁」把长说明收起来（可展开）；「详细」一律展开
        boolean showLong = viewMode == MODE_DETAIL || (viewMode == MODE_SIMPLE && helpExpanded);
        if (modeSimple != null) {
            styleChip(modeSimple, viewMode == MODE_SIMPLE, "简洁");
            styleChip(modeDetail, viewMode == MODE_DETAIL, "详细");
            styleChip(modeMonitor, viewMode == MODE_MONITOR, "监控");
        }
        if (monitorCard != null) {
            monitorCard.setVisibility(monitor ? View.VISIBLE : View.GONE);
        }
        if (channelCard != null) {
            channelCard.setVisibility(monitor ? View.GONE : View.VISIBLE);
        }
        if (listBox != null) {
            listBox.setVisibility(monitor ? View.GONE : View.VISIBLE);
        }
        if (controlCard != null) {
            controlCard.setVisibility(monitor ? View.GONE : View.VISIBLE);
        }
        if (hintCard != null) {
            // 底部说明：只有"简洁 + 展开了说明"或"详细"档才显示（监控档没有这些块）
            hintCard.setVisibility(!monitor && showLong ? View.VISIBLE : View.GONE);
        }
        for (View v : longTextViews) {
            v.setVisibility(showLong ? View.VISIBLE : View.GONE);
        }
        if (helpChip != null) {
            // 「展开/收起说明」只在简洁档有意义（详细档本来就全展开，监控档没有说明块）
            setTextIfChanged(helpChip, helpExpanded ? "❔ 收起说明" : "❔ 展开说明");
            helpChip.setVisibility(viewMode == MODE_SIMPLE ? View.VISIBLE : View.GONE);
        }
        if (detailWrap != null) {
            // 「详细」档：本机信息块（IP/端口/统计/对端回话…）常驻展开；
            // 其他档回到用户自己的「▸ 详情」开关。
            boolean showDetails = viewMode == MODE_DETAIL || detailsOpen;
            detailWrap.setVisibility(showDetails ? View.VISIBLE : View.GONE);
        }
    }

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!ticking) {
                return;
            }
            refresh();
            tickCount++;
            if (tickCount % 4 == 0) {
                refreshDetails();     // 详情里的统计没必要 150ms 刷一次
            }
            ui.postDelayed(this, TICK_MS);
        }
    };

    // ────────────────────────────── 生命周期 ──────────────────────────────

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        engine = Engine.get(this);
        ui = new Handler(Looper.getMainLooper());
        audioManager = (AudioManager) getSystemService(AUDIO_SERVICE);
        sysVolMax = (audioManager == null)
                ? 0 : audioManager.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        viewMode = loadViewMode();     // ★ 上次看的是哪一档，这次还看哪一档

        Window w = getWindow();
        w.setStatusBarColor(BG);
        w.setNavigationBarColor(BG);

        setContentView(buildUi());

        engine.start();               // 打开 App 就开始找设备，不需要用户做任何事
        MixService.safeStart(this);   // 常驻通知；服务起不来也不影响界面可用
        askNotificationPermission();  // 安卓 13+ 通知要运行时授权
    }

    @Override
    protected void onResume() {
        super.onResume();
        ticking = true;
        ui.removeCallbacks(tick);
        ui.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        ticking = false;              // 界面不可见就别刷了（音频照跑，那是 Service 的事）
        ui.removeCallbacks(tick);
    }

    @Override
    protected void onDestroy() {
        // 故意不 stop 引擎：界面被回收不代表传输要断，那是前台服务在管的事。
        ui.removeCallbacks(tick);
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int req, String[] perms, int[] granted) {
        super.onRequestPermissionsResult(req, perms, granted);
        if (req != REQ_MIC) {
            return;
        }
        boolean ok = granted.length > 0 && granted[0] == PackageManager.PERMISSION_GRANTED;
        Discovery.Peer p = pendingSend;
        pendingSend = null;
        if (!ok) {
            // 采本机声音也要这个权限（系统对 AudioRecord 一律要求），所以这句话必须说准，
            // 否则用户会以为"我不给麦克风权限就送不了本机声音"是 Bug。
            toast("没有录音权限，送不了声音（采本机声音也需要它，但不会采你说话的声音）");
            engine.cancelPendingSend("没有录音权限");
            refreshNow();
            return;
        }
        // 权限到手了，再叫一次服务把 microphone 这个前台类型补上（安卓 14 要求）。
        // 注意是"再叫一次"而不是 stop+start —— 后者会把刚开起来的送声一起掐掉。
        MixService.safeStart(this);
        if (p != null) {
            startSendFlow(p);     // 接着走：如果选的是本机声音，下一步就是投屏授权
        }
        refreshNow();
    }

    /**
     * ★ 投屏授权的返回。这里<b>不</b>自己换 MediaProjection，而是交给前台服务去做：
     * targetSdk 34 起必须先有带 mediaProjection 类型的前台服务，再调 getMediaProjection，
     * 顺序反了系统直接抛异常。服务那边会把这两步按顺序做完（见 MixService.armProjection）。
     */
    @Override
    protected void onActivityResult(int req, int resultCode, Intent data) {
        super.onActivityResult(req, resultCode, data);
        if (req != REQ_PROJ) {
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            pendingSend = null;
            engine.cancelPendingSend("你在系统窗口里拒绝了授权");
            toast("没有授权采不到本机声音；也可以改用麦克风");
            refreshNow();
            return;
        }
        MixService.armProjection(this, resultCode, data);
        toast("已授权，正在开始送本机声音…");
        refreshNow();
    }

    // ────────────────────────────── 列表刷新 ──────────────────────────────

    private void refresh() {
        // 服务被停掉（通知里的「停止」）而界面还开着：自动恢复发现，别让用户对着空列表发呆。
        if (!engine.isStarted()) {
            engine.start();
        }
        // 目标设备 15 秒没信标就自动停掉：否则对端走了，麦克风还一直开着，界面上却找不到开关。
        engine.pruneGoneTargets();

        List<Discovery.Peer> peers;
        try {
            peers = engine.peers();
        } catch (Throwable t) {
            peers = new ArrayList<Discovery.Peer>();
        }
        String sig = signature(peers);
        if (lastSig == null || !sig.equals(lastSig)) {
            lastSig = sig;
            rebuild(peers);
        }
        for (PeerRow r : rows.values()) {
            r.sync();
        }
        refreshSourceRow();
        refreshChannelRow();
        refreshVolumeRow();
        if (subLine != null) {
            // 名字被改过之后，标题下面那行要跟着变（信标 2 秒内带出去，界面立刻变）
            setTextIfChanged(subLine, engine.localName() + " ✏️ · " + engine.platform());
        }
        if (viewMode == MODE_MONITOR && monitorText != null && tickCount % 2 == 0) {
            // 监控档的数字每 300ms 刷一次就够（屏幕上的数字跳得太快反而看不清）
            setTextIfChanged(monitorText, engine.monitorText());
        }
        updateHeader(peers.size());
    }

    /** 刷新音量那两块（我们这一层的值 + 只读的系统音量）。 */
    private void refreshVolumeRow() {
        if (sysVolText != null) {
            setTextIfChanged(sysVolText, readSystemVolume() + " · " + engine.volumeExplainText());
        }
        if (volumeBar != null && !volumeBar.isPressed()) {
            // 滑杆没被按住时跟一下引擎里的真实值（比如服务重启后回到默认）
            int want = Math.round(engine.volume() * 100);
            if (volumeBar.getProgress() != want) {
                volumeBar.setProgress(want);
            }
        }
    }

    /** 刷新「听广播 / 广播我的声音」那一块（状态会随"收到几个包"变，所以每 150ms 一起刷）。 */
    private void refreshChannelRow() {
        if (channelNow == null) {
            return;
        }
        boolean on = engine.isChannelListening();
        if (channelChip != null && on != lastChannelOn) {
            lastChannelOn = on;
            styleChip(channelChip, on, "📻 听广播");
        }
        setTextIfChanged(channelNow, engine.channelStateText());
        int color = on ? OK : DIM;
        if (channelNow.getCurrentTextColor() != color) {
            channelNow.setTextColor(color);
        }

        // ★ 发送侧：📢 广播我的声音
        boolean bc = engine.isBroadcasting();
        if (broadcastChip != null && bc != lastBroadcastOn) {
            lastBroadcastOn = bc;
            styleChip(broadcastChip, bc, "📢 广播我的声音");
        }
        if (broadcastNow != null) {
            setTextIfChanged(broadcastNow, engine.broadcastStateText());
            broadcastNow.setVisibility(
                    (bc || engine.sendTargetCount() > 0) ? View.VISIBLE : View.GONE);
            // 目标多的时候用警告色 —— 带宽/分片的代价要显眼，但不拦着用户用
            int bcColor = !bc ? DIM : (engine.sendTargetCount() >= 4 ? WARN : OK);
            if (broadcastNow.getCurrentTextColor() != bcColor) {
                broadcastNow.setTextColor(bcColor);
            }
        }

        // ★ ① 「XXX 请求收听，已自动打开（你可以随时关掉）」
        if (autoListenLine != null) {
            String notice = engine.autoListenNotice();
            setTextIfChanged(autoListenLine, notice.length() > 0 ? ("📣 " + notice) : "");
            autoListenLine.setVisibility(notice.length() > 0 ? View.VISIBLE : View.GONE);
        }
        // ★ ④ 「已停止向 XXX 发送 —— 它已经 18 秒没有消息了…」
        if (suspendedLine != null) {
            String susp = engine.sendSuspendedText();
            setTextIfChanged(suspendedLine, susp.length() > 0 ? ("⏸ " + susp) : "");
            suspendedLine.setVisibility(susp.length() > 0 ? View.VISIBLE : View.GONE);
        }
        // 自动接受开关的选中态 + 一句解释
        if (autoAcceptChip != null) {
            boolean autoOn = engine.isAutoAcceptListen();
            if (autoOn != lastAutoAccept) {
                lastAutoAccept = autoOn;
                styleChip(autoAcceptChip, autoOn, "自动接受收听请求");
            }
            setTextIfChanged(autoAcceptHint, autoOn
                    ? "开：别人点「送给它」我就自动开始听（那台的收听按钮随时能关）"
                    : "关：别人点「送给它」我也不会自动听，要自己点「🎧 收听」");
        }
    }

    /**
     * 刷新「送什么声音」那一块。
     *
     * <p>为什么每 150ms 都刷：音源会因为"授权被系统收回""设备采不到本机声音"而<b>自己变</b>，
     * 用户看到的必须是当下真实的那一个，而不是他上次点的那个。
     */
    private void refreshSourceRow() {
        if (sourceNow == null) {
            return;
        }
        int want = engine.sourceKind();
        boolean supported = engine.playbackSupported();
        boolean sending = engine.senderRunning();
        int act = engine.activeSourceKind();
        // 只在状态真的变了才重建按钮背景（150ms 一次，别一直产生垃圾对象）
        String sig = want + "|" + supported + "|" + sending + "|" + act + "|" + engine.hasProjection();
        if (!sig.equals(lastSrcSig)) {
            lastSrcSig = sig;
            // 低版本把「本机声音」画成没选中的样子：点了会明确告诉你"不支持"，
            // 而不是选中了却什么也采不到（那正是会误导人的地方）。
            styleChip(playChip, supported && want == AudioSend.SRC_PLAYBACK, "🔊 本机声音");
            styleChip(micChip, want == AudioSend.SRC_MIC, "🎤 麦克风");
        }
        setTextIfChanged(sourceNow, engine.sourceStateText());
        int color = engine.sourceMismatch() ? WARN : (sending ? OK : DIM);
        if (sourceNow.getCurrentTextColor() != color) {
            sourceNow.setTextColor(color);
        }
        String hint = engine.silenceHintText();
        setTextIfChanged(sourceWarn, hint);
        sourceWarn.setVisibility(hint.length() == 0 ? View.GONE : View.VISIBLE);
    }

    private void refreshNow() {
        refresh();
        refreshDetails();
    }

    private static String signature(List<Discovery.Peer> peers) {
        StringBuilder sb = new StringBuilder();
        for (Discovery.Peer p : peers) {
            sb.append(Engine.idOf(p)).append('|');
        }
        return sb.toString();
    }

    private void rebuild(List<Discovery.Peer> peers) {
        listBox.removeAllViews();
        rows.clear();
        if (peers.isEmpty()) {
            listBox.addView(buildEmptyCard());
            return;
        }
        for (Discovery.Peer p : peers) {
            PeerRow r = new PeerRow(p);
            rows.put(Engine.idOf(p), r);
            listBox.addView(r.card);
        }
    }

    private void updateHeader(int peerCount) {
        int conn = engine.connectedCount();
        // ★ 在线数含本机（用户原话：「右上角应当显示三台设备在线。因为本机也算。」）。
        //   文案上写清楚"含本机"，不让人自己猜这个数字包不包含自己。
        int online = peerCount + 1;
        if (conn > 0) {
            setTextIfChanged(pill, "正在传输 本机 + " + conn + " 台");
            stylePill(OK);
        } else if (peerCount > 0) {
            setTextIfChanged(pill, online + " 台在线（含本机）");
            stylePill(ACC);
        } else {
            setTextIfChanged(pill, "搜索中…（本机 1 台）");
            stylePill(DIM);
        }
    }

    private void refreshDetails() {
        if (!detailsOpen) {
            return;
        }
        put("name", engine.nodeName() + " · " + engine.platform()
                + (engine.localNameIsCustom() ? "（名字：用户自定义）" : "（名字：默认）"));
        put("ip", engine.localIpsText());
        put("port", engine.audioPort() + "（UDP，收发都用它）");
        put("found", engine.peers().size() + " 台");
        put("conn", "收听 " + engine.listeningCount() + " 台 / 送声 " + engine.sendingCount() + " 台");
        // ★ 「实际收到哪几路」：开关是意图，这一行才是事实（放最前面，跟"已连接"挨着）
        put("streams", engine.recvStreamsText());
        // ★ 发出去：几台、逐台发了多少、带宽是多少（不藏代价）
        put("send", engine.sendStatsText());
        put("volume", engine.volumeText() + "\n" + engine.systemVolumeText());
        put("recv", engine.statsText());
        put("send", engine.sendStatsText());
        // 对端回话：这是"哪一端没在发"最直接的证据（对方自己说的）。
        put("peers", Diagnosis.peersSummary(engine.peerStatsList(), SystemClock.elapsedRealtime()));
        put("disc", engine.discoveryStatsText());
    }

    private void put(String key, String value) {
        TextView v = kvValues.get(key);
        if (v != null) {
            setTextIfChanged(v, value);
        }
    }

    // ────────────────────────────── 交互 ──────────────────────────────

    private void onListenClick(Discovery.Peer p) {
        boolean on = engine.isListening(p);
        engine.setListening(p, !on);
        toast((on ? "已停止收听 " : "开始收听 ") + p.label());
        refreshNow();
    }

    private void onSendClick(Discovery.Peer p) {
        // ★ 广播开着的时候，每台都会收到我的声音 —— 单独关某一台没有意义（它还会从广播那条路收到），
        //   所以这里直说清楚，而不是让用户点了没反应还以为是 bug。
        if (engine.isBroadcasting()) {
            toast("正在「📢 广播我的声音」，每一台都会收到；想只送给某台就先关掉广播");
            return;
        }
        if (engine.isSending(p)) {
            engine.setSending(p, false);
            toast("已停止送给 " + p.label());
            refreshNow();
            return;
        }
        startSendFlow(p);
    }
    /**
     * 「送声音」的完整流程。顺序是有原因的：
     * <ol>
     *   <li>低于安卓 10 → 本机声音这个能力根本不存在，直接改成麦克风并说清楚；</li>
     *   <li>要录音权限 —— <b>采本机声音同样需要它</b>（系统对 AudioRecord 一律要求 RECORD_AUDIO），
     *       界面上会写明"不会采你说话的声音"，免得用户又以为这是对讲机；</li>
     *   <li>选的是本机声音且还没授权 → 弹一次系统授权窗口，授权回来之后由前台服务接着开始送。</li>
     * </ol>
     */
    private void startSendFlow(Discovery.Peer p) {
        if (p == null) {
            return;
        }
        int want = engine.sourceKind();
        if (want == AudioSend.SRC_PLAYBACK && !engine.playbackSupported()) {
            toast("你的安卓版本不支持采本机声音（要 Android 10 及以上），只能用麦克风");
            engine.setSourceKind(AudioSend.SRC_MIC);
            want = AudioSend.SRC_MIC;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            pendingSend = p;
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        if (want == AudioSend.SRC_PLAYBACK && !engine.hasProjection()) {
            pendingSend = p;
            engine.requestSendWithPlayback(p);      // 授权回来之后还知道要送给谁
            if (!requestProjection()) {
                pendingSend = null;
                engine.cancelPendingSend("这台设备没有投屏服务，送不了本机声音");
                toast("这台设备没有投屏服务，送不了本机声音；可以改用麦克风");
            }
            return;
        }
        engine.setSending(p, true);
        // 提示里说的是"打算送什么"；真实采到的是什么，下面那行状态 150ms 内就会如实刷新
        // （采不到本机声音时会写"已退回麦克风 + 原因"）。
        toast("开始送声音：" + AudioSend.sourceShortName(want) + " → " + p.label());
        MixService.safeStart(this);
        refreshNow();
    }

    /** 弹系统授权窗口（「是否允许 Audio Mixing 录制或投放？」）。返回 false = 连窗口都拉不起来。 */
    private boolean requestProjection() {
        try {
            MediaProjectionManager mgr =
                    (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
            if (mgr == null) {
                return false;
            }
            startActivityForResult(mgr.createScreenCaptureIntent(), REQ_PROJ);
            return true;
        } catch (Throwable t) {
            toast("拉不起授权窗口：" + t);
            return false;
        }
    }

    /** 用户点了音源按钮。正在送声音时是热切换（不中断发送、不会两路声音叠着发）。 */
    private void onSourceClick(int kind) {
        if (kind == AudioSend.SRC_PLAYBACK && !engine.playbackSupported()) {
            toast("你的安卓版本不支持采集本机声音（要 Android 10 及以上），只能用麦克风");
            return;
        }
        engine.setSourceKind(kind);
        if (kind == AudioSend.SRC_PLAYBACK && !engine.hasProjection()) {
            if (requestProjection()) {
                toast("请在系统窗口里点「立即开始」/「允许」（授权一次，之后一直用）");
            }
        } else {
            toast(kind == AudioSend.SRC_PLAYBACK
                    ? "已切到本机声音（正在送的话会立刻换过来）"
                    : "已切到麦克风");
        }
        refreshNow();
    }

    private void toggleDetails() {
        detailsOpen = !detailsOpen;
        detailWrap.setVisibility(detailsOpen ? View.VISIBLE : View.GONE);
        setTextIfChanged(detailToggle, detailsOpen ? "▾ 详情" : "▸ 详情");
        if (detailsOpen) {
            refreshDetails();
        }
    }

    // ────────────────────────────── 诊断 ──────────────────────────────

    /**
     * ★ 「🚀 快速上手」对话框：把"该点哪里"写成 30 秒能读完的步骤。
     *
     * <p>为什么要有它：用户的评价标准是"打开就知道点哪里"。三档切换之后，
     * 简洁档把长说明收起来了，所以必须有一个**三档都进得来**的地方把这些话说清楚。
     */
    private void showQuickStartDialog() {
        final Dialog d = new Dialog(this);
        d.requestWindowFeature(Window.FEATURE_NO_TITLE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackground(roundRect(0xF5070B12, dp(18), LINE, Math.max(1, dpi(1))));
        int pad = dpi(16);
        box.setPadding(pad, pad, pad, pad);

        box.addView(text("🚀 快速上手（30 秒）", 16f, FG, true));

        TextView body = text(
                "① 手机和电脑 / 另一台手机连同一个 Wi-Fi，打开 App —— 设备会自己出现，不用配对、不用扫码。\n"
                + "② 想听电脑正在放的声音：电脑上开「广播我的声音」→ 手机上点「📻 听广播」。\n"
                + "③ 想让别的设备听我这台的声音：点「📢 广播我的声音」（所有人都听，逐个单播、不盲发），"
                + "或者点某一台的「🔊 送声音」（只送它）。\n"
                + "④ 送出去的是手机正在放的音乐/视频/游戏（第一次会让你授权一次）；"
                + "换成「🎤 麦克风」就是对讲。\n"
                + "⑤ 没声音时点「🩺 诊断」，把整段复制出来发给对方 —— 报告里写明了是哪一端没在发。\n"
                + "⑥ 音量滑杆只管「我们这一层」（不改你的系统音量）；系统音量单独一行只显示。\n"
                + "⑦ 顶部三档：简洁（只留操作）/ 详细（带完整信息）/ 监控（只读实时数字）。",
                12f, FG, false);
        body.setLineSpacing(dp(5), 1f);
        ScrollView sv = new ScrollView(this);
        sv.addView(body, new ViewGroup.LayoutParams(MATCH, WRAP));
        int bodyH = Math.max(dpi(200),
                (int) (getResources().getDisplayMetrics().heightPixels * 0.46f));
        box.addView(sv, margins(MATCH, bodyH, 0, dpi(10), 0, dpi(12)));

        TextView ok = chip("知道了");
        ok.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                d.dismiss();
            }
        });
        box.addView(ok, margins(MATCH, dpi(46), 0, 0, 0, 0));

        d.setContentView(box);
        Window dw = d.getWindow();
        if (dw != null) {
            dw.setBackgroundDrawable(new ColorDrawable(0));
            dw.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
            dw.setDimAmount(0.62f);
            dw.setLayout(MATCH, WRAP);
        }
        d.show();
    }

    /**
     * ★ 「✏️ 改本机名称」对话框。
     *
     * <p>用户原话：「本机id最好支持自己编辑，不排除多人用同型号设备」。
     * 这里改的只是<b>名字</b>（别人列表里看到的那个）；节点 ID 是身份，不在界面上暴露、也不允许改
     * —— 改重复了电脑端就分不清谁是谁，路由表也会指向不清。
     */
    private void showRenameDialog() {
        final Dialog d = new Dialog(this);
        d.requestWindowFeature(Window.FEATURE_NO_TITLE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackground(roundRect(0xF5070B12, dp(18), LINE, Math.max(1, dpi(1))));
        int pad = dpi(16);
        box.setPadding(pad, pad, pad, pad);

        box.addView(text("✏️ 改本机名称", 16f, FG, true));
        TextView hint = text("别人在设备列表里看到的就是这个名字（下一个信标 2 秒内带出去）。\n"
                + "两台同型号手机默认名已经用后缀区分（例如 M332BF-7f3a），想改成「小红的手机」这种更好认的，在这里改。\n"
                + "改完会记住，重启还在。（节点 ID 是身份，不在这里改 —— 改重了别人就分不清了。）",
                11.5f, DIM, false);
        hint.setLineSpacing(dp(3), 1f);
        box.addView(hint, margins(MATCH, WRAP, 0, dpi(8), 0, dpi(10)));

        final EditText et = new EditText(this);
        et.setSingleLine(true);
        et.setText(engine.localName());
        et.setSelection(et.getText().length());
        et.setTextColor(FG);
        et.setHintTextColor(DIM);
        et.setHint("例如：小红的手机");
        et.setBackground(roundRect(0x12FFFFFF, dp(10), LINE, Math.max(1, dpi(1))));
        et.setPadding(dpi(10), dpi(10), dpi(10), dpi(10));
        box.addView(et, margins(MATCH, WRAP, 0, 0, 0, dpi(12)));

        LinearLayout btns = new LinearLayout(this);
        btns.setOrientation(LinearLayout.HORIZONTAL);

        TextView save = chip("保存");
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String name = et.getText() == null ? "" : et.getText().toString().trim();
                if (name.isEmpty()) {
                    toast("名字不能是空的（保持原名字就行）");
                    return;
                }
                engine.setLocalName(name);
                toast("已改名为：" + engine.localName());
                d.dismiss();
                refreshNow();
            }
        });
        LinearLayout.LayoutParams slp = margins(0, dpi(46), 0, 0, dpi(8), 0);
        slp.weight = 1f;
        btns.addView(save, slp);

        TextView cancel = chip("取消");
        cancel.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                d.dismiss();
            }
        });
        LinearLayout.LayoutParams clp = margins(0, dpi(46), 0, 0, 0, 0);
        clp.weight = 1f;
        btns.addView(cancel, clp);
        box.addView(btns);

        d.setContentView(box);
        Window dw = d.getWindow();
        if (dw != null) {
            dw.setBackgroundDrawable(new ColorDrawable(0));
            dw.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
            dw.setDimAmount(0.62f);
            dw.setLayout(MATCH, WRAP);
            // 弹出键盘：改名是个纯输入动作，用户不该再点一次输入框
            dw.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_VISIBLE);
        }
        d.show();
    }

    /**
     * 「🩺 诊断」对话框：上半部分是可滚动、可长按选中的报告全文，下面是「复制」。
     *
     * <p><b>为什么不用 AlertDialog</b>：这个工程零依赖、主题是深色的，
     * 系统 AlertDialog 的亮色底板和整套视觉不搭，还得为它单独调主题；自己搭一个
     * {@link Dialog} 反而代码更少、圆角和配色都能直接复用页面那套 tools。
     *
     * <p><b>为什么自动刷新</b>：这报告是给"现场盯着数字变化"用的（对方还在不在发、
     * 丢包涨不涨），一打开就定格的快照没什么用。所以每 1.5 秒重算一次；
     * 用户点一下正文（想手动选中一段）就停掉自动刷新，免得把选区刷没。
     */
    private void showDiagnosisDialog() {
        final Dialog d = new Dialog(this);
        d.requestWindowFeature(Window.FEATURE_NO_TITLE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackground(roundRect(0xF5070B12, dp(18), LINE, Math.max(1, dpi(1))));
        int pad = dpi(16);
        box.setPadding(pad, pad, pad, pad);

        box.addView(text("🩺 诊断报告", 16f, FG, true));

        TextView hint = text("整段可以长按选中；点「复制」一键拷走。"
                + "数字每 1.5 秒自动刷新，点一下正文可暂停刷新再慢慢选。", 11.5f, DIM, false);
        hint.setLineSpacing(dp(3), 1f);
        box.addView(hint, margins(MATCH, WRAP, 0, dpi(6), 0, dpi(10)));

        final TextView body = text("", 11f, FG, false);
        body.setTypeface(Typeface.MONOSPACE);
        body.setTextIsSelectable(true);      // 用户要的是"能复制"，这里连手动选也一并给上
        body.setLineSpacing(dp(3), 1f);

        ScrollView sv = new ScrollView(this);
        sv.setVerticalScrollBarEnabled(true);
        sv.addView(body, new ViewGroup.LayoutParams(MATCH, WRAP));
        // 固定占屏幕一半多一点：报告很长必须能滚，又不能把对话框撑出屏幕。
        int bodyH = Math.max(dpi(220),
                (int) (getResources().getDisplayMetrics().heightPixels * 0.52f));
        box.addView(sv, new LinearLayout.LayoutParams(MATCH, bodyH));

        final boolean[] auto = {true};
        body.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                if (auto[0]) {
                    auto[0] = false;
                    toast("已暂停自动刷新，可以慢慢选了");
                }
            }
        });

        final Runnable refreshBody = new Runnable() {
            @Override
            public void run() {
                try {
                    setTextIfChanged(body, Diagnosis.build(MainActivity.this));
                } catch (Throwable t) {
                    setTextIfChanged(body, "生成诊断报告失败：" + t);
                }
                // 对话框关了（isShowing 为假）就自然停掉，不会一直挂着刷。
                if (auto[0] && d.isShowing()) {
                    ui.postDelayed(this, 1500L);
                }
            }
        };

        LinearLayout btns = new LinearLayout(this);
        btns.setOrientation(LinearLayout.HORIZONTAL);

        TextView copy = chip("📋 复制");
        copy.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                copyToClipboard("Audio Mixing 诊断报告", body.getText().toString());
            }
        });
        LinearLayout.LayoutParams clp = margins(0, dpi(46), 0, dpi(12), dpi(8), 0);
        clp.weight = 1f;
        btns.addView(copy, clp);

        TextView close = chip("关闭");
        close.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                d.dismiss();
            }
        });
        LinearLayout.LayoutParams xlp = margins(0, dpi(46), 0, dpi(12), 0, 0);
        xlp.weight = 1f;
        btns.addView(close, xlp);
        box.addView(btns);

        d.setContentView(box);
        Window dw = d.getWindow();
        if (dw != null) {
            dw.setBackgroundDrawable(new ColorDrawable(0));   // 背景透明，圆角才看得出来
            dw.addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND);
            dw.setDimAmount(0.62f);
            dw.setLayout(MATCH, WRAP);
        }
        d.show();
        refreshBody.run();      // 立刻出一版，别让用户对着空白等 1.5 秒
    }

    /** 复制到剪贴板并提示。任何异常都只提示、不崩（某些 ROM 在受限状态下会抛）。 */
    private void copyToClipboard(String label, String value) {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            if (cm == null) {
                toast("这台设备没有剪贴板服务");
                return;
            }
            cm.setPrimaryClip(ClipData.newPlainText(label, value == null ? "" : value));
            toast("已复制");
        } catch (Throwable t) {
            toast("复制失败：" + t);
        }
    }

    private void askNotificationPermission() {
        // 安卓 13（TIRAMISU）起通知也要运行时授权；12 及以下不用问。
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            return;
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }
    }

    /**
     * 读系统媒体音量（<b>只读</b>）。
     *
     * <p>这里原来叫 {@code applySystemVolume}，会调用 {@code AudioManager.setStreamVolume}
     * 真的去改系统音量 —— 那是错的（用户明确指出过）：我们要调的是"我们这一层"的增益，
     * 不该顺手把用户手机自己的声音也改了。现在这个函数只读不写，读出来是为了把
     * "系统音量 × 播放器音量 × 我们这一层"这三层关系摆在界面上。
     */
    private String readSystemVolume() {
        if (audioManager == null || sysVolMax <= 0) {
            return "系统媒体音量：读不到（没有音频服务）";
        }
        try {
            int cur = audioManager.getStreamVolume(AudioManager.STREAM_MUSIC);
            return "系统媒体音量 " + cur + " / " + sysVolMax + "（只显示，我们不改它）";
        } catch (Throwable t) {
            return "系统媒体音量：读不到（" + t + "）";
        }
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    // ────────────────────────────── 界面搭建 ──────────────────────────────

    private View buildUi() {
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(BG);
        scroll.setFillViewport(true);
        scroll.setVerticalScrollBarEnabled(false);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackground(new PageBg());
        int pad = dpi(18);
        root.setPadding(pad, dpi(20), pad, dpi(28));
        scroll.addView(root, new ViewGroup.LayoutParams(MATCH, WRAP));

        root.addView(buildHeader());
        root.addView(buildModeBar());
        // ★ 工具栏（🩺 诊断 / 🚀 快速上手 / ✏️ 改名）放在三档切换下面、三档都看得见 ——
        //   用户明确要求过"不要藏在某一档里"。
        root.addView(buildToolsCard());
        // ★ 「听广播 / 广播我的声音」放在设备列表【上面】：用户现在最常见的情况就是列表里 0 台设备
        //   （电脑在广播、手机看不见它），那时候列表里没有任何东西可点，
        //   而这个开关恰恰是"电脑在广播，我要能听见"的唯一入口 —— 放进列表里等于藏起来。
        channelCard = buildChannelCard();
        root.addView(channelCard);

        listBox = new LinearLayout(this);
        listBox.setOrientation(LinearLayout.VERTICAL);
        root.addView(listBox, margins(MATCH, WRAP, 0, dpi(16), 0, 0));

        // 监控档：只读的实时数字面板（其他两档隐藏）
        monitorCard = buildMonitorCard();
        root.addView(monitorCard);

        controlCard = buildControlCard();
        root.addView(controlCard);
        hintCard = buildHintCard();
        root.addView(hintCard);
        applyMode();
        return scroll;
    }

    /** ★ 顶部三档切换：简洁 / 详细 / 监控（分段控件，和「送什么声音」同一套视觉）。 */
    private View buildModeBar() {
        LinearLayout card = card();
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        modeSimple = chip("简洁");
        modeSimple.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                setViewMode(MODE_SIMPLE);
            }
        });
        LinearLayout.LayoutParams p1 = margins(0, dpi(40), 0, 0, dpi(6), 0);
        p1.weight = 1f;
        row.addView(modeSimple, p1);

        modeDetail = chip("详细");
        modeDetail.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                setViewMode(MODE_DETAIL);
            }
        });
        LinearLayout.LayoutParams p2 = margins(0, dpi(40), 0, 0, dpi(6), 0);
        p2.weight = 1f;
        row.addView(modeDetail, p2);

        modeMonitor = chip("监控");
        modeMonitor.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                setViewMode(MODE_MONITOR);
            }
        });
        LinearLayout.LayoutParams p3 = margins(0, dpi(40), 0, 0, 0, 0);
        p3.weight = 1f;
        row.addView(modeMonitor, p3);
        card.addView(row);

        // 一键展开/收起长说明（简洁档用；详细档不需要，监控档没有这些块）
        helpChip = chip("❔ 展开说明");
        helpChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                helpExpanded = !helpExpanded;
                applyMode();
            }
        });
        card.addView(helpChip, margins(MATCH, dpi(36), 0, dpi(8), 0, 0));
        return card;
    }

    /** ★ 工具栏：三档都常驻（用户要求"不要藏在某一档里"）。 */
    private View buildToolsCard() {
        LinearLayout card = card();
        LinearLayout tools = new LinearLayout(this);
        tools.setOrientation(LinearLayout.HORIZONTAL);
        tools.setGravity(Gravity.CENTER_VERTICAL);

        // ★ 「诊断」入口：用户原话"要不加个诊断吧"——点开就是一段可复制的完整报告。
        TextView diagChip = chip("🩺 诊断");
        diagChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showDiagnosisDialog();
            }
        });
        LinearLayout.LayoutParams dlp = margins(0, dpi(42), 0, 0, dpi(6), 0);
        dlp.weight = 1f;
        tools.addView(diagChip, dlp);

        // ★ 「快速上手」：把"该点哪里"写成 30 秒能读完的步骤（三档都进得来）
        TextView qsChip = chip("🚀 快速上手");
        qsChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showQuickStartDialog();
            }
        });
        LinearLayout.LayoutParams qlp = margins(0, dpi(42), 0, 0, dpi(6), 0);
        qlp.weight = 1f;
        tools.addView(qsChip, qlp);

        // ★ 「✏️ 改名」：点标题下那行名字也能改，这里再给一个显式入口
        TextView renameChip = chip("✏️ 改名");
        renameChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showRenameDialog();
            }
        });
        LinearLayout.LayoutParams rlp = margins(0, dpi(42), 0, 0, 0, 0);
        rlp.weight = 1f;
        tools.addView(renameChip, rlp);
        card.addView(tools);
        return card;
    }

    /** ★ 监控档：一屏读完的实时数字面板（全部来自 Engine 已有的查询接口，不另算一套）。 */
    private View buildMonitorCard() {
        LinearLayout card = card();
        card.addView(text("📈 监控（只读）", 12.5f, DIM, false));
        monitorText = text("", 11f, FG, false);
        monitorText.setTypeface(Typeface.MONOSPACE);
        monitorText.setLineSpacing(dp(2), 1f);
        monitorText.setTextIsSelectable(true);
        card.addView(monitorText, margins(MATCH, WRAP, 0, dpi(8), 0, 0));
        TextView note = text("这一档只看数字；要操作（收听 / 送声音 / 广播）点上面的「简洁」或「详细」。",
                11f, DIM, false);
        note.setLineSpacing(dp(2), 1f);
        card.addView(note, margins(MATCH, WRAP, 0, dpi(8), 0, 0));
        return card;
    }

    /**
     * ★ 「频道」卡片：一对开关 —— 📻 听广播（我收谁的）+ 📢 广播我的声音（我发给谁）。
     *
     * <p>为什么必须是独立开关：我们设计里广播和点对点是两条通道。
     * 电脑开「广播我的声音」时声音发给<b>所有在线节点</b>，而手机原来只播"用户点过🎧收听的那几台"，
     * 于是电脑广播过来的包全被按名单过滤掉 —— 真机现场就是这样，一个包都没播。
     * 反过来，手机原来也<b>没有</b>"把我的声音广播出去"（用户原话：「没有发广播」），这一对现在补齐。
     */
    private View buildChannelCard() {
        LinearLayout card = card();

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        channelChip = chip("📻 听广播");
        channelChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                boolean on = !engine.isChannelListening();
                engine.setChannelListen(on);
                toast(on ? "已打开听广播：谁在广播就听谁" : "已关闭听广播");
                refreshNow();
            }
        });
        LinearLayout.LayoutParams clp = margins(0, dpi(44), 0, 0, 0, 0);
        clp.weight = 1f;
        row.addView(channelChip, clp);

        // ★ 「📢 广播我的声音」：用户原话「没有发广播」。
        //   做法：对发现列表里每个在线节点各建一条单播发送（一次采集、一包编码、发 N 份）。
        broadcastChip = chip("📢 广播我的声音");
        broadcastChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                boolean on = !engine.isBroadcasting();
                engine.setBroadcasting(on);
                toast(on ? "已开始广播：我的声音送给每一台在线设备" : "已停止广播");
                refreshNow();
            }
        });
        LinearLayout.LayoutParams blp = margins(0, dpi(44), dpi(8), 0, 0, 0);
        blp.weight = 1f;
        row.addView(broadcastChip, blp);
        card.addView(row);

        channelNow = text("", 12f, DIM, false);
        channelNow.setLineSpacing(dp(3), 1f);
        card.addView(channelNow, margins(MATCH, WRAP, 0, dpi(10), 0, 0));

        // ★ ① 「请求收听」的横幅：别人点「送给它」时我这边自动打开了收听，
        //   界面上必须明说 + 可撤销（点一下收起；那台的「收听」按钮照常能关）。
        autoListenLine = text("", 11.5f, OK, false);
        autoListenLine.setLineSpacing(dp(3), 1f);
        autoListenLine.setVisibility(View.GONE);
        autoListenLine.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                engine.clearAutoListenNotice();     // 点一下先收起这句话
                refreshNow();
            }
        });
        card.addView(autoListenLine, margins(MATCH, WRAP, 0, dpi(8), 0, 0));

        // ★ ④ 安全检查的结果：「已停止向 XXX 发送 —— 它已经 18 秒没有消息了…」
        suspendedLine = text("", 11.5f, WARN, false);
        suspendedLine.setLineSpacing(dp(3), 1f);
        suspendedLine.setVisibility(View.GONE);
        card.addView(suspendedLine, margins(MATCH, WRAP, 0, dpi(8), 0, 0));

        // ★ 「自动接受别人的收听请求」全局开关（默认开，用户可关）
        LinearLayout autoRow = new LinearLayout(this);
        autoRow.setOrientation(LinearLayout.HORIZONTAL);
        autoRow.setGravity(Gravity.CENTER_VERTICAL);
        autoAcceptChip = chip("自动接受收听请求");
        autoAcceptChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                boolean on = !engine.isAutoAcceptListen();
                engine.setAutoAcceptListen(on);
                toast(on ? "已打开：别人点「送给它」时我会自动打开收听"
                        : "已关闭：别人点「送给它」时我要自己点收听");
                refreshNow();
            }
        });
        LinearLayout.LayoutParams alp = margins(0, dpi(38), 0, 0, dpi(8), 0);
        alp.weight = 1f;
        autoRow.addView(autoAcceptChip, alp);
        autoAcceptHint = text("", 10.5f, DIM, false);
        autoAcceptHint.setLineSpacing(dp(2), 1f);
        LinearLayout.LayoutParams hlp = margins(0, WRAP, 0, 0, 0, 0);
        hlp.weight = 1.1f;
        autoRow.addView(autoAcceptHint, hlp);
        card.addView(autoRow, margins(MATCH, WRAP, 0, dpi(6), 0, 0));

        // 发送侧的状态（含"送给几台 + 真实带宽"）
        broadcastNow = text("", 12f, DIM, false);
        broadcastNow.setLineSpacing(dp(3), 1f);
        broadcastNow.setVisibility(View.GONE);
        card.addView(broadcastNow, margins(MATCH, WRAP, 0, dpi(8), 0, 0));

        TextView hint = text("📻 听广播 = 「谁在广播就听谁」：电脑上开着「广播我的声音」时选这个就能听见，"
                + "不用先在这里点某台设备。\n"
                + "📢 广播我的声音 = 反过来：我这台的声音同时送给局域网里每一台在线设备"
                + "（逐个单播，不往广播地址盲发 —— 盲发在 Wi-Fi 上不可靠）。\n"
                + "🎧 收听 / 🔊 送声音 = 点对点，只跟你点名的那一台来往。\n"
                + "这些开关是【并存】的：可以只开一个，也可以都开。上面两行写的就是现在实际在收/在发什么。\n"
                + "同一台设备的声音如果从两条路都进来（电脑同时开着广播和点对点路由），只会播一路、不叠加。",
                11.5f, DIM, false);
        hint.setLineSpacing(dp(3), 1f);
        longTextViews.add(hint);      // 简洁档收起来
        card.addView(hint, margins(MATCH, WRAP, 0, dpi(10), 0, 0));
        return card;
    }

    private View buildHeader() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);

        row.addView(new LogoView(this), new LinearLayout.LayoutParams(dpi(38), dpi(38)));

        LinearLayout titles = new LinearLayout(this);
        titles.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams tlp = margins(0, WRAP, dpi(11), 0, 0, 0);
        tlp.weight = 1f;
        row.addView(titles, tlp);

        titles.addView(text("Audio Mixing", 16f, FG, true));
        TextView sub = text(engine.localName() + " ✏️ · " + engine.platform(), 12f, DIM, false);
        sub.setSingleLine(true);
        // ★ 点名字就能改（用户原话：「本机id最好支持自己编辑，不排除多人用同型号设备」）
        sub.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showRenameDialog();
            }
        });
        subLine = sub;
        titles.addView(sub);

        pill = text("搜索中…", 11.5f, DIM, false);
        pill.setPadding(dpi(10), dpi(5), dpi(10), dpi(5));
        stylePill(DIM);
        row.addView(pill);
        return row;
    }

    private View buildControlCard() {
        LinearLayout card = card();

        // ★ 『送什么声音』—— 这一块必须放在最显眼的地方。
        //   用户的原话：「你这手机都是采集的麦克风的声音，那不成对讲机了吗？
        //   我的需求一直都是桌面音频啊」—— 音源选错是这个 App 最大的方向性缺陷，
        //   所以选择、当前实际音源、限制说明三样都摆在第一屏。
        card.addView(text("送什么声音", 12.5f, DIM, false));

        LinearLayout srcRow = new LinearLayout(this);
        srcRow.setOrientation(LinearLayout.HORIZONTAL);

        playChip = chip("🔊 本机声音");
        playChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onSourceClick(AudioSend.SRC_PLAYBACK);
            }
        });
        LinearLayout.LayoutParams plp = margins(0, dpi(44), 0, 0, 0, 0);
        plp.weight = 1f;
        srcRow.addView(playChip, plp);

        micChip = chip("🎤 麦克风");
        micChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onSourceClick(AudioSend.SRC_MIC);
            }
        });
        LinearLayout.LayoutParams mlp = margins(0, dpi(44), dpi(8), 0, 0, 0);
        mlp.weight = 1f;
        srcRow.addView(micChip, mlp);
        card.addView(srcRow, margins(MATCH, WRAP, 0, dpi(8), 0, 0));

        // 这一行是"现在到底送的是什么"的唯一权威说法（绿=正常，黄=和你要的不一样）
        sourceNow = text("", 12f, FG, false);
        sourceNow.setLineSpacing(dp(3), 1f);
        card.addView(sourceNow, margins(MATCH, WRAP, 0, dpi(10), 0, 0));

        // 采本机声音却一直是静音时的提醒（DRM / 禁止被采集的 App 就是这样）
        sourceWarn = text("", 11.5f, WARN, false);
        sourceWarn.setLineSpacing(dp(3), 1f);
        sourceWarn.setVisibility(View.GONE);
        card.addView(sourceWarn, margins(MATCH, WRAP, 0, dpi(8), 0, 0));

        // 限制一次写清：别让用户以为"选上了就一定能采到"
        TextView limit = text("🔊 本机声音 = 手机上正在放的音乐/视频/游戏（第一次要授权一次，之后一直用；"
                + "安卓 10 以下没有这个能力，只能用麦克风）。\n"
                + "注意：DRM 保护的内容（部分音乐/影视 App）和主动禁止被采集的 App 采不到，会是静音；"
                + "少数厂商 ROM 也阉割了这个接口。\n"
                + "🎤 麦克风 = 对讲：说给对面听。两种都走同一条音频链路，音质一样。", 11.5f, DIM, false);
        limit.setLineSpacing(dp(3), 1f);
        card.addView(limit, margins(MATCH, WRAP, 0, dpi(10), 0, dpi(4)));
        longTextViews.add(limit);     // 简洁档收起来（点「❔ 展开说明」还能看）

        View hr = new View(this);
        hr.setBackgroundColor(LINE);
        card.addView(hr, margins(MATCH, Math.max(1, dpi(1)), 0, dpi(4), 0, dpi(12)));

        // ★ 音量：只管"我们这一层"，绝不动系统音量。
        //
        //   用户原话（2026-10-12）：「手机在调节本机音量时（本意应当是使收听方音量更适合），
        //   会直接调节系统声音大小。我们的声音计算方式应当是：
        //   系统声音 × 被监听软件声音 × 我们软件声音 = 实际接收端声音大小」。
        //   所以这里：滑杆 → 只作用于我们自己处理的 PCM；系统音量单独一行【只读】。
        card.addView(text("我们这一层的音量", 12.5f, DIM, false));

        LinearLayout volRow = new LinearLayout(this);
        volRow.setOrientation(LinearLayout.HORIZONTAL);
        volRow.setGravity(Gravity.CENTER_VERTICAL);

        volumeBar = new SeekBar(this);
        volumeBar.setMax(150);          // 0~1.5 倍，和网页版滑杆一致
        volumeBar.setProgress(100);
        volumeBar.setProgressTintList(ColorStateList.valueOf(ACC));
        volumeBar.setThumbTintList(ColorStateList.valueOf(ACC));
        LinearLayout.LayoutParams vlp = margins(0, WRAP, 0, 0, 0, 0);
        vlp.weight = 1f;
        volRow.addView(volumeBar, vlp);

        volumeVal = text("1.00", 12.5f, DIM, false);
        volumeVal.setGravity(Gravity.END);
        volumeVal.setTypeface(Typeface.MONOSPACE);
        volRow.addView(volumeVal, new LinearLayout.LayoutParams(dpi(44), WRAP));

        volumeBar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar sb, int progress, boolean fromUser) {
                float g = progress / 100f;
                if (volumeVal != null) {
                    setTextIfChanged(volumeVal, String.format(Locale.US, "%.2f", g));
                }
                // 只改我们自己的增益（发侧乘 PCM / 收侧 AudioTrack 音量）。
                // 注意：这里<b>刻意不再调用</b> AudioManager.setStreamVolume —— 见上面那段说明。
                engine.setVolume(g);
            }

            @Override
            public void onStartTrackingTouch(SeekBar sb) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar sb) {
            }
        });
        card.addView(volRow, margins(MATCH, WRAP, 0, dpi(6), 0, 0));

        TextView volHint = text("只影响 Audio Mixing 送出去/播放出来的声音，不改你的系统音量。"
                + "（播放侧上限 1.00，发送侧可以到 1.50。）", 11.5f, DIM, false);
        volHint.setLineSpacing(dp(3), 1f);
        card.addView(volHint, margins(MATCH, WRAP, 0, dpi(4), 0, 0));

        // 系统音量：只显示、不修改。写在界面上是为了让三层关系一眼可见。
        sysVolText = text("", 11.5f, DIM, false);
        sysVolText.setLineSpacing(dp(3), 1f);
        card.addView(sysVolText, margins(MATCH, WRAP, 0, dpi(2), 0, dpi(6)));

        // 「详情」和「诊断」并排：详情是常驻的实时统计（看一眼），
        // 诊断是一次性的完整快照（能整段复制发出去）—— 真机排查要的正是后者。
        LinearLayout tools = new LinearLayout(this);
        tools.setOrientation(LinearLayout.HORIZONTAL);
        tools.setGravity(Gravity.CENTER_VERTICAL);

        detailToggle = text("▸ 详情", 12.5f, DIM, false);
        detailToggle.setGravity(Gravity.CENTER_VERTICAL);
        detailToggle.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                toggleDetails();
            }
        });
        LinearLayout.LayoutParams tlp = margins(0, dpi(40), 0, 0, 0, 0);
        tlp.weight = 1f;
        tools.addView(detailToggle, tlp);

        // ★ 「诊断」入口：用户原话"要不加个诊断吧"——点开就是一段可复制的完整报告，
        //   不用再靠截图里的几个数字猜是哪一端的问题。
        TextView diagChip = chip("🩺 诊断");
        diagChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showDiagnosisDialog();
            }
        });
        tools.addView(diagChip, margins(dpi(100), dpi(40), 0, dpi(12), 0, 0));

        // ★ 「✏️ 改名」：和「诊断」并排（点标题下那行名字也能改，这里再给一个显式入口）
        TextView renameChip = chip("✏️ 改名");
        renameChip.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                showRenameDialog();
            }
        });
        tools.addView(renameChip, margins(dpi(88), dpi(40), dpi(8), dpi(12), 0, 0));
        card.addView(tools);

        detailWrap = new LinearLayout(this);
        detailWrap.setOrientation(LinearLayout.VERTICAL);
        detailWrap.setVisibility(View.GONE);
        card.addView(detailWrap);

        kv("本机名称", "name");
        kv("本机 IP", "ip");
        kv("音频端口", "port");
        kv("已发现", "found");
        kv("已连接", "conn");
        kv("实际收到", "streams");
        kv("音量分层", "volume");
        kv("收端统计", "recv");
        kv("发端统计", "send");
        // ★ 对端自己报的统计（两台设备互报的"我发了多少/我收了你多少"）
        kv("对端回话", "peers");
        kv("发现统计", "disc");
        return card;
    }

    private View buildHintCard() {
        LinearLayout card = card();
        TextView t = text("同一 Wi-Fi 下打开这个 App，设备会自己出现 —— 不用配对、不用房间号、不用扫码。\n"
                + "📻 听广播 = 谁在广播就听谁（电脑上开着「广播我的声音」时用这个）；\n"
                + "🎧 收听 = 点对点，只收你点名的那一台（可多台混音）。\n"
                + "🔊 送声音 = 把【本机正在放的声音】送给它（音乐/视频/游戏）—— 这才是这个 App 的用法；\n"
                + "🎤 换成麦克风就是对讲（说给对面听）。\n"
                + "手机之间直连，关掉电脑照常工作。", 12f, DIM, false);
        t.setLineSpacing(dp(4), 1f);
        card.addView(t);
        return card;
    }

    /**
     * 一条「标签 + 电平条 + 说明」的行（设备卡片上的「收」/「送」）。
     *
     * <p>为什么要带文字标签：用户原话「每个设备都应当配备独立的收/送声音音频条」——
     * 光有两根条子还是分不清哪根是哪根，所以每条前面写「收」/「送」，后面写一句人话。
     */
    private LinearLayout levelRow(String tag, LevelBar bar, String tail) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);

        TextView k = text(tag, 11.5f, DIM, true);
        row.addView(k, new LinearLayout.LayoutParams(dpi(18), WRAP));

        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(0, dpi(10));
        blp.weight = 1f;
        row.addView(bar, blp);

        TextView t = text(tail, 10.5f, DIM, false);
        row.addView(t, margins(WRAP, WRAP, dpi(8), 0, 0, 0));
        return row;
    }

    /**
     * 一条「标签 + 可调滑杆 + 数值」的行（设备卡片上的「我听它的」/「我送它的」）。
     *
     * <p>为什么和电平条分开两条：用户原话「我要求的是各设备<b>可调节</b>的音量条。
     * 当然现在有的显示实际音量条也不错」—— 电平条看实时、滑杆动手调，两件事不合并。
     *
     * @param recv true = 收侧增益（只乘这一路的样本）；false = 送侧增益（只缩放发给这一台的包）
     */
    private LinearLayout gainRow(final Discovery.Peer peer, String tag, final SeekBar bar,
                                 final boolean recv) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);

        TextView k = text(tag, 11f, DIM, false);
        row.addView(k, new LinearLayout.LayoutParams(dpi(52), WRAP));

        bar.setMax(150);              // 0.00 ~ 1.50，和「我们这一层的音量」滑杆同一口径
        bar.setProgress(100);
        bar.setProgressTintList(ColorStateList.valueOf(ACC));
        bar.setThumbTintList(ColorStateList.valueOf(ACC));
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(0, WRAP);
        blp.weight = 1f;
        row.addView(bar, blp);

        final TextView val = text("1.00", 11f, DIM, false);
        val.setGravity(Gravity.END);
        val.setTypeface(Typeface.MONOSPACE);
        val.setTag("gainVal");                 // 只是标识：这行显示的是数值
        row.addView(val, new LinearLayout.LayoutParams(dpi(38), WRAP));
        bar.setTag(val);                       // 监听器里要更新它

        bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar sb, int progress, boolean fromUser) {
                float g = progress / 100f;
                Object t = sb.getTag();
                if (t instanceof TextView) {
                    setTextIfChanged((TextView) t, String.format(Locale.US, "%.2f", g));
                }
                if (fromUser && peer != null && peer.host != null && !peer.host.isEmpty()) {
                    // 立刻生效：收侧改这一路的样本增益，送侧改发给这一台的包
                    if (recv) {
                        engine.setPeerRecvGain(peer.host, g);
                    } else {
                        engine.setPeerSendGain(peer.host, g);
                    }
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar sb) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar sb) {
            }
        });
        return row;
    }

    /** 「送声音」按钮上的字：图标直接表达当前音源（🔊 本机声音 / 🎤 麦克风）。 */
    private static String sendChipLabel(int kind) {
        return kind == AudioSend.SRC_PLAYBACK ? "🔊 送声音" : "🎤 送声音";
    }

    private View buildEmptyCard() {
        LinearLayout card = card();
        TextView t = text("正在搜索局域网里的设备…", 14.5f, FG, true);
        t.setGravity(Gravity.CENTER);
        card.addView(t);

        ProgressBar pb = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        pb.setIndeterminate(true);
        pb.setIndeterminateTintList(ColorStateList.valueOf(ACC));
        card.addView(pb, margins(MATCH, dpi(4), 0, dpi(14), 0, 0));

        TextView h = text("让另一台手机 / 电脑打开 Audio Mixing，它就会自动出现在这里。", 12f, DIM, false);
        h.setGravity(Gravity.CENTER);
        card.addView(h, margins(MATCH, WRAP, 0, dpi(12), 0, 0));

        // ★ 列表空着不等于没得听：电脑在广播时「听广播」照样能出声。
        //   真机现场最常见的就是"列表 0 台设备"，这时候必须给用户一条能走的路。
        TextView h2 = text("电脑上开着「广播我的声音」？直接点上面的「📻 听广播」就能听见 —— "
                + "不用等它出现在这个列表里。", 12f, ACC, false);
        h2.setGravity(Gravity.CENTER);
        card.addView(h2, margins(MATCH, WRAP, 0, dpi(10), 0, 0));

        TextView e = text("本机 " + engine.nodeName() + " · " + engine.localIpsText(),
                11.5f, DIM, false);
        e.setGravity(Gravity.CENTER);
        card.addView(e, margins(MATCH, WRAP, 0, dpi(6), 0, 0));

        // 一张小抄：列表空着时按这个顺序查（和诊断报告里的【发现自检】对得上）
        TextView tip = text("一直搜不到设备？点「详情」看「发现统计」里的自检："
                + "如果写着\"从没收到过自己的信标\"，那是本机/路由器的组播问题（换 Wi-Fi、关掉 AP 隔离）。",
                11.5f, DIM, false);
        tip.setGravity(Gravity.CENTER);
        tip.setLineSpacing(dp(3), 1f);
        card.addView(tip, margins(MATCH, WRAP, 0, dpi(10), 0, 0));
        return card;
    }

    private void kv(String label, String key) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        TextView k = text(label, 12f, DIM, false);
        k.setTypeface(Typeface.MONOSPACE);
        row.addView(k, new LinearLayout.LayoutParams(dpi(70), WRAP));
        TextView v = text("—", 12f, FG, false);
        v.setTypeface(Typeface.MONOSPACE);
        LinearLayout.LayoutParams vlp = margins(0, WRAP, 0, 0, 0, 0);
        vlp.weight = 1f;
        row.addView(v, vlp);
        kvValues.put(key, v);
        detailWrap.addView(row, margins(MATCH, WRAP, 0, dpi(10), 0, 0));
    }

    // ────────────────────────────── 小工具 ──────────────────────────────

    /** 一张深色圆角卡片：rgba(255,255,255,.045) 底 + 1px 白边，对齐网页版 .card。 */
    private LinearLayout card() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setBackground(roundRect(CARD_BG, dp(16), LINE, Math.max(1, dpi(1))));
        int p = dpi(14);
        l.setPadding(p, p, p, p);
        l.setLayoutParams(margins(MATCH, WRAP, 0, dpi(10), 0, 0));
        return l;
    }

    private TextView text(String s, float sizeSp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp);
        t.setTextColor(color);
        t.setTypeface(Typeface.create("sans-serif-medium",
                bold ? Typeface.BOLD : Typeface.NORMAL));
        return t;
    }

    private static void setTextIfChanged(TextView t, String s) {
        String cur = (t.getText() == null) ? "" : t.getText().toString();
        String next = (s == null) ? "" : s;
        if (!next.equals(cur)) {
            t.setText(next);
        }
    }

    private void stylePill(int color) {
        int stroke = (color & 0x00FFFFFF) | 0x59000000;   // 同色 35% 透明描边
        pill.setBackground(roundRect(0x0AFFFFFF, dp(999), stroke, Math.max(1, dpi(1))));
        pill.setTextColor(color);
    }

    /** 开关按钮：关 = 透明底 + 细描边；开 = 蓝紫渐变实底（对齐网页版 .seg button.sel）。 */
    private void styleChip(TextView v, boolean on, String label) {
        GradientDrawable d = new GradientDrawable();
        d.setCornerRadius(dp(11));
        if (on) {
            d.setOrientation(GradientDrawable.Orientation.LEFT_RIGHT);
            d.setColors(new int[]{ACC, ACC2});
            v.setTextColor(ON_TEXT);
        } else {
            d.setColor(0x12FFFFFF);
            d.setStroke(Math.max(1, dpi(1)), LINE);
            v.setTextColor(DIM);
        }
        v.setBackground(new RippleDrawable(ColorStateList.valueOf(0x33FFFFFF), d, null));
        setTextIfChanged(v, label);
    }

    /** radius / strokeWidth 都按像素算（GradientDrawable 的单位就是 px），调用处用 dp() 换算。 */
    private static GradientDrawable roundRect(int fill, float radiusPx, int stroke, int strokePx) {
        GradientDrawable d = new GradientDrawable();
        d.setShape(GradientDrawable.RECTANGLE);
        d.setColor(fill);
        d.setCornerRadius(radiusPx);
        if (strokePx > 0) {
            d.setStroke(strokePx, stroke);
        }
        return d;
    }

    private LinearLayout.LayoutParams margins(int w, int h, int l, int t, int r, int b) {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(w, h);
        p.leftMargin = l;
        p.topMargin = t;
        p.rightMargin = r;
        p.bottomMargin = b;
        return p;
    }

    private float dp(float v) {
        return TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private int dpi(float v) {
        return Math.round(dp(v));
    }

    // ────────────────────────────── 一行设备 ──────────────────────────────

    private final class PeerRow {
        final Discovery.Peer peer;
        final LinearLayout card;
        final TextView name;
        final TextView meta;
        final TextView state;
        final TextView listenChip;
        final TextView sendChip;
        /** 「收」电平条：我从它收到多少。 */
        final LevelBar recvLevel;
        /** 「送」电平条：我送给它多少（逐目标统计，见 SendFanout）。 */
        final LevelBar sendLevel;
        /** ★ 「我听它的」可调滑杆（收侧增益，只影响这一路）。 */
        final SeekBar recvGainBar;
        /** ★ 「我送它的」可调滑杆（送侧逐目标增益）。 */
        final SeekBar sendGainBar;
        /** ★ 「对方：…」那一行（两台设备互报的统计）。 */
        final TextView peerLine;
        private boolean styledOnce;
        private boolean styledListen;
        private boolean styledSend;
        private int styledSrc = -1;

        PeerRow(Discovery.Peer p) {
            this.peer = p;
            card = card();

            LinearLayout head = new LinearLayout(MainActivity.this);
            head.setOrientation(LinearLayout.HORIZONTAL);
            head.setGravity(Gravity.CENTER_VERTICAL);

            LinearLayout col = new LinearLayout(MainActivity.this);
            col.setOrientation(LinearLayout.VERTICAL);
            LinearLayout.LayoutParams clp = margins(0, WRAP, 0, 0, 0, 0);
            clp.weight = 1f;
            head.addView(col, clp);

            name = text(p.label(), 15.5f, FG, true);
            name.setSingleLine(true);
            col.addView(name);
            meta = text(metaText(p), 12f, DIM, false);
            meta.setSingleLine(true);
            col.addView(meta);

            state = text("", 12f, OK, true);
            head.addView(state, margins(WRAP, WRAP, dpi(8), 0, 0, 0));
            card.addView(head);

            // ★ 两条独立电平条（收 / 送）—— 用户原话：
            //   「每个设备都应当配备独立的收/送声音音频条」。原来只有一条，看不出是收还是送。
            recvLevel = new LevelBar(MainActivity.this, Math.max(2, dpi(3)));
            card.addView(levelRow("收", recvLevel, "我从它收到多少"),
                    margins(MATCH, WRAP, 0, dpi(11), 0, 0));
            sendLevel = new LevelBar(MainActivity.this, Math.max(2, dpi(3)));
            card.addView(levelRow("送", sendLevel, "我送给它多少"),
                    margins(MATCH, WRAP, 0, dpi(5), 0, 0));

            // ★★ 可调音量条（用户原话：「我要求的是各设备【可调节】的音量条。
            //    当然现在有的显示实际音量条也不错」）—— 电平条保留，旁边再加一条可调滑杆。
            //    两条滑杆控制的都是【我们这一层】的增益：
            //      · 我听它的 → 只乘这一路的样本（AudioRecv.Stream.gain）
            //      · 我送它的 → 只缩放发给这一台的那份包（SendFanout.Target.gain）
            //    都不碰手机系统音量。
            recvGainBar = new SeekBar(MainActivity.this);
            card.addView(gainRow(p, "我听它的", recvGainBar, true),
                    margins(MATCH, WRAP, 0, dpi(9), 0, 0));
            sendGainBar = new SeekBar(MainActivity.this);
            card.addView(gainRow(p, "我送它的", sendGainBar, false),
                    margins(MATCH, WRAP, 0, dpi(4), 0, 0));

            // ★ 「对方」那一行：两台设备互报的结果。用户原话是"不知道是哪一端没在发"，
            //   这一行就是答案 —— 对方自己说它有没有在发、它收了我多少。
            //   放宽在电平条下面（而不是塞进右上角），因为这句话有时候要换行。
            peerLine = text("", 11.5f, DIM, false);
            peerLine.setLineSpacing(dp(3), 1f);
            peerLine.setVisibility(View.GONE);
            card.addView(peerLine, margins(MATCH, WRAP, 0, dpi(9), 0, 0));

            LinearLayout btns = new LinearLayout(MainActivity.this);
            btns.setOrientation(LinearLayout.HORIZONTAL);

            listenChip = chip("🎧 收听");
            listenChip.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    onListenClick(peer);
                }
            });
            LinearLayout.LayoutParams llp = margins(0, dpi(44), 0, 0, 0, 0);
            llp.weight = 1f;
            btns.addView(listenChip, llp);

            sendChip = chip(sendChipLabel(engine.sourceKind()));            sendChip.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    onSendClick(peer);
                }
            });
            LinearLayout.LayoutParams slp = margins(0, dpi(44), dpi(8), 0, 0, 0);
            slp.weight = 1f;
            btns.addView(sendChip, slp);

            card.addView(btns, margins(MATCH, WRAP, 0, dpi(12), 0, 0));
            sync();
        }

        void sync() {
            boolean listen = engine.isListening(peer);
            boolean send = engine.isSending(peer);
            // 按钮上的图标直接表达"送的是什么"：🔊 本机声音 / 🎤 麦克风。
            int src = engine.sourceKind();
            // 只在状态真的变了才重建背景（每 150ms 重来一次会一直产生垃圾对象）
            if (!styledOnce) {
                styledOnce = true;
                styledListen = listen;
                styledSend = send;
                styledSrc = src;
                styleChip(listenChip, listen, "🎧 收听");
                styleChip(sendChip, send, sendChipLabel(src));
            } else {
                if (listen != styledListen) {
                    styledListen = listen;
                    styleChip(listenChip, listen, "🎧 收听");
                }
                if (send != styledSend || src != styledSrc) {
                    styledSend = send;
                    styledSrc = src;
                    styleChip(sendChip, send, sendChipLabel(src));
                }
            }

            // ★ 两条电平条各画各的（用户要的"每个设备独立的收/送音频条"）：
            //   「收」= 我从它收到多少（对方那一路的收包电平，不是混音总值）；
            //   「送」= 我送给它多少（SendFanout 逐目标统计，这一路发不出去它会自己塌下去）。
            recvLevel.setLevel(listen ? engine.recvPeakFor(peer.host) : 0f);
            sendLevel.setLevel(send ? engine.sendPeakFor(peer.host) : 0f);

            // ★ 两条可调滑杆跟着引擎里的真实值走（对方重新上线、接收器重建之后不会"跳回 1.00"）；
            //   正在被用户拖的那条不动（否则会跟手指打架）。
            if (recvGainBar != null && !recvGainBar.isPressed()) {
                int want = Math.round(engine.peerRecvGain(peer.host) * 100);
                if (recvGainBar.getProgress() != want) {
                    recvGainBar.setProgress(want);
                }
            }
            if (sendGainBar != null && !sendGainBar.isPressed()) {
                int want = Math.round(engine.peerSendGain(peer.host) * 100);
                if (sendGainBar.getProgress() != want) {
                    sendGainBar.setProgress(want);
                }
            }

            String st;
            int color;
            // ★ 「实际生效状态」：点了「收听」不等于真的在收 —— 用户说的"路由模式和广播模式
            //   混淆"就出在这里。所以这里区分三种：真的在收 / 点了但没收到包 / 没在听。
            boolean reallyReceiving = listen && engine.recvReceivingFrom(peer.host);
            boolean broadcasting = engine.isBroadcasting();
            if (listen && send) {
                st = "收听✓ + 送声·" + AudioSend.sourceShortName(engine.activeSourceKind())
                        + (broadcasting ? "（广播）" : "");
                color = OK;
            } else if (listen) {
                st = reallyReceiving ? "收听中 ✓ 正在收" : "收听中…（还没收到包）";
                color = reallyReceiving ? OK : WARN;
            } else if (send) {
                // 送声时把音源也写在状态里：用户一眼就知道送出去的是本机声音还是他说话
                st = "送声·" + AudioSend.sourceShortName(engine.activeSourceKind())
                        + (broadcasting ? "（广播）" : "");
                color = WARN;
            } else if (peer.busy) {
                st = "对端忙";
                color = WARN;
            } else {
                st = "";
                color = DIM;
            }
            setTextIfChanged(state, st);
            state.setTextColor(color);

            setTextIfChanged(name, peer.label());
            setTextIfChanged(meta, metaText(peer));

            // ★ 「对方：已发 5938 包 / 11.0 MB · 正在发 ✓」/「没在发 ✗」那一行。
            //   只在有「收听/送声音」关系时显示：没关系时对方本来就不会回话，
            //   显示了反而让人以为"对方坏了"。
            //   ★ 三档差异：简洁档不显示（"打开就知道点哪里"），详细/监控档显示。
            if ((listen || send) && viewMode != MODE_SIMPLE) {
                long nowMs = SystemClock.elapsedRealtime();
                Diagnosis.PeerStats ps = engine.peerStats(peer.host);
                setTextIfChanged(peerLine, Diagnosis.cardLine(ps, nowMs));
                // 颜色也表达结论：绿=对面在发、黄=对面没在发、灰=还没回话/已过期。
                int pc = DIM;
                if (ps != null && ps.fresh(nowMs)) {
                    pc = ps.sending ? OK : WARN;
                }
                if (peerLine.getCurrentTextColor() != pc) {
                    peerLine.setTextColor(pc);
                }
                peerLine.setVisibility(View.VISIBLE);
            } else {
                peerLine.setVisibility(View.GONE);
            }
        }
    }

    private static String metaText(Discovery.Peer p) {
        StringBuilder sb = new StringBuilder();
        String plat = (p.platform == null || p.platform.isEmpty()) ? "未知平台" : p.platform;
        sb.append(plat);
        if (p.host != null && !p.host.isEmpty()) {
            sb.append(" · ").append(p.host);
        }
        if (p.webPort > 0) {
            sb.append(" · 有网页面板");
        }
        return sb.toString();
    }

    private TextView chip(String label) {
        TextView t = text(label, 14.5f, DIM, true);
        t.setGravity(Gravity.CENTER);
        t.setClickable(true);
        t.setFocusable(true);
        styleChip(t, false, label);
        return t;
    }

    // ────────────────────────────── 自绘小控件 ──────────────────────────────

    /** 电平条：22 段小方块，从青到绿，和网页版那排竖条一个观感。 */
    private static final class LevelBar extends View {
        private static final int SEG = 22;
        private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final int gap;
        private float shown;

        LevelBar(Context c, int gapPx) {
            super(c);
            gap = gapPx;
        }

        void setLevel(float v) {
            if (Float.isNaN(v) || v < 0f) {
                v = 0f;
            }
            if (v > 1f) {
                v = 1f;
            }
            // 快起慢落：真实电平表都是这个观感，否则 150ms 一刷会闪得看不清。
            shown = Math.max(v, shown * 0.72f);
            invalidate();
        }

        @Override
        protected void onDraw(Canvas cv) {
            float w = getWidth();
            float h = getHeight();
            if (w <= 0 || h <= 0) {
                return;
            }
            float segW = (w - gap * (SEG - 1)) / SEG;
            if (segW <= 0) {
                segW = w / SEG;
            }
            int lit = Math.round(shown * SEG);
            for (int i = 0; i < SEG; i++) {
                float left = i * (segW + gap);
                p.setColor(i < lit ? blend(ACC, OK, i / (float) (SEG - 1)) : TRACK);
                cv.drawRoundRect(left, 0, left + segW, h, h / 2f, h / 2f, p);
            }
        }

        private static int blend(int a, int b, float t) {
            int ar = (a >> 16) & 0xFF, ag = (a >> 8) & 0xFF, ab = a & 0xFF;
            int br = (b >> 16) & 0xFF, bg = (b >> 8) & 0xFF, bb = b & 0xFF;
            return 0xFF000000
                    | ((int) (ar + (br - ar) * t) << 16)
                    | ((int) (ag + (bg - ag) * t) << 8)
                    | (int) (ab + (bb - ab) * t);
        }
    }

    /** 左上角的小图标：渐变圆角方块 + 四根声波竖条（对齐网页版那个 svg）。 */
    private static final class LogoView extends View {
        private static final float[] HEIGHTS = {0.30f, 0.62f, 0.78f, 0.44f};
        private final Paint box = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint bar = new Paint(Paint.ANTI_ALIAS_FLAG);

        LogoView(Context c) {
            super(c);
            bar.setColor(ON_TEXT);
            bar.setStrokeCap(Paint.Cap.ROUND);
        }

        @Override
        protected void onDraw(Canvas cv) {
            float w = getWidth();
            float h = getHeight();
            if (w <= 0 || h <= 0) {
                return;
            }
            box.setShader(new LinearGradient(0, 0, w, h, ACC, ACC2, Shader.TileMode.CLAMP));
            cv.drawRoundRect(0, 0, w, h, w * 0.30f, w * 0.30f, box);
            bar.setStrokeWidth(Math.max(1f, w * 0.085f));
            float cy = h / 2f;
            float x0 = w * 0.27f;
            float step = (w * 0.46f) / (HEIGHTS.length - 1);
            for (int i = 0; i < HEIGHTS.length; i++) {
                float x = x0 + step * i;
                float half = h * HEIGHTS[i] / 2f;
                cv.drawLine(x, cy - half, x, cy + half, bar);
            }
        }
    }

    /** 页面底色：深蓝黑 + 顶部偏蓝、右下偏紫两团辉光（网页版的 CSS 渐变用 Canvas 重画）。 */
    private static final class PageBg extends Drawable {
        private final Paint p = new Paint(Paint.ANTI_ALIAS_FLAG);

        @Override
        public void draw(Canvas c) {
            int w = getBounds().width();
            int h = getBounds().height();
            if (w <= 0 || h <= 0) {
                return;
            }
            c.drawColor(BG);
            p.setShader(new RadialGradient(w * 0.5f, -h * 0.10f, Math.max(1f, w * 0.78f),
                    0xFF14324D, 0x0014324D, Shader.TileMode.CLAMP));
            c.drawRect(0, 0, w, h, p);
            p.setShader(new RadialGradient(w * 1.10f, h * 1.10f, Math.max(1f, w * 0.72f),
                    0xFF2A1B52, 0x002A1B52, Shader.TileMode.CLAMP));
            c.drawRect(0, 0, w, h, p);
            p.setShader(null);
        }

        @Override
        public void setAlpha(int alpha) {
        }

        @Override
        public void setColorFilter(ColorFilter cf) {
        }

        @Override
        public int getOpacity() {
            return PixelFormat.OPAQUE;
        }
    }
}
