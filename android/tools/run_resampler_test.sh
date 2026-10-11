#!/usr/bin/env bash
#
# 重采样离线测试 —— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_resampler_test.sh
#
# 为什么值得存在：system-audio 采集在个别设备上只给得出 44100Hz（或单声道），
# 那就必须把采集格式换算成协议要的 48000Hz 立体声。这段换算是最容易"看起来对、
# 听起来不对"的代码（变调、断点爆音、长时间漂移），而真机上只能靠耳朵判断。
# 在 JVM 上它是纯算术，可以逐帧断言。
#
# 做法和 run_jitter_test.sh 一样：把 AudioSend.java 里的 Resampler 类原样抽出来，
# 用 android.media.AudioRecord / Proto 的桩类编译（源码一行不改），再拿正弦波验：
#   ① 换出来的波形频率对不对（变调 = 频率不对）
#   ② 消耗输入的速度对不对（漂移 = 长时间跑会积累误差）
#   ③ 块边界有没有跳变（爆音 = 相邻样本差值异常）
#   ④ 单声道有没有正确复制成两声道
#
# 环境变量：JAVAC / JAVA 可以强制指定；不设就自动找。传 --keep 保留临时目录。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/../app/src/main/java/com/audiomixing/app/AudioSend.java"
EXTRACT="$DIR/extract_resampler.awk"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$SRC" ]     || die "找不到被测文件 $SRC"
[ -f "$EXTRACT" ] || die "找不到 $EXTRACT"

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
[ -n "$JAVA" ] || die "找不到 java，请设 JAVA 或 JAVA_HOME"

WORK="${TMPDIR:-/tmp}/am-resampler-test.$$"
mkdir -p "$WORK/out" "$WORK/src/android/media" "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

awk -f "$EXTRACT" "$SRC" > "$WORK/resampler.part" || die "抽取脚本执行失败"
LINES=$(wc -l < "$WORK/resampler.part" | tr -d ' ')
grep -q "int fill(" "$WORK/resampler.part" || die "抽到的代码里没有 fill()，AudioSend.java 的结构可能变了"

# --- 桩：android.media.AudioRecord（只实现 Resampler 用到的那一个方法）------
cat > "$WORK/src/android/media/AudioRecord.java" <<'EOF'
package android.media;

/** 测试桩：把"采集"变成探针塞进来的正弦波发生器。 */
public class AudioRecord {
    public static final int ERROR_INVALID_OPERATION = -3;

    /** 由探针设置的数据来源。 */
    public interface Source {
        int read(short[] buf, int off, int len);
    }

    public static volatile Source source;

    public int read(short[] audioData, int offsetInShorts, int sizeInShorts) {
        Source s = source;
        if (s == null) {
            return 0;
        }
        return s.read(audioData, offsetInShorts, sizeInShorts);
    }
}
EOF

# --- 桩：Proto 的两个常量 ------------------------------------------------
cat > "$WORK/src/com/audiomixing/app/Proto.java" <<'EOF'
package com.audiomixing.app;

public final class Proto {
    public static final int SAMPLE_RATE = 48000;
    public static final int FRAME_SAMPLES = 480;
}
EOF

{
    echo "package com.audiomixing.app;"
    echo
    echo "import android.media.AudioRecord;"
    echo "import java.util.Arrays;"
    echo
    echo "public final class ResamplerUnderTest {"
    # 抽出来的是 private static final class，包一层外壳就能直接编译
    cat "$WORK/resampler.part"
    echo "    public static Object make(int rate, int ch) { return new Resampler(rate, ch, 480); }"
    echo "    public static int fill(Object o, android.media.AudioRecord r, byte[] out) {"
    echo "        return ((Resampler) o).fill(r, out, 480);"
    echo "    }"
    echo "}"
} > "$WORK/src/com/audiomixing/app/ResamplerUnderTest.java"

echo "被测代码：$SRC"
echo "抽取结果：$LINES 行（Resampler 类，源码一字未改，只包了一层可调用的外壳）"
echo "JDK     ：$JAVAC"
echo

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" \
        "$WORK/src/android/media/AudioRecord.java" \
        "$WORK/src/com/audiomixing/app/Proto.java" \
        "$WORK/src/com/audiomixing/app/ResamplerUnderTest.java" \
        "$DIR/ResamplerProbe.java"; then
    die "编译失败（Resampler 的接口变了？加 --keep 看生成的源码）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" ResamplerProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 重采样离线测试通过 =="
else
    echo "== 重采样离线测试失败（退出码 $RC）=="
fi
exit "$RC"
