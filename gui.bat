@echo off
rem autoDownloads 图形界面启动器：优先 pythonw（无控制台窗口），失败回退到 python 以显示错误。
chcp 65001 >nul
setlocal EnableExtensions
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
cd /d "%~dp0"
set "APP=%~dp0gui_app.py"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%APP%" (
    echo [错误] 找不到 gui_app.py：%APP%
    pause
    exit /b 1
)

rem 把自己的控制台重新拉成最小化窗口，避免 cmd 窗口挡住界面；/min 已传入时不再递归。
if /i not "%~1"=="/min" (
    start "" /min cmd /c ""%~f0" /min"
    exit /b 0
)

rem 1) 首选项目虚拟环境的 pythonw.exe：没有控制台窗口，双击即用。
if exist "%VENV_PYW%" (
    "%VENV_PYW%" "%APP%"
    if errorlevel 1 goto console_fallback
    goto done
)

rem 2) 没有虚拟环境时用 PATH 里的 pythonw.exe。
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    pythonw.exe "%APP%"
    if errorlevel 1 goto console_fallback
    goto done
)

:console_fallback
echo.
echo [提示] 图形界面没有正常启动，下面改用控制台方式重跑，方便看到具体的错误信息。
echo.
if exist "%VENV_PY%" (
    "%VENV_PY%" -X utf8 "%APP%"
) else (
    python -X utf8 "%APP%"
)
if errorlevel 1 (
    echo.
    echo [错误] 启动失败。请确认依赖已安装：python -m pip install -r requirements.txt
    echo [错误] 如果提示没有 tkinter，请使用带 tkinter 的官方 Python 安装包。
    pause
    exit /b 1
)
goto done

:done
endlocal
