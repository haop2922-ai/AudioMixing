"""Bluetooth audio receiving (A2DP sink): let a phone send its sound to this PC.

The Windows feature lives in the WinRT API ``Windows.Media.Audio.AudioPlaybackConnection``.
Reaching it from Python turned out to be the hard part:

* ``pywinrt``'s projection of ``DeviceInformation.FindAllAsync`` (the only documented
  way to get the device ids) raises ``Invalid parameter count`` for every arity - the
  static method is simply not usable from Python;
* building the id by hand from a Bluetooth address or an interface path does not work
  either (``TryCreateFromId`` answers ``E_NOINTERFACE``, or ``None``).

Windows PowerShell 5.1 *can* call WinRT directly, so that is the bridge used here. It
needs no package at all and ships with every Windows 10/11, which also means the
packaged exe carries no extra runtime.

A connection only lives as long as the object that created it, so ``connect()`` starts
a small background PowerShell process that holds it; ``disconnect()`` removes the
sentinel file it waits on, and it exits by itself.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

# Every child process must be created without a console window. The one-shot listing runs
# every few seconds in the background, and without this flag Windows flashes a console
# window each time - which looks exactly like malware and cannot be ignored.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_STATE_DIR = pathlib.Path(tempfile.gettempdir()) / "audiomixing-bt"
_LOCK = threading.Lock()

# Shared by both scripts: awaits a WinRT IAsyncOperation from PowerShell 5.1.
_AWAIT = r"""
# Windows PowerShell 5.1 writes its output in the OEM code page, which mangles the
# Chinese device names the endpoint query returns - and made the reader raise.
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
function Await($op, $type) {
  $m = [System.WindowsRuntimeSystemExtensions].GetMethods() |
       Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
                      $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1" } |
       Select-Object -First 1
  $t = $m.MakeGenericMethod($type).Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}
function AwaitAction($action, $timeoutMs) {
  $m = [System.WindowsRuntimeSystemExtensions].GetMethods() |
       Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
                      $_.GetParameters()[0].ParameterType.Name -eq "IAsyncAction" } |
       Select-Object -First 1
  $t = $m.Invoke($null, @($action))
  $deadline = (Get-Date).AddMilliseconds($timeoutMs)
  while (-not $t.IsCompleted -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    Write-Status "starting"
  }
  if (-not $t.IsCompleted) { throw ("phone did not answer within " + $timeoutMs + " ms") }
}
$apcT = [Windows.Media.Audio.AudioPlaybackConnection, Windows.Media.Audio, ContentType=WindowsRuntime]
$diT  = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
$coll = [Windows.Devices.Enumeration.DeviceInformationCollection, Windows.Devices.Enumeration, ContentType=WindowsRuntime]
"""

# Windows PowerShell 5.1's ConvertTo-Json has no -AsArray, and it renders a
# single-element array as a bare object - the caller accepts both shapes.
_LIST_SCRIPT = _AWAIT + r"""
$sel = $apcT::GetDeviceSelector()
$devs = Await ($diT::FindAllAsync($sel)) $coll
$endpoints = @()
try { $endpoints = Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue } catch { }
$out = @()
foreach ($d in $devs) {
  $state = "Unknown"
  try { $c = $apcT::TryCreateFromId($d.Id); if ($c) { $state = $c.State.ToString() } } catch { }
  $active = $false
  $hfp = $false
  # ★ v2.26 第 3 条：原来用 "*名字*" 做子串匹配 —— 短名字 A 会把
  #   "A的平板 A2DP SNK" 也认成自己的端点，于是一张卡下挂了好几个
  #   别的设备的端点（用户诊断包里的"端点交叉命名"）。改成**按词边界**匹配：
  #   名字前面必须是开头/空格/左括号，后面必须是空格/右括号/结尾。
  #   "麦克风 (A A2DP SNK)" 仍然命中；"A的平板..." 因为后面是"的"而不再命中。
  $pattern = '(^|\s|\()' + [regex]::Escape([string]$d.Name) + '(\s|\)|$)'
  foreach ($e in $endpoints) {
    if ($e.FriendlyName -match $pattern -and $e.Status -eq "OK") {
      if ($e.FriendlyName -like "*A2DP*") { $active = $true }
      if ($e.FriendlyName -like "*Hands-Free*") { $hfp = $true }
    }
  }
  $out += [pscustomobject]@{ id = $d.Id; name = $d.Name; state = $state; endpoint = $active; hfp = $hfp }
}
if ($out.Count -eq 0) { Write-Output "[]" } else { Write-Output ($out | ConvertTo-Json -Compress) }
"""

# NOTE: `param()` is not used on purpose - it has to be the very first statement in a
# script file, and this script starts with the Add-Type/Await preamble.
_HOLD_SCRIPT = _AWAIT + r"""
$deviceId = $env:AUDIOMIXING_BT_ID
$sentinel = $env:AUDIOMIXING_BT_SENTINEL
$status   = $env:AUDIOMIXING_BT_STATUS
function Write-Status($s) {
  try {
    $tmp = $status + ".tmp"
    [System.IO.File]::WriteAllText($tmp, $s)
    Move-Item -Force -Path $tmp -Destination $status
  } catch { }
}
try {
  $conn = $apcT::TryCreateFromId($deviceId)
  if (-not $conn) { Write-Status "error:no-connection-object"; exit 2 }
  Write-Status "starting"
  AwaitAction ($conn.StartAsync()) 15000
  $conn.Open()
  Write-Status ("open:" + $conn.State.ToString())
  while (Test-Path $sentinel) {
    Start-Sleep -Milliseconds 700
    # 把 A2DP 媒体链路的状态一起写进去。只看 State=Opened 是不够的：手机可能只挂着
    # 免提（通话）档，这时连接是开着的但一个字节的声音都不会来。之前这个判断要靠
    # 另一路枚举，而枚举很贵又会被缓存，于是刚连上的手机会被显示成未连接。
    Write-Status ("open:" + $conn.State.ToString())
  }
  Write-Status "closing"
  try { $conn.Close() } catch { }
  Write-Status "closed"
} catch {
  Write-Status ("error:" + $_.Exception.Message)
  exit 3
}
"""


# HWHHYN 用 DEEPSEEK 制作
def available() -> bool:
    """True on Windows with a usable PowerShell - i.e. where this can work at all."""
    return sys.platform == "win32" and bool(POWERSHELL)


def _radio_from_registry() -> str:
    """直接从 PnP 注册表读蓝牙无线电模块的名字（不启动 PowerShell）。

    ★ 2026-10-26 现场：打包版第一次诊断时 PowerShell 那条路 9.8 秒还返回空，
      同一次请求里后续再查一次却正常。诊断页不该为了一行型号等十秒，也不该
      第一次就显示「未知」。蓝牙适配器在 `Enum\\USB` 里的 `ClassGUID` 是固定的
      Bluetooth 类 GUID、`Service` 是 BTHUSB，`DeviceDesc` 里带真实型号；
      读注册表只要几毫秒，也不受 PowerShell/杀软第一次拦截影响。
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    bt_class = "{e0cbf06c-cd8b-4647-bb8a-263b43f0f974}"

    def clean(value: Any) -> str:
        text = str(value or "").strip()
        if ";" in text:
            text = text.rsplit(";", 1)[-1].strip()
        if not text or text.startswith("@"):
            return ""
        return text

    for bus in ("USB", "PCI"):
        try:
            root = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Enum\%s" % bus)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    vendor = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    vendor_key = winreg.OpenKey(root, vendor)
                except OSError:
                    continue
                try:
                    child = 0
                    while True:
                        try:
                            instance = winreg.EnumKey(vendor_key, child)
                        except OSError:
                            break
                        child += 1
                        try:
                            dev = winreg.OpenKey(vendor_key, instance)
                        except OSError:
                            continue
                        try:
                            service = guid = desc = friendly = ""
                            for value_name in ("Service", "ClassGUID", "DeviceDesc", "FriendlyName"):
                                try:
                                    value, _typ = winreg.QueryValueEx(dev, value_name)
                                except OSError:
                                    value = ""
                                if value_name == "Service":
                                    service = str(value or "")
                                elif value_name == "ClassGUID":
                                    guid = str(value or "")
                                elif value_name == "DeviceDesc":
                                    desc = clean(value)
                                else:
                                    friendly = clean(value)
                            if service.lower() != "bthusb" and guid.lower() != bt_class:
                                continue
                            name = desc or friendly
                            if name:
                                return name
                        finally:
                            dev.Close()
                finally:
                    vendor_key.Close()
        finally:
            root.Close()
    return ""


