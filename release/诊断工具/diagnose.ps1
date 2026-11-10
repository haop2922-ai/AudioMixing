param([string]$Folder = "")

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Folder) { $Folder = $scriptDir }
$Folder = $Folder.Trim().Trim('"')
while ($Folder.EndsWith("\") -or $Folder.EndsWith("/")) {
    $Folder = $Folder.Substring(0, $Folder.Length - 1)
}
if (-not $Folder) { $Folder = $scriptDir }
try {
    $Folder = (Resolve-Path -LiteralPath $Folder -ErrorAction Stop).Path
} catch {
    Write-Host ("路径解析失败，改用脚本所在目录: " + $_.Exception.Message)
    $Folder = $scriptDir
}

$lines = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text = "") {
    $lines.Add($text)
    Write-Host $text
}
function Add-Section([string]$title) {
    Add-Line ""
    Add-Line ("=" * 60)
    Add-Line $title
    Add-Line ("=" * 60)
}
function Get-FreeTcpPort {
    $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $l.Start()
    $p = $l.LocalEndpoint.Port
    $l.Stop()
    return $p
}
function Get-FreeUdpPort {
    $u = New-Object System.Net.Sockets.UdpClient(0)
    $p = $u.Client.LocalEndPoint.Port
    $u.Close()
    return $p
}
function Test-Port([int]$port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $r = $c.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $r.AsyncWaitHandle.WaitOne(1200)) { return $false }
        $c.EndConnect($r)
        return $true
    } catch {
        return $false
    } finally {
        $c.Close()
    }
}

