"""WaveLink network doctor - prints everything needed to diagnose "phone cannot connect".

Run it, then copy the whole output. Output is ASCII-only on purpose so it survives
copy/paste and any console code page.

    dist\\AudioMixing.exe netcheck          (packaged)
    python -m audiomixing netcheck          (from source)

What it answers:
  1. is a WaveLink node actually running, and on which port?
  2. is the UDP audio port bound?
  3. which of this machine's addresses are usable from another device?
  4. are the Windows firewall rules present?
  5. is the active network profile Private or Public?
  6. can this machine reach itself over its LAN address (loopback bypass test)?
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Config, config_path
from .net import discovery as disc

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"

problems: list[str] = []


def line(text: str = "") -> None:
    print(text)


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run_cmd(args: list[str], timeout: float = 20.0) -> str:
    try:
        proc = subprocess.run(args, creationflags=NO_WINDOW, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        return (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_label(ok: bool, name: str, detail: str = "") -> None:
    print("%s %s%s" % (OK if ok else BAD, name, (" - " + detail) if detail else ""))
    if not ok:
        problems.append(name)


def probe_health(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=2.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def port_bound(port: int, kind: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if kind == "udp" else socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wavelink netcheck", description="诊断手机/别的电脑连不上的原因")
    parser.add_argument("--web-port", type=int, default=0)
    args = parser.parse_args(argv)

    # Read the config but never create or modify it: a diagnostic that changes state
    # is not a diagnostic, and an earlier version left test ports in %APPDATA%.
    cfg = Config.load(create=False)
    web_port = args.web_port or cfg.web_port
    audio_port = cfg.audio_port

    line("=" * 72)
    line(" WaveLink netcheck")
    line("=" * 72)
    line("%s system      : %s %s" % (INFO, platform.system(), platform.release()))
    line("%s python      : %s" % (INFO, sys.version.split()[0]))
    line("%s config file : %s" % (INFO, config_path()))
    line("%s saved ports : web=%d audio=%d discovery=%d" % (INFO, cfg.web_port, cfg.audio_port, cfg.discovery_port))
    # Non-default ports are the number one cause of "I typed 17800 and it does not work":
    # the config remembers whatever a previous `--web-port` run used.
    defaults = (17800, 47830, 47831)
    actual = (cfg.web_port, cfg.discovery_port, cfg.audio_port)
    if actual != defaults:
        line("%s these differ from the defaults %s" % (WARN, defaults))
        line("       a previous run with --web-port/--audio-port was saved here.")
        line("       the phone must use the port printed below, not 17800.")
        line("       to go back to the defaults, run fix-phone-connection.bat")
        problems.append("ports changed by a previous run (web=%d)" % cfg.web_port)
    line()

    # ---- 1. is the node running? ----------------------------------------
    line("--- 1. node status ---")
    status = probe_health(web_port)
    if status is None and web_port != 17800:
        if probe_health(17800) is not None:
            line("%s nothing on port %d, but a node answers on 17800" % (WARN, web_port))
            problems.append("saved port (%d) does not match the running node (17800)" % web_port)
            web_port = 17800
            status = probe_health(web_port)
    if status is None:
        line("%s no WaveLink node answers on port %d" % (BAD, web_port))
        line("       -> start it first: dist\\AudioMixing.exe  (or python -m audiomixing serve)")
        problems.append("node is not running")
    else:
        node = status.get("node", {})
        line("%s node running: %s (port %s, up %.0fs)"
             % (OK, node.get("name"), node.get("audio_port"), float(node.get("uptime_s") or 0)))
        line("%s listening     : %s" % (INFO, json.dumps(
            {k: v for k, v in status.get("totals", {}).items()}, ensure_ascii=True)))
    line()

    # ---- 2. audio port ---------------------------------------------------
    line("--- 2. audio port ---")
    check_label(port_bound(audio_port, "udp"), "udp/%d is bound (audio can be received)" % audio_port)
    line()

    # ---- 3. addresses ----------------------------------------------------
    line("--- 3. addresses (use the LAN one on the phone) ---")
    report = disc.lan_report()
    lan: list[str] = []
    for iface in report["interfaces"]:
        tag = "LAN  <-- use this" if iface["kind"] == "private" else "NOT LAN (VPN/public, phone cannot use)"
        line("%s %-16s %-18s %s" % (INFO, iface["address"], iface["subnet"], tag))
        if iface["kind"] == "private":
            lan.append(iface["address"])
    if not lan:
        line("%s no LAN address found - connect Wi-Fi or a network cable" % BAD)
        problems.append("no LAN address")
    else:
        for ip in lan:
            line("       -> http://%s:%d" % (ip, web_port))
    line()

    # ---- 4. firewall -----------------------------------------------------
    line("--- 4. Windows firewall ---")
    if platform.system() == "Windows":
        rules = run_cmd(["powershell", "-NoProfile", "-Command",
                         "Get-NetFirewallRule -DisplayName 'WaveLink*' -ErrorAction SilentlyContinue | "
                         "Select-Object DisplayName,Enabled,Direction,Action,Profile,Program | "
                         "Format-Table -AutoSize | Out-String -Width 200"])
        rules_lower = rules.lower()
        if rules.strip():
            for r in rules.strip().splitlines():
                line("%s %s" % (INFO, r.rstrip()))
            # Rule names are case-insensitive in Windows, but our own string check was
            # not: a rule literally named "audiomixing" failed a "AudioMixing" test.
            check_label("audiomixing" in rules_lower, "WaveLink firewall rules exist")
            if "audiomixing.exe" not in rules_lower:
                line("%s the rules are not tied to AudioMixing.exe (port-only rules)" % INFO)
            elif "deep blue" not in rules_lower:
                line("%s note: the program path in the rules may be from another machine/copy" % WARN)
        else:
            line("%s no WaveLink firewall rule found" % BAD)
            line("       -> run fix-phone-connection.bat as administrator")
            problems.append("no firewall rule")

        profiles = run_cmd(["powershell", "-NoProfile", "-Command",
                            "Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction | "
                            "Format-Table -AutoSize | Out-String -Width 120"])
        for r in profiles.strip().splitlines():
            line("%s %s" % (INFO, r.rstrip()))

        netprof = run_cmd(["powershell", "-NoProfile", "-Command",
                           "Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,NetworkCategory | "
                           "Format-Table -AutoSize | Out-String -Width 160"])
        for r in netprof.strip().splitlines():
            line("%s %s" % (INFO, r.rstrip()))
        if "Public" in netprof:
            line("%s the active network is categorised as 'Public'." % WARN)
            line("       Windows applies its strictest rules there. The fix tool adds rules for all profiles,")
            line("       or set it to Private: Settings > Network > Ethernet/Wi-Fi > Private network.")
    else:
        line("%s not Windows, skipping firewall inspection" % INFO)
    line()

    # ---- 5. self reachability -------------------------------------------
    line("--- 5. reachability from another device (self test) ---")
    for ip in lan:
        u = "http://%s:%d/healthz" % (ip, web_port)
        try:
            with urllib.request.urlopen(u, timeout=3.0) as resp:
                ok = resp.status == 200
            check_label(ok, "reachable via %s" % u, "HTTP %s" % resp.status)
            line("       (this only proves the server listens on that address; the phone also needs")
            line("        the same subnet, no AP isolation, and the firewall rule above)")
        except Exception as exc:
            check_label(False, "reachable via %s" % u, str(exc)[:80])
    line()

    # ---- verdict ---------------------------------------------------------
    line("=" * 72)
    if problems:
        line(" problems found: %d" % len(problems))
        for p in problems:
            line("   - %s" % p)
        line()
        line(" most common fixes, in order:")
        line("   1. run fix-phone-connection.bat as administrator (adds rules, resets ports)")
        line("   2. use the LAN address printed in section 3, with the port printed there too")
        line("   3. make sure the phone is on the same Wi-Fi (not mobile data, not a guest network)")
        line("   4. if the Wi-Fi has AP isolation, use the phone as a hotspot and join it from the PC")
    else:
        line(" no problems found on this machine.")
        line(" if the phone still cannot connect, the block is in the network path")
        line(" (AP isolation, different subnet, or a VPN on the phone).")
    line("=" * 72)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