def radio_name() -> str:
    """本机蓝牙适配器的名字（诊断用）。拿不到就空串。

    ★ 2026-10-26 用户（图1）：AMD 机器的诊断里这一行写「未知」。根因不是没适配器，
      而是旧实现取的是 `Get-PnpDevice -Class Bluetooth` 里第一个 Status=OK 的设备 ——
      那通常是「对象推送服务」「蓝牙外围设备」之类的协议服务，有的机器（例如
      MediaTek/Realtek 模块）适配器本身根本不在这个 Class 里，于是什么也取不到。
      现在按 PnP 实体全表找名字含 Bluetooth/蓝牙、但不含“服务/枚举/外围…”的那一条，
      优先 `USB\\VID...` 的真实无线电模块（Intel / MediaTek / Realtek 模块都是这种）。
    """
    # 注册表这条最快、最稳，优先用它；拿不到才退回 PowerShell 枚举。
    registry = _radio_from_registry()
    if registry:
        return registry
    if not available():
        return ""
    # ★ 2026-10-26 现场：脚本文件里的中文会让 Windows PowerShell 5.1 的
    #   “无 BOM UTF-8 按 ANSI 读”把整段解析坏掉（实测：`适配器` 那一行一加进去，
    #   同一份脚本立刻从有输出变成空输出）。所以脚本里**一个中文都不写**：
    #   “蓝牙”用两个字符码拼出来，其余筛选用英文关键字。
    script = r"""
$btWord = [string][char]0x84DD + [string][char]0x7259
$all = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object {
  $_.Status -eq 'OK' -and
  $_.FriendlyName -match ('Bluetooth|' + $btWord) -and
  $_.FriendlyName -notmatch 'Service|Enumerator|RFCOMM|Rfcomm|Profile|Device Information|Generic|Personal Area|Phonebook|SIM|Gateway|Serial|XINPUT|HID|Chat|PAN|AVRCP|Transport'
})
$radio = $all | Where-Object { $_.InstanceId -match '^USB\\VID' } | Select-Object -First 1
if (-not $radio) {
  $radio = $all | Where-Object { $_.FriendlyName -match 'Adapter' } | Select-Object -First 1
}
if (-not $radio) { $radio = $all | Select-Object -First 1 }
if ($radio) { $radio.FriendlyName }
"""
    # 打包版第一次诊断偶发空结果（第一次 PowerShell/WMI 被安全软件拦下，或
    # WMI 提供程序尚未预热）。空结果重试一次，成功就返回。
    for attempt in range(2):
        try:
            text = (_run(script, timeout=20 if attempt == 0 else 8) or "").strip()
        except Exception:
            text = ""
        if text:
            return text
    return ""


