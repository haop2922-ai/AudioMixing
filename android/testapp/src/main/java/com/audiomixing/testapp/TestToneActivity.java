package com.audiomixing.testapp;

import android.app.Activity;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.TextView;

import java.util.Locale;
import java.util.Random;

/**
 * 自建测试源：把「系统媒体音量」和「内部增益」两个滑杆分开，用来给任意 App 归类。
 *
 * <ul>
 *   <li><b>系统媒体音量</b>：直接 {@link AudioManager#setStreamVolume}，并实时回读
 *       {@link AudioManager#getStreamVolume}。滑杆动 = 系统音量动。</li>
 *   <li><b>内部增益</b>：只在我们写 {@link AudioTrack} 之前乘一个 0..100% 系数，
 *       <b>完全不碰系统音量</b>。</li>
 * </ul>
 *
 * 判据（配合 AudioMixing 诊断里的 vol 序列与手机采集层一行）：
 * <ul>
 *   <li>拖本 App 的「系统媒体音量」→ AudioMixing 的 vol 跟着变 → 这台设备上的
 *       第三方 App 若同样行为，就是<b>类型 A（滑杆=系统音量）</b>。</li>
 *   <li>拖「内部增益」→ 声音大小变、vol 读数不变 → <b>类型 B（内部增益）</b>，
 *       例如 YouTube 播放器、VLC。</li>
 * </ul>
 *
 * 注意：AudioMixing 静音档会把 STREAM_MUSIC 钉在 0，此时本 App 的系统音量滑杆
 * 会被反复按回去（回读值会显示 0）—— 这正好演示了静音档里「系统音量类 App
 * 调不动」的限制；要验系统音量请切外放档。
 */
public class TestToneActivity extends Activity {

    private static final int SR = 48000;
    private static final int FRAMES = 480;          // 10 ms
    private static final double TWO_PI = Math.PI * 2.0;

    private AudioManager am;
    private AudioTrack track;
    private Thread worker;
    private volatile boolean running;
    private volatile float gain = 0.40f;             // 内部增益 0..1
    private volatile boolean usePink = true;         // true=粉噪，false=1kHz 正弦
    private volatile float peakHold;

    private TextView tvSignal, tvGainValue, tvSystemValue, tvReadback, tvPeak, tvNote;
    private SeekBar sbSystem, sbGain;
    private Button btnPlay, btnSignal;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            updateReadback();
            ui.postDelayed(this, 500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        am = (AudioManager) getSystemService(AUDIO_SERVICE);
        setContentView(buildUi());
        ui.post(tick);
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(tick);
        stopPlayback();
        super.onDestroy();
    }

    // ---------------------------------------------------------------- 界面

    private View buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        root.setPadding(pad, pad, pad, pad);

        TextView title = text("音频测试源（AudioMixing 对照用）", 20, true);
        root.addView(title);

        tvNote = text("两个滑杆互相独立：上面改系统媒体音量（setStreamVolume），"
                + "下面只改本 App 的内部增益（不动系统音量）。", 13, false);
        tvNote.setPadding(0, dp(6), 0, dp(10));
        root.addView(tvNote);

