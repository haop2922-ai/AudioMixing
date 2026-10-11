package com.audiomixing.app;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
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
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.media.AudioManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;
import android.widget.Toast;

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
 *   <li><b>🎤 送声音</b>：把我的麦克风送给这台（同一时刻只送一台）。</li>
 * </ul>
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

    private final Map<String, PeerRow> rows = new LinkedHashMap<String, PeerRow>();
    private final Map<String, TextView> kvValues = new LinkedHashMap<String, TextView>();

    /** null 表示还没建过列表，这样第一次刷新一定会把"搜索中"的空状态画出来。 */
    private String lastSig;
    private boolean detailsOpen;
    private boolean ticking;
    private int tickCount;
    private Discovery.Peer pendingSend;   // 等麦克风权限回来之后再开始送的那台

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
        if (ok && pendingSend != null) {
            engine.setSending(pendingSend, true);
            // 权限到手了，再叫一次服务把 microphone 这个前台类型补上（安卓 14 要求）。
            // 注意是"再叫一次"而不是 stop+start —— 后者会把刚开起来的送声一起掐掉。
            MixService.safeStart(this);
        } else if (!ok) {
            toast("没有麦克风权限，送不了声音");
        }
        pendingSend = null;
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
        updateHeader(peers.size());
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
        if (conn > 0) {
            setTextIfChanged(pill, "正在传输 " + conn + " 台");
            stylePill(OK);
        } else if (peerCount > 0) {
            setTextIfChanged(pill, peerCount + " 台在线");
            stylePill(ACC);
        } else {
            setTextIfChanged(pill, "搜索中…");
            stylePill(DIM);
        }
    }

    private void refreshDetails() {
        if (!detailsOpen) {
            return;
        }
        put("name", engine.nodeName() + " · " + engine.platform());
        put("ip", engine.localIpsText());
        put("port", engine.audioPort() + "（UDP，收发都用它）");
        put("found", engine.peers().size() + " 台");
        put("conn", "收听 " + engine.listeningCount() + " 台 / 送声 " + engine.sendingCount() + " 台");
        put("recv", engine.statsText());
        put("send", engine.sendStatsText());
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
        if (engine.isSending(p)) {
            engine.setSending(p, false);
            toast("已停止送声音");
            refreshNow();
            return;
        }
        // 麦克风权限：点「送声音」时才要，别一打开 App 就弹窗（用户会反感）。
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            pendingSend = p;
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            return;
        }
        engine.setSending(p, true);
        toast("正在把本机麦克风送给 " + p.label());
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

    private void applySystemVolume(int progress) {
        // 只有用户真的拖了滑杆才动系统音量，别一打开 App 就把人家的媒体音量改了。
        if (audioManager == null || sysVolMax <= 0) {
            return;
        }
        int idx = Math.round(Math.min(1f, progress / 100f) * sysVolMax);
        try {
            audioManager.setStreamVolume(AudioManager.STREAM_MUSIC, idx, 0);
        } catch (Throwable ignored) {
            // 某些 ROM 在勿扰模式下会拒绝，忽略即可
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

        listBox = new LinearLayout(this);
        listBox.setOrientation(LinearLayout.VERTICAL);
        root.addView(listBox, margins(MATCH, WRAP, 0, dpi(16), 0, 0));

        root.addView(buildControlCard());
        root.addView(buildHintCard());
        return scroll;
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
        TextView sub = text(engine.nodeName() + " · " + engine.platform(), 12f, DIM, false);
        sub.setSingleLine(true);
        titles.addView(sub);

        pill = text("搜索中…", 11.5f, DIM, false);
        pill.setPadding(dpi(10), dpi(5), dpi(10), dpi(5));
        stylePill(DIM);
        row.addView(pill);
        return row;
    }

    private View buildControlCard() {
        LinearLayout card = card();

        LinearLayout volRow = new LinearLayout(this);
        volRow.setOrientation(LinearLayout.HORIZONTAL);
        volRow.setGravity(Gravity.CENTER_VERTICAL);
        volRow.addView(text("本机音量", 12.5f, DIM, false),
                new LinearLayout.LayoutParams(dpi(62), WRAP));

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
                engine.setVolume(g);
                if (fromUser) {
                    applySystemVolume(progress);
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar sb) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar sb) {
            }
        });
        card.addView(volRow);

        detailToggle = text("▸ 详情", 12.5f, DIM, false);
        detailToggle.setPadding(0, dpi(14), 0, dpi(2));
        detailToggle.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                toggleDetails();
            }
        });
        card.addView(detailToggle);

        detailWrap = new LinearLayout(this);
        detailWrap.setOrientation(LinearLayout.VERTICAL);
        detailWrap.setVisibility(View.GONE);
        card.addView(detailWrap);

        kv("本机名称", "name");
        kv("本机 IP", "ip");
        kv("音频端口", "port");
        kv("已发现", "found");
        kv("已连接", "conn");
        kv("收端统计", "recv");
        kv("发端统计", "send");
        kv("发现统计", "disc");
        return card;
    }

    private View buildHintCard() {
        LinearLayout card = card();
        TextView t = text("同一 Wi-Fi 下打开这个 App，设备会自己出现 —— 不用配对、不用房间号、不用扫码。\n"
                + "🎧 收听 = 收它发来的声音（可多台混音）；🎤 送声音 = 把本机麦克风送给它。\n"
                + "手机之间直连，关掉电脑照常工作。", 12f, DIM, false);
        t.setLineSpacing(dp(4), 1f);
        card.addView(t);
        return card;
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

        TextView h = text("让另一台手机 / 电脑打开 Audio Mixing，它就会自动出现在这里。",
                12f, DIM, false);
        h.setGravity(Gravity.CENTER);
        card.addView(h, margins(MATCH, WRAP, 0, dpi(12), 0, 0));

        TextView e = text("本机 " + engine.nodeName() + " · " + engine.localIpsText(),
                11.5f, DIM, false);
        e.setGravity(Gravity.CENTER);
        card.addView(e, margins(MATCH, WRAP, 0, dpi(6), 0, 0));
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
        final LevelBar level;
        private boolean styledOnce;
        private boolean styledListen;
        private boolean styledSend;

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

            level = new LevelBar(MainActivity.this, Math.max(2, dpi(3)));
            card.addView(level, margins(MATCH, dpi(10), 0, dpi(11), 0, 0));

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

            sendChip = chip("🎤 送声音");
            sendChip.setOnClickListener(new View.OnClickListener() {
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
            // 只在状态真的变了才重建背景（每 150ms 重来一次会一直产生垃圾对象）
            if (!styledOnce) {
                styledOnce = true;
                styledListen = listen;
                styledSend = send;
                styleChip(listenChip, listen, "🎧 收听");
                styleChip(sendChip, send, "🎤 送声音");
            } else {
                if (listen != styledListen) {
                    styledListen = listen;
                    styleChip(listenChip, listen, "🎧 收听");
                }
                if (send != styledSend) {
                    styledSend = send;
                    styleChip(sendChip, send, "🎤 送声音");
                }
            }

            // 电平条画的是"这一路"的电平：送声看采集电平，收听看对方那一路的收包电平，
            // 而不是混音总电平 —— 否则多台一起听的时候每行都在跳同一个值，看不出谁在响。
            float lv;
            if (send) {
                lv = engine.sendPeak();
            } else if (listen) {
                lv = engine.recvPeakFor(peer.host);
            } else {
                lv = 0f;
            }
            level.setLevel(lv);

            String st;
            int color;
            if (listen && send) {
                st = "收听 + 送声";
                color = OK;
            } else if (listen) {
                st = "收听中";
                color = OK;
            } else if (send) {
                st = "送声中";
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
