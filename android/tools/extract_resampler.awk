# 从 AudioSend.java 里把 Resampler 类原样抽出来（和 run_jitter_test.sh 抽 Stream 是同一套做法）。
# Resampler 是文件里最后一个类，所以：从 "private static final class Resampler {" 开始，
# 一直到文件末尾，再把最后那行关闭 AudioSend 的 "}" 去掉。
/^    private static final class Resampler \{/ { on = 1 }
on { lines[++n] = $0 }
END {
    if (n < 20) { print "只抽到 " n " 行，AudioSend.java 的结构可能变了" > "/dev/stderr"; exit 2 }
    # 最后一行是 AudioSend 自己的结尾大括号，丢掉
    for (i = 1; i < n; i++) print lines[i]
}
