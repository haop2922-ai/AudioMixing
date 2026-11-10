# 真机一键流程（Android Debug Bridge）
#
# 用途：adb 授权后一条命令装测试源、设音量、抓 dumpsys / logcat / 截屏、
#       并把 AudioMixing 手机的 SharedPreferences（档位、静音档策略等）拉下来。
#
# 纪律（与项目一致）：
#   · 不主动放声：只把测试源界面打开，播放必须由你在手机上点。
#   · 不改系统音量，除非你显式传 -Volume。
#   · 不卸载任何 App、不动其它包。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\adb_test_flow.ps1
#   powershell -ExecutionPolicy Bypass -File tools\adb_test_flow.ps1 -Volume 8
#   powershell -ExecutionPolicy Bypass -File tools\adb_test_flow.ps1 -Action dump
#   powershell -ExecutionPolicy Bypass -File tools\adb_test_flow.ps1 -Apk "D:\x\testapp-debug.apk"
#
# 参数：
#   -Action  all（默认）| install | start | volume | dump | prefs | stop
#   -Volume  0..15，设置 STREAM_MUSIC（不传 = 完全不动系统音量）
#   -Apk     测试源 APK 路径（默认取 release\测试工具 下最新的）
#   -OutDir  产物目录（默认 tools\adb-logs\<时间戳>）
#   -Serial  多台设备时指定 adb serial

[CmdletBinding()]
param(
    [ValidateSet('all', 'install', 'start', 'volume', 'dump', 'prefs', 'stop')]
    [string]$Action = 'all',
    [int]$Volume = -1,
    [string]$Apk = '',
    [string]$OutDir = '',
    [string]$Serial = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Write-Step([string]$text) { Write-Host "`n== $text ==" -ForegroundColor Cyan }
function Write-Ok([string]$text) { Write-Host "   OK  $text" -ForegroundColor Green }
function Write-Warn2([string]$text) { Write-Host "   注意 $text" -ForegroundColor Yellow }
function Write-Fail([string]$text) { Write-Host "   失败 $text" -ForegroundColor Red }

# ---------------------------------------------------------------- adb

function Find-Adb {
    if ($env:ADB) {
        if (Test-Path $env:ADB) { return $env:ADB }
    }
    $sdkCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'),
        'E:\Android\sdk\platform-tools\adb.exe',
        'C:\Android\sdk\platform-tools\adb.exe',
        'D:\Android\sdk\platform-tools\adb.exe'
    )
    foreach ($p in $sdkCandidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    $cmd = Get-Command adb -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ''
}

$adb = Find-Adb
if (-not $adb) {
    Write-Fail '找不到 adb。请装 Android SDK platform-tools，或设环境变量 ADB 指向 adb.exe。'
    exit 1
}
Write-Host "adb = $adb"

$adbArgs = @()
if ($Serial) { $adbArgs += @('-s', $Serial) }

function Invoke-Adb {
    param([string[]]$Arguments, [switch]$AllowFail)
    # adb 会把 "daemon not running" 之类写到 stderr；在 $ErrorActionPreference='Stop'
    # 下，2>&1 合并后的 stderr 记录会被当成终止错误。这里临时放宽，只看退出码。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $adb @($adbArgs + $Arguments) 2>&1 | ForEach-Object { "$_" } | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0 -and -not $AllowFail) {
        throw "adb $($Arguments -join ' ') 失败（$code）：$out"
    }
    return $out
}

Write-Step '检查设备授权'
$devices = Invoke-Adb -Arguments @('devices', '-l') -AllowFail
$devices | Write-Host
if ($devices -match 'unauthorized') {
    Write-Fail '设备未授权。请在手机上点「允许 USB 调试」，然后重跑本脚本。'
    exit 3
}
$online = @()
foreach ($line in ($devices -split "`r?`n")) {
    if ($line -match '^\S+\s+device\b') { $online += $line }
}
if ($online.Count -eq 0) {
    Write-Fail '没有已授权的设备。请插 USB、开启「USB 调试」，或先运行 adb devices 确认。'
    exit 2
}
Write-Ok ("已授权设备 " + $online.Count + " 台")

# ---------------------------------------------------------------- 默认路径

if (-not $OutDir) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutDir = Join-Path $root "tools\adb-logs\$stamp"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

if (-not $Apk) {
    $cand = Get-ChildItem (Join-Path $root 'release\测试工具') -Filter '*.apk' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($cand) { $Apk = $cand.FullName }
}
if ($Apk -and -not (Test-Path $Apk)) {
    Write-Fail "APK 不存在：$Apk"
    exit 4
}

$phonePkg = 'com.audiomixing.app'
$testPkg = 'com.audiomixing.testapp'
$testActivity = 'com.audiomixing.testapp/.TestToneActivity'

# ---------------------------------------------------------------- 动作

function Do-Install {
    if (-not $Apk) {
        Write-Fail '没找到测试源 APK（release\测试工具 下没有 .apk，也没传 -Apk）。'
        return $false
    }
    Write-Step "安装测试源：$Apk"
    # -g：装上就授予运行时权限（测试源只需要 MODIFY_AUDIO_SETTINGS，普通权限）。
    $out = Invoke-Adb -Arguments @('install', '-r', '-t', '-g', $Apk) -AllowFail
    $out | Write-Host
    if ($out -match 'Success') {
        Write-Ok '测试源已安装/覆盖安装'
        return $true
    }
    Write-Fail '安装失败（常见原因：未开「USB 安装」/ 未确认手机弹窗 / 存储空间不足）'
    return $false
}

function Do-Start {
    Write-Step '打开测试源界面（不会自动出声）'
    Invoke-Adb -Arguments @('shell', 'am', 'start', '-n', $testActivity) | Write-Host
    Write-Ok '测试源界面已请求打开；点「开始播放」才会出声'
}

