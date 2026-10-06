@echo off
rem 双击构建单文件 exe。首次会自动建 .venv-build/ 并装 PyInstaller。
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python tools\build_exe.py --verify %*
) else if exist "E:\ENV\Python\python.exe" (
    "E:\ENV\Python\python.exe" tools\build_exe.py --verify %*
) else (
    echo 找不到 python.exe
)

echo.
pause