Add-Line "Audio Mixing 远程诊断报告"
Add-Line ("生成时间: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Add-Line ("电脑名:   " + $env:COMPUTERNAME)
Add-Line ("用户名:   " + $env:USERNAME)
Add-Line ("用户目录: " + $env:USERPROFILE)
Add-Line ("APPDATA:  " + $env:APPDATA)
Add-Line ("TEMP:     " + $env:TEMP)
Add-Line ("路径含中文/非ASCII: " + (($env:USERPROFILE -match '[^\x00-\x7F]') -or ($env:TEMP -match '[^\x00-\x7F]')))

# ---- 系统信息 ----
Add-Section "1. Windows 版本"
try {
    $os = Get-CimInstance Win32_OperatingSystem
    $cv = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    Add-Line ("系统:     " + $os.Caption)
    Add-Line ("版本号:   " + $cv.DisplayVersion + "  Build " + $cv.CurrentBuild + "." + $cv.UBR)
    Add-Line ("架构:     " + $os.OSArchitecture + "   (进程架构: " + $env:PROCESSOR_ARCHITECTURE + ")")
    Add-Line ("内存:     " + [math]::Round($os.TotalVisibleMemorySize / 1MB, 1) + " GB")
} catch {
    Add-Line ("读取系统信息失败: " + $_.Exception.Message)
}
try {
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    Add-Line ("CPU:      " + $cpu.Name)
} catch {}

# ---- 找 exe ----
Add-Section "2. 程序文件"
$exe = $null
$candidates = @()
try {
    $candidates = Get-ChildItem -LiteralPath $Folder -File -Filter "*.exe" |
        Where-Object { $_.Name -notmatch "^(diagnose|unins)" } |
        Sort-Object Length -Descending
} catch {}
if ($candidates.Count -ge 1) {
    $exe = $candidates[0].FullName
    Add-Line ("找到 exe: " + $exe)
    if ($candidates.Count -gt 1) {
        Add-Line ("同目录还有其它 exe（诊断使用最大的那个）:")
        foreach ($c in $candidates) { Add-Line ("  - " + $c.Name + "  " + $c.Length + " 字节") }
    }
} else {
    Add-Line ("程序目录里没有找到 exe: " + $Folder)
}

if ($exe) {
    try {
        $fi = Get-Item -LiteralPath $exe
        Add-Line ("大小:     " + $fi.Length + " 字节")
        Add-Line ("修改时间: " + $fi.LastWriteTime)
        Add-Line ("SHA256:   " + (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash)
    } catch {
        Add-Line ("读取文件信息失败: " + $_.Exception.Message)
    }
    try {
        $vi = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($exe)
        Add-Line ("文件版本: " + $vi.FileVersion + "   产品版本: " + $vi.ProductVersion)
    } catch {}
    try {
        $sig = Get-AuthenticodeSignature -LiteralPath $exe
        Add-Line ("数字签名: " + $sig.Status + "   " + $sig.StatusMessage)
    } catch {
        Add-Line ("读取数字签名失败")
    }
    try {
        $zone = Get-Content -LiteralPath ($exe + ":Zone.Identifier") -ErrorAction Stop
        Add-Line ("网络标记: 有（文件被 Windows 标记为来自网络）")
        foreach ($z in $zone) { Add-Line ("  " + $z) }
    } catch {
        Add-Line ("网络标记: 无")
    }
}

# ---- 安全软件 ----
Add-Section "3. 安全软件 / 系统拦截"
try {
    $avs = Get-CimInstance -Namespace "root/SecurityCenter2" -ClassName AntiVirusProduct -ErrorAction Stop
    if ($avs) {
        foreach ($av in $avs) {
            Add-Line ("安全软件: " + $av.displayName + "   state=0x" + ("{0:X}" -f $av.productState))
        }
    } else {
        Add-Line "安全软件: 未登记"
    }
} catch {
    Add-Line ("安全软件列表读取失败: " + $_.Exception.Message)
}
try {
    $mp = Get-MpComputerStatus -ErrorAction Stop
    Add-Line ("Defender 实时防护: " + $mp.RealTimeProtectionEnabled + "   篡改保护: " + $mp.IsTamperProtected)
} catch {
    Add-Line ("Defender 状态读取失败（可能被第三方杀软接管）: " + $_.Exception.Message)
}
try {
    $sac = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction Stop).VerifiedAndReputablePolicyState
    switch ([int]$sac) {
        0 { Add-Line "智能应用控制(SAC): 关闭 (0)" }
        1 { Add-Line "智能应用控制(SAC): 强制开启 (1)  <-- 会拦截未签名程序" }
        2 { Add-Line "智能应用控制(SAC): 评估模式 (2)" }
        default { Add-Line ("智能应用控制(SAC): " + $sac) }
    }
} catch {
    Add-Line "智能应用控制(SAC): 无此设置（较旧的系统）"
}
try {
    $threats = Get-MpThreatDetection -ErrorAction Stop |
        Where-Object { $_.InitialDetectionTime -gt (Get-Date).AddDays(-7) } |
        Select-Object -First 20
    if ($threats) {
        Add-Line "最近 7 天 Defender 拦截记录:"
        foreach ($t in $threats) {
            Add-Line ("  [" + $t.InitialDetectionTime + "] " + $t.ThreatID + "  " + ($t.Resources -join "; "))
        }
    } else {
        Add-Line "最近 7 天 Defender 无拦截记录"
    }
} catch {
    Add-Line ("Defender 拦截记录读取失败: " + $_.Exception.Message)
}

# ---- 系统日志 ----
Add-Section "4. 系统日志里的相关记录（近 3 天）"
try {
    $evts = Get-WinEvent -FilterHashtable @{ LogName = "Application"; StartTime = (Get-Date).AddDays(-3) } -MaxEvents 300 -ErrorAction Stop |
        Where-Object { $_.Message -match "AudioMixing|Audio Mixing|audiomixing" } |
        Select-Object -First 15
    if ($evts) {
        foreach ($e in $evts) {
            Add-Line ("[" + $e.TimeCreated + "] " + $e.ProviderName + " id=" + $e.Id + " level=" + $e.LevelDisplayName)
            Add-Line (($e.Message -split "`r?`n" | Select-Object -First 5) -join " | ")
            Add-Line ""
        }
    } else {
        Add-Line "没有找到相关系统日志"
    }
} catch {
    Add-Line ("读取系统日志失败: " + $_.Exception.Message)
}

# ---- 当前是否已有实例 ----
Add-Section "5. 当前实例检查"
$listening = Test-Port 17800
Add-Line ("17800 端口正在监听: " + $listening)
if ($listening) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:17800/healthz" -TimeoutSec 3
        Add-Line ("/healthz 状态: " + $r.StatusCode + "  " + $r.Content)
    } catch {
        Add-Line ("/healthz 请求失败: " + $_.Exception.Message)
    }
}
$procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match "AudioMixing|Audio Mixing" }
if ($procs) {
    foreach ($p in $procs) { Add-Line ("相关进程: " + $p.Id + "  " + $p.ProcessName + "  " + $p.Path) }
} else {
    Add-Line "没有正在运行的相关进程"
}

