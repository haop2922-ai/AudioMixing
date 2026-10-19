#!/usr/bin/env bash
#
# ★ 音源开关离线测试（2026-10-19 用户真机三条反馈）—— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_source_test.sh
#
# 为什么值得存在：用户报了三件只有在真机上点按钮才试得出来的事，而它们的边界最容易错：
#   ① 停止本机声音采集后【绝不自动开麦】（自动开麦是用户点名的安全事故）；
#   ② 两颗按钮要是【真 toggle】（未激活→激活、已激活→关闭），而且"点一下状态一定变"；
#   ③ 同一时刻只能采一路（开了麦时「本机声音」那颗必须灭，不许两颗都亮）。
#
# 被测：SourcePolicy.java —— 纯函数，**零桩**（不用造安卓桩，测的就是线上那一份）。
#
# 环境变量：JAVAC / JAVA 可以强制指定。传 --keep 保留临时目录。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../app/src/main/java/com/audiomixing/app"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$APP/SourcePolicy.java" ] || die "找不到 $APP/SourcePolicy.java"
[ -f "$DIR/SourceProbe.java" ] || die "找不到 $DIR/SourceProbe.java"

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

WORK="${TMPDIR:-/tmp}/am-source-test.$$"
mkdir -p "$WORK/out" "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

echo "被测代码：$APP/SourcePolicy.java（纯函数，零桩编译）"
echo "JDK     ：$JAVAC"
echo

cp "$APP/SourcePolicy.java" "$WORK/src/com/audiomixing/app/"
cp "$DIR/SourceProbe.java" "$WORK/src/"

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" $(find "$WORK/src" -name '*.java'); then
    die "编译失败（SourcePolicy 用到了安卓 API？它必须是纯逻辑）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" SourceProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 音源开关离线测试通过 =="
else
    echo "== 音源开关离线测试失败（退出码 $RC）=="
fi
exit "$RC"
