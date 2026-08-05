@echo off
chcp 65001 >nul
REM ============================================================
REM  抗体纯化文献推送机器人 —— 常驻定时模式
REM  按 config.yaml 中 scheduler.run_at 的时间每天自动执行
REM  关闭本窗口即停止；建议改用「任务计划程序」+ run_daily.bat
REM ============================================================
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" main.py schedule
pause