        // ① 系统媒体音量
        rule(root);
        root.addView(text("① 系统媒体音量（STREAM_MUSIC）", 16, true));
        tvSystemValue = text("", 14, false);
        root.addView(tvSystemValue);
        sbSystem = new SeekBar(this);
        int maxVol = am != null ? Math.max(1, am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)) : 15;
        sbSystem.setMax(maxVol);
        int now = am != null ? am.getStreamVolume(AudioManager.STREAM_MUSIC) : 0;
        sbSystem.setProgress(Math.max(0, Math.min(maxVol, now)));
        sbSystem.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar bar, int progress, boolean fromUser) {
                if (!fromUser || am == null) {
                    return;
                }
                try {
                    am.setStreamVolume(AudioManager.STREAM_MUSIC, progress, 0);
                } catch (Exception e) {
                    tvNote.setText("setStreamVolume 失败：" + e);
                }
                updateReadback();
            }

            @Override public void onStartTrackingTouch(SeekBar bar) { }

            @Override public void onStopTrackingTouch(SeekBar bar) { }
        });
        root.addView(sbSystem, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        tvReadback = text("", 13, false);
        tvReadback.setPadding(0, dp(2), 0, dp(10));
        root.addView(tvReadback);

        // ② 内部增益
        rule(root);
        root.addView(text("② 内部增益（只在 AudioTrack 写入前乘系数）", 16, true));
        tvGainValue = text("", 14, false);
        root.addView(tvGainValue);
        sbGain = new SeekBar(this);
        sbGain.setMax(100);
        sbGain.setProgress(Math.round(gain * 100f));
        sbGain.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar bar, int progress, boolean fromUser) {
                gain = Math.max(0f, Math.min(1f, progress / 100f));
                updateGainLabel();
            }

            @Override public void onStartTrackingTouch(SeekBar bar) { }

            @Override public void onStopTrackingTouch(SeekBar bar) { }
        });
        root.addView(sbGain, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        updateGainLabel();

        // ③ 信号与播放
        rule(root);
        root.addView(text("③ 信号与播放", 16, true));
        tvSignal = text("", 14, false);
        root.addView(tvSignal);
        updateSignalLabel();

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(8), 0, dp(8));

        btnSignal = new Button(this);
        btnSignal.setText("切换信号");
        btnSignal.setOnClickListener(v -> {
            usePink = !usePink;
            updateSignalLabel();
        });
        row.addView(btnSignal, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        btnPlay = new Button(this);
        btnPlay.setText("开始播放");
        btnPlay.setOnClickListener(v -> {
            if (running) {
                stopPlayback();
            } else {
                startPlayback();
            }
            btnPlay.setText(running ? "停止播放" : "开始播放");
        });
        row.addView(btnPlay, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(row);

        tvPeak = text("输出峰值：—", 15, true);
        tvPeak.setPadding(0, dp(4), 0, dp(10));
        root.addView(tvPeak);

        rule(root);
        TextView judge = text(
                "判据：拖①时 AudioMixing 诊断里的 vol 跟着变 → 系统音量类（类型 A）；"
                + "拖②时声音变、vol 不变 → 内部增益类（类型 B）。"
                + "\n注意：AudioMixing 静音档会把 STREAM_MUSIC 钉在 0，此时①会被按回去、"
                + "回读为 0 —— 要验系统音量请先在 AudioMixing 里切外放档。",
                13, false);
        root.addView(judge);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(root);
        return scroll;
    }

    private void updateGainLabel() {
        tvGainValue.setText(String.format(Locale.US, "内部增益：%.0f%%（不改系统音量）", gain * 100f));
    }

    private void updateSignalLabel() {
        tvSignal.setText(usePink ? "当前信号：粉噪（循环）" : "当前信号：1 kHz 正弦（循环）");
    }

    private void updateReadback() {
        if (am == null) {
            return;
        }
        int cur = am.getStreamVolume(AudioManager.STREAM_MUSIC);
        int max = am.getStreamMaxVolume(AudioManager.STREAM_MUSIC);
        tvSystemValue.setText(String.format(Locale.US, "滑杆设定：%d / %d", sbSystem.getProgress(), max));
        tvReadback.setText(String.format(Locale.US,
                "系统回读：getStreamVolume(STREAM_MUSIC) = %d / %d%s",
                cur, max, running ? "" : "（未播放）"));
        float pk = peakHold;
        if (pk > 1e-4f) {
            tvPeak.setText(String.format(Locale.US, "输出峰值：%.3f（%.1f dBFS）",
                    pk, 20.0 * Math.log10(pk)));
        } else {
            tvPeak.setText("输出峰值：—");
        }
    }

    // ---------------------------------------------------------------- 播放

    private void startPlayback() {
        if (running) {
            return;
        }
        AudioAttributes attrs = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                .build();
        AudioFormat fmt = new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_FLOAT)
                .setSampleRate(SR)
                .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
                .build();
        int minBuf = AudioTrack.getMinBufferSize(SR, AudioFormat.CHANNEL_OUT_STEREO,
                AudioFormat.ENCODING_PCM_FLOAT);
        int buf = Math.max(minBuf, FRAMES * 2 * 4 * 4);
        try {
            track = new AudioTrack.Builder()
                    .setAudioAttributes(attrs)
                    .setAudioFormat(fmt)
                    .setBufferSizeInBytes(buf)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            track.play();
        } catch (Exception e) {
            track = null;
            tvNote.setText("AudioTrack 创建失败：" + e);
            return;
        }
        running = true;
        peakHold = 0f;
        worker = new Thread(this::pump, "test-tone");
        worker.start();
    }

    private void pump() {
        final float[] out = new float[FRAMES * 2];
        final Random rnd = new Random(20261108L);
        // 粉噪滤波状态（Paul Kellet 经济版）
        float b0 = 0f, b1 = 0f, b2 = 0f, b3 = 0f, b4 = 0f, b5 = 0f, b6 = 0f;
        double phase = 0.0;
        while (running) {
            AudioTrack t = track;
            if (t == null) {
                break;
            }
            float g = gain;
            float pk = 0f;
            for (int i = 0; i < FRAMES; i++) {
                float s;
                if (usePink) {
                    float white = (float) (rnd.nextDouble() * 2.0 - 1.0);
                    b0 = 0.99886f * b0 + white * 0.0555179f;
                    b1 = 0.99332f * b1 + white * 0.0750759f;
                    b2 = 0.96900f * b2 + white * 0.1538520f;
                    b3 = 0.86650f * b3 + white * 0.3104856f;
                    b4 = 0.55000f * b4 + white * 0.5329522f;
                    b5 = -0.7616f * b5 - white * 0.0168980f;
                    s = (b0 + b1 + b2 + b3 + b4 + b5 + b6 + white * 0.5362f) * 0.11f;
                    b6 = white * 0.115926f;
                } else {
                    s = (float) Math.sin(phase);
                    phase += TWO_PI * 1000.0 / SR;
                    if (phase >= TWO_PI) {
                        phase -= TWO_PI;
                    }
                }
                if (s > 1f) {
                    s = 1f;
                } else if (s < -1f) {
                    s = -1f;
                }
                s *= g;
                out[2 * i] = s;
                out[2 * i + 1] = s;
                float a = s < 0 ? -s : s;
                if (a > pk) {
                    pk = a;
                }
            }
            try {
                t.write(out, 0, out.length, AudioTrack.WRITE_BLOCKING);
            } catch (Exception e) {
                break;
            }
            float held = peakHold * 0.90f;
            peakHold = pk > held ? pk : held;
        }
    }

    private void stopPlayback() {
        running = false;
        Thread w = worker;
        worker = null;
        if (w != null) {
            try {
                w.join(500);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
        AudioTrack t = track;
        track = null;
        if (t != null) {
            try {
                t.pause();
                t.flush();
            } catch (Exception ignored) {
                // 释放阶段的异常不影响结论
            }
            t.release();
        }
        peakHold = 0f;
        if (btnPlay != null) {
            btnPlay.setText("开始播放");
        }
    }

    // ---------------------------------------------------------------- 小工具

    private TextView text(String s, int sp, boolean bold) {
        TextView v = new TextView(this);
        v.setText(s);
        v.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        if (bold) {
            v.setTypeface(null, android.graphics.Typeface.BOLD);
        }
        return v;
    }

    private void rule(LinearLayout root) {
        TextView line = text("————————————————————————", 12, false);
        line.setGravity(Gravity.CENTER_HORIZONTAL);
        line.setPadding(0, dp(10), 0, dp(4));
        root.addView(line);
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
