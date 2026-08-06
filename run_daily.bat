@echo off
chcp 65001 >nul
REM ============================================================
REM  SciRobot —— 单次运行
REM  用于 Windows「任务计划程序」每天定时调用
REM ============================================================
cd /d "%~dp0"

if exist ".venv311\Scripts\python.exe" (
    set "PY=.venv311\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [%date% %time%] 开始执行文献推送任务
"%PY%" main.py run
set EXITCODE=%ERRORLEVEL%
echo [%date% %time%] 任务结束，退出码 %EXITCODE%
exit /b %EXITCODE%
