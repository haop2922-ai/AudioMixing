"""Drive the panel's own rings + the ball's centre readout with synthetic data.

大圆球是三层同心圆（2026-10-16 用户原话：「我全都要。电平是声音大小。然后外面套一圈蓝牙，
再套一圈WiFi。点对点本质还是WiFi」）：
    内圈 = 声音大小、中圈 = 蓝牙链路**状态**、外圈 = Wi-Fi 链路质量（点对点并入外圈）。

★ 内圈为什么只认 ``s.output``、不再认 ``s.local``（2026-10-16 用户真机：「手机播放声音
  这个电平没有反应」）：手机走蓝牙进来时声音是 **Windows 直接播放**的，本机直通那一路
  根本没有数据（实机 /api/status 里 local.note 正是"音源就是输出设备本身，这里不再重复
  处理"），旧写法只看 local.peak，于是永远是 0。现在改成 ``s.output.peak`` —— 它来自
  WASAPI 的峰值表，直读**当前输出设备**，不采集、不播放、不动系统音量。
  本工具特意**反过来测一遍**：output 有值而 local 没值时圈要画出来；output 是 0 而 local
  有值时圈必须空着 —— 这样才能挡住"又偷偷回去读 local"。

★ 圆心为什么改了（用户（图1）：「这些字全删了。只留信号强度和声音大小百分比
  （三个数据采用相对应环的颜色。3秒切换一次）」）：原来写着「Wi-Fi —」「蓝牙 已连接」
  「本机播放电平 %」「空闲：没有声音在流」，现在一个字都不留 —— 圆心每 3 秒在三个读数
  之间轮换，各用自己那一圈的颜色。这个工具把这三格依次切出来，验证值对、颜色对。

这几圈是面板对"哪条链路在变差"的唯一答案，所以"圈画不出来 / 读数不更新"是最危险的静默
故障 —— 服务端数据明明在，页面上却是空的。这个工具把真实页面脚本抽出来，在极小的 DOM 桩
里跑，喂一份 /api/status 形状的数据，再看三圈各自画了多少、圆心写成了什么。

    python tools/check_quality_curves.py
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "audiomixing" / "ui" / "index.html"

HARNESS = r"""
const fs = require('fs');
const path = process.argv[2];
const page = fs.readFileSync(path, 'utf8');

// --- a DOM stub just rich enough for the page script -------------------------
const written = {};
const attrs = {};
function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', value: '', checked: false,
    style: {}, dataset: {}, children: [],
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    addEventListener(){}, removeEventListener(){}, appendChild(c){ this.children.push(c); return c; },
    removeChild(){}, insertBefore(){}, querySelector(){ return null; },
    querySelectorAll(){ return []; }, closest(){ return null; },
    getBoundingClientRect(){ return { top:0, left:0, width:300, height:40 }; },
    // ★ 圆环靠 setAttribute('stroke-dashoffset', …) 画出来 —— 桩必须真的记下来，
    //   否则"圈根本没画"和"圈画对了"在这个工具里长得一模一样。
    setAttribute(n, v){ (attrs[id] = attrs[id] || {})[n] = String(v); },
    getAttribute(n){ return (attrs[id] || {})[n] === undefined ? null : (attrs[id] || {})[n]; },
    focus(){}, click(){},
  };
  Object.defineProperty(el, 'innerHTML', {
    get(){ return written[id] || ''; },
    set(v){ written[id] = String(v); },
  });
  return el;
}
const elements = {};
global.document = {
  documentElement: makeEl('html'),
  body: makeEl('body'),
  getElementById(id){ return elements[id] || (elements[id] = makeEl(id)); },
  querySelector(){ return null; },
  querySelectorAll(){ return []; },
  createElement(tag){ return makeEl('made-' + tag); },
  addEventListener(){},
};
global.window = { addEventListener(){}, location:{ href:'http://127.0.0.1:17800/' }, setTimeout, clearTimeout };
// The page starts its refresh timers on load; they would keep node alive forever and
// have nothing to do with what this checks. 圆心轮换也走 setInterval，这里靠手动切格验证。
global.setInterval = () => 0;
global.setTimeout = (fn) => 0;
global.clearInterval = () => {};
global.clearTimeout = () => {};
global.localStorage = { getItem(){ return null; }, setItem(){}, removeItem(){} };
global.fetch = async () => ({ ok:true, json: async () => ({}) });
global.WebSocket = function(){ this.close = function(){}; this.send = function(){}; };
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.alert = () => {};
global.navigator = { userAgent: 'node' };

const scripts = [];
const re = /<script>([\s\S]*?)<\/script>/g;
let m;
while ((m = re.exec(page)) !== null) scripts.push(m[1]);
const js = scripts.join('\n;\n');

