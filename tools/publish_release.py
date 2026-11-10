#!/usr/bin/env python3
"""publish_release.py — 用 GitHub REST API 创建 Release 并上传附件

用法:
    GH_TOK=<token> python tools/publish_release.py

只做三件事：建 release（tag 已存在则复用/更新正文）→ 上传附件 → 回读校验。
token 仅从环境变量读取，不落盘、不打印。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = "haop2922-ai/AudioMixing"
TAG = "v2.32"
NAME = "Audio Mixing v2.32（PC）+ 手机端 v2.20"
TARGET = "main"
ASSETS = [
    (r"release\Audio Mixing v2.32.exe", "Audio Mixing v2.32.exe"),
    (r"release\AudioMixing-手机版-v2.20.apk", "AudioMixing-手机版-v2.20.apk"),
]
BODY = """## 下载

| 文件 | 平台 | 说明 |
|---|---|---|
| **Audio Mixing v2.32.exe** | Windows 10/11 x64 | 单文件，双击即用，无需安装 |
| **AudioMixing-手机版-v2.20.apk** | Android 8.0+ | 手机端（versionCode 34） |

## 版本

- 电脑端 **v2.32**（`audiomixing/__init__.py: __version__ = "2.32"`）
- 手机端 **v2.20**（`versionName 2.20` / `versionCode 34`）

## 这一版改了什么

- 把**音量算法绑到送声档位**：外放档 = `gain × media / 16384`，静音档 `media` 固定 16384
  （切档位后音量不再跳变，送声与本地音量行为一致）
- 新增**静音档乘数策略**设置项
- 修 `selftest_twonode` 偶发失败
- 修 `verify_exe` 验错对象的问题

## 怎么用（纯局域网，无账号）

1. 同一个 Wi-Fi 下，双击 exe → 面板地址形如 `http://<本机IP>:17800`
2. 手机装 APK（或用浏览器打开同一网址）→ 两端自动出现在彼此设备列表
3. 点设备卡上的「收听 / 送声音」互传声音；「广播我的声音」= 一台电脑喂多副耳机
4. 「多耳机」可同时选多块输出设备，并给每副单独调音量

## 校验（sha256）

```
530FD77517635919E7C4472D717EFEE58F42A58A5BF8AC6131493DA108B1B68A  Audio Mixing v2.32.exe        (22,318,592 B)
8A7839435C198FF8868742AEDB5EE571070A8E2D11B6E5137CE102BFC14977A8  AudioMixing-手机版-v2.20.apk  (170,567 B)
```

## 说明

- 本仓库只包含**源码**；可执行文件以本 Release 附件形式提供
- 仓库里的真实渲染自检截图已全部移除（其中含本机内网地址 / 主机名 / MAC 等环境信息）
"""


def api(method: str, url: str, token: str, payload: dict | None = None,
        data: bytes | None = None, ctype: str = "application/json") -> tuple[int, dict]:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "audiomixing-release-script",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:400]}


def main() -> int:
    token = os.environ.get("GH_TOK", "").strip()
    if not token:
        print("✗ 缺少 GH_TOK 环境变量")
        return 2

    # 1) 已存在则复用，否则新建
    code, rel = api("GET", f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}", token)
    if code == 200:
        print(f"  · Release {TAG} 已存在（id={rel['id']}），更新正文")
        code, rel = api("PATCH", f"https://api.github.com/repos/{REPO}/releases/{rel['id']}",
                        token, {"name": NAME, "body": BODY})
        if code != 200:
            print("  ✗ 更新失败:", code, rel); return 1
    else:
        print(f"  · 创建 Release {TAG}")
        code, rel = api("POST", f"https://api.github.com/repos/{REPO}/releases", token, {
            "tag_name": TAG, "target_commitish": TARGET, "name": NAME,
            "body": BODY, "draft": False, "prerelease": False,
        })
        if code not in (200, 201):
            print("  ✗ 创建失败:", code, rel); return 1
    rid = rel["id"]
    print(f"  ✓ Release id={rid}  {rel.get('html_url')}")

    # 2) 上传附件（已存在同名则先删）
    code, existing = api("GET", f"https://api.github.com/repos/{REPO}/releases/{rid}/assets", token)
    have = {a["name"]: a["id"] for a in (existing if isinstance(existing, list) else [])}
    ok = 0
    for path, name in ASSETS:
        if not os.path.isfile(path):
            print(f"  ✗ 文件不存在: {path}"); continue
        size = os.path.getsize(path)
        if name in have:
            api("DELETE", f"https://api.github.com/repos/{REPO}/releases/assets/{have[name]}", token)
        print(f"  ↑ 上传 {name}（{size/1048576:.2f} MB）…")
        with open(path, "rb") as f:
            blob = f.read()
        url = (f"https://uploads.github.com/repos/{REPO}/releases/{rid}/assets"
               f"?name={urllib.parse.quote(name)}")
        c, res = api("POST", url, token, data=blob, ctype="application/octet-stream")
        if c in (200, 201):
            print(f"    ✓ {res.get('name')}  {res.get('size')} B  state={res.get('state')}")
            ok += 1
        else:
            print(f"    ✗ 上传失败 {c}: {str(res)[:300]}")

    # 3) 回读校验
    code, rel2 = api("GET", f"https://api.github.com/repos/{REPO}/releases/{rid}", token)
    print("\n=== 回读校验 ===")
    print(f"  tag: {rel2.get('tag_name')}  name: {rel2.get('name')}")
    print(f"  draft={rel2.get('draft')} prerelease={rel2.get('prerelease')} 页面: {rel2.get('html_url')}")
    for a in rel2.get("assets", []):
        print(f"  · {a['name']}  {a['size']} B  ↓ {a['browser_download_url']}")
    return 0 if ok == len(ASSETS) else 1


if __name__ == "__main__":
    sys.exit(main())
