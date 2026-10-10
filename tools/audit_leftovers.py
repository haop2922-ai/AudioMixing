"""WaveLink self-audit: find leftovers this project may have left behind.

Run before shipping anything:

    python tools/audit_leftovers.py
    python tools/audit_leftovers.py --fix     (clean what is safe to clean)

It checks, in two groups:

  ENVIRONMENT - things on the machine that WaveLink may have created and forgotten
      * running WaveLink processes (a double-launch leaves a zombie)
      * %APPDATA%\\WaveLink config carrying non-default ports from a test run
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
        defaults = {"web_port": 17800, "audio_port": 47831, "discovery_port": 47830}
        off = {k: cfg.get(k) for k, v in defaults.items() if cfg.get(k) not in (None, v)}
        if off:
            report("WARN", "config",
                   "ports differ from defaults %s (a previous test run saved them; "
                   "the phone/other PC must use the port shown in the black window)" % off,
                   fixable=True, fix=lambda: cfg_path.unlink())
        peers = cfg.get("manual_peers") or []
        testish = [p.get("host") for p in peers if str(p.get("host", "")).startswith(("192.168.10.", "192.168.1.", "10.0.0."))]
        if testish:
            report("WARN", "config", "manual peers that look like test data: %s" % testish,
                   fixable=True, fix=lambda: cfg_path.unlink())
        routes = cfg.get("routes") or []
        suspicious = [r.get("id") for r in routes if str(r.get("id", "")).startswith(("exe", "w1", "r1", "rec", "in-", "many", "one-to", "wild", "both", "self2"))]
        if suspicious:
            report("WARN", "config", "routes left over from tests: %s" % suspicious,
                   fixable=True, fix=lambda: cfg_path.unlink())
        if not off and not testish and not suspicious:
            print("[ok] config     ports=defaults, no test peers/routes")
    else:
        print("[ok] config     no saved config (defaults will be used)")

    # 3. firewall rules pointing at a stale path
    if sys.platform == "win32":
        ps = ("Get-NetFirewallRule -DisplayName 'WaveLink*','wavelink*' -ErrorAction SilentlyContinue | "
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
            print("[ok] firewall   %d WaveLink rule(s), all program paths valid" % len(lines))
        else:
            print("[i ] firewall   no WaveLink rules (run fix-phone-connection.bat if the phone must connect)")

    # 4. temp sandboxes from the verification scripts
    tmp = pathlib.Path(tempfile.gettempdir())
    leftovers = [p for p in tmp.glob("wavelink-verify-*")]
    leftovers += [p for p in tmp.glob("wavelink-check-*")]
    leftovers += [p for p in tmp.glob("wavelink-selftest-*")]
    leftovers += [p for p in tmp.glob("wavelink-diag-*")]
    if leftovers:
        report("INFO", "temp", "%d verification sandbox dirs in %s" % (len(leftovers), tmp),
               fixable=True, fix=lambda: [shutil.rmtree(p, ignore_errors=True) for p in leftovers])
    else:
        print("[ok] temp       no verification sandbox dirs")

    # 5. dev scratch dirs inside the repo
    for name in ("_t", "_t2", "_t3", "_t4", "_t5", "_nc", "_deltest", "_preview_home", "build"):
        p = ROOT / name
        if p.exists():
            fixable = name != "build"
            report("INFO" if name == "build" else "WARN", "scratch",
                   "%s/ exists (%s)" % (name, "PyInstaller build cache, safe to delete" if name == "build" else "development scratch dir"),
                   fixable=fixable, fix=(lambda pp=p: shutil.rmtree(pp, ignore_errors=True)) if fixable else None)

    # 6. a log the fix script may have written
    if sys.platform == "win32":
        log = tmp / "wavelink-fix.log"
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
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in ROOT.rglob("*.py")}
    sources.update({p: p.read_text(encoding="utf-8", errors="replace") for p in ROOT.rglob("*.js")})
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
    exe = ROOT / "dist" / "AudioMixing.exe"
    newest_src = max((p.stat().st_mtime for p in PKG.rglob("*.py")), default=0)
    ui = PKG / "ui" / "index.html"
    if ui.is_file():
        newest_src = max(newest_src, ui.stat().st_mtime)
    if exe.is_file():
        if exe.stat().st_mtime < newest_src:
            report("ERROR", "package", "dist/AudioMixing.exe is OLDER than the newest source file - rebuild it (python tools/build_exe.py)")
        else:
            age_min = (time.time() - exe.stat().st_mtime) / 60.0
            print("[ok] package    dist/AudioMixing.exe is newer than all sources (built %.0f min ago)" % age_min)
    else:
        report("WARN", "package", "dist/AudioMixing.exe does not exist")

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
        "audiomixing/engine.py": ["WaveLinkNode"],
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
        for m in set(re.findall(r"`(wavelink [a-z]+)", text)):
            pass
        if not any(f[1] == "docs" for f in findings):
            print("[ok] docs       README script references all exist")

    # 7. CLI subcommands advertised in README vs implemented
    cli = (PKG / "cli.py").read_text(encoding="utf-8")
    implemented = set(re.findall(r'sub\.add_parser\("([a-z]+)"', cli)) | set(re.findall(r'\w+ = sub\.add_parser\("([a-z]+)"', cli))
    implemented |= set(re.findall(r'add_parser\("([a-z]+)"', cli))
    readme_text = readme.read_text(encoding="utf-8", errors="replace") if readme.is_file() else ""
    advertised = set(re.findall(r"wavelink ([a-z]+)", readme_text))
    missing = sorted(a for a in advertised if a not in implemented and a not in ("ack",))
    if missing:
        report("WARN", "docs", "README mentions commands that are not implemented: %s" % missing)
    else:
        print("[ok] docs       every documented command exists (%d subcommands)" % len(implemented))


def main() -> int:
    global FIX
    ap = argparse.ArgumentParser(description="audit WaveLink for leftovers")
    ap.add_argument("--fix", action="store_true", help="clean what is safe to clean")
    args = ap.parse_args()
    FIX = args.fix

    print("WaveLink self-audit   repo=%s" % ROOT)
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
