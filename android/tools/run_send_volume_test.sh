#!/usr/bin/env bash
#
# ★ 送声档位音量离线测试（2026-11-08 用户方案）—— 不需要设备、Gradle、联网。
#
#     bash android/tools/run_send_volume_test.sh
#
# 被测：SendVolume.java（纯逻辑，零桩）——外放档乘系统音量、静音档默认只保留
# App 内部音量；两档的 post/raw 与 vol_in_capture 必须和文档口径一致。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../app/src/main/java/com/audiomixing/app"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$APP/SendVolume.java" ] || die "找不到 $APP/SendVolume.java"
[ -f "$DIR/SendVolumeProbe.java" ] || die "找不到 $DIR/SendVolumeProbe.java"

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

WORK="${TMPDIR:-/tmp}/am-send-volume-test.$$"
mkdir -p "$WORK/out" "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

echo "被测代码：$APP/SendVolume.java（纯逻辑，零桩编译）"
echo "JDK     ：$JAVAC"
echo

cp "$APP/SendVolume.java" "$WORK/src/com/audiomixing/app/"
cp "$DIR/SendVolumeProbe.java" "$WORK/src/"

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" $(find "$WORK/src" -name '*.java'); then
    die "编译失败（SendVolume 用到了安卓 API？它必须是纯逻辑）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" SendVolumeProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 送声档位音量离线测试通过 =="
else
    echo "== 送声档位音量离线测试失败（退出码 $RC）=="
fi
exit "$RC"