# ---- 实际启动尝试 ----
Add-Section "6. 实际启动尝试（临时配置，不会影响已有数据）"
if ($exe) {
    $sandbox = Join-Path $env:TEMP ("AudioMixing-diag-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $sandbox | Out-Null
    $webPort = Get-FreeTcpPort
    $audioPort = Get-FreeUdpPort
    Add-Line ("临时端口: web=" + $webPort + " audio=" + $audioPort)
    $oldHome = $env:AUDIOMIXING_HOME
    $env:AUDIOMIXING_HOME = $sandbox
    $proc = $null
    try {
        $proc = Start-Process -FilePath $exe -ArgumentList @("serve", "--host", "127.0.0.1", "--web-port", "$webPort", "--audio-port", "$audioPort") -PassThru
        Add-Line ("已尝试启动 PID: " + $proc.Id)
        $started = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 1000
            $proc.Refresh()
            if ($proc.HasExited) {
                Add-Line ("进程在第 " + ($i + 1) + " 秒退出，退出码: " + $proc.ExitCode)
                break
            }
            try {
                $r = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:" + $webPort + "/healthz") -TimeoutSec 2
                if ($r.StatusCode -eq 200) {
                    Add-Line ("启动成功：第 " + ($i + 1) + " 秒面板已响应")
                    Add-Line ("返回: " + $r.Content)
                    $started = $true
                    break
                }
            } catch {}
        }
        if (-not $started -and -not $proc.HasExited) {
            Add-Line "30 秒内进程一直活着，但面板没有响应（可能卡在启动或没有权限）"
        }
        if (-not $proc.HasExited) {
            try {
                $status = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:" + $webPort + "/api/status") -TimeoutSec 5
                Add-Line ("/api/status: " + $status.Content.Substring(0, [Math]::Min(500, $status.Content.Length)))
            } catch {}
        }
    } catch {
        Add-Line ("启动调用失败: " + $_.Exception.Message)
    }
    $diagLog = Join-Path $sandbox "launcher.log"
    if (Test-Path -LiteralPath $diagLog) {
        Add-Line "--- 本次启动的 launcher.log ---"
        Get-Content -LiteralPath $diagLog -Encoding UTF8 -Tail 100 | ForEach-Object { Add-Line $_ }
    } else {
        Add-Line "本次 serve 启动没有 launcher.log（serve 模式本来就不写它，正常；判断以进程/端口为准）"
    }
    if ($proc -and -not $proc.HasExited) {
        taskkill /F /T /PID $proc.Id | Out-Null
    }
    $env:AUDIOMIXING_HOME = $oldHome
    Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue
} else {
    Add-Line "没有 exe，跳过启动测试"
}

# ---- 历史日志 ----
Add-Section "7. 这台电脑以前的 launcher.log"
$realLog = Join-Path ($env:APPDATA + "\AudioMixing") "launcher.log"
if (Test-Path -LiteralPath $realLog) {
    Add-Line ("文件: " + $realLog)
    Get-Content -LiteralPath $realLog -Encoding UTF8 -Tail 120 | ForEach-Object { Add-Line $_ }
} else {
    Add-Line ("没有找到 " + $realLog + "（说明程序可能从来没有成功启动过）")
}

# ---- 浏览器 ----
Add-Section "8. 监控窗依赖"
$edge = @(
    (Join-Path ${env:ProgramFiles(x86)} "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:ProgramFiles "Microsoft\Edge\Application\msedge.exe"),
    (Join-Path $env:LOCALAPPDATA "Microsoft\Edge\Application\msedge.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if ($edge) { Add-Line ("Edge: 已安装 " + $edge) } else { Add-Line "Edge: 没找到（监控窗会改用默认浏览器）" }
try {
    $progId = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice" -ErrorAction Stop).ProgId
    Add-Line ("默认浏览器: " + $progId)
} catch {
    Add-Line "默认浏览器: 读取失败（可能没有设置默认浏览器）"
}

# ---- 收尾 ----
Add-Section "结论提示"
Add-Line "如果第 6 节显示进程立刻退出："
Add-Line "  → 程序在启动阶段就失败了，请把本报告发回。"
Add-Line "如果第 6 节显示启动成功、但双击仍然没反应："
Add-Line "  → 程序本身能跑，问题出在双击后的窗口/托盘环节，请把本报告发回。"
Add-Line ""
Add-Line "报告结束。"

$report = Join-Path $Folder ("AudioMixing-诊断结果-" + $env:COMPUTERNAME + "-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
try {
    $lines | Out-File -LiteralPath $report -Encoding UTF8
    Write-Host ""
    Write-Host ("报告已保存: " + $report)
    if ($env:AUDIOMIXING_DIAG_NO_OPEN -ne "1") {
        Start-Process notepad.exe -ArgumentList ('"' + $report + '"')
    }
} catch {
    Write-Host ("报告保存失败: " + $_.Exception.Message)
}
