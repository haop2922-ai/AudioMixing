"""Audio Mixing self-audit: find leftovers this project may have left behind.

Run before shipping anything:

    python tools/audit_leftovers.py
    python tools/audit_leftovers.py --fix     (clean what is safe to clean)

It checks, in two groups:

  ENVIRONMENT - things on the machine that Audio Mixing may have created and forgotten
      * running Audio Mixing processes (a double-launch leaves a zombie)
      * %APPDATA%\\AudioMixing config carrying non-default ports from a test run
      * manual_peers entries that are obviously test data
      * routes left over from automated tests
      * firewall rules pointing at an exe path that no longer exists
      * temp sandbox dirs from the verification scripts
      * the preview/test directories used during development

  PROJECT - things in the repository that should not ship
      * leftover temp files (logs, wav captures, .txt dumps)
      * dead modules that nothing imports
      * constants/functions defined but never used
      * the packaged exe being older than the newest source file

Exit code 0 when clean, 1 when findings exist (so CI can gate on it).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "audiomixing"
# 审计脚本要从仓库根导入合成身份判定（与运行时代码同一份规则，避免两处清单漂移）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

findings: list[tuple[str, str, str]] = []   # (severity, area, message)
FIX = False


def report(severity: str, area: str, message: str, fixable: bool = False, fix=None) -> None:
    findings.append((severity, area, message))
    mark = {"ERROR": "[!!]", "WARN": "[! ]", "INFO": "[i ]"}[severity]
    print("%s %-11s %s" % (mark, area, message))
    if FIX and fixable and fix is not None:
        try:
            fix()
            print("      -> fixed")
        except Exception as exc:
            print("      -> fix failed: %s" % exc)


# ---------------------------------------------------------------- environment

def identity_findings(cfg: dict) -> tuple[list[str], list[str]]:
    """配置里明显的测试身份：返回 (身份命中, 路由命中)。

    只报告，不修改 —— 用户可能正在用这台机器的身份跑，审计工具无权替他改名。
    """
    from audiomixing.config import _is_synthetic_node_id

    identity_hits: list[str] = []
    route_hits: list[str] = []
    if _is_synthetic_node_id(str(cfg.get("node_id") or "")):
        identity_hits.append("node_id=%r" % cfg.get("node_id"))
    for value in cfg.get("smart_link") or []:
        if _is_synthetic_node_id(str(value)):
            identity_hits.append("smart_link 里有 %r" % value)
    for route in cfg.get("routes") or []:
        if not isinstance(route, dict):
            continue
        rid = str(route.get("id") or "")
        if _is_synthetic_node_id(str(route.get("origin") or "")):
            route_hits.append("%s 的 origin=%r" % (rid, route.get("origin")))
        for key in ("from", "to"):
            for value in route.get(key) or []:
                if _is_synthetic_node_id(str(value)):
                    route_hits.append("%s 的 %s 里 %r" % (rid, key, value))
    return identity_hits, route_hits


def audit_environment() -> None:
    print("=" * 74)
    print(" ENVIRONMENT")
    print("=" * 74)

    # 1. running processes
    #
    # A PyInstaller onefile app *always* shows two processes (the bootloader plus the
    # child it extracts and runs), so a count of 2 says nothing by itself. What
    # matters is whether the node is answering: a second launch detects the live
    # panel and exits by itself, while a zombie serves nothing.
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AudioMixing.exe", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        if "AudioMixing.exe" in out:
            count = out.lower().count("audiomixing.exe")
            ports: list[str] = []
            try:
                cfg = pathlib.Path(os.environ.get("APPDATA", "")) / "AudioMixing" / "config.json"
                if cfg.is_file():
                    ports.append(str(json.loads(cfg.read_text(encoding="utf-8")).get("web_port") or ""))
            except (OSError, ValueError):
                pass
            ports.append("17800")
            live = ""
            for port in [p for p in ports if p]:
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=3) as resp:
                        if resp.status == 200:
                            live = port
                            break
                except Exception:
                    continue
            if live:
                print("[ok] process    AudioMixing.exe up (%d process = onefile parent+child), "
                      "panel http://127.0.0.1:%s answers" % (count, live))
            else:
                report("WARN", "process", "%d AudioMixing.exe process(es) but no panel answers - "
                                           "kill them and start a single instance" % count)
        else:
            print("[ok] process    no AudioMixing.exe running")

    # 2. config file
    appdata = os.environ.get("APPDATA")
    cfg_path = pathlib.Path(appdata) / "AudioMixing" / "config.json" if appdata else None
    if cfg_path and cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report("ERROR", "config", "unreadable config: %s" % exc)
            cfg = {}
        defaults = {"web_port": 17800, "audio_port": 47831, "discovery_port": 17830}
        off = {k: cfg.get(k) for k, v in defaults.items() if cfg.get(k) not in (None, v)}
        if off:
            # 端口是用户可以在「设置」里改的，也可能是启动时自动避开冲突选出来的；
            # 这里只提示，绝不能当成测试残留把整份配置删掉（那会丢路由和设置）。
            print("[i ] config     ports are non-default %s (user-custom or auto-selected; "
                  "keep unless you know a test run wrote them)" % off)
        peers = cfg.get("manual_peers") or []
        testish = [p.get("host") for p in peers if str(p.get("host", "")).startswith(("192.168.10.", "192.168.1.", "10.0.0."))]
        if testish:
            report("WARN", "config", "manual peers that look like test data: %s" % testish)
        routes = cfg.get("routes") or []

        def _looks_like_test(route: dict) -> bool:
            rid = str(route.get("id", ""))
            label = str(route.get("label") or "").strip().lower()
            return (rid.startswith(("exe", "w1", "r1", "rec", "in-", "many", "one-to",
                                    "wild", "both", "self2"))
                    or "test" in label or "测试" in label)

        suspicious = [r.get("id") for r in routes if _looks_like_test(r)]

        def _drop_test_routes() -> None:
            # 只删测试路由，保留用户自己的端口/路由/设置。
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            data["routes"] = [r for r in (data.get("routes") or []) if not _looks_like_test(r)]
            cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if suspicious:
            report("WARN", "config", "routes left over from tests: %s" % suspicious,
                   fixable=True, fix=_drop_test_routes)
        # ★ 2026-11-03（Q1）：以前只查 manual_peers 和路由前缀，于是 node_id="self-node"、
        #   smart_link=["peer-1"] 这种**测试身份**明明写在用户配置里，工具却打印
        #   `[ok] no test peers/routes` —— 假 PASS，让幽灵设备排查被误导很久。
        #   这里把合成身份全部找出来，但**只 WARN、不自动删**：动身份=改用户的设备身份，
        #   动 smart_link=改用户明确勾过的设备，审计工具没有资格替用户决定。
        #   启动时 Config.load() 会先备份 config，再把身份迁成真 UUID、把测试名单去掉。
        identity_hits, route_hits = identity_findings(cfg)
        if identity_hits:
            report("WARN", "identity", "test identity in config (启动时会备份并迁移，本次不自动改): %s"
                   % ", ".join(identity_hits))
        if route_hits:
            report("WARN", "identity", "routes reference test identities (启动时原地改写，不删路由): %s"
                   % ", ".join(route_hits))
        if not testish and not suspicious and not identity_hits and not route_hits:
            print("[ok] config     no test peers/routes/identities")
    else:
        print("[ok] config     no saved config (defaults will be used)")

    # 3. firewall rules pointing at a stale path
    if sys.platform == "win32":
        ps = ("Get-NetFirewallRule -DisplayName 'Audio Mixing*','WaveLink*','wavelink*' -ErrorAction SilentlyContinue | "
              "ForEach-Object { $af = $_ | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue; "
              "Write-Output ($_.DisplayName + '|' + $_.Enabled + '|' + $af.Program) }")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace").stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        lines = [l.strip() for l in out.splitlines() if l.strip() and "|" in l]
        stale = []
        for l in lines:
            parts = l.split("|")
            prog = parts[2] if len(parts) > 2 else ""
            if prog and prog not in ("", "Any", "NotConfigured") and not pathlib.Path(prog).exists():
                stale.append(prog)
        if stale:
            report("WARN", "firewall", "rules reference an exe that no longer exists: %s" % stale)
        elif lines:
            print("[ok] firewall   %d Audio Mixing rule(s), all program paths valid" % len(lines))
        else:
            print("[i ] firewall   no Audio Mixing rules (if the phone must connect, use the tray menu's 放行防火墙)")

    # 4. temp sandboxes from the verification scripts
    tmp = pathlib.Path(tempfile.gettempdir())
    # 新名 + 旧名都扫：v1.22 及更早版本建的临时目录叫 wavelink-*，
    # 它们可能还躺在 %TEMP% 里，顺手一起清掉。
    leftovers = []
    # 测试与验证脚本用过的所有前缀（含各测试类自己的 am-* 沙箱）。
    for _prefix in ("audiomixing-", "wavelink-", "am-blocked-", "am-beacon",
                    "am-ports-", "am-mig-", "am-home-", "am-p2p-"):
        leftovers += [p for p in tmp.glob(_prefix + "*")]
    # 构建/页面脚本留下的日志与导出的脚本文件。
    for _pattern in ("audiomixing-build-*.log", "audiomixing-page-*.js",
                     "wavelink-page-*.js", "wavelink-firewall.bat"):
        leftovers += [p for p in tmp.glob(_pattern)]
    if leftovers:
        def _drop_temp() -> None:
            for p in leftovers:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except OSError:
                        pass
        report("INFO", "temp", "%d test/verification leftovers in %s" % (len(leftovers), tmp),
               fixable=True, fix=_drop_temp)
    else:
        print("[ok] temp       no test/verification leftovers")

    # 5. dev scratch dirs inside the repo
    for name in ("_t", "_t2", "_t3", "_t4", "_t5", "_nc", "_deltest", "_preview_home",
                 ".tmp", "build"):
        p = ROOT / name
        if p.exists():
            fixable = name != "build"
            report("INFO" if name == "build" else "WARN", "scratch",
                   "%s/ exists (%s)" % (name, "PyInstaller build cache, safe to delete" if name == "build" else "development scratch dir"),
                   fixable=fixable, fix=(lambda pp=p: shutil.rmtree(pp, ignore_errors=True)) if fixable else None)

    # 6. a log the fix script may have written
    if sys.platform == "win32":
        log = tmp / "audiomixing-fix.log"
        if log.is_file():
            age_h = (time.time() - log.stat().st_mtime) / 3600.0
            if age_h > 24:
                report("INFO", "temp", "old firewall-fix log (%.0fh): %s" % (age_h, log),
                       fixable=True, fix=lambda: log.unlink())
            else:
                print("[ok] temp       firewall-fix log is recent (%.1fh)" % age_h)


# ---------------------------------------------------------------- project

def audit_project() -> None:
    print()
    print("=" * 74)
    print(" PROJECT")
    print("=" * 74)

    # 1. temp / junk files. Deliberately excludes __init__.py: the pattern "_*.py"
    # matched them and produced five bogus "leftover file" warnings.
    junk_patterns = ("*.log", "*.out", "*.err", "*.tmp", "*.wav", "*.orig", "*.bak")
    junk: list[pathlib.Path] = []
    for pat in junk_patterns:
        for p in ROOT.rglob(pat):
            if any(part in ("build", "dist", "__pycache__", ".git", "attachments") for part in p.parts):
                continue
            junk.append(p)
    # _*.txt / _*.py scratch dumps only count inside tools/ and the repo root, and
    # never when they are package files.
    for pat in ("_*.txt", "_*.py", "_*.log"):
        for p in list(ROOT.glob(pat)) + list((ROOT / "tools").glob(pat)):
            if p.name.startswith("__") or p not in junk:
                if not p.name.startswith("__"):
                    junk.append(p)
    if junk:
        report("WARN", "junk", "%d leftover file(s): %s" % (len(junk), [str(p.relative_to(ROOT)) for p in junk[:8]]),
               fixable=True, fix=lambda: [p.unlink() for p in junk])
    else:
        print("[ok] junk       no leftover logs/captures/scratch files")

    # 2. __pycache__
    caches = [p for p in ROOT.rglob("__pycache__") if "build" not in p.parts]
    if caches:
        report("INFO", "junk", "%d __pycache__ dirs" % len(caches),
               fixable=True, fix=lambda: [shutil.rmtree(p, ignore_errors=True) for p in caches])
    else:
        print("[ok] junk       no __pycache__ dirs")

    # 3. dead modules: a file in the package that nothing imports
    modules: dict[str, pathlib.Path] = {}
    for p in PKG.rglob("*.py"):
        if p.name in ("__init__.py", "__main__.py"):
            continue
        mod = ".".join(p.relative_to(ROOT).with_suffix("").parts)
        modules[mod] = p

    def _scan_sources(pattern: str) -> dict[pathlib.Path, str]:
        """读源码做引用扫描；跳过第三方/历史备份目录，读不了的文件也跳过。

        promo/node_modules 这类目录里有权限受限的文件，以前这里直接 PermissionError
        把整个审计工具崩掉 —— 门禁跑不完比漏报更糟。
        """
        skip_dirs = {"promo", "版本备份", "release", "dist_nuitka", ".git",
                     "node_modules", "__pycache__", ".screenshots"}
        out: dict[pathlib.Path, str] = {}
        for p in ROOT.rglob(pattern):
            if any(part in skip_dirs for part in p.parts):
                continue
            try:
                out[p] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return out

    sources = _scan_sources("*.py")
    sources.update(_scan_sources("*.js"))
    for mod, path in sorted(modules.items()):
        short = mod.split(".")[-1]
        referenced = False
        for src_path, text in sources.items():
            if src_path == path:
                continue
            if mod in text or re.search(r"\b%s\b" % re.escape(short), text):
                referenced = True
                break
        if not referenced:
            report("WARN", "dead-code", "module never imported anywhere: %s" % mod)
    if not any(f[1] == "dead-code" for f in findings):
        print("[ok] dead-code  every module is referenced")

    # 4. unused module-level *functions* only.
    #    Constants are deliberately not reported: protocol constants, WASAPI flag
    #    names and enum-like values document the wire/OS contract, and several exist
    #    only to name a value that appears in a comment or is reserved for a planned
    #    code path. Reporting them produced ~35 lines of noise hiding real findings.
    PUBLIC_API_HINTS = ("base.py", "__init__.py")
    # Documented public API / deliberate reference implementations. Each entry says
    # WHY it exists, so the list cannot silently become a dumping ground.
    ALLOWED_UNUSED = {
        "frames_to_samples": "unit helper for the audio API surface",
        "frames_to_ms": "unit helper; used by tests and ad-hoc debugging",
        "bytes_per_frame": "documents codec bandwidth; kept next to the codec table",
        "apply_gain": "documented API for offline/gain processing",
        "trim_leading_silence": "diagnostic helper; used by tools",
        "from_float32_fmt": "documented API: encode float32 to a device format",
        "best_lag": "reference correlation helper; covered by tests",
        "resample_linear": "reference resampler implementation; covered by tests",
        "wav_info": "documented helper for inspecting captured WAV files",
        "read_wav_float": "documented helper for offline analysis",
    }
    for p in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            report("ERROR", "syntax", "%s: %s" % (p.relative_to(ROOT), exc))
            continue
        if any(h in str(p) for h in PUBLIC_API_HINTS):
            continue
        funcs = [n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and not n.name.startswith("_") and n.name != "main"]
        text_all = "".join(sources.values())
        for name in funcs:
            if len(re.findall(r"\b%s\b" % re.escape(name), text_all)) <= 1:
                if name in ALLOWED_UNUSED:
                    continue
                report("WARN", "dead-code", "%s: function '%s' is defined but never called" % (p.relative_to(ROOT), name))
    if not any(f[1] == "dead-code" for f in findings):
        print("[ok] dead-code  no orphaned functions (%d documented exceptions)" % len(ALLOWED_UNUSED))

    # 5. is the packaged exe stale?
    #    ★ 2026-10-15：交付流程早就只剩 Nuitka 这一条了（tools/build_nuitka.py → dist_nuitka/），
    #      这里却一直只看 PyInstaller 时代的 dist/ —— 于是每轮自查都挂着一条
    #      "dist/AudioMixing.exe does not exist" 的假警告。假警告多了，这一项就没人看了。
    exe = next((p for p in (ROOT / "dist_nuitka" / "AudioMixing.exe",
                            ROOT / "dist" / "AudioMixing.exe") if p.is_file()), None)
    newest_src = max((p.stat().st_mtime for p in PKG.rglob("*.py")), default=0)
    ui = PKG / "ui" / "index.html"
    if ui.is_file():
        newest_src = max(newest_src, ui.stat().st_mtime)
    if exe is not None:
        shown = exe.relative_to(ROOT)
        if exe.stat().st_mtime < newest_src:
            report("ERROR", "package", "%s is OLDER than the newest source file - rebuild it (python tools/build_nuitka.py)" % shown)
        else:
            age_min = (time.time() - exe.stat().st_mtime) / 60.0
            print("[ok] package    %s is newer than all sources (built %.0f min ago)" % (shown, age_min))
    else:
        report("WARN", "package", "还没有打包好的 exe（跑 python tools/build_nuitka.py）")

    # 7. critical API surface: an editing accident once removed a dozen functions
    #    from audio/format.py (sine/chirp generators, Resampler, the device format
    #    converters) and nothing caught it until tests failed. Pin the names that the
    #    rest of the project imports, so a silent removal becomes a loud failure.
    CRITICAL = {
        "audiomixing/audio/format.py": [
            "make_tone", "Chirp", "to_float32", "from_float32_f32", "from_float32_s16",
            "from_float32_fmt", "channel_map", "Resampler", "resample_linear", "peak",
            "rms", "saturating_mix_into", "apply_gain", "signal_report",
            "measure_frequency", "best_lag", "trim_leading_silence", "correlation",
            "to_mono", "as_float_array", "clamp", "ms_to_frames", "frames_to_ms",
            "bytes_per_frame", "frames_to_samples",
        ],
        "audiomixing/proto.py": [
            "Packet", "decode", "encode_payload", "decode_payload", "ClockSync",
            "ping_body", "pong_body", "clock_sample", "StreamStats",
        ],
        "audiomixing/net/rtpaudio.py": ["Receiver", "SenderSession", "Sender", "JitterBuffer"],
        "audiomixing/audio/capture.py": ["CaptureHub", "TapReader"],
        "audiomixing/engine.py": ["AudioMixingNode"],
        "audiomixing/webui.py": ["WebUI", "index_html"],
    }
    missing_api: list[str] = []
    for rel, names in CRITICAL.items():
        path = ROOT / rel
        if not path.is_file():
            missing_api.append("%s (file missing)" % rel)
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for name in names:
            if name not in defined:
                missing_api.append("%s::%s" % (rel, name))
    if missing_api:
        report("ERROR", "api", "critical names missing: %s" % ", ".join(missing_api))
    else:
        total = sum(len(v) for v in CRITICAL.values())
        print("[ok] api        %d critical names present across %d modules" % (total, len(CRITICAL)))

    # 8. docs consistency: README must not name a script that no longer exists
    readme = ROOT / "README.md"
    if readme.is_file():
        text = readme.read_text(encoding="utf-8", errors="replace")
        for m in set(re.findall(r"([A-Za-z0-9_\-]+\.(?:bat|ps1))", text)):
            if not (ROOT / m).exists():
                report("ERROR", "docs", "README references %s which does not exist" % m)
        for m in set(re.findall(r"`(audiomixing [a-z]+)", text)):
            pass
        if not any(f[1] == "docs" for f in findings):
            print("[ok] docs       README script references all exist")

    # 7. CLI subcommands advertised in README vs implemented
    cli = (PKG / "cli.py").read_text(encoding="utf-8")
    implemented = set(re.findall(r'sub\.add_parser\("([a-z]+)"', cli)) | set(re.findall(r'\w+ = sub\.add_parser\("([a-z]+)"', cli))
    implemented |= set(re.findall(r'add_parser\("([a-z]+)"', cli))
    readme_text = readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else ""
    advertised = set(re.findall(r"audiomixing ([a-z]+)", readme_text))
    missing = sorted(a for a in advertised if a not in implemented and a not in ("ack",))
    if missing:
        report("WARN", "docs", "README mentions commands that are not implemented: %s" % missing)
    else:
        print("[ok] docs       every documented command exists (%d subcommands)" % len(implemented))


def main() -> int:
    global FIX
    ap = argparse.ArgumentParser(description="audit Audio Mixing for leftovers")
    ap.add_argument("--fix", action="store_true", help="clean what is safe to clean")
    args = ap.parse_args()
    FIX = args.fix

    print("Audio Mixing self-audit   repo=%s" % ROOT)
    print("mode: %s" % ("FIX (will clean safe items)" if FIX else "report only (use --fix to clean)"))
    print()
    audit_environment()
    audit_project()

    errors = [f for f in findings if f[0] == "ERROR"]
    warns = [f for f in findings if f[0] == "WARN"]
    infos = [f for f in findings if f[0] == "INFO"]
    print()
    print("=" * 74)
    print(" %d error(s), %d warning(s), %d note(s)" % (len(errors), len(warns), len(infos)))
    print("=" * 74)
    return 1 if (errors or warns) else 0


if __name__ == "__main__":
    raise SystemExit(main())
