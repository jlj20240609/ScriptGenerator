# M2-1 基线跑批（带自愈循环）
# 每轮立刻落盘 + 每轮看门狗；屏幕不可用会停批（退出码 4），这里等一会儿再续跑。
# 续跑会跳过已完成的轮次、"屏幕不可用"的轮次会重跑。
$ErrorActionPreference = 'Continue'
$py = 'smoke\.venv-ortcpu\Scripts\python.exe'
for ($i = 1; $i -le 8; $i++) {
  Write-Output "===== 尝试 $i ====="
  & $py engine\scripts\bench.py --rounds 20 --max-round-s 120 --watchdog-s 300
  $code = $LASTEXITCODE
  Write-Output "===== 尝试 $i 结束，退出码 $code ====="
  if ($code -eq 0) { break }
  Start-Sleep -Seconds 30
}
Write-Output "循环结束"
