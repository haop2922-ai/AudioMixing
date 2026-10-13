#!/usr/bin/env bash
#
# 信标 / 节点身份离线测试 —— 一条命令复跑，不需要安卓设备、不需要 Gradle、不需要联网。
#
#     bash android/tools/run_beacon_test.sh
#
# 为什么值得存在（task-11 第 ① 条真机反馈）："手机重启一次，电脑的路由里就多一个重名设备"。
# 现场证据（电脑端 /api/status）：同一台手机、同一个 192.168.0.101:47831，挂了三条记录，
# 三个不同的 node_id —— 因为手机端原来每次启动都 UUID.randomUUID() 新建一个。
# 这件事在真机上只能靠"数电脑上的设备条数"发现，在 JVM 上一眼就能断言：
#   ① 同一个 Context 建两次 Discovery（= 重启一次），id 必须一模一样；
#   ② 我们要发出去的信标 JSON 里，id 必须非空。
#
# 做法和 run_jitter_test.sh 一样：Discovery.java 原样编译（不抽类、不改一个字），
# 只补 android.* 和 org.json 的桩类。
#
# 环境变量：JAVAC / JAVA 可以强制指定。传 --keep 保留临时目录。

set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$DIR/../app/src/main/java/com/audiomixing/app"

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

die() { echo "错误：$*" >&2; exit 2; }

[ -f "$APP/Discovery.java" ] || die "找不到 $APP/Discovery.java"
[ -f "$DIR/BeaconProbe.java" ] || die "找不到 $DIR/BeaconProbe.java"

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

WORK="${TMPDIR:-/tmp}/am-beacon-test.$$"
mkdir -p "$WORK/out" "$WORK/src/android/content" "$WORK/src/android/net/wifi" \
         "$WORK/src/android/os" "$WORK/src/android/util" "$WORK/src/org/json" \
         "$WORK/src/com/audiomixing/app" || die "建不了临时目录 $WORK"
cleanup() { [ "$KEEP" = 1 ] || rm -rf "$WORK"; }
trap cleanup EXIT

S="$WORK/src"

# ─── 桩：android.content（Context + SharedPreferences，用内存实现模拟"手机存储"）───
cat > "$S/android/content/SharedPreferences.java" <<'EOF'
package android.content;

import java.util.Map;

/** 桩：内存版 SharedPreferences（同一份 map = 同一台手机的存储）。 */
public class SharedPreferences {
    private final Map<String, String> store;

    public SharedPreferences(Map<String, String> store) {
        this.store = store;
    }

    public String getString(String key, String def) {
        String v = store.get(key);
        return v == null ? def : v;
    }

    public Editor edit() {
        return new Editor(store);
    }

    public static class Editor {
        private final Map<String, String> store;

        Editor(Map<String, String> store) {
            this.store = store;
        }

        public Editor putString(String key, String value) {
            store.put(key, value);
            return this;
        }

        public void apply() {
            // 内存版：立即生效
        }

        public boolean commit() {
            return true;
        }
    }
}
EOF

cat > "$S/android/content/Context.java" <<'EOF'
package android.content;

import java.util.HashMap;
import java.util.Map;

/**
 * 桩：Context。每个实例自带一份"存储"，所以：
 *   同一个 Context 反复 getSharedPreferences = 同一台手机重启多次；
 *   不同 Context = 换了一台手机（存储是空的）。
 */
public class Context {
    public static final int MODE_PRIVATE = 0;
    public static final String WIFI_SERVICE = "wifi";

    private final Map<String, Map<String, String>> prefs = new HashMap<>();

    public Context getApplicationContext() {
        return this;
    }

    public SharedPreferences getSharedPreferences(String name, int mode) {
        Map<String, String> store = prefs.get(name);
        if (store == null) {
            store = new HashMap<>();
            prefs.put(name, store);
        }
        return new SharedPreferences(store);
    }

    public Object getSystemService(String name) {
        return null;   // 本测试不碰 WifiManager
    }
}
EOF

cat > "$S/android/net/wifi/WifiManager.java" <<'EOF'
package android.net.wifi;
public class WifiManager {
    public static final int WIFI_MODE_FULL_LOW_LATENCY = 4, WIFI_MODE_FULL_HIGH_PERF = 1;
    public MulticastLock createMulticastLock(String tag) { return new MulticastLock(); }
    public static class MulticastLock {
        public void setReferenceCounted(boolean b) { }
        public void acquire() { }
        public void release() { }
        public boolean isHeld() { return false; }
    }
}
EOF

