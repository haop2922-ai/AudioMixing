"""UI 残留门禁：把"肉眼能看出来的错误"变成可执行的断言。

背景（2026-11-06 用户图 1）：
  面板「局域网里的设备」里出现了一张「自启动复验」卡片 —— 那是上一轮验证
  "开机自启动"时随手起的测试节点名，写进了真实 config.json；同一张卡还
  标着「本机」，并在信号一栏写「Wi-Fi 还没测到」。单元测试全绿，因为没有任何
  用例真的去渲染页面、也没有任何用例检查"名字是不是测试残留"。

这个脚本检查四类东西：
  1. 配置里的本机名 / 手动设备名 / 路由标签，不得是测试或占位命名；
  2. 页面源码里不得出现测试节点名和未采集占位文案；
  3. renderDeviceCards() 必须把本机（local）从「局域网里的设备」列表剔除；
  4. 可选：对着一个正在跑的实例查 /api/peers，确认本机身份（node_id /
     device_id / 全部别名 / 本机地址）没有混进对端列表，名字也没有测试残留。

    python tools/check_ui_residue.py
    python tools/check_ui_residue.py --config 某个config.json
    python tools/check_ui_residue.py --url http://127.0.0.1:17800
    python tools/check_ui_residue.py --allow "我的测试机"    # 白名单（可重复）

退出码 0 = PASS，1 = FAIL（和 check_apk.py 的约定一致）。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - Python < 3.7 或非常规 stdout
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "audiomixing" / "ui"
UI_FILES = ("index.html", "app.html", "listen.html", "mic.html", "phone.html")

# 明确的测试 / 占位命名。中文直接子串匹配；英文按"词"匹配（避免 test-tone
# 这类正当用法误报，见下面的 WHITELIST_TOKENS）。
BANNED_SUBSTRINGS = ("自启动复验",)
BANNED_TOKENS = {"test", "probe", "selftest", "demo", "example",
                 "self-node", "selfnode", "peer-1", "peer1"}
#: 这些英文词在本项目里是**功能名**，不是测试残留。
WHITELIST_TOKENS = {"test-tone", "testtone"}
#: 页面源码里只查有辨识度的词（"test" 在源码里是正当功能名/注释，见上）。
SOURCE_BANNED_TOKENS = BANNED_TOKENS - WHITELIST_TOKENS - {"test"}
#: 未采集 / 占位文案：不允许出现在页面源码里（发现就该补采集或改成明确说明）。
BANNED_UI_TEXT = ("还没测到",)

#: ★ v2.42：多耳机页那张「一台电脑，多个耳机」配置卡已整块删除。
#: 这些 id 只要还在页面或脚本里，就说明删漏了（不允许折叠/隐藏版本）。
REMOVED_MULTI_IDS = ("moList", "moApply", "moBody", "moSummary")
#: 这四样控件挪进了画布卡里的 .motools 工具条，必须各出现且只出现一次。
MULTI_TOOL_IDS = ("moStop", "moRefresh", "moLockDevice", "moRestore",
                  "moStatus", "moLockInfo")


def check_bt_card_buttons(text: str) -> list[str]:
    """★ v2.42：蓝牙卡片只允许「打开 / 关闭 / 隐藏」三颗，旧入口必须真删。
    ★ 2.42.1：卡片下方不再有黄字提示；「打开」默认黑色，成功后才是 primary（蓝色）。"""
    problems: list[str] = []
    for gone in ("btToggle(", "btAuto(", "btAlways(", "btAutoList",
                 ">${map[2] ? '断开' : '连接'}</button>",
                 "const hint = d.link === 'hfp'", "${hint}"):
        if gone in text:
            problems.append("蓝牙卡片还残留已删除的按钮/脚本/黄字提示：%s" % gone)
    for keep in ("async function btOpen(", "async function btClose(",
                 'onclick="btOpen(${i})"', 'onclick="btClose(${i})"',
                 'onclick="btHide(${i})"'):
        if keep not in text:
            problems.append("蓝牙卡片缺少 %s" % keep)
    if text.count('onclick="btOpen(${i})"') != 1:
        problems.append("「打开」按钮必须恰好 1 颗")
    if text.count('onclick="btClose(${i})"') != 1:
        problems.append("「关闭」按钮必须恰好 1 颗")
    # ★ 2.42.1：初始不能是蓝的（primary），必须由 openOk 决定。
    if "class=\"tiny ${openOk ? 'primary' : ''}\" onclick=\"btOpen" not in text:
        problems.append("「打开」按钮默认必须是黑色，只有成功后才变蓝（缺 openOk 条件）")
    if "btOpenOk" not in text:
        problems.append("缺少 btOpenOk：无法证明「打开」成功后按钮才变蓝")
    # ★ 2.42.2（第 3 条）：简洁档不许再有整段说明/黄字块 —— .btrules 必须彻底删除。
    for gone in ('class="btrules"', ".btrules{", "只剩两颗按钮",
                 "一个蓝牙适配器同一时刻只能接一台手机", "adapterNote"):
        if gone in text:
            problems.append("简洁档还残留整段说明/黄字块：%s" % gone)
    return problems


def check_multi_sources(text: str) -> list[str]:
    """★ 2.42.1：多耳机必须支持"一路多音源 + 空音源静音"，且复杂档只列工作中的输出。"""
    problems: list[str] = []
    for token in ("function sourcesOf(", "function sourceKey(", "sources: []",
                  "sources: sourcesOf(cfg).map", "renderPeerCards(wrap)"):
        if token not in text:
            problems.append("index.html 缺少 2.42.1 多音源/对端卡片实现：%s" % token)
    if "未连音源 · 这一路静音" not in text:
        problems.append("index.html 没有写清「没有音源时这一路静音」")
    if "已回落桌面混音" in text:
        problems.append("index.html 还残留「已回落桌面混音」文案（2.42.1 禁止回落）")
    # 复杂档输出列必须过滤到"工作中"的设备。
    if "isPassthrough(name) || isChosen(name) || remSink.has(name)" not in text:
        problems.append("复杂档「输出音频设备」列没有过滤未启用设备")
    # ★ 2.42.2（第 4 条）：对端 audio 卡只送不收。
    start = text.find("function renderPeerCards")
    if start < 0:
        problems.append("index.html 找不到 renderPeerCards()")
    else:
        nxt = text.find("\n        function ", start + 10)
        body = text[start:nxt if nxt > start else len(text)]
        for gone in ("收听它的", "停止收听", "to: ['self']", "peerRoute(peer, 'in')",
                     "送声音到它", "本机扬声器"):
            if gone in body:
                problems.append("对端 audio 卡还残留「本机扬声器/本机收听它」的入口或文案：%s" % gone)
        # ★ 2.42.3（第 2 条）：简洁档的对端卡只留设备名 + 在线/送声状态；
        #   解释块必须包在 `if (!simpleMode)` 里，简洁档一个字都不加。
        if "if (!simpleMode)" not in body:
            problems.append("对端 audio 卡的说明块没有只在详细档出现（if (!simpleMode)）")
        if "card.onclick" not in body:
            problems.append("对端 audio 卡缺少「点卡片＝送/停」的入口")
    return problems


def check_multi_card_removed(text: str) -> list[str]:
    """证明「一台电脑，多个耳机」整卡已删、四样有用控件只在画布工具条里。"""
    problems: list[str] = []
    for token in REMOVED_MULTI_IDS:
        if (re.search(r'id\s*=\s*"%s"' % token, text)
                or re.search(r"getElementById\(\s*['\"]%s['\"]\s*\)" % token, text)):
            problems.append("index.html 还残留已删除的多耳机配置卡节点/脚本 %s" % token)
    if re.search(r'data-card\s*=\s*"multiout"', text):
        problems.append('index.html 还残留 data-card="multiout"（那张配置卡应整块删除）')
    for token in MULTI_TOOL_IDS:
        count = len(re.findall(r'id\s*=\s*"%s"' % token, text))
        if count != 1:
            problems.append("工具条节点 %s 出现 %d 次（应恰好 1 次）" % (token, count))
    card = re.search(r'<section[^>]*data-card="multiview"[\s\S]*?</section>', text)
    if not card:
        problems.append('index.html 找不到画布卡 data-card="multiview"')
    else:
        block = card.group(0)
        if 'class="motools"' not in block:
            problems.append("画布卡里没有 .motools 工具条")
        for token in MULTI_TOOL_IDS:
            if 'id="%s"' % token not in block:
                problems.append("工具条节点 %s 不在画布卡里" % token)
        if block.find('class="motools"') > block.find('id="moCards"'):
            problems.append("工具条必须在卡片墙/画布之前")
    for dead in (".mohead .moTitle", ".mohead #moToggle"):
        if dead in text:
            problems.append("index.html 还留着死样式 %s" % dead)
    if re.search(r">\s*应用\s*<", text):
        problems.append("index.html 还有「应用」按钮（已改成即时生效）")
    return problems


def _default_config() -> pathlib.Path | None:
    home = os.environ.get("AUDIOMIXING_HOME")
    if home:
        p = pathlib.Path(home) / "config.json"
    else:
        appdata = os.environ.get("APPDATA")
        p = pathlib.Path(appdata) / "AudioMixing" / "config.json" if appdata else None
    return p if p and p.is_file() else None


def _tokens(text: str) -> set[str]:
    low = str(text or "").strip().lower()
    low = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", low)
    return {t for t in low.split() if t}


def bad_name(name: str, allow: set[str]) -> str | None:
    """返回不合格的原因；合格返回 None。"""
    text = " ".join(str(name or "").split())
    if not text or text in allow:
        return None
    for sub in BANNED_SUBSTRINGS:
        if sub in text:
            return "含测试节点名「%s」" % sub
    toks = _tokens(text)
    hit = (toks & BANNED_TOKENS) - WHITELIST_TOKENS
    if hit:
        return "含测试/占位词 %s" % "、".join(sorted(hit))
    # "peer-1" / "self-node" 会被分词成两个词，按原顺序的短语再查一遍。
    low = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text.lower()).strip()
    for pair in ("peer 1", "self node"):
        if pair in low:
            return "含测试节点名「%s」" % pair
    return None


def check_config(path: pathlib.Path, allow: set[str]) -> list[str]:
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ["读不了配置 %s: %s" % (path, exc)]
    print("配置：%s" % path)
    why = bad_name(data.get("name"), allow)
    print("  本机名：%r%s" % (data.get("name"), "" if not why else "  ✗ " + why))
    if why:
        problems.append("本机名是测试残留：%s（%s）" % (data.get("name"), why))
    for i, item in enumerate(data.get("manual_peers") or []):
        if isinstance(item, dict):
            for field in ("name", "host"):
                why = bad_name(item.get(field), allow)
                if why:
                    problems.append("手动设备 #%d %s=%r：%s" % (i, field, item.get(field), why))
    for route in data.get("routes") or []:
        if isinstance(route, dict):
            why = bad_name(route.get("label"), allow)
            if why:
                problems.append("路由 %s 的标签 %r：%s" % (route.get("id"), route.get("label"), why))
    return problems


def check_ui_source(allow: set[str]) -> list[str]:
    problems: list[str] = []
    print("页面源码：%s" % UI_DIR)
    for name in UI_FILES:
        path = UI_DIR / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for sub in BANNED_SUBSTRINGS + BANNED_UI_TEXT:
            if sub in text:
                problems.append("%s 含禁用文案「%s」" % (path.name, sub))
        # 源码里 "test" 是大量正当用法（test-tone / 测试音箱 / 单元测试注释），
        # 所以源码只查**有辨识度**的测试名；"test" 这个词留给配置与运行时设备名去查。
        for tok in sorted(SOURCE_BANNED_TOKENS):
            if re.search(r"(?i)(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(tok), text):
                problems.append("%s 含测试/占位词「%s」" % (path.name, tok))
        if name == "index.html":
            problems += check_multi_card_removed(text)
            problems += check_bt_card_buttons(text)
            problems += check_multi_sources(text)
            m = re.search(r"function\s+renderDeviceCards\s*\(\)\s*\{([\s\S]*?)\n\}", text)
            if not m:
                problems.append("index.html 找不到 renderDeviceCards()")
            else:
                body = m.group(1)
                if not re.search(r"allNodes\(\)\s*\.filter\([^)]*local", body):
                    problems.append("renderDeviceCards() 没有把本机(local)剔除出设备卡片列表")
    return problems


def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def check_runtime(url: str, allow: set[str]) -> list[str]:
    problems: list[str] = []
    base = url.rstrip("/")
    print("运行实例：%s" % base)
    try:
        report = _get_json(base + "/api/peers")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return ["连不上 %s: %s" % (base, exc)]
    me = report.get("self") or {}
    self_ids = {str(me.get(k) or "") for k in ("id", "device_id", "instance_id", "host")}
    self_ids.update(str(x) for x in (me.get("alias_ids") or []))
    self_ids.discard("")
    why = bad_name(me.get("name"), allow)
    print("  本机名：%r%s" % (me.get("name"), "" if not why else "  ✗ " + why))
    if why:
        problems.append("运行时本机名是测试残留：%s（%s）" % (me.get("name"), why))
    peers = report.get("peers") or []
    print("  对端数量：%d" % len(peers))
    for peer in peers:
        pname = peer.get("name")
        why = bad_name(pname, allow)
        if why:
            problems.append("对端 %r 是测试残留：%s" % (pname, why))
        ids = {str(peer.get(k) or "") for k in ("node_id", "device_id", "host")}
        ids.update(str(x) for x in (peer.get("alias_ids") or []))
        ids.update(str(x) for x in (peer.get("hosts") or []))
        overlap = (ids & self_ids) - {""}
        if overlap:
            problems.append("对端 %r 带着本机身份 %s，混进了局域网设备列表"
                            % (pname, "、".join(sorted(overlap))))
    return problems


def main() -> int:
    args = sys.argv[1:]
    allow: set[str] = set()
    config: pathlib.Path | None = None
    url = ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--config" and i + 1 < len(args):
            config = pathlib.Path(args[i + 1])
            i += 2
        elif a == "--url" and i + 1 < len(args):
            url = args[i + 1]
            i += 2
        elif a == "--allow" and i + 1 < len(args):
            allow.add(" ".join(args[i + 1].split()))
            i += 2
        else:
            print("不认识的参数：%s" % a)
            return 2

    if config is None:
        config = _default_config()
    problems: list[str] = []
    if config is not None:
        problems += check_config(config, allow)
    else:
        print("配置：没有找到 config.json（用 --config 指定；跳过这一项）")
    problems += check_ui_source(allow)
    if url:
        problems += check_runtime(url, allow)

    print("\n== 结论 ==")
    if problems:
        for p in problems:
            print("   ✗ %s" % p)
        print("   RESULT: FAIL")
        return 1
    print("   RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
