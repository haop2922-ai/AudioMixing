"""Drive the panel's own chart code with synthetic data and report what it renders.

The curves are the panel's answer to "which link is misbehaving", so a chart that
silently stays empty is a serious bug - and one that is invisible from the server side,
where the data is provably present. This extracts the real page script, runs it against
a tiny DOM stub, hands `drawQuality()` a payload shaped like /api/status, and prints
what the two SVG elements and their captions ended up containing.

    python tools/check_quality_curves.py
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
HTML = ROOT / "wavelink" / "ui" / "index.html"

HARNESS = r"""
const fs = require('fs');
const path = process.argv[2];
const page = fs.readFileSync(path, 'utf8');

// --- a DOM stub just rich enough for the page script -------------------------
const written = {};
function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', value: '', checked: false,
    style: {}, dataset: {}, children: [],
    classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
    addEventListener(){}, removeEventListener(){}, appendChild(c){ this.children.push(c); return c; },
    removeChild(){}, insertBefore(){}, querySelector(){ return null; },
    querySelectorAll(){ return []; }, closest(){ return null; },
    getBoundingClientRect(){ return { top:0, left:0, width:300, height:40 }; },
    setAttribute(){}, getAttribute(){ return null; }, focus(){}, click(){},
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
// have nothing to do with what this checks.
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
state.status = {
  quality: {
    wifi: { latest: { score: 88, v: 21.5, unit:'ms', label:'m4',
                      detail:'块间隔 21.5ms · 最大 120ms · 断档 1 次' },
            history: [ {v:20.1,score:100}, {v:22.4,score:92}, {v:21.5,score:88} ] },
    bt:   { latest: { score: 100, v: 10.4, unit:'ms', label:'headset',
                      detail:'喂声卡间隔最大 10.4ms · 短写 0 · 写入最大 0.3ms' },
            history: [ {v:10.4,score:100}, {v:10.6,score:100} ] },
    p2p:  { latest: null, history: [] },
  },
};
drawQuality();
// The big chart on 本机状态: it must plot the link, and it must still draw something
// when there is only one sample (a bare baseline is better than an empty box).
state.status.inputs = [
  { path:'wifi', path_label:'Wi-Fi', name:'phone-a', state:'正在传入', score:100,
    detail:'间隔中位 21.2ms', metric:'到达间隔 (ms)', ideal:20,
    history:[20.1, 21.4, 20.2, 22.0] },
  { path:'bluetooth', path_label:'蓝牙', name:'电话A', state:'正在接收手机声音', score:null,
    detail:'蓝牙链路不可测', metric:'链路状态 (满格=正在接收)', ideal:100,
    history:[100, 100, 40, 100] },
];
renderInputs();
function probeReport(id) {
  const html = (typeof written !== 'undefined' && written[id]) || '';
  return { id, chars: html.length, hasPolyline: html.indexOf('<polyline') >= 0,
           hasDashed: html.indexOf('stroke-dasharray') >= 0 };
}
global.__curveOut = {
  qWifi: probeReport('qWifi'), qWifiText: (typeof written !== 'undefined' && written['qWifiText']) || '',
  qBt: probeReport('qBt'), qBtText: (typeof written !== 'undefined' && written['qBtText']) || '',
  qP2p: probeReport('qP2p'), qP2pText: (typeof written !== 'undefined' && written['qP2pText']) || '',
  in0: probeReport('inCh0'), in1: probeReport('inCh1'),
};
`;
eval(js + '\n;\n' + probe);
const out = global.__curveOut || {};
console.log(JSON.stringify(out));
"""


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
    line = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not line:
        print("FAIL 没有拿到结果:")
        print(proc.stdout[:1000])
        return 1
    data = json.loads(line[-1])
    problems = []
    for key in ("qWifi", "qBt"):
        info = data[key]
        print("[%s] %-6s 写入 %d 字符  折线=%s  基准虚线=%s"
              % ("PASS" if info["hasPolyline"] else "FAIL", key, info["chars"],
                 info["hasPolyline"], info["hasDashed"]))
        if not info["hasPolyline"]:
            problems.append(key + " 没有画出折线")
        if not info["hasDashed"]:
            problems.append(key + " 没有画理想值虚线")
    for key in ("qWifiText", "qBtText", "qP2pText"):
        pass
    for key in ("qWifiText", "qBtText", "qP2pText"):
        text = data[key]
        ok = bool(text.strip())
        print("[%s] %-10s -> %s" % ("PASS" if ok else "FAIL", key, text[:80] or "(空)"))
        if not ok:
            problems.append(key + " 没有被更新")
    for key, label in (("in0", "输入1(Wi-Fi)"), ("in1", "输入2(蓝牙)")):
        info = data.get(key) or {}
        print("[%s] %-12s 写入 %d 字符  折线=%s  基准虚线=%s"
              % ("PASS" if info.get("hasPolyline") else "FAIL", label,
                 info.get("chars", 0), info.get("hasPolyline"), info.get("hasDashed")))
        if not info.get("hasPolyline"):
            problems.append(label + " 没有画出自己的曲线")
    if problems:
        print("\n问题: " + "; ".join(problems))
        return 1
    print("\nRESULT: PASS 三条曲线都能画出内容并更新说明文字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
