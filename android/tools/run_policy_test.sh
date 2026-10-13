#!/usr/bin/env bash
#
# ★ 链路策略离线测试（task-17）—— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_policy_test.sh
#
# 为什么值得存在：这一轮的三件事都要两台设备配合才能在真机上试，而它们的边界恰恰最容易错：
#   ④ 对端关掉 App 之后多久算"走了"（太短会被 Wi-Fi 抖帧误判让声音忽断、太长则一直对着关掉的
#      程序发声音 —— 这是安全问题）；
#   ① 自动收听必须可撤销（用户手动关过的设备不能再被自动打开）；
#   ① 共享音量两端同步要能收敛（谁后改以谁为准，还要兜住两台设备时钟不一致）。
#
# 被测：LinkPolicy.java —— 纯函数，**零桩**（不用造安卓桩，测的就是线上那一份）。
#
# 环境变量：JAVAC / JAVA 可以强制指定。传 --keep 保留临时目录。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../app/src/main/java/com/audiomixing/app"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$APP/LinkPolicy.java" ] || die "找不到 $APP/LinkPolicy.java"
[ -f "$DIR/PolicyProbe.java" ] || die "找不到 $DIR/PolicyProbe.java"

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

WORK="${TMPDIR:-/tmp}/am-policy-test.$$"
mkdir -p "$WORK/out" "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

echo "被测代码：$APP/LinkPolicy.java（纯函数，零桩编译）"
echo "JDK     ：$JAVAC"
echo

cp "$APP/LinkPolicy.java" "$WORK/src/com/audiomixing/app/"
cp "$DIR/PolicyProbe.java" "$WORK/src/"

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" $(find "$WORK/src" -name '*.java'); then
    die "编译失败（LinkPolicy 用到了安卓 API？它必须是纯逻辑）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" PolicyProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 链路策略离线测试通过 =="
else
    echo "== 链路策略离线测试失败（退出码 $RC）=="
fi
exit "$RC"
