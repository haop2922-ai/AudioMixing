"""Audio Mixing: cast the sound of many devices to the speakers you choose, over LAN."""

# 版本号会显示在面板顶栏，并写进信标告诉对端。
# 用户上一次拿一个 7 小时前的旧 exe 测了一整天 —— 版本必须让人一眼看到。
__version__ = "2.32"

# ＃ 手机端版本号（与 android/app/build.gradle 的 versionName 同步）。
# 交付 APK 的文件名、面板二维码说明都用它 —— 用户实机装错过版本，名字里必须能一眼看出来。
__phone_version__ = "2.20"

# 打包时由 tools/build_nuitka.py 生成 audiomixing/_buildmeta.py 写入本次构建时间，
# 再随包编进去；源码运行时没有这个文件，就是空串（此时面板回退去看文件时间）。
# 为什么要写死：Nuitka onefile 下 sys.executable 指向解包出来的临时 python.exe，
# 靠文件时间拿不到构建时间；而且 exe 复制到桌面后文件时间会变，构建时间不该跟着变。
try:
    from ._buildmeta import BUILD_TIME as __build_time__
except Exception:          # 源码运行 / 文件缺失
    __build_time__ = ""

__all__ = ["__version__", "__phone_version__"]
