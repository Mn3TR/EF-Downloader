@echo off
rem 双击启动图形界面。用 pythonw 是为了不弹出控制台窗口。
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw -m efd gui
    exit /b 0
)

if exist "E:\ENV\Python\pythonw.exe" (
    start "" "E:\ENV\Python\pythonw.exe" -m efd gui
    exit /b 0
)

echo 找不到 pythonw.exe，请改用：  python -m efd gui
pause
