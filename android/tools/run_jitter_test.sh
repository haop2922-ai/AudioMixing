#!/usr/bin/env bash
#
# 抖动缓冲离线测试 —— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_jitter_test.sh
#
# 它做四件事：
#   1. 把 app/src/main/java/com/audiomixing/app/AudioRecv.java 里的 Stream 类
#      （抖动缓冲）按大括号配平原样抽出来；
#   2. 只做两处机械替换：private final class -> static final class、
#      SystemClock.elapsedRealtime() -> JitterProbe.now（可控时钟），
#      逻辑一行不改，插进 JitterProbe.java 的 INSERT 标记处；
#   3. 用 JDK 编译并运行（默认找 JAVA_HOME，其次项目文档里的 jdk-17）；
#   4. 全部断言通过返回 0，否则返回 1 —— 可以直接当回归门禁。
#
# 为什么值得存在：序号回绕、乱序、丢包补静音、断流恢复、缓冲积压这几件事在真机上只能靠"听"，
# 出了问题是"断一次就再也回不来"这种没法复现的怪状；在 JVM 上它是纯算术，可以逐帧断言。
# （这套测试抓到过真 bug：断流恢复时 nextSeq=-1 被末尾无条件自增推成 0，播放头空转，
#   对端恢复后新包全被判"太超前"丢掉 —— 也就是"断了以后一路静音、再也回不来"。）
#
# 环境变量：JAVAC / JAVA 可以强制指定；不设就自动找。
# 传 --keep 保留生成的临时目录，便于排查抽取出来的代码长什么样。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/../app/src/main/java/com/audiomixing/app/AudioRecv.java"
PROBE="$DIR/JitterProbe.java"
EXTRACT="$DIR/extract_stream.awk"
MARKER="// >>> STREAM_SOURCE_INSERT_HERE <<<"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$SRC" ]     || die "找不到被测文件 $SRC"
[ -f "$PROBE" ]   || die "找不到 $PROBE"
[ -f "$EXTRACT" ] || die "找不到 $EXTRACT"

# --- 1. 找 JDK -------------------------------------------------------------
JAVAC="${JAVAC:-}"
JAVA="${JAVA:-}"
if [ -z "$JAVAC" ]; then
    if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" ]; then
        JAVAC="$JAVA_HOME/bin/javac"
    elif [ -x "C:/Program Files/Java/jdk-17/bin/javac" ]; then
        JAVAC="C:/Program Files/Java/jdk-17/bin/javac"   # 本项目文档里指定的 JDK
    else
        JAVAC="$(command -v javac || true)"
    fi
fi
[ -n "$JAVAC" ] || die "找不到 javac，请设 JAVA_HOME 或 JAVAC"
[ -n "$JAVA" ] || JAVA="${JAVAC%/javac}/java"
[ -x "$JAVA" ] || JAVA="$(command -v java || true)"
[ -n "$JAVA" ] || die "找不到 java，请设 JAVA 或 JAVA_HOME"

# --- 2. 抽取 Stream 类 -----------------------------------------------------
WORK="${TMPDIR:-/tmp}/am-jitter-test.$$"
mkdir -p "$WORK/out" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

awk -f "$EXTRACT" "$SRC" > "$WORK/stream.part" || die "抽取脚本执行失败"
LINES=$(wc -l < "$WORK/stream.part" | tr -d ' ')
[ "$LINES" -gt 20 ] || die "只抽到 $LINES 行，AudioRecv.java 的结构可能变了（找不到 private final class Stream）"
grep -q "int pump(" "$WORK/stream.part" || die "抽到的代码里没有 pump()，AudioRecv.java 的结构可能变了"

# 两处机械替换：静态嵌套 + 可控时钟。逻辑不碰。
sed -e 's/private final class Stream/static final class Stream/' \
    -e 's/SystemClock\.elapsedRealtime()/JitterProbe.now/g' \
    "$WORK/stream.part" > "$WORK/stream.java.part"

# --- 3. 把抽出来的类插进 INSERT 标记处 ------------------------------------
grep -qF "$MARKER" "$PROBE" || die "JitterProbe.java 里找不到标记行：$MARKER"
awk -v marker="$MARKER" -v part="$WORK/stream.java.part" '
    index($0, marker) { print; while ((getline l < part) > 0) print l; close(part); next }
    { print }
' "$PROBE" > "$WORK/JitterProbe.java"

# --- 4. 编译 + 运行 -------------------------------------------------------
echo "被测代码：$SRC"
echo "抽取结果：$LINES 行（Stream 类，已插入 $WORK/JitterProbe.java）"
echo "JDK     ：$JAVAC"
echo

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" "$WORK/JitterProbe.java"; then
    die "编译失败（抽出来的代码和 JitterProbe.java 的常量对不上了？加 --keep 看生成的源码）"
fi

# 明确把输出固定成 UTF-8：Windows 控制台默认 GBK 会把中文和符号打成乱码，
# 结论行 RESULT: PASS/FAIL 用的是 ASCII，任何编码下都看得清。
"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" JitterProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 抖动缓冲离线测试通过 =="
else
    echo "== 抖动缓冲离线测试失败（退出码 $RC）=="
fi
exit "$RC"
