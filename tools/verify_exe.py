"""Fast smoke test for dist/WaveLink.exe — finishes in well under a minute.

    python tools/verify_exe.py          # full check (also runs `doctor`, ~40 s)
    python tools/verify_exe.py --quick  # just the critical paths, ~10 s

Checks, in order:
  1. `net`            - bundle starts, LAN detection works
  2. `devices`        - numpy + WASAPI/ctypes survived packaging
  3. `serve`          - real node on real ports, HTTP API answers, page is served
  4. `doctor`         - full offline chain (skipped with --quick)

Every external process gets a short timeout and is always killed, so a hung bundle
can never hang this script.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "WaveLink.exe"
QUICK = "--quick" in sys.argv

WEB_PORT = 17991
AUDIO_PORT = 48721
DISC_PORT = 48722

# Run the bundle against a throwaway config directory. Without this, starting the
# exe rewrites the *user's* %APPDATA%\WaveLink\config.json (node name, routes from
# the API calls below) and leaves test nodes broadcasting on their LAN -- which is
# exactly what happened during development and is not acceptable behaviour for a
# verification script.
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="wavelink-verify-"))

results: list[tuple[str, bool, str]] = []


def exe_env() -> dict[str, str]:
    env = dict(os.environ)
    env["WAVELINK_HOME"] = str(SANDBOX)
    return env


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail else ""), flush=True)
    return ok


def run(args: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [str(EXE), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=exe_env(),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT after %.0fs" % timeout


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a PyInstaller onefile process *and* its children.

    ``--onefile`` runs a bootloader parent that spawns the real Python child. Calling
    ``terminate()`` on the parent leaves the child alive, holding the exe and the UDP
    ports, which then breaks the next build with a locked file. taskkill /T is the
    reliable way on Windows.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=20)
    else:  # pragma: no cover - other platforms
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_port_free(port: int, timeout: float = 10.0) -> bool:
    """Wait until nothing is listening on `port` (so the next run is clean)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.3)
        finally:
            s.close()
    return False


def ping_audio_port(port: int, timeout: float = 3.0) -> bool:
    """Send the node a PING and wait for its PONG on the audio port."""
    sys.path.insert(0, str(ROOT))
    from audiomixing import proto  # imported lazily so this script works standalone

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        pkt = proto.Packet.control(proto.T_PING, {"t0": proto.now_ns(), "node": "exe-verify"})
        sock.sendto(pkt.encode(), ("127.0.0.1", port))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _addr = sock.recvfrom(4096)
            except socket.timeout:
                return False
            reply = proto.decode(data)
            if reply is not None and reply.type == proto.T_PONG:
                return True
        return False
    except OSError:
        return False
    finally:
        sock.close()


def user_config_fingerprint() -> str:
    """Snapshot of the real user config, to prove this run does not touch it."""
    import hashlib

    cfg = pathlib.Path(os.environ.get("APPDATA", "")) / "WaveLink" / "config.json"
    if not cfg.is_file():
        return "absent"
    try:
        return "sha256:" + hashlib.sha256(cfg.read_bytes()).hexdigest()[:16]
    except OSError as exc:
        return "unreadable:%s" % exc