def _run(script: str, timeout: float = 25.0) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            creationflags=_NO_WINDOW,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return proc.stdout or ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


_RADIO_CACHE: dict[str, Any] = {"at": 0.0, "present": False, "state": "unknown", "name": ""}


def radio_state(force: bool = False) -> dict[str, Any]:
    """蓝牙无线电的**实际状态**（第 3 条）：有没有适配器、开没开。

    为什么需要它：面板原来只要"配对列表为空"就写「没有可用于接收音频的蓝牙设备，
    请先配对」—— 可用户现场是**蓝牙开关没开**，按这句话去配对当然配不了，
    文案直接把方向指错了。这里区分：
      · present=False           → 这台机器没有蓝牙模块（或驱动未装）
      · state=off / disabled    → 有适配器，但蓝牙开关是关的
      · state=on                → 有适配器且开着，此时列表为空才是"还没配对"
    只读、不弹窗、不改任何系统设置；读不到就如实返回 unknown（面板据它说"读不到"，
    不编结论）。
    """
    with _LOCK:
        now = time.monotonic()
        cached = dict(_RADIO_CACHE)
        if not force and now - float(cached.get("at") or 0.0) < 30.0 and cached.get("at"):
            return cached
    name = radio_name()
    present = bool(name)
    state_text = "unknown"
    if present and available():
        # 只取状态；脚本里不写中文（PowerShell 5.1 无 BOM 时会把中文解析坏）。
        script = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
