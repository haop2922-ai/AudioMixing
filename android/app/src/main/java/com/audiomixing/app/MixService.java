package com.audiomixing.app;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

/**
 * 前台服务：让「引擎」活在界面之外。
 *
 * <p>为什么必须有它（而不是让 Activity 自己跑）：
 * 用户点完「收听 / 送声音」就把手机揣兜里了，Activity 一被回收，声音就断。
 * 前台服务 + 常驻通知 = 系统不会随手杀进程，用户也能一眼看到"还在传"，
 * 点通知直接回到界面。
 *
 * <p><b>通知类型</b>：我们做三件事 —— 播对方的声音（mediaPlayback）、
 * 采自己的麦克风（microphone）、采"本机正在播放的声音"（mediaProjection）。
 * 清单里 {@code foregroundServiceType} 三个都声明了。
 * <ul>
 *   <li>microphone 类型在安卓 14 上要求已经拿到 RECORD_AUDIO 运行时权限，
 *       所以没授权时只声明 mediaPlayback，否则 startForeground 会直接抛 SecurityException；</li>
 *   <li><b>mediaProjection 类型是"采本机声音"的硬门槛</b>：targetSdk 34 起，
 *       {@code getMediaProjection()} 必须在<b>已经启动的、带这个类型的前台服务里</b>调用，
 *       顺序反了系统直接抛 SecurityException。所以这里把"起前台服务带类型"和
 *       "换 MediaProjection"放在同一个 onStartCommand 里按顺序做，中间不给任何人插队的机会。</li>
 * </ul>
 *
 * <p>所有 startForeground 都做了层层降级兜底：真起不来也只是没有常驻通知，
 * 进程内引擎照样跑（界面可见时不影响使用），绝不让 App 崩掉。
 */
public class MixService extends Service {

    private static final String TAG = "AM-Service";

    public static final String ACTION_START = "com.audiomixing.app.action.START";
    public static final String ACTION_STOP = "com.audiomixing.app.action.STOP";
    /** ★ 拿"采本机声音"的授权（用户在系统窗口点完允许之后走这里）。 */
    public static final String ACTION_ARM_PROJECTION = "com.audiomixing.app.action.ARM_PROJECTION";

    private static final String EXTRA_RESULT_CODE = "am_result_code";
    private static final String EXTRA_RESULT_DATA = "am_result_data";

    private static final String CHANNEL_ID = "am_running";
    private static final String CHANNEL_NAME = "音频传输";
    /** 通知 ID 用 'WL'（0x574C），和协议 magic 一致，日志里好认。 */
    private static final int NOTI_ID = 0x574C;
    /** 3 秒刷一次通知就够了：太勤费电，太慢看不出"正在传"。 */
    private static final long TICK_MS = 3000L;

