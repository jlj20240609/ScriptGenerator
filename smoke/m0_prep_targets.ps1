# M0 测试目标准备：启动自建、可控的真实窗口（不碰用户正在用的窗口）
# 用法: pwsh -File smoke/m0_prep_targets.ps1
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$fix = Join-Path $root 'smoke\fixtures'
$icons = Join-Path $fix 'icons'

# --- 图标类：自建图标文件夹 ---
$items = @('项目资料','报销单','合同模板','会议纪要','培训课件','数据备份','使用说明.txt')
foreach ($it in $items) {
    $p = Join-Path $icons $it
    if ($it -like '*.txt') {
        if (-not (Test-Path $p)) { Set-Content -Path $p -Value '说明文档内容占位' -Encoding UTF8 }
    } elseif (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Path $p | Out-Null
    }
}

# --- 桌面客户端：记事本 ---
$note = Join-Path $fix 'desktop-note.txt'
Start-Process notepad.exe -ArgumentList "`"$note`""

# --- 图标类窗口：文件夹视图（图标+标签） ---
Start-Process explorer.exe -ArgumentList $icons

# --- 网页类：Edge 独立 profile 的 app 窗口（与用户日常 Edge 隔离） ---
$edge = "$env:ProgramFiles (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
if (Test-Path $edge) {
    $prof = Join-Path $env:TEMP 'm0_edge_profile'
    $u1 = (Join-Path $fix 'web-login.html') -replace '\\','/'
    $u2 = (Join-Path $fix 'erp-web.html') -replace '\\','/'
    $u3 = (Join-Path $fix 'dynamic-web.html') -replace '\\','/'
    foreach ($u in @($u1,$u2,$u3)) {
        Start-Process $edge -ArgumentList "--user-data-dir=`"$prof`"","--no-first-run","--no-default-browser-check","--window-size=1000,760","--app=file:///$u"
        Start-Sleep -Seconds 3
    }
} else {
    Write-Host '未找到 msedge.exe'
}

Start-Sleep -Seconds 6
Write-Host '--- 已启动窗口 ---'
Get-Process | Where-Object { $_.MainWindowTitle -match 'M0|desktop-note|icons' } |
    Select-Object ProcessName, Id, MainWindowTitle | Format-Table -AutoSize
