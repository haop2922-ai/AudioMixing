"""用真实 Chromium 渲染面板并逐页截图（给 docs/selfcheck 用）。

为什么要有这个：
  2026-11-06 用户批评"图 1 那种一眼就能看出来的错误居然没有检查出来"。
  单元测试和 API 断言看不到「本机混进局域网设备列表」「测试名当真名」这类
  纯渲染问题。这里启动真正的 Chrome/Edge（headless + CDP），点标签页、
  滚到关系图，截图后交给人看；截图本身就是验收证据。

    python tools/capture_ui_screenshots.py
    python tools/capture_ui_screenshots.py --url http://127.0.0.1:18090 --out docs/selfcheck
    python tools/capture_ui_screenshots.py --browser "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"

前置：面板已经在 --url 上跑着（建议用 AUDIOMIXING_HOME=临时目录启动 exe，
      避免自测把真实 config.json 写脏）。本脚本只读页面，不改任何设置。
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class CDP:
    """极简 Chrome DevTools Protocol 客户端（只用到导航、执行 JS、截图）。"""

    def __init__(self, ws_url: str) -> None:
        import websocket  # websocket-client；只在截图时用，不进产品依赖

        self.ws = websocket.create_connection(ws_url, timeout=20)
        self.next_id = 0

    def call(self, method: str, **params) -> dict:
        self.next_id += 1
        msg_id = self.next_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = self.ws.recv()
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError("%s: %s" % (method, data["error"]))
                return data.get("result") or {}

    def js(self, expression: str) -> object:
        result = self.call("Runtime.evaluate", expression=expression,
                           awaitPromise=True, returnByValue=True)
        return (result.get("result") or {}).get("value")

    def goto(self, url: str, wait: float = 3.0) -> None:
        self.call("Page.navigate", url=url)
        # 页面是本地服务，没有外部资源；等状态轮询跑完一轮再截图。
        time.sleep(wait)

    def wait_until(self, expression: str, timeout: float = 25.0) -> bool:
        """轮询一段 JS 直到为真；用于等"正在读取…"这类**加载中**文案消失。

        ★ 2026-11-06（真实截图自查）：蓝牙枚举要起 PowerShell，实测约 7 秒
        （`/api/bluetooth?refresh=1`）；原来固定等 0.8 秒就截图，验收图里
        会留着一张「正在读取…」，看着像面板卡死。截图必须等真实内容落定。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.js(expression):
                return True
            time.sleep(0.5)
        return False

    def shot(self, path: pathlib.Path, full: bool = False) -> None:
        params = {"format": "png", "fromSurface": True}
        if full:
            params["captureBeyondViewport"] = True
        data = self.call("Page.captureScreenshot", **params)["data"]
        path.write_bytes(base64.b64decode(data))
        print("   截图 %s（%.0f KB）" % (path, path.stat().st_size / 1024))

    def shot_clip(self, path: pathlib.Path, x: float, y: float,
                  width: float, height: float) -> None:
        """只截页面上的一块（文档坐标）—— 关系图这类"整页里只占一段"的模块用它。"""
        data = self.call("Page.captureScreenshot", format="png", fromSurface=True,
                         clip={"x": x, "y": y, "width": width, "height": height,
                               "scale": 1})["data"]
        path.write_bytes(base64.b64decode(data))
        print("   截图 %s（%.0f KB）" % (path, path.stat().st_size / 1024))


