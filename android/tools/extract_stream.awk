# 从 AudioRecv.java 里原样抽出 Stream 类（抖动缓冲）的源码文本。
#
# 为什么要"抽"而不是"抄一份"：抄一份的话，生产代码改了、测试还在测旧的，
# 测试就变成自我安慰。这里按大括号配平把真文件里的那段代码抠出来编译，
# 测的永远是仓库里当前那份实现。
#
# 触发行：private final class Stream {
# 结束条件：括号深度回到 0（即该类的收尾大括号）
/private final class Stream \{/ { grab = 1 }
grab {
    print
    # 复制一份再数括号，避免 gsub 改写原行
    line = $0
    o = gsub(/\{/, "{", line)
    line = $0
    c = gsub(/\}/, "}", line)
    depth += o - c
    if (depth == 0) { exit }
}