// The page keeps everything in top-level consts, which direct eval scopes away, so the
// probe is appended to the page script and runs inside the very same scope.
const probe = `
function readEl(id){ const el = elements[id]; return el ? el.textContent : null; }
function attrOf(id, name){ return (attrs[id] || {})[name]; }
function colorOf(id){ const el = elements[id]; return (el && el.style) ? el.style.color : null; }

// ---- 外圈 Wi-Fi 用真实质量分；中圈蓝牙用真实链路状态（见 state.btLink）---------
// ★ 这份 local / output 就是真机现场：本机直通根本没在跑，声音由 Windows 直接播放。
state.status = {
  quality: { wifi: { latest: { score: 88 } }, p2p: { latest: null } },
  receiver: { streams: [] },
  local: { enabled: true, peak: 0, frames: 0 },
  output: { peak: 0.5, source: 'meter', measured: true, meter_on: true, device: '耳机' },
};
state.btLink = 'connected';
renderQualityRings(state.status);
renderLevelMeter(state.status);
ringCellIndex = 0; renderRingCore();
const snap1 = {
  wifiOff: attrOf('wifiProg', 'stroke-dashoffset'),
  btOff:   attrOf('btProg', 'stroke-dashoffset'),
  lvlOff:  attrOf('ringProg', 'stroke-dashoffset'),
  cells: { wifi: ringCells.wifi, bt: ringCells.bt, vol: ringCells.vol },
  core: readEl('ringMetric'),
  coreColor: colorOf('ringMetric'),
};
// ---- 三个读数每 3 秒轮换：切到第 2 / 第 3 格，圆心换值也换色 -------------------
ringCellIndex = 1; renderRingCore();
const snap2 = { core: readEl('ringMetric'), coreColor: colorOf('ringMetric') };
ringCellIndex = 2; renderRingCore();
const snap3 = { core: readEl('ringMetric'), coreColor: colorOf('ringMetric') };
// ---- 蓝牙只剩通话通路 → 小半圈 + 圆心轮到时写「只有通话」 ---------------------
state.btLink = 'hfp';
renderQualityRings(state.status);
ringCellIndex = 1; renderRingCore();
const snap4 = { btOff: attrOf('btProg', 'stroke-dashoffset'), core: readEl('ringMetric') };
// ---- 只有点对点有数据时，外圈（Wi-Fi 那一圈）必须照样有读数 -------------------
state.btLink = 'off';
state.status.quality = { wifi: { latest: null }, p2p: { latest: { score: 40 } } };
renderQualityRings(state.status);
const snap5 = {
  wifiOff: attrOf('wifiProg', 'stroke-dashoffset'),
  cells: { wifi: ringCells.wifi, bt: ringCells.bt },
};
// ---- 反向一测：output 是 0、local 有值 → 内圈必须空着（不许再回去读 local）------
// levelSmooth 是显示自己的衰减记忆（避免数字乱跳），这里先清掉，读到的才是这一帧的输入。
state.status.local = { enabled: true, peak: 0.9, frames: 48000 };
state.status.output = { peak: 0, source: 'none', measured: true, meter_on: true };
levelSmooth = 0;
renderLevelMeter(state.status);
const snap6 = { lvlOff: attrOf('ringProg', 'stroke-dashoffset'), vol: ringCells.vol };

global.__curveOut = { snap1, snap2, snap3, snap4, snap5, snap6 };
`;
eval(js + '\n;\n' + probe);
console.log(JSON.stringify(global.__curveOut || {}));
"""


def _near(value, expected: float, tol: float = 1.0) -> bool:
    try:
        return abs(float(value) - expected) < tol
    except (TypeError, ValueError):
        return False


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("找不到 node，无法执行页面脚本")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        harness = pathlib.Path(tmp) / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run([node, str(harness), str(HTML)],
                              capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        print("FAIL 页面脚本执行失败:")
        print((proc.stderr or proc.stdout).strip()[:2000])
        return 1
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not lines:
        print("FAIL 没有拿到结果:")
        print(proc.stdout[:1000])
        return 1
    data = json.loads(lines[-1])
    snap1 = data.get("snap1") or {}
    snap2 = data.get("snap2") or {}
    snap3 = data.get("snap3") or {}
    snap4 = data.get("snap4") or {}
    snap5 = data.get("snap5") or {}
    snap6 = data.get("snap6") or {}

    problems: list[str] = []

    def show(label: str, ok: bool, detail: str) -> None:
        print("[%s] %-26s %s" % ("PASS" if ok else "FAIL", label, detail))
        if not ok:
            problems.append(label)

    # 外圈 Wi-Fi：88 分 → 弧长应是周长的 (1-0.88)。半径 86 → 周长 540.4。
    show("外圈 Wi-Fi 画出弧", _near(snap1.get("wifiOff"), 540.4 * (1 - 0.88)),
         "dashoffset=%s（期望 %.1f）" % (snap1.get("wifiOff"), 540.4 * (1 - 0.88)))
    # 中圈 蓝牙：正在收手机的声音 → 满圈（offset 0）。半径 72 → 周长 452.4。
    show("中圈 蓝牙 满圈", _near(snap1.get("btOff"), 0.0),
         "dashoffset=%s（期望 0.0）" % (snap1.get("btOff"),))
    # 内圈 声音大小：0.5 → 弧长 364.4×(1-0.5×0.92)（半径 58 → 周长 364.4）。
    # 注意这里的 local.peak 是 **0**（真机现场）—— 圈还能画出来，就证明它认的是 output。
    lvl = snap1.get("lvlOff")
    show("内圈 按输出电平画弧", _near(lvl, 364.4 * (1 - 0.5 * 0.92), 2.0),
         "dashoffset=%s（期望约 %.1f；local.peak=0 也能画出来）" % (lvl, 364.4 * (1 - 0.5 * 0.92)))
    # 三个读数：Wi-Fi 分 / 蓝牙状态词 / 声音百分比 —— 就是用户要的那三个。
    cells = snap1.get("cells") or {}
    show("圆心三格备好", str(cells.get("wifi")) == "88" and str(cells.get("bt")) == "已连接"
         and str(cells.get("vol")) == "50%",
         "wifi=%s 蓝牙=%s 声音=%s" % (cells.get("wifi"), cells.get("bt"), cells.get("vol")))
    # 轮换：依次是 Wi-Fi（蓝）→ 蓝牙（紫）→ 声音（绿）。
    show("圆心轮换换值换色",
         str(snap1.get("core")) == "88" and snap1.get("coreColor") == "#38bdf8"
         and str(snap2.get("core")) == "已连接" and snap2.get("coreColor") == "#a78bfa"
         and str(snap3.get("core")) == "50%" and snap3.get("coreColor") == "#34d399",
         "依次 [%s %s] [%s %s] [%s %s]" % (snap1.get("core"), snap1.get("coreColor"),
                                          snap2.get("core"), snap2.get("coreColor"),
                                          snap3.get("core"), snap3.get("coreColor")))
    # 只剩通话通路：小半圈 + 状态词（不是假分数）。
    show("蓝牙只剩通话 → 小半圈",
         _near(snap4.get("btOff"), 452.4 * (1 - 0.35)) and str(snap4.get("core")) == "只有通话",
         "dashoffset=%s 圆心=%s" % (snap4.get("btOff"), snap4.get("core")))
    # 只有点对点有读数时（点对点本质还是 Wi-Fi），外圈照样要动。
    cells5 = snap5.get("cells") or {}
    show("点对点并入外圈", _near(snap5.get("wifiOff"), 540.4 * (1 - 0.40)) and str(cells5.get("wifi")) == "40",
         "dashoffset=%s 分数=%s" % (snap5.get("wifiOff"), cells5.get("wifi")))
    show("没连上写状态词", str(cells5.get("bt")) == "未连接",
         "蓝牙=%s" % (cells5.get("bt"),))
    # 反向一测：output 是 0 而 local 有值 → 内圈必须空着（挡住"又偷偷回去读 local"）。
    show("内圈不再偷读 local",
         _near(snap6.get("lvlOff"), 364.4, 0.5) and str(snap6.get("vol")) == "0%",
         "dashoffset=%s（期望 364.4 空圈） 圆心=%s" % (snap6.get("lvlOff"), snap6.get("vol")))

    # ---- 圈名/说明那四行字挂着的元素必须真的没了（用户：「这些字全删了」）--------
    # 只查标识符，不查那两行文案本身 —— 注释里引用被删掉的文案是**说明**，
    # 真正会漏回来的是"某个还在往这些元素里写字的代码路径"，而那必然带着这些 id。
    html = HTML.read_text(encoding="utf-8")
    leftovers = [x for x in ("wifiScore", "btScore", "ringUnit", "ringState") if x in html]
    show("被删的元素没回来", not leftovers, "仍然存在: %s" % (leftovers or "无"))

    if problems:
        print("\n问题: " + "; ".join(problems))
        return 1
    print("\nRESULT: PASS 三层圆环按真实数据画出来；圆心三个读数每 3 秒轮换、各用自己那一圈的颜色")
    return 0


if __name__ == "__main__":
    sys.exit(main())