    private Engine engine;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private String lastText = "";
    private boolean foreground;

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            // ★ 安全检查必须在后台也跑：用户把手机揣兜里、电脑那边关掉了 App，
            //   界面的 150ms tick 停了，但"对端是不是走了"的判断不能停 ——
            //   否则会一直对着一个已经关掉的程序发声音（2026-10-14 用户反馈）。
            //   这里每 3 秒过一遍，用的是和界面完全同一段逻辑（Engine.pruneGoneTargets）。
            try {
                engine.pruneGoneTargets();
            } catch (Throwable t) {
                Log.w(TAG, "后台目标检查失败（不影响通知）", t);
            }
            updateNotification();
            handler.postDelayed(this, TICK_MS);
        }
    };

    /** 起服务（界面 onCreate 时叫一次）。起不来只记日志，不崩。 */
    public static void safeStart(Context ctx) {
        Intent i = new Intent(ctx, MixService.class).setAction(ACTION_START);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i);
            } else {
                ctx.startService(i);
            }
        } catch (Throwable t) {
            Log.w(TAG, "前台服务没起来（清单缺 <service> 声明？），退化为界面在时才有声音", t);
        }
    }

    /** 停服务（通知里的「停止」也走这里）。 */
    public static void safeStop(Context ctx) {
        try {
            ctx.startService(new Intent(ctx, MixService.class).setAction(ACTION_STOP));
        } catch (Throwable t) {
            Log.w(TAG, "停止服务失败", t);
        }
    }

    /**
     * ★ 用户在系统窗口里点完「允许」之后，把授权结果交给前台服务去换 MediaProjection。
     *
     * <p><b>为什么要绕这一道、而不是在 Activity 里直接 getMediaProjection</b>：
     * targetSdk 34（Android 14）起，{@code getMediaProjection()} 必须在<b>已经启动的、
     * 类型含 mediaProjection 的前台服务</b>里调用，否则直接抛 SecurityException。
     * 把这两件事都放进服务自己的 onStartCommand，顺序就是天然的：
     * 先 startForeground 带上类型，紧接着再换授权。
     */
    public static void armProjection(Context ctx, int resultCode, Intent data) {
        if (data == null) {
            return;
        }
        Intent i = new Intent(ctx, MixService.class)
                .setAction(ACTION_ARM_PROJECTION)
                .putExtra(EXTRA_RESULT_CODE, resultCode)
                .putExtra(EXTRA_RESULT_DATA, data);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i);
            } else {
                ctx.startService(i);
            }
        } catch (Throwable t) {
            Log.w(TAG, "启动前台服务去换投屏授权失败", t);
            // 服务起不来就等于拿不到授权 —— 必须让引擎如实记下来（界面会显示原因）。
            Engine e = Engine.peek();
            if (e != null) {
                e.onProjectionFailed("前台服务起不来，拿不到采本机声音的授权：" + t);
            }
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        engine = Engine.get(this);
        engine.start();          // 幂等：界面已经启动过就是空操作
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = (intent == null) ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            engine.stop();
            // 必须先摘掉前台身份再去掉通知：只 stopSelf() 的话，
            // 部分 ROM 上那条"正在运行"会一直挂在通知栏里，用户以为还没停。
            try {
                stopForeground(STOP_FOREGROUND_REMOVE);
            } catch (Throwable t) {
                Log.w(TAG, "停止前台状态失败", t);
            }
            foreground = false;
            removeNotification();
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_ARM_PROJECTION.equals(action)) {
            engine.start();
            // ★ 顺序不能变、也不能拆成两次 startService：
            //   ①先把前台服务带 mediaProjection 类型起来（安卓 14 的硬性要求）
            //   ②再换 MediaProjection
            goForeground(true);
            acquireProjection(intent.getIntExtra(EXTRA_RESULT_CODE, 0), resultData(intent));
            handler.removeCallbacks(tick);
            handler.post(tick);
            return START_STICKY;
        }
        engine.start();
        goForeground(false);
        handler.removeCallbacks(tick);
        handler.post(tick);
        // 被系统回收后自动回来：常驻不断流靠的就是它。
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;             // 不需要绑定，界面直接拿 Engine 单例
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacks(tick);
        if (engine != null) {
            engine.stop();       // 服务没了就没有"后台常驻"的意义了，释放采集、端口与投屏授权
        }
        super.onDestroy();
    }

    // ────────────────────── ★ 采本机声音的授权（Android 10+） ──────────────────────

    /** 取出授权数据。API 33 起旧的 getParcelableExtra 被标了过时，两个都留着兼容。 */
    @SuppressWarnings("deprecation")
    private static Intent resultData(Intent intent) {
        if (intent == null) {
            return null;
        }
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                return intent.getParcelableExtra(EXTRA_RESULT_DATA, Intent.class);
            }
            return intent.getParcelableExtra(EXTRA_RESULT_DATA);
        } catch (Throwable t) {
            Log.w(TAG, "读取投屏授权数据失败", t);
            return null;
        }
    }

    private void acquireProjection(int resultCode, Intent data) {
        if (data == null) {
            engine.onProjectionFailed("系统没有返回授权数据");
            return;
        }
        if (resultCode != android.app.Activity.RESULT_OK) {
            engine.onProjectionFailed("你在系统窗口里拒绝了授权");
            return;
        }
        acquireProjectionAttempt(resultCode, data, 0);
    }

    /**
     * 换 MediaProjection。<b>只能在 {@link #goForeground(boolean)} 带 mediaProjection 类型
     * 成功之后调用。</b>
     *
     * <p>失败会再试一次：真机上"前台服务刚起来、系统那边还没登记完"导致第一次被拒是有概率的
     * （同一份授权数据在成功之前不会作废），多试一次能救回来；彻底失败就把原因交给引擎 ——
     * 界面和诊断报告会写清楚，<b>绝不假装拿到了授权</b>。
     */
    private void acquireProjectionAttempt(final int resultCode, final Intent data, final int attempt) {
        Throwable err = null;
        try {
            MediaProjectionManager mgr =
                    (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
            if (mgr == null) {
                engine.onProjectionFailed("这台设备没有投屏服务（MEDIA_PROJECTION_SERVICE）");
                return;
            }
            MediaProjection p = mgr.getMediaProjection(resultCode, data);
            if (p == null) {
                engine.onProjectionFailed("系统没有给出授权对象（授权被拒？）");
                return;
            }
            engine.onProjectionGranted(p);
            Log.i(TAG, "拿到「采集本机声音」的授权");
            return;
        } catch (Throwable t) {
            err = t;
        }
        if (attempt < 1) {
            Log.w(TAG, "第一次 getMediaProjection 失败，250ms 后重试一次", err);
            handler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    acquireProjectionAttempt(resultCode, data, attempt + 1);
                }
            }, 250);
            return;
        }
        engine.onProjectionFailed("拿不到采本机声音的授权：" + err);
    }

    // ────────────────────────────── 通知 ──────────────────────────────

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm == null || nm.getNotificationChannel(CHANNEL_ID) != null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("局域网音频传输的常驻状态");
        ch.setShowBadge(false);
        ch.setSound(null, null);
        ch.enableVibration(false);
        nm.createNotificationChannel(ch);
    }

    /** 通知上那句人话：「2 台设备已连接 · 收听 1 / 送声 1」。 */
    private String buildText() {
        int conn = engine.connectedCount();
        int listen = engine.listeningCount();
        int send = engine.sendingCount();
        if (conn == 0) {
            int seen = engine.peers().size();
            if (seen == 0) {
                return "正在搜索局域网里的设备…";
            }
            return "已发现 " + seen + " 台设备 · 点开就能用";
        }
        StringBuilder sb = new StringBuilder(conn + " 台设备已连接");
        if (listen > 0 || send > 0) {
            sb.append(" · ");
            if (listen > 0) {
                sb.append("收听 ").append(listen);
            }
            if (listen > 0 && send > 0) {
                sb.append(" / ");
            }
            if (send > 0) {
                // ★ 通知栏也写清"送的是什么声音"：用户被"只采麦克风"误导过一次，
                //   这个信息在哪儿都不能含糊。
                sb.append("送声 ").append(send)
                        .append("（").append(AudioSend.sourceShortName(engine.activeSourceKind()))
                        .append("）");
                // ★ 安全检查的可见结果：有目标因为"确认走了"被暂停时，通知栏必须如实说，
                //   不能出现"在广播但实际一台都没在收"的假象。
                if (engine.sendSuspendedText().length() > 0) {
                    int actual = engine.sendTargetCount();
                    if (actual == 0) {
                        sb.append(" · ⚠ 目标已离线，已停止发送");
                    } else {
                        sb.append(" · ⚠ 有目标已离线（实际在发 ").append(actual).append(" 台）");
                    }
                }
            } else if (engine.isBroadcasting()) {
                // 广播开着但一台都没在发（目标全离线）
                sb.append("· 广播中，但一台设备都联系不上");
            }
        }
        return sb.toString();
    }

    private void goForeground(boolean wantProjection) {
        String text = buildText();
        lastText = text;
        Notification n = build(text);
        int types = foregroundTypes(wantProjection);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTI_ID, n, types);
            } else {
                startForeground(NOTI_ID, n);
            }
            foreground = true;
            return;
        } catch (Throwable t) {
            Log.w(TAG, "带类型的前台服务没起来，逐步降级", t);
        }
        // 第二档：只保留 mediaPlayback。个别 ROM 对 mediaProjection 类型有额外限制。
        try {
            startForeground(NOTI_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
            foreground = true;
            if (wantProjection) {
                // 前台服务带不上 mediaProjection 类型 → 安卓 14 会拒绝 getMediaProjection。
                // 如实报告（界面会写"拿不到授权"），不装作还能采本机声音。
                engine.onProjectionFailed("系统不允许前台服务带上 mediaProjection 类型，这台机器采不了本机声音");
            }
            return;
        } catch (Throwable t2) {
            Log.w(TAG, "退回不带类型的前台服务", t2);
        }
        try {
            startForeground(NOTI_ID, n);
            foreground = true;
        } catch (Throwable t3) {
            Log.w(TAG, "前台服务起不来，改成普通常驻通知", t3);
            foreground = false;
            postPlain(n);
        }
    }

    /**
     * 前台服务类型位。
     *
     * <p>microphone 需要 RECORD_AUDIO 已授权（安卓 14 的硬性要求），没授权就不带它。
     * <b>mediaProjection 只要手里有授权（或正准备去换）就一直带着</b>：
     * 安卓 14 起投屏授权一旦和前台服务脱钩，系统会直接把授权收回去 ——
     * 表现就是"送本机声音突然变静音"。
     */
    private int foregroundTypes(boolean wantProjection) {
        int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
        try {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED) {
                type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            }
        } catch (Throwable ignored) {
            // 取不到就当没授权
        }
        if (wantProjection || (engine != null && engine.hasProjection())) {
            type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
        }
        return type;
    }

    private void updateNotification() {
        String text = buildText();
        if (text.equals(lastText)) {
            return;              // 内容没变就别打扰系统（省电，也避免通知栏闪）
        }
        lastText = text;
        Notification n = build(text);
        if (foreground) {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) {
                try {
                    nm.notify(NOTI_ID, n);
                } catch (Throwable t) {
                    Log.w(TAG, "更新通知失败", t);
                }
            }
        } else {
            postPlain(n);
        }
    }

    private void postPlain(Notification n) {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) {
            try {
                nm.notify(NOTI_ID, n);
            } catch (Throwable t) {
                Log.w(TAG, "显示通知失败（安卓 13+ 需要 POST_NOTIFICATIONS 权限）", t);
            }
        }
    }

    private void removeNotification() {
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (nm != null) {
            nm.cancel(NOTI_ID);
        }
    }

    @SuppressWarnings("deprecation")
    private Notification build(String text) {
        Intent open = new Intent(this, MainActivity.class);
        open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent content = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1,
                new Intent(this, MixService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Notification.Builder b = (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O)
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("Audio Mixing 正在运行")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(content)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setCategory(Notification.CATEGORY_SERVICE)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setColor(0xFF38BDF8)
                .addAction(new Notification.Action.Builder(null, "停止", stop).build());
        return b.build();
    }
}
