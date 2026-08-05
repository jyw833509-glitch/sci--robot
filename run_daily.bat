@echo off
chcp 65001 >nul
REM ============================================================
REM  抗体纯化文献推送机器人 —— 单次运行
REM  用于 Windows「任务计划程序」每天定时调用
REM ============================================================
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [%date% %time%] 开始执行文献推送任务
"%PY%" main.py run
set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] 任务结束，退出码 %EXITCODE%
exit /b %EXITCODE%