try {
  $null = [Windows.Devices.Radios.Radio,Windows.Devices.Radios,ContentType=WindowsRuntime]
  $op = [Windows.Devices.Radios.Radio]::GetRadiosAsync()
  $n = 0
  while ($op.Status -ne 1 -and $n -lt 200) { Start-Sleep -Milliseconds 10; $n++ }
  $radios = $op.GetResults()
  $bt = $radios | Where-Object { "$($_.Kind)" -eq 'Bluetooth' } | Select-Object -First 1
  if ($bt) { "$($bt.State)" } else { "None" }
} catch { "Unknown" }
"""
        try:
            state_text = (_run(script, timeout=8) or "").strip() or "unknown"
        except Exception:
            state_text = "unknown"
        low = state_text.lower()
        state_text = ("on" if low == "on" else "off" if low == "off"
                      else "disabled" if low == "disabled" else "unknown")
    elif not present:
        state_text = "absent"
    result = {"at": time.monotonic(), "present": present,
              "state": state_text, "name": name}
    with _LOCK:
        _RADIO_CACHE.update(result)
    return result


def empty_reason() -> str:
    """配对列表为空时，**按真实原因**给一句该怎么做的提示（第 3 条）。"""
    if not available():
        return "这台电脑不是 Windows（或找不到 PowerShell），无法读取蓝牙设备。"
    rs = radio_state()
    name = str(rs.get("name") or "").strip()
    if not rs.get("present"):
        return ("没有检测到蓝牙适配器：这台电脑没有蓝牙模块，或蓝牙驱动没装好。"
                "台式机可以插一个 USB 蓝牙适配器后再试。")
    if rs.get("state") in ("off", "disabled"):
        return ("蓝牙已关闭：先打开 Windows 的蓝牙开关（设置 → 蓝牙和其他设备），"
                "再回到这里刷新。")
    if rs.get("state") == "unknown":
        return ("检测到蓝牙适配器（%s），但读不到蓝牙开关状态。请先确认 Windows 蓝牙开关已打开，"
                "再在 Windows 设置里配对手机一次（手机端不用装任何 App）。" % (name or "未知型号"))
    return ("蓝牙已打开（%s），但还没有配对过能送声音的手机："
            "到 Windows 设置 → 蓝牙和其他设备 → 添加设备，把手机配对一次"
            "（手机端不用装任何 App）。" % (name or "未知型号"))


def list_devices() -> list[dict[str, Any]]:
    """Every paired device that can send this PC its audio (usually the phones)."""
    if not available():
        return []
    try:
        raw = _run(_LIST_SCRIPT).strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        device_id = str(item.get("id"))
        key = _key(device_id)
        held = False
        if (_STATE_DIR / ("%s.keep" % key)).is_file() and (_STATE_DIR / ("%s.pid" % key)).is_file():
            held = _helper_live(device_id)
            if not held:
                _forget(device_id)  # self-heal: a dead helper must not linger
        state = str(item.get("state") or "Unknown")
        if held:
            # A fresh process always sees `Closed`: the connection state lives inside
            # the helper process that holds it. So ours is the authority while held.
            ours = status(device_id)
            if ours:
                state = ours
        # Our own connection decides, and nothing else. The Windows endpoint staying
        # "OK" only means the endpoint device exists - it survives the phone switching
        # its Bluetooth off - so it must never override an explicit disconnect.
        #
        # `State == Opened` on its own is NOT proof that sound is arriving: the phone
        # can hold the Hands-Free (call) profile instead of A2DP (media), and then the
        # PC receives nothing while the connection still reads Opened. That is what
        # made the panel promise "正在接收手机声音" with no audio, so the two profiles
        # are reported separately and honestly.
        hfp = bool(item.get("hfp"))
        endpoint_active = bool(item.get("endpoint"))
        if state.startswith("error:"):
            link = "error"
        elif held and endpoint_active:
            # Both halves must be true: we opened the sink, and the phone actually
            # brought up its A2DP media link. Either one alone was a false "connected".
            link = "connected"
        elif hfp:
            # Worth saying even when we hold nothing: the phone is on the call profile,
            # and that alone explains "why is there no sound" without any further test.
            link = "hfp"
        elif held:
            link = "waiting"
        else:
            link = "off"
        out.append({
            "id": device_id,
            "address": device_address(device_id),
            "name": str(item.get("name") or "未知设备"),
            "state": state,
            "held": held,
            "endpoint": endpoint_active,
            "hfp": hfp,
            "link": link,
        })
    return merge_paired_devices(out)


def merge_paired_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    '''把**同一台物理设备**的多条历史配对记录合成一张卡。

    ★ v2.26 第 3 条：用户诊断包里是 4 条历史配对，全 Closed，端点还交叉命名。
      Windows 的 AudioPlaybackConnection 会为同一台手机重装/换用不同配对通道
      留下多条记录。这里只按**强证据**合并：

        · 同一个蓝牙地址（device_address 取出的 12 位 MAC）—— 同 MAC 一定是同一台；
        · 没有地址时，名字归一后完全相同。

      刻意**不**按"名字是另一个名字的前缀/包含关系"合并：`A` 和 `A的平板`
      很像，但完全可能是手机和平板两台真实设备，合并错了比多一张卡更糟
      （项目里一直用"宁可多一张卡"的原则）。那些按词边界不再交叉命中的端点，
      会在各自的卡片上正确显示。

    合并后的卡带 `ids`（全部历史配对 id）与 `merged_count`，断开时要把它们都断掉，
    否则另一条历史记录还会把链路拉起来。
    '''
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for device in devices:
        address = "".join(ch for ch in str(device.get("address") or "")
                          if ch.isalnum()).upper()
        name = " ".join(str(device.get("name") or "").split()).casefold()
        if address:
            key = "mac:" + address
        elif name:
            key = "name:" + name
        else:
            key = "id:" + str(device.get("id") or "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(device)

    def rank(device: dict[str, Any]) -> tuple[int, int]:
        link = str(device.get("link") or "")
        return ({'connected': 0, 'waiting': 1, 'hfp': 2}.get(link, 3),
                -int(device.get("endpoint") or 0))

    merged: list[dict[str, Any]] = []
    for key in order:
        items = sorted(groups[key], key=rank)
        primary = dict(items[0])
        ids = []
        for item in items:
            value = str(item.get("id") or "")
            if value and value not in ids:
                ids.append(value)
        names = []
        for item in items:
            label = str(item.get("name") or "")
            if label and label not in names:
                names.append(label)
        primary["ids"] = ids
        primary["merged_count"] = len(items)
        primary["aliases"] = names[1:]
        primary["held"] = any(bool(item.get("held")) for item in items)
        primary["endpoint"] = any(bool(item.get("endpoint")) for item in items)
        primary["hfp"] = any(bool(item.get("hfp")) for item in items)
        if len(items) > 1:
            # 两条历史配对里只要有一条还连着，这张卡就按连着的那条显示。
            primary["state"] = next((str(item.get("state")) for item in items
                                     if str(item.get("link")) == "connected"),
                                    primary.get("state"))
        merged.append(primary)
    return merged


def cleanup_stale_records() -> dict[str, Any]:
    '''清理**本程序自己**留下的旧蓝牙记录（临时目录里的 .keep/.pid/.status）。

    ★ v2.26 第 3 条：用户要求"历史失效配对能够清理"。这里必须诚实：
      Windows 的配对记录在注册表 / 系统设置里，本程序**只读不写**，删不掉。
      所以这个按钮只清我们自己的 helper 状态文件；Windows 里那 4 条配对记录
      需要用户到「设置 → 蓝牙和其他设备」手动删除（界面按钮下会写明这一点）。
    '''
    removed = 0
    # 助手状态文件的名字是 `_key(device_id)`（sha1 前 12 位），不是原始 id。
    keep = {_key(str(item.get("id") or "")) for item in list_devices()}
    try:
        files = list(_STATE_DIR.glob("*"))
    except OSError:
        files = []
    for path in files:
        if not path.is_file():
            continue
        stem = path.name.split('.', 1)[0]
        if stem in keep:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return {"removed": removed,
            "note": ("已清理本程序自己的 %d 个旧蓝牙记录。Windows 的配对记录本程序只读不写，"
                     "如果列表里还有失效的旧配对，请到 Windows 设置 → 蓝牙和其他设备里删除。"
                     % removed)}


def device_address(instance_id: str) -> str:
    r"""从 Windows 的设备实例 ID 里取出蓝牙地址，形如 5C:A0:6C:CA:0A:6E。

    实例 ID 长这样，中间那 12 位十六进制**就是地址本身**：
        \\?\BTHENUM#{...}_VID&..._PID&...#b&62612bf&0&5CA06CCA0A6E_C00000000#{...}\SNK

    2026-10-16 实机核对过：这 12 位与 Windows 自己的
    DEVPKEY_Bluetooth_DeviceAddress、以及
    HKLM\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Devices 下的子键
    **完全一致**，所以按两位一组分开、大写显示就是 Windows 上那个地址。
    取不到就返回空串 —— 面板会写「—」，绝不编造。
    """
    import re

    m = re.search(r"&0&([0-9A-Fa-f]{12})_", instance_id or "")
    hexs = m.group(1) if m else ""
    if len(hexs) != 12:
        return ""
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2)).upper()


def _key(device_id: str) -> str:
    import hashlib

    return hashlib.sha1(device_id.encode("utf-8")).hexdigest()[:12]


def _alive(pid: int | None) -> bool:
    """Is that process still running?

    On Windows this must not be os.kill(pid, 0): there the call is not a probe at all.
    Any signal other than the two console events terminates the process, so every
    "is the helper alive?" check was killing the helper - which is why a phone would
    connect and then drop a few seconds later, every time, and why it looked like a
    Bluetooth problem instead of a bug in this file.
    """
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            SYNCHRONIZE = 0x00100000
            WAIT_TIMEOUT = 0x00000102
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _forget(device_id: str) -> None:
    """Drop every file belonging to one device's helper.

    Called when the helper is gone: a stale "open:..." must never outlive the process
    that owned it, or the panel would keep promising a connection that is not there.
    """
    key = _key(device_id)
    for suffix in ("keep", "pid", "status", "log", "ps1"):
        try:
            (_STATE_DIR / ("%s.%s" % (key, suffix))).unlink(missing_ok=True)
        except OSError:
            pass



def _helper_live(device_id: str) -> bool:
    """Is the helper process for this device still there?

    The helper rewrites its status file every 0.7 s, so a stale file means it died -
    and a dead helper used to leave `open:Opened` behind forever, which made the panel
    claim a connection that no longer existed (restarting the app did not help either,
    because the files survived).
    """
    key = _key(device_id)
    sentinel = _STATE_DIR / ("%s.keep" % key)
    pid_file = _STATE_DIR / ("%s.pid" % key)
    status_file = _STATE_DIR / ("%s.status" % key)
    if not (sentinel.is_file() and pid_file.is_file()):
        return False
    if not _alive(_pid(pid_file)):
        return False
    # 进程活着就算握着。以前这里还要求状态文件六秒内更新过，而助手只在状态变化时
    # 写文件：连上之后文件就不再动，于是六秒后任何一次列表查询都会判定助手已死，
    # 顺手删掉哨兵，助手发现哨兵没了就自己退出——连接随之断开。面板每次刷新蓝牙
    # 都会走到这里，所以现象是「连上没多久就断」，而且每台手机都一样。
    # 文件时效只用来兜住真正卡死的助手，所以给一个宽松的上限。
    try:
        return (time.time() - status_file.stat().st_mtime) < 180.0
    except OSError:
        return False


def status(device_id: str) -> str:
    path = _STATE_DIR / ("%s.status" % _key(device_id))

    def read() -> str:
        try:
            # NULs appear when a write is caught mid-flight, and PowerShell's
            # Set-Content -Encoding UTF8 adds a BOM; strip both.
            return path.read_text(encoding="utf-8").replace("\x00", "").lstrip("\ufeff").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    text = read()
    if not text:
        # One retry: the helper may be renaming the file right now.
        time.sleep(0.08)
        text = read()
    return text


def connect_by_name(name: str) -> dict[str, Any]:
    """按名字在一堆"已配对的设备"里找到一台，并打开 A2DP sink。

    为什么要这个入口（2026-10-15 用户真机反馈④）：「手机确认走蓝牙后应当由电脑发送
    蓝牙请求并配对互相连接」—— 也就是这一步该由**电脑**发起，而不是把用户丢进
    系统蓝牙设置里自己点。

    能做到什么、不能做到什么，说清楚：
      · **连接**（打开 sink、等手机把媒体音频发过来）能做到 —— 就是面板 ⑦ 里
        「连接」按钮做的事；
      · **配对**做不到：Windows 不给普通程序配对 API。所以这里只在"已经配对过"的
        设备里找；没找到就如实回报 {"state": "no_device"}，由手机端提示用户
        先去 Windows 蓝牙设置里配对一次（配一次就够，以后都由电脑这边连）。
    名字用"包含即算"匹配：电脑看到的蓝牙设备名和手机自己报的名字常常差一截后缀。
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return {"ok": False, "state": "no_name", "error": "对端没报名字"}
    try:
        devices = list_devices_cached()
    except Exception as exc:
        return {"ok": False, "state": "error", "error": "读蓝牙设备列表失败: %s" % exc}
    for device in devices:
        dev_name = str((device or {}).get("name") or "").strip()
        key = dev_name.lower()
        if not key:
            continue
        if key == wanted or wanted in key or key in wanted:
            if str(device.get("link") or "") in ("connected", "waiting"):
                return {"ok": True, "state": "already", "device": dev_name,
                        "note": "已经连着或电脑这边已就绪，没有重复连接"}
            try:
                result = connect(str(device.get("id") or ""))
            except Exception as exc:
                return {"ok": False, "state": "error", "device": dev_name, "error": str(exc)}
            return {"ok": bool(result.get("ok")), "state": str(result.get("state") or ""),
                    "device": dev_name, "error": str(result.get("error") or "")}
    return {"ok": False, "state": "no_device",
            "error": "「%s」不在蓝牙列表里 —— 要先在 Windows 蓝牙设置里配对一次" % name}