def _launch_browser(exe: str, port: int, profile: pathlib.Path) -> subprocess.Popen:
    args = [exe, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--remote-allow-origins=*",
            "--remote-debugging-port=%d" % port,
            "--user-data-dir=%s" % profile, "--window-size=1600,1200",
            "about:blank"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_ws(port: int, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                pages = json.loads(r.read().decode("utf-8"))
            for page in pages:
                if page.get("type") == "page" and page.get("webSocketDebuggerUrl"):
                    return page["webSocketDebuggerUrl"]
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.3)
    raise RuntimeError("浏览器调试端口 %d 没起来" % port)


def main() -> int:
    args = sys.argv[1:]
    url = "http://127.0.0.1:17800"
    out = ROOT / "docs" / "selfcheck"
    browser = ""
    i = 0
    while i < len(args):
        if args[i] == "--url" and i + 1 < len(args):
            url = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out = pathlib.Path(args[i + 1])
            i += 2
        elif args[i] == "--browser" and i + 1 < len(args):
            browser = args[i + 1]
            i += 2
        else:
            print("不认识的参数：%s" % args[i])
            return 2

    exe = browser or next((p for p in BROWSERS if pathlib.Path(p).is_file()), "")
    if not exe:
        print("找不到 Chrome/Edge；用 --browser 指定")
        return 2
    base = url.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
            if r.status != 200:
                print("面板没响应：%s" % base)
                return 2
    except (urllib.error.URLError, OSError) as exc:
        print("面板没响应：%s（%s）" % (base, exc))
        return 2

    out.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    profile = pathlib.Path(tempfile.mkdtemp(prefix="am-shot-"))
    proc = _launch_browser(exe, port, profile)
    try:
        ws_url = _wait_ws(port)
        cdp = CDP(ws_url)
        cdp.call("Emulation.setDeviceMetricsOverride", width=1600, height=1200,
                 deviceScaleFactor=1, mobile=False)

        print("主面板（本机状态）")
        cdp.goto(base + "/")
        cdp.js("document.querySelector('[data-tab=status]').click(); window.scrollTo(0,0);")
        time.sleep(0.8)
        cdp.shot(out / "01-status.png")

        print("设备与路由")
        cdp.js("document.querySelector('[data-tab=devices]').click();"
               "document.querySelector('[data-card=devices]').scrollIntoView({block:'start'});")
        # 等蓝牙枚举结束（约 7 秒），否则截图里是「正在读取…」。
        cdp.wait_until("(() => { const b = document.getElementById('btList');"
                       " return !!b && !b.textContent.includes('正在读取'); })()")
        time.sleep(0.8)
        cdp.shot(out / "02-devices.png")

        print("关系图")
        # ★ 2026-11-06：`setRail('ver')` 里会调 relRefresh(true)，一旦它抛错，后面拼接的
        #   scrollIntoView 就整段不执行 —— 第一版 03 图其实停在设置页顶部，没拍到关系图。
        #   现在拆成两次调用，并且用明确的坐标滚动；返回值打出来便于核对。
        cdp.js("if(typeof setRail==='function') setRail('ver');")
        time.sleep(0.6)
        moved = cdp.js(
            "(() => { const el = document.querySelector('[data-card=rel]');"
            " if(!el) return 'no-rel';"
            " window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 90);"
            " return 'rel@' + Math.round(el.getBoundingClientRect().top + window.scrollY); })()")
        print("   滚动到关系图：%s" % moved)
        time.sleep(1.2)
        print("   截图前 scrollY=%s" % cdp.js("window.scrollY"))
        cdp.shot(out / "03-relation.png")
        box = cdp.js("(() => { const r = document.querySelector('[data-card=rel]')"
                     ".getBoundingClientRect();"
                     " return {x: r.left + window.scrollX, y: r.top + window.scrollY,"
                     " w: r.width, h: r.height}; })()")
        if box:
            cdp.shot_clip(out / "03b-relation-card.png", max(0, box["x"] - 8),
                          box["y"] - 6, box["w"] + 16, box["h"] + 12)

        print("设置")
        cdp.js("if(typeof setRail==='function') setRail('ver'); window.scrollTo(0,0);")
        time.sleep(0.8)
        cdp.shot(out / "04-settings.png")

        print("监控页")
        cdp.goto(base + "/monitor")
        cdp.shot(out / "05-monitor.png", full=True)

        print("诊断页")
        cdp.goto(base + "/diagnose", wait=4.0)
        cdp.shot(out / "06-diagnose.png", full=True)

        print("广播页")
        cdp.goto(base + "/")
        cdp.js("document.querySelector('[data-tab=cast]').click(); window.scrollTo(0,0);")
        time.sleep(0.8)
        cdp.shot(out / "07-cast.png")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    print("\n全部截图已写入 %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
