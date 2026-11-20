# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

# ★ 2026-11-02：这个 spec 原来把开发机的绝对路径（E:/AI/deep blue/…）写死，
#   换一台机器/换一个检出目录就构建失败，或者把另一份旧代码打进 exe。
#   一律以 spec 文件所在目录为根（PyInstaller 提供 SPECPATH）。
ROOT = os.path.normpath(SPECPATH)
UI = os.path.join(ROOT, "audiomixing", "ui")
LAUNCHER = os.path.join(ROOT, "audiomixing_launcher.py")
ICON = os.path.join(ROOT, "tools", "audiomixing.ico")

datas = [(UI, 'audiomixing/ui')]
binaries = []
hiddenimports = ['audiomixing.tray', 'audiomixing.audio.opus', 'opuslib_next', 'qrcode', 'audiomixing.audio.backends.wasapi', 'audiomixing.audio.backends.wasapi_com', 'audiomixing.audio.backends.file', 'audiomixing.audio.backends.null', 'audiomixing.net.discovery', 'audiomixing.net.rtpaudio', 'audiomixing.netcheck', 'audiomixing.webui']
tmp_ret = collect_all('numpy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pyogg')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('qrcode')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    [LAUNCHER],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='AudioMixing',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON],
)