def connect(device_id: str) -> dict[str, Any]:
    """Open the A2DP sink for `device_id` and keep it open in a helper process."""
    if not available():
        return {"ok": False, "error": "这台机器不是 Windows，或找不到 powershell.exe"}
    if not device_id:
        return {"ok": False, "error": "缺少设备 id"}
    with _LOCK:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        key = _key(device_id)
        sentinel = _STATE_DIR / ("%s.keep" % key)
        status_file = _STATE_DIR / ("%s.status" % key)
        pid_file = _STATE_DIR / ("%s.pid" % key)
        log_file = _STATE_DIR / ("%s.log" % key)
        # Already open? Then this is a no-op, not a restart: reconnecting a live phone
        # would drop the audio it is sending right now.
        if sentinel.is_file() and pid_file.is_file():
            current = status(device_id)
            # open:Closed 意味着我们这边开着但手机没过来。这种情况必须重建，
            # 否则按「连接」只会被当成已连接而复用，用户点什么都没反应。
            if current.startswith("open:") and "Closed" not in current:
                return {"ok": True, "state": current, "pid": _pid(pid_file), "reused": True}
            _stop_helper(pid_file)
        # A previous attempt may have left files behind; start from a clean slate so a
        # stale "open:..." can never make the panel claim a connection that is gone.
        for stale in (sentinel, status_file, pid_file, log_file,
                      _STATE_DIR / ("%s.ps1" % key)):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                # A helper that is still shutting down can hold its log for a moment.
                pass
        sentinel.write_text("1", encoding="utf-8")
        status_file.write_text("starting", encoding="utf-8")
        # 脚本落在插件自己的状态目录里（按设备哈希命名），不再往 %TEMP% 里
        # 每次连接丢一个 .ps1 —— 那些临时文件以前从来没被删过。
        script_path = _STATE_DIR / ("%s.ps1" % key)
        script_path.write_text(_HOLD_SCRIPT, encoding="utf-8-sig")
        env = dict(os.environ)
        env.update({
            "AUDIOMIXING_BT_ID": device_id,
            "AUDIOMIXING_BT_SENTINEL": str(sentinel),
            "AUDIOMIXING_BT_STATUS": str(status_file),
        })
        # A fully detached process (DETACHED_PROCESS) could not bring up WinRT and
        # silently died before writing anything; CREATE_NO_WINDOW alone is enough to
        # keep it invisible, and its output goes to a log so a failure is visible.
        log_file = _STATE_DIR / ("%s.log" % key)
        flags = _NO_WINDOW
        try:
            # The child inherits this handle; the parent must let go of its own copy or
            # the log stays locked and a later reconnect cannot clean up.
            with open(log_file, "wb") as log:
                proc = subprocess.Popen(
                    [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
                    env=env, creationflags=flags, close_fds=True, stdout=log, stderr=subprocess.STDOUT,
                    cwd=str(_STATE_DIR),
                )
        except OSError as exc:
            sentinel.unlink(missing_ok=True)
            return {"ok": False, "error": "启动 helper 失败: %s" % exc}
        pid_file.write_text(str(proc.pid), encoding="utf-8")
    # Give the helper a moment to report; the phone itself connects asynchronously.
    deadline = time.time() + 8.0
    while time.time() < deadline:
        state = status(device_id)
        if state.startswith(("open:", "error:")):
            break
        time.sleep(0.3)
    state = status(device_id) or "starting"
    result = {"ok": not state.startswith("error:"), "state": state, "pid": proc.pid}
    if not state.startswith("open:"):
        try:
            tail = log_file.read_text(encoding="utf-8", errors="replace").strip()[-400:]
        except OSError:
            tail = ""
        if tail:
            result["log"] = tail
    return result


def _pid(pid_file: pathlib.Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _stop_helper(pid_file: pathlib.Path) -> None:
    """Make sure a helper that is no longer wanted really goes away."""
    pid = _pid(pid_file)
    if pid is None or sys.platform != "win32":
        return
    for _ in range(6):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.3)
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=_NO_WINDOW, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def disconnect(device_id: str) -> dict[str, Any]:
    """Ask the helper to let go; it closes the connection and exits."""
    key = _key(device_id or "")
    sentinel = _STATE_DIR / ("%s.keep" % key)
    pid_file = _STATE_DIR / ("%s.pid" % key)
    sentinel.unlink(missing_ok=True)
    _stop_helper(pid_file)
    _forget(device_id)
    return {"ok": True, "state": "closed"}


def disconnect_all() -> dict[str, Any]:
    """把**所有**还握着的蓝牙连接一次挂断 —— 退出程序时必须调这个。

    为什么必须做（用户真机反馈）：「关闭服务后，手机声音依旧通过蓝牙传入电脑」。
    根因是连接的寿命绑在**助手子进程**上：主进程一退，那个助手的哨兵文件还在，
    它就一直握着 A2DP 链路，手机也还以为连着、继续把声音往里送。这里逐个删哨兵，
    助手发现哨兵没了就自己 Close 并退出；卡住的再 taskkill。

    顺序上先把所有哨兵一次删光、再逐个等：那样所有助手会在同一个 0.7 秒周期里
    一起发现，握着两台手机也不用等两倍时间。

    说清楚能覆盖到什么：**正常退出**（托盘「停止服务并退出」/ Ctrl+C）都会走到这里；
    被强杀（任务管理器结束进程、断电、蓝屏）根本来不及跑，那就清不掉 —— 只能等
    Windows 自己回收，这里不假装能覆盖。
    """
    try:
        keys = [keep.stem for keep in _STATE_DIR.glob("*.keep")]
    except OSError:
        return {"ok": True, "closed": [], "failed": [], "count": 0}
    for key in keys:
        try:
            (_STATE_DIR / ("%s.keep" % key)).unlink(missing_ok=True)
        except OSError:
            pass
    closed: list[str] = []
    failed: list[str] = []
    for key in keys:
        try:
            _stop_helper(_STATE_DIR / ("%s.pid" % key))
            _forget_key(key)
            closed.append(key)
        except Exception as exc:            # 一个失败不能拖住其余的
            failed.append("%s: %s" % (key, exc))
    return {"ok": not failed, "closed": closed, "failed": failed, "count": len(closed)}


def _forget_key(key: str) -> None:
    """按内部 key（文件名前缀）删掉一个助手留下的全部文件。"""
    for suffix in ("keep", "pid", "status", "log"):
        try:
            (_STATE_DIR / ("%s.%s" % (key, suffix))).unlink(missing_ok=True)
        except OSError:
            pass

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "devices": []}


