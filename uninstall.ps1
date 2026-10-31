# 肥鱼输入器 · 卸载脚本
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "== 肥鱼输入器 卸载 ==" -ForegroundColor Cyan

# 停进程
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process -Force
Write-Host "已停止运行中的进程"

# 删自启
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) "肥鱼输入器.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "已删除开机自启快捷方式" }

# 可选：删模型（232MB）
if ($args -contains "-PurgeModels") {
    $m = Join-Path $here "models"
    if (Test-Path $m) { Remove-Item $m -Recurse -Force; Write-Host "已删除语音识别模型" }
}

Write-Host "`n完成。程序文件还在 $here，想彻底清掉直接删目录即可。" -ForegroundColor Green