cat > "$S/android/os/Build.java" <<'EOF'
package android.os;
public class Build {
    public static final String MODEL = "M332BF";
    public static class VERSION {
        public static final String RELEASE = "14";
        public static int SDK_INT = 34;
    }
    public static class VERSION_CODES { public static final int O = 26, Q = 29; }
}
EOF

cat > "$S/android/os/SystemClock.java" <<'EOF'
package android.os;
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

# ─── 桩：org.json（只实现 Discovery 用到的那几个方法 + 真的序列化）───
cat > "$S/org/json/JSONArray.java" <<'EOF'
package org.json;
public class JSONArray {
    private final java.util.List<Object> list = new java.util.ArrayList<>();
    public JSONArray() { }
    public JSONArray put(Object v) { list.add(v); return this; }
    public int length() { return list.size(); }
    public JSONObject optJSONObject(int i) {
        Object v = i >= 0 && i < list.size() ? list.get(i) : null;
        return (v instanceof JSONObject) ? (JSONObject) v : null;
    }
    @Override public String toString() {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) { sb.append(','); }
            sb.append(JSONObject.value(list.get(i)));
        }
        return sb.append(']').toString();
    }
}
EOF

cat > "$S/org/json/JSONObject.java" <<'EOF'
package org.json;

/** 桩：够 Discovery 组信标用；字符串是真的按 JSON 规则转义的。 */
public class JSONObject {
    private final java.util.LinkedHashMap<String, Object> map = new java.util.LinkedHashMap<>();

    public JSONObject() { }

    public JSONObject(String text) {
        throw new UnsupportedOperationException("桩不支持解析（本测试只验证信标的组装）");
    }

    public JSONObject put(String key, Object value) { map.put(key, value); return this; }

    public int optInt(String key, int def) {
        Object v = map.get(key);
        return (v instanceof Number) ? ((Number) v).intValue() : def;
    }

    public String optString(String key, String def) {
        Object v = map.get(key);
        return v == null ? def : String.valueOf(v);
    }

    public boolean optBoolean(String key, boolean def) {
        Object v = map.get(key);
        return (v instanceof Boolean) ? ((Boolean) v) : def;
    }

    public JSONArray optJSONArray(String key) {
        Object v = map.get(key);
        return (v instanceof JSONArray) ? (JSONArray) v : null;
    }

    @Override public String toString() {
        StringBuilder sb = new StringBuilder("{");
        boolean first = true;
        for (java.util.Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) { sb.append(','); }
            first = false;
            sb.append(quote(e.getKey())).append(':').append(value(e.getValue()));
        }
        return sb.append('}').toString();
    }

    static String value(Object v) {
        if (v == null) { return "null"; }
        if (v instanceof Number || v instanceof Boolean) { return String.valueOf(v); }
        if (v instanceof JSONObject || v instanceof JSONArray) { return v.toString(); }
        return quote(String.valueOf(v));
    }

    private static String quote(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 2).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) { sb.append(String.format("\\u%04x", (int) c)); }
                    else { sb.append(c); }
            }
        }
        return sb.append('"').toString();
    }
}
EOF

echo "被测代码：$APP/Discovery.java（原样编译，配 android.* / org.json 桩类）"
echo "JDK     ：$JAVAC"
echo

cp "$APP/Discovery.java" "$APP/Proto.java" "$S/com/audiomixing/app/"
cp "$DIR/BeaconProbe.java" "$S/"

if ! "$JAVAC" -encoding UTF-8 -d "$WORK/out" $(find "$S" -name '*.java'); then
    die "编译失败（Discovery 用到了新的安卓 API？用完的桩要在本脚本里补上）"
fi

"$JAVA" -Dfile.encoding=UTF-8 -Dsun.stdout.encoding=UTF-8 -Dsun.stderr.encoding=UTF-8 \
        -cp "$WORK/out" BeaconProbe
RC=$?

echo
if [ "$RC" -eq 0 ]; then
    echo "== 信标 / 节点身份离线测试通过 =="
else
    echo "== 信标 / 节点身份离线测试失败（退出码 $RC）=="
fi
exit "$RC"
