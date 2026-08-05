# ============================================================
#  一键注册 Windows 计划任务（每天定时执行文献推送）
#  用法（在项目目录下、以普通用户权限即可）：
#      powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#      powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Time "07:30"
#  卸载：
#      powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall
# ============================================================

param(
    [string]$Time = "08:30",
    [string]$TaskName = "SciRobot",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$BatFile = Join-Path $ProjectDir "run_daily.bat"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[完成] 已删除计划任务：$TaskName" -ForegroundColor Green
    } else {
        Write-Host "[跳过] 未找到计划任务：$TaskName" -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $BatFile)) {
    Write-Host "[错误] 找不到 run_daily.bat：$BatFile" -ForegroundColor Red
    exit 1
}

$action    = New-ScheduledTaskAction -Execute $BatFile -WorkingDirectory $ProjectDir
$trigger   = New-ScheduledTaskTrigger -Daily -At $Time
$settings  = New-ScheduledTaskSettingsSet `
                -StartWhenAvailable `
                -DontStopOnIdleEnd `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -RunOnlyIfLoggedIn `
                -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[提示] 已存在同名任务，正在覆盖" -ForegroundColor Yellow
}

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Description "SciRobot 文献推送机器人 - 每日 $Time 执行" | Out-Null

Write-Host ""
Write-Host "[完成] 计划任务已注册" -ForegroundColor Green
Write-Host "  任务名称：$TaskName"
Write-Host "  执行时间：每天 $Time"
Write-Host "  执行脚本：$BatFile"
Write-Host ""
Write-Host "常用命令："
Write-Host "  立即测试执行 ： Start-ScheduledTask -TaskName $TaskName"
Write-Host "  查看运行结果 ： Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  删除该任务   ： powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall"
Write-Host ""
Write-Host "注意：计划任务只在电脑开机时才会触发。需要 7x24 无人值守请部署到服务器。" -ForegroundColor Yellow