def main() -> int:
    if not EXE.is_file():
        print("找不到 %s，先运行 python tools/build_exe.py" % EXE)
        return 1
    real_config = pathlib.Path(os.environ.get("APPDATA", "")) / "WaveLink" / "config.json"
    before = user_config_fingerprint()
    print("验证对象: %s (%.1f MB)  %s" % (EXE, EXE.stat().st_size / 1024 / 1024, "快速模式" if QUICK else "完整模式"))
    print("沙箱配置目录: %s" % SANDBOX)
    print("用户配置: %s (启动前指纹 %s)" % (real_config, before))
    print("=" * 70)

    started = time.time()
    code, out = run(["net"], 40)
    # Use an ASCII marker: the bundle always prints the discovered LAN address.
    check("exe net（局域网识别）", code == 0 and ("192.168." in out or "10." in out or "172." in out),
          "%.1fs" % (time.time() - started))

    started = time.time()
    code, out = run(["devices"], 60)
    check("exe devices（numpy + WASAPI）", code == 0 and "wasapi" in out, "%.1fs" % (time.time() - started))

    if not QUICK:
        started = time.time()
        code, out = run(["doctor"], 150)
        check("exe doctor（端到端自检）", code == 0 and "自检通过" in out, "%.1fs" % (time.time() - started))
    # --- real node over HTTP -------------------------------------------
    proc = subprocess.Popen(
        [str(EXE), "serve", "--host", "127.0.0.1", "--web-port", str(WEB_PORT),
         "--audio-port", str(AUDIO_PORT), "--discovery-port", str(DISC_PORT), "--name", "exe验证"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=exe_env(),
    )
    try:
        base = "http://127.0.0.1:%d" % WEB_PORT
        healthy = False
        t0 = time.time()
        while time.time() - t0 < 30:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
                    healthy = bool(json.loads(resp.read().decode("utf-8")).get("ok"))
                if healthy:
                    break
            except Exception:
                time.sleep(0.3)
        check("exe serve 启动", healthy, "%.1fs" % (time.time() - t0))

        if healthy:
            with urllib.request.urlopen(base + "/", timeout=5) as resp:
                html = resp.read().decode("utf-8")
            check("控制台页面可打开", all(m in html for m in ('id="peersBox"', 'id="routesBox"', 'id="lanUrl"')),
                  "%d 字节" % len(html))

            with urllib.request.urlopen(base + "/api/status", timeout=5) as resp:
                status = json.loads(resp.read().decode("utf-8"))
            node = status.get("node", {})
            check("节点信息正确", node.get("name") == "exe验证", "%s / udp %s" % (node.get("name"), node.get("audio_port")))
            check("打包后音频后端可用", bool(status.get("backends", {}).get("wasapi")), str(status.get("backends")))
            check("打包后能枚举设备", len(status.get("devices", {}).get("sources", [])) > 0,
                  "采集 %d 个" % len(status.get("devices", {}).get("sources", [])))

            # Prove the bundled node is really listening on its UDP audio port by
            # sending it a PING and expecting a PONG. Checking "can I bind this port"
            # is unreliable on Windows: a second UDP bind to a different local
            # address can succeed even while the first socket is alive.
            check("音频端口响应 PING", ping_audio_port(AUDIO_PORT), "udp/%d" % AUDIO_PORT)

            req = urllib.request.Request(
                base + "/api/routes",
                data=json.dumps({"routes": [{"id": "exe1", "from": ["self"], "to": ["*"], "codec": "pcm_s16"}]}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            check("通过 API 添加路由", bool(body.get("ok")) and body["routes"][0]["id"] == "exe1")

        breadcrumb = SANDBOX / "last-run.txt"
        check("写入 last-run.txt（沙箱内）", breadcrumb.is_file(), str(breadcrumb))
        # The sandbox must have absorbed everything: the user's real config must be
        # byte-for-byte unchanged after this run.
        after = user_config_fingerprint()
        check("用户配置未被改动", after == before, "前 %s / 后 %s" % (before, after))
    finally:
        kill_tree(proc)
        check("关闭后端口被释放", wait_port_free(AUDIO_PORT, 10.0), "udp/%d" % AUDIO_PORT)
        import shutil

        shutil.rmtree(SANDBOX, ignore_errors=True)

    print("=" * 70)
    failed = [r for r in results if not r[1]]
    print("共 %d 项检查，通过 %d，失败 %d" % (len(results), len(results) - len(failed), len(failed)))
    for name, _ok, detail in failed:
        print("  失败: %s %s" % (name, detail))
    print("RESULT:", "PASS" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