def list_devices_cached(max_age: float = 10.0, force: bool = False) -> list[dict]:
    """The device list, refreshed at most every `max_age` seconds.

    Listing costs about three seconds - it starts a PowerShell helper - so it must never
    happen on a request the panel polls. Only the background refresh and the explicit
    刷新 button come through here.

    ★ 2026-10-16 修一个**真缺陷**（打包成 v1.29 后实测抓到的性能故障）：这里原来要求
    ``_CACHE["devices"]`` **非空**才算命中缓存 —— 于是"这台机器现在一台配对的蓝牙设备都没有"
    时，每一次调用都会重跑那个约 2.6 秒的 PowerShell。而引擎的 ``apply_routes()`` 每轮
    （维护循环 0.5 秒一轮）都要问一次蓝牙名单，结果就是**整台机器的音频路径被一个 2.6 秒的
    子进程反复拖住**：实测 ``tick()`` 单次 2.7 秒，两个节点的音频流直接超时（unittest 里
    test_audio_flows_between_two_nodes 等三条就是这么红的），用户侧的表现是"越用越卡"。
    现在**空结果同样算"刚查过"**：把查询时刻记下来，空表也在 max_age 内照常命中缓存。
    """
    now = time.monotonic()
    cached: list[dict] | None = None
    with _CACHE_LOCK:
        # at 为 0 = 从来没查过；查过之后（哪怕结果是空表）就在 max_age 内都算新鲜。
        if not force and _CACHE["at"] and now - _CACHE["at"] < max_age:
            cached = list(_CACHE["devices"])
    if cached is not None:
        # 出口也必须合并实时状态，否则面板读到的还是枚举那一刻的旧结论
        return _apply_live_states(cached)
    devices = list_devices()
    with _CACHE_LOCK:
        _CACHE["at"] = time.monotonic()
        _CACHE["devices"] = list(devices)
    return _apply_live_states(devices)


