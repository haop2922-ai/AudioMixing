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
_STATE_DIR = pathlib.Path(tempfile.gettempdir()) / "wavelink-bt"
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
  foreach ($e in $endpoints) {
    if ($e.FriendlyName -like ("*" + $d.Name + "*") -and $e.Status -eq "OK") {
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
$deviceId = $env:WAVELINK_BT_ID
$sentinel = $env:WAVELINK_BT_SENTINEL
$status   = $env:WAVELINK_BT_STATUS
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


def available() -> bool:
    """True on Windows with a usable PowerShell - i.e. where this can work at all."""
    return sys.platform == "win32" and bool(POWERSHELL)


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
            "name": str(item.get("name") or "未知设备"),
            "state": state,
            "held": held,
            "endpoint": endpoint_active,
            "hfp": hfp,
            "link": link,
        })
    return out


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
    for suffix in ("keep", "pid", "status", "log"):
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
        for stale in (sentinel, status_file, pid_file, log_file):
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                # A helper that is still shutting down can hold its log for a moment.
                pass
        sentinel.write_text("1", encoding="utf-8")
        status_file.write_text("starting", encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as fh:
            fh.write(_HOLD_SCRIPT)
            script = fh.name
        env = dict(os.environ)
        env.update({
            "WAVELINK_BT_ID": device_id,
            "WAVELINK_BT_SENTINEL": str(sentinel),
            "WAVELINK_BT_STATUS": str(status_file),
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
                    [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
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

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "devices": []}


def list_devices_cached(max_age: float = 10.0, force: bool = False) -> list[dict]:
    """The device list, refreshed at most every `max_age` seconds.

    Listing costs about three seconds - it starts a PowerShell helper - so it must never
    happen on a request the panel polls. Only the background refresh and the explicit
    刷新 button come through here.
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        if not force and _CACHE["devices"] and now - _CACHE["at"] < max_age:
            return _apply_live_states(_CACHE["devices"])
    devices = list_devices()
    with _CACHE_LOCK:
        _CACHE["at"] = time.monotonic()
        _CACHE["devices"] = list(devices)
    # 出口也必须合并实时状态，否则面板读到的还是枚举那一刻的旧结论
    return _apply_live_states(devices)


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
    for device in out:
        if str(device.get("link") or "") == "connected":
            if _key(str(device.get("id") or "")) not in held_keys:
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
                if _key(str(device.get("id") or "")) != key:
                    continue
                device["held"] = True
                device["state"] = text
                if text == "open:Opened" or text.startswith("open:Opened:"):
                    device["link"] = "connected"
                elif text.startswith("open:"):
                    device["link"] = "waiting"
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