function Do-Volume {
    if ($Volume -lt 0) {
        $cur = Invoke-Adb -Arguments @('shell', 'media', 'volume', '--stream', '3', '--get') -AllowFail
        Write-Warn2 ("未指定 -Volume，保持系统媒体音量不动（当前：$($cur.Trim())）")
        return
    }
    $v = [Math]::Max(0, [Math]::Min(15, $Volume))
    Write-Step "设置 STREAM_MUSIC = $v（这一步会改手机系统音量）"
    Invoke-Adb -Arguments @('shell', 'media', 'volume', '--stream', '3', '--set', "$v") | Write-Host
    $back = Invoke-Adb -Arguments @('shell', 'media', 'volume', '--stream', '3', '--get') -AllowFail
    Write-Ok ("回读：$($back.Trim())")
}

function Do-Dump {
    Write-Step "抓取证据到 $OutDir"
    $pairs = @(
        @('dumpsys audio', 'dumpsys_audio.txt'),
        @('dumpsys media.audio_flinger', 'dumpsys_audio_flinger.txt'),
        @('dumpsys media_session', 'dumpsys_media_session.txt'),
        @('dumpsys package com.audiomixing.app', 'dumpsys_package_audiomixing.txt'),
        @('dumpsys package com.audiomixing.testapp', 'dumpsys_package_testapp.txt'),
        @('settings get system volume_music_speaker', 'settings_volume_music_speaker.txt'),
        @('settings get system volume_music_headset', 'settings_volume_music_headset.txt'),
        @('getprop ro.product.model', 'device_model.txt'),
        @('getprop ro.build.version.release', 'android_version.txt')
    )
    foreach ($pair in $pairs) {
        try {
            $text = Invoke-Adb -Arguments (@('shell') + ($pair[0] -split ' ')) -AllowFail
            $text | Out-File -Encoding utf8 (Join-Path $OutDir $pair[1])
            Write-Ok $pair[1]
        } catch {
            Write-Warn2 "$($pair[1]) 抓取失败：$($_.Exception.Message)"
        }
    }

    # logcat：只留本项目与测试源相关 tag，避免几万行无关日志。
    try {
        $tags = @('AM-AudioSend', 'AM-AudioRecv', 'AM-Discovery', 'AM-Service', 'AM-SendFanout',
            'AudioMixing', 'TestTone', 'AudioTrack', 'AudioRecord')
        $log = Invoke-Adb -Arguments (@('logcat', '-d', '-v', 'time', '-s') + $tags) -AllowFail
        $log | Out-File -Encoding utf8 (Join-Path $OutDir 'logcat.txt')
        Write-Ok 'logcat.txt'
    } catch {
        Write-Warn2 "logcat 抓取失败：$($_.Exception.Message)"
    }

    # 截屏（测试源界面 / AudioMixing 详情页）。
    try {
        $png = Join-Path $OutDir 'screen.png'
        # PowerShell 的 > 会破坏二进制；改用 cmd 的 > 直接写文件。
        cmd /c "`"$adb`" $($adbArgs -join ' ') exec-out screencap -p > `"$png`""
        if ((Test-Path $png) -and ((Get-Item $png).Length -gt 1024)) {
            Write-Ok 'screen.png'
        } else {
            Write-Warn2 '截屏失败（可忽略，不影响结论）'
        }
    } catch {
        Write-Warn2 "截屏失败：$($_.Exception.Message)"
    }
}

function Do-Prefs {
    Write-Step '拉取 AudioMixing 手机的 SharedPreferences（档位/静音档策略等）'
    $prefsDir = Join-Path $OutDir 'phone_prefs'
    New-Item -ItemType Directory -Force -Path $prefsDir | Out-Null
    $listing = Invoke-Adb -Arguments @('shell', 'run-as', $phonePkg, 'ls', 'shared_prefs') -AllowFail
    if ($listing -match 'not debuggable|unknown package|Permission denied|No such file') {
        Write-Warn2 "读不到（$($listing.Trim())）。正式包不可 run-as；debug 包才行。"
        return
    }
    $files = @()
    foreach ($line in ($listing -split "`r?`n")) {
        $t = $line.Trim()
        if ($t -and $t -match '\.xml$') { $files += $t }
    }
    if ($files.Count -eq 0) {
        Write-Warn2 "shared_prefs 为空（手机还没保存过设置）。原始输出：$($listing.Trim())"
        return
    }
    foreach ($f in $files) {
        $text = Invoke-Adb -Arguments @('shell', 'run-as', $phonePkg, 'cat', "shared_prefs/$f") -AllowFail
        $text | Out-File -Encoding utf8 (Join-Path $prefsDir $f)
        Write-Ok "shared_prefs/$f"
    }
}

function Do-Stop {
    Write-Step '停止测试源（不留后台播放）'
    Invoke-Adb -Arguments @('shell', 'am', 'force-stop', $testPkg) -AllowFail | Write-Host
    Write-Ok '测试源已停止'
}

switch ($Action) {
    'install' { Do-Install | Out-Null }
    'start' { Do-Start }
    'volume' { Do-Volume }
    'dump' { Do-Dump }
    'prefs' { Do-Prefs }
    'stop' { Do-Stop }
    'all' {
        Do-Install | Out-Null
        Do-Volume
        Do-Start
        Start-Sleep -Seconds 2
        Do-Prefs
        Do-Dump
        Write-Step '完成'
        Write-Host "产物目录：$OutDir"
        Write-Host '下一步：在手机上点测试源的「开始播放」，再按 docs\测试App清单.md 的判据记录 vol 序列与峰值。'
    }
}

if (Test-Path $OutDir) {
    Write-Host "`n产物：$OutDir"
}
exit 0
