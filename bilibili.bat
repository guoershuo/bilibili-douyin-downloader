@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "SCRIPT=%~dp0bilibili_downloader.py"

rem 缺虚拟环境或依赖时就地安装，第一次双击也能直接跑。
"%VENV_PY%" -c "import yt_dlp, websocket" >nul 2>&1
if errorlevel 1 (
    echo [环境] 正在准备虚拟环境与依赖，首次可能需要 1-2 分钟 ...
    call "%~dp0setup.bat" /quiet
    if errorlevel 1 goto :setup_failed
)

if not exist "%VENV_PY%" goto :setup_failed

"%VENV_PY%" "%SCRIPT%" %*
set "RC=%errorlevel%"
if "%~1"=="" pause
exit /b %RC%

:setup_failed
echo.
echo [错误] 环境准备失败，无法运行。
echo 请查看 setup.log，或手动运行 setup.bat 查看详细提示。
pause
exit /b 1
