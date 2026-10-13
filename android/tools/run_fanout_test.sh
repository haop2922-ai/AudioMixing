#!/usr/bin/env bash
#
# ★ 一发多（多目标发送）离线测试 —— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_fanout_test.sh
#
# 为什么值得存在（task-12 第 ③④ 条用户反馈）：
#   · 「没有发广播」—— 手机端原来只能收广播，不能发；
#   · 「每个设备都应当配备独立的收/送声音音频条」—— 要显示"送"的那条，必须先能同时送给多台。
# 这两条的共同底层能力是：**一次采集 → 一包编码 → 分发给 N 个目标**（绝不采集 N 次）。
#
# 被测的是 SendFanout.java 本体（原样编译，只补一个 SystemClock 桩），
# 而且用**真实 UDP 回环端口**当"3 台设备"：每一包到底到没到、内容是不是同一份、
# 目标下线会不会影响别人、目标集合变化会不会打断发送 —— 全都是真的收包验的。
#
# 环境变量：JAVAC / JAVA 可以强制指定。传 --keep 保留临时目录。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../app/src/main/java/com/audiomixing/app"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$APP/SendFanout.java" ] || die "找不到 $APP/SendFanout.java"
[ -f "$DIR/FanoutProbe.java" ] || die "找不到 $DIR/FanoutProbe.java"

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

WORK="${TMPDIR:-/tmp}/am-fanout-test.$$"
mkdir -p "$WORK/out" "$WORK/src/android/os" "$WORK/src/android/util" \
         "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

S="$WORK/src"

# ─── 桩：只补 SendFanout 用到的那两个安卓类 ───
cat > "$S/android/os/SystemClock.java" <<'EOF'
package android.os;
/** 可控时钟：本测试只看"最近一次成功"的时间窗，能拨快就行。 */
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

echo "被测代码：$APP/SendFanout.java（原样编译，只补 SystemClock/Log 桩）"
echo "被测方式：真实 UDP 回环端口（3 个真 socket 当 3 台设备）"
echo "JDK     ：$JAVAC"
echo

cp "$APP/SendFanout.java" "$APP/Proto.java" "$S/com/audiomixing/app/"
cp "$DIR/FanoutProbe.java" "$S/"

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" $(find "$S" -name '*.java'); then
    die "编译失败（SendFanout 用到了新的安卓 API？用完的桩要在本脚本里补上）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" FanoutProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 一发多（多目标发送）离线测试通过 =="
else
    echo "== 一发多（多目标发送）离线测试失败（退出码 $RC）=="
fi
exit "$RC"