def cached_names() -> list[str]:
    """设备名列表，**只读缓存、绝不刷新**（给每 2 秒一次的发现信标用）。

    为什么要单独开一个口子：`list_devices_cached()` 在缓存过期时会去跑一次
    PowerShell（约 3 秒）。信标是每 2 秒发一次的，一旦那里触发刷新，广播会被
    整整卡住 3 秒 —— 对端就可能判我们离线。这里只回上次后台刷新的结果：
    名单晚个几十秒无所谓，它只是用来决定"要不要问用户走蓝牙还是 WiFi"。
    """
    with _CACHE_LOCK:
        devices = list(_CACHE["devices"] or [])
    names: list[str] = []
    for item in devices:
        name = str((item or {}).get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def held_count() -> int:
    """How many connections we are holding right now.

    Used to decide how often the device list may be refreshed: the listing can disturb a
    connection we hold, but when we hold nothing it is harmless and should be fresh.
    """
    count = 0
    try:
        for keep in _STATE_DIR.glob("*.keep"):
            pid_file = _STATE_DIR / ("%s.pid" % keep.stem)
            if pid_file.is_file() and _alive(_pid(pid_file)):
                count += 1
    except OSError:
        pass
    return count



def _norm_device_text(text: object) -> str:
    """设备名比较用：去掉所有空白并转小写。

    ★ 2026-10-26 用户现场：手机蓝牙已经出声、Windows 也在播放，面板却报
      「媒体音频未到」。旧判断对设备名做的是**大小写敏感的子串匹配**，而
      Windows 有些机器返回的端点名是全大写/带不同空格，于是明明有端点也匹配不上。
    """
    return "".join(str(text or "").split()).lower()


def mmdevice_endpoints(name: str = "") -> list[dict[str, Any]]:
    """列出 MMDevices 里和设备名有关的音频端点（Capture + Render 都看）。

    `name` 为空时返回所有带 A2DP/SNK 字样的端点（诊断用）；给了名字就只返回
    名字包含它的端点。每项带 `flow`（Capture/Render）、`name`、`state`、`active`。
    只读注册表，几毫秒，不碰蓝牙连接。
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    want = _norm_device_text(name)
    out: list[dict[str, Any]] = []
    for flow in ("Capture", "Render"):
        base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\%s" % flow
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
                index = 0
                while True:
                    try:
                        guid = winreg.EnumKey(root, index)
                    except OSError:
                        break
                    index += 1
                    friendly = ""
                    state = None
                    try:
                        with winreg.OpenKey(root, guid) as dev:
                            try:
                                state, _ = winreg.QueryValueEx(dev, "DeviceState")
                            except OSError:
                                pass
                            try:
                                with winreg.OpenKey(dev, "Properties") as props:
                                    try:
                                        friendly, _ = winreg.QueryValueEx(
                                            props, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                                    except OSError:
                                        pass
                            except OSError:
                                pass
                    except OSError:
                        continue
                    text = str(friendly or "")
                    if not text:
                        continue
                    norm = _norm_device_text(text)
                    bluetooth = ("a2dp" in norm) or ("snk" in norm)
                    if not bluetooth and not want:
                        continue
                    if want and want not in norm:
                        continue
                    try:
                        state_value = int(state) if state is not None else None
                    except (TypeError, ValueError):
                        state_value = None
                    out.append({
                        "flow": flow,
                        "name": text,
                        "state": state_value,
                        "active": state_value == 1,
                        "bluetooth": bluetooth,
                        # ★ 2026-10-26：部分机器（Intel/Realtek 驱动）的 A2DP SNK 端点
                        #   在注册表里是 active，但 WASAPI 的 ACTIVE 枚举里看不到它。
                        #   把完整 MMDevice id 带出去，采集端可以按 id 直接 GetDevice。
                        "endpoint_id": (("{0.0.1.00000000}." if flow == "Capture"
                                         else "{0.0.0.00000000}.") + guid),
                    })
        except OSError:
            continue
    return out


def endpoint_report(name: str = "") -> list[dict[str, Any]]:
    """诊断报告用：这台设备在 MMDevice 里的端点实况（只读）。"""
    return mmdevice_endpoints(name)


def _endpoint_state_from_registry(name: str) -> bool | None:
    """读 Windows MMDevice 注册表，判断这台手机的媒体音频端点是否 active。

    ★ 2026-10-26 用户现场：手机蓝牙已经在播放、电脑也听得到，面板却仍写
      「手机已连接，但媒体音频未到」。根因不是连接，是这里：
        · 旧代码只扫 Capture，且要求名字里必须出现 "A2DP SNK"；
        · 名字比较大小写敏感、空格敏感；
        · 找不到时返回 None，调用方就沿用枚举缓存里的 False。
      现在 Capture/Render 都扫，名字归一化后匹配；优先看 A2DP/SNK 端点，
      没有这种名字时退回任何含设备名的端点。找不到任何端点才返回 None。
    """
    if not name:
        return None
    entries = mmdevice_endpoints(str(name))
    if not entries:
        return None
    preferred = [e for e in entries if e.get("bluetooth")]
    chosen = preferred or entries
    return any(bool(e.get("active")) for e in chosen)


def _apply_live_states(devices: list[dict]) -> list[dict]:
    """把我们自己握着的连接状态合并进去，并让没有依据的"已连接"过期。

    两件事：
      一是我们握着的连接，状态就在助手的文件里，读一下就知道，永远新鲜，
         不必依赖那份很贵、而且会打扰连接的设备枚举。
      二是枚举得出的"已连接"是有保质期的，手机可能已经关了蓝牙。只要现在我们
         没有握着对应连接，就不该继续当真，否则会出现"手机蓝牙都没开，面板还说
         正在接收手机声音"。

    这个函数必须被所有对外出口调用。之前只有 cached_devices 调了它，而面板走的
    是 list_devices_cached，于是修了也没效果。
    """
    out = [dict(d) for d in devices]
    held_keys = set()
    try:
        for keep in _STATE_DIR.glob("*.keep"):
            pid_file = _STATE_DIR / ("%s.pid" % keep.stem)
            if pid_file.is_file() and _alive(_pid(pid_file)):
                held_keys.add(keep.stem)
    except OSError:
        pass
    def device_keys(device: dict) -> set[str]:
        # ★ v2.26 第 3 条：合并卡的主 id 可能不是真正握着连接的那条历史配对。
        values = device.get("ids") or [device.get("id")]
        return {_key(str(x)) for x in values if x}

    for device in out:
        if str(device.get("link") or "") == "connected":
            if not (device_keys(device) & held_keys):
                device["link"] = "off"
                device["held"] = False
    try:
        for keep in _STATE_DIR.glob("*.keep"):
            key = keep.stem
            status_file = _STATE_DIR / ("%s.status" % key)
            pid_file = _STATE_DIR / ("%s.pid" % key)
            if not (status_file.is_file() and pid_file.is_file()):
                continue
            if not _alive(_pid(pid_file)):
                continue
            text = status_file.read_text(encoding="utf-8").strip().replace("\x00", "")
            if not text:
                continue
            for device in out:
                if key not in device_keys(device):
                    continue
                device["held"] = True
                device["state"] = text
                if text == "open:Opened" or text.startswith("open:Opened:"):
                    # ★ 2026-10-21 用户现场：“控制台显示正在接收手机声音，但一点声音都没有”。
                    #   helper 把 sink 打开（open:Opened）并不等于手机把 **A2DP 媒体音频**
                    #   送过来了：手机可能只挂着连接/控制通路。此时 Windows 的
                    #   `麦克风 (<手机名> A2DP SNK)` 采集端点还没起来（endpoint=False），
                    #   本工具自然没有可播的声音。必须报 waiting（黄色提示），不能报
                    #   connected —— 否则面板会骗用户“正在接收”，而实际是静音。
                    #   反过来，用户打开手机「媒体音频」后端点会变成 active，而设备枚举
                    #   缓存 10 分钟才刷新一次；这里补一次**注册表级**的低成本实时判断。
                    live_endpoint = _endpoint_state_from_registry(str(device.get("name") or ""))
                    if live_endpoint is not None:
                        device["endpoint"] = live_endpoint
                    device["link"] = "connected" if bool(device.get("endpoint")) else "waiting"
                elif text.startswith("open:"):
                    # ★ 2026-11-06（Q5）：现场同一张卡同时写「手机已连接，但媒体音频未到」
                    #   和「sink=True｜手机媒体链路=True」—— 因为这一支只把 link 改成
                    #   waiting，却沿用了枚举缓存里的 endpoint=True。现在统一以**注册表
                    #   实时值**为准：端点真的 active 就报 connected；读不到/未激活就
                    #   把 endpoint 一并置 False，绝不出现"说没到、布尔却是 True"。
                    live_endpoint = _endpoint_state_from_registry(str(device.get("name") or ""))
                    device["endpoint"] = bool(live_endpoint) if live_endpoint is not None else False
                    if bool(device.get("endpoint")):
                        device["link"] = "connected"
                        device["state_note"] = ("接收通道 state=%s，但 Windows 媒体端点已激活"
                                                "（声音由系统端点直接送达）" % text)
                    else:
                        device["link"] = "waiting"
                        device["state_note"] = ""
                elif text.startswith("error:"):
                    device["link"] = "error"
                else:
                    device["link"] = "off"
                    device["held"] = False
    except OSError:
        pass
    return out


def cached_devices() -> list[dict]:
    """Whatever the last refresh found, with live state applied. Never blocks."""
    with _CACHE_LOCK:
        devices = list(_CACHE["devices"])
    return _apply_live_states(devices)


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0
