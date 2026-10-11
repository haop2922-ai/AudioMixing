#!/usr/bin/env bash
#
# 收包准入逻辑离线测试 —— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_recv_admission_test.sh
#
# 为什么值得存在：用户真机上报"已收包 2 · 坏包 3220 · 缓冲 0ms · 流 0"，而"为什么"
# 在真机上完全看不出来。这一套把 AudioRecv.java 原样抽过来（配 android.* 桩类编译，
# 源码一字未改），然后拿**电脑端 proto.py 真编出来的字节**喂进 onPacket，逐项断言：
#   ① 电脑端标准包（PCM16/480 帧/1948 字节）手机收不收 —— 跨语言互通的核心；
#   ② 5 种坏包是不是各归各类（这次排查最卡人的就是只有一个总数）；
#   ③ 「听广播」= 名单留空 = 谁来都听 —— 手机能不能听见电脑的广播全靠这条语义。
#
# 环境变量：JAVAC / JAVA 可以强制指定。传 --keep 保留临时目录。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../app/src/main/java/com/audiomixing/app"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$APP/AudioRecv.java" ] || die "找不到 $APP/AudioRecv.java"
[ -f "$APP/Proto.java" ]     || die "找不到 $APP/Proto.java"
[ -f "$DIR/RecvProbe.java" ] || die "找不到 $DIR/RecvProbe.java"

JAVAC="${JAVAC:-}"
JAVA="${JAVA:-}"
if [ -z "$JAVAC" ]; then
    if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" ]; then
        JAVAC="$JAVA_HOME/bin/javac"
    elif [ -x "C:/Program Files/Java/jdk-17/bin/javac" ]; then
        JAVAC="C:/Program Files/Java/jdk-17/bin/javac"
    else
        JAVAC="$(command -v javac || true)"
    fi
fi
[ -n "$JAVAC" ] || die "找不到 javac，请设 JAVA_HOME 或 JAVAC"
[ -n "$JAVA" ] || JAVA="${JAVAC%/javac}/java"
[ -x "$JAVA" ] || JAVA="$(command -v java || true)"
[ -n "$JAVA" ] || die "找不到 java，请设 JAVA_HOME"

WORK="${TMPDIR:-/tmp}/am-recv-test.$$"
mkdir -p "$WORK/out" "$WORK/src/android/content" "$WORK/src/android/media" \
         "$WORK/src/android/net/wifi" "$WORK/src/android/os" "$WORK/src/android/util" \
         "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

S="$WORK/src"

# ─── 桩类：只实现 AudioRecv 用到的那部分安卓 API（行为都写清楚，不做隐式假装）───
cat > "$S/android/content/Context.java" <<'EOF'
package android.content;
public class Context {
    public static final String WIFI_SERVICE = "wifi";
    public Context getApplicationContext() { return this; }
    public Object getSystemService(String name) { return null; }
}
EOF

cat > "$S/android/os/Build.java" <<'EOF'
package android.os;
public class Build {
    public static class VERSION { public static int SDK_INT = 34; }
    public static class VERSION_CODES { public static final int O = 26, Q = 29; }
}
EOF

cat > "$S/android/os/SystemClock.java" <<'EOF'
package android.os;
/** 可控时钟：探针每喂一个包就往前走一点，用来放行"最近样本"的节流。 */
public class SystemClock {
    public static long now = 1_000_000L;
    public static long elapsedRealtime() { return now; }
}
EOF

cat > "$S/android/util/Log.java" <<'EOF'
package android.util;
public class Log {
    public static int i(String t, String m) { return 0; }
    public static int w(String t, String m) { return 0; }
    public static int w(String t, String m, Throwable e) { return 0; }
    public static int e(String t, String m) { return 0; }
}
EOF

cat > "$S/android/media/AudioAttributes.java" <<'EOF'
package android.media;
public class AudioAttributes {
    public static final int USAGE_MEDIA = 1, CONTENT_TYPE_MUSIC = 2;
    public static class Builder {
        public Builder setUsage(int u) { return this; }
        public Builder setContentType(int c) { return this; }
        public AudioAttributes build() { return new AudioAttributes(); }
    }
}
EOF

cat > "$S/android/media/AudioFormat.java" <<'EOF'
package android.media;
public class AudioFormat {
    public static final int ENCODING_PCM_16BIT = 2, CHANNEL_OUT_STEREO = 12, CHANNEL_IN_STEREO = 12;
    public static class Builder {
        public Builder setSampleRate(int r) { return this; }
        public Builder setEncoding(int e) { return this; }
        public Builder setChannelMask(int m) { return this; }
        public AudioFormat build() { return new AudioFormat(); }
    }
}
EOF

cat > "$S/android/media/AudioTrack.java" <<'EOF'
package android.media;
/** 桩：本测试只走收包逻辑，播放相关的方法不会被调用（调用了就抛，免得"假装成功"）。 */
public class AudioTrack {
    public static final int MODE_STREAM = 1, PERFORMANCE_MODE_LOW_LATENCY = 1, STATE_INITIALIZED = 1;
    public static int getMinBufferSize(int r, int c, int e) { return 0; }
    public static class Builder {
        public Builder setAudioAttributes(AudioAttributes a) { return this; }
        public Builder setAudioFormat(AudioFormat f) { return this; }
        public Builder setBufferSizeInBytes(int b) { return this; }
        public Builder setTransferMode(int m) { return this; }
        public Builder setPerformanceMode(int m) { return this; }
        public AudioTrack build() { throw new UnsupportedOperationException("桩不支持真的建 AudioTrack"); }
    }
    public int getState() { return STATE_INITIALIZED; }
    public void play() { }
    public void pause() { }
    public void flush() { }
    public void release() { }
    public void setVolume(float v) { }
    public int write(byte[] b, int o, int n) { return n; }
}
EOF

cat > "$S/android/net/wifi/WifiManager.java" <<'EOF'
package android.net.wifi;
public class WifiManager {
    public static final int WIFI_MODE_FULL_LOW_LATENCY = 4, WIFI_MODE_FULL_HIGH_PERF = 1;
    public WifiLock createWifiLock(int mode, String tag) { return new WifiLock(); }
    public static class WifiLock {
        public void setReferenceCounted(boolean b) { }
        public void acquire() { }
        public void release() { }
        public boolean isHeld() { return false; }
    }
}
EOF

echo "被测代码：$APP/AudioRecv.java（原样编译，配 android.* 桩类）"
echo "被测数据：RecvProbe 里内嵌的包 = 电脑端 proto.py 真编出来的十六进制"
echo "JDK     ：$JAVAC"
echo

cp "$APP/AudioRecv.java" "$APP/Proto.java" "$S/com/audiomixing/app/"
cp "$DIR/RecvProbe.java" "$S/"

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" $(find "$S" -name '*.java'); then
    die "编译失败（AudioRecv 用到了新的安卓 API？用完的桩要在本脚本里补上）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" RecvProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 收包准入离线测试通过 =="
else
    echo "== 收包准入离线测试失败（退出码 $RC）=="
fi
exit "$RC"
