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
 * <p><b>通知类型</b>：我们只做两件事 —— 播对方的声音（mediaPlayback）和采自己的麦克风（microphone），
 * 清单里 {@code foregroundServiceType="mediaPlayback|microphone"} 就是这两个。
 * microphone 类型在安卓 14 上要求已经拿到 RECORD_AUDIO 运行时权限，
 * 所以没授权时<b>只声明 mediaPlayback</b>，否则 startForeground 会直接抛 SecurityException。
 *
 * <p>所有 startForeground 都做了层层降级兜底：真起不来也只是没有常驻通知，
 * 进程内引擎照样跑（界面可见时不影响使用），绝不让 App 崩掉。
 */
public class MixService extends Service {

    private static final String TAG = "AM-Service";

    public static final String ACTION_START = "com.audiomixing.app.action.START";
    public static final String ACTION_STOP = "com.audiomixing.app.action.STOP";

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
        engine.start();
        goForeground();
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
            engine.stop();       // 服务没了就没有"后台常驻"的意义了，释放麦克风与端口
        }
        super.onDestroy();
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
                sb.append("送声 ").append(send);
            }
        }
        return sb.toString();
    }

    private void goForeground() {
        String text = buildText();
        lastText = text;
        Notification n = build(text);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTI_ID, n, foregroundTypes());
            } else {
                startForeground(NOTI_ID, n);
            }
            foreground = true;
        } catch (Throwable t) {
            Log.w(TAG, "带类型的前台服务没起来，退回不带类型", t);
            try {
                startForeground(NOTI_ID, n);
                foreground = true;
            } catch (Throwable t2) {
                Log.w(TAG, "前台服务起不来，改成普通常驻通知", t2);
                foreground = false;
                postPlain(n);
            }
        }
    }

    /**
     * 前台服务类型位。microphone 需要 RECORD_AUDIO 已授权（安卓 14 的硬性要求），
     * 没授权就先只要 mediaPlayback，等用户点了「送声音」再重启服务补上。
     */
    private int foregroundTypes() {
        int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK;
        try {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED) {
                type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
            }
        } catch (Throwable ignored) {
            // 取不到就当没授权
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
