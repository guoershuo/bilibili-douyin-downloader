@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "APP=%~dp0web_app.py"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
set "PORT_FILE=%~dp0web.port"
set "PROBE_FILE=%~dp0web.port.probe"

if not exist "%APP%" (
    echo [错误] 找不到 web_app.py
    pause
    exit /b 1
)

echo [1/4] 检查依赖 ...
"%VENV_PY%" -c "import yt_dlp, websocket" >nul 2>&1
if errorlevel 1 (
    echo       缺少依赖，正在自动安装（首次可能需要 1-2 分钟）...
    call "%~dp0setup.bat" /quiet
    if errorlevel 1 goto :env_failed
)
if not exist "%VENV_PY%" goto :env_failed

echo [2/4] 预检程序 ...
"%VENV_PY%" -c "import web_app" >nul 2>&1
if errorlevel 1 goto :import_failed

echo [3/4] 检查是否已有服务在运行 ...
call :probe
if defined RUNNING (
    rem 代码更新过就自动停掉旧实例，否则双击只会打开还是旧代码的页面。
    "%VENV_PY%" "%APP%" --stop
    call :probe
)
if defined RUNNING goto :reuse

echo [4/4] 启动服务 ...
del "%PORT_FILE%" >nul 2>&1
start "" "%VENV_PYW%" "%APP%"
set /a WAITED=0

:wait
if exist "%PORT_FILE%" goto :read_port
timeout /t 1 /nobreak >nul 2>&1
set /a WAITED+=1
if %WAITED% lss 20 goto :wait
goto :launch_failed

:read_port
set "READY="
set /p READY=<"%PORT_FILE%"
if not defined READY goto :launch_failed
echo       已启动，正在打开页面 ...
start "" "http://127.0.0.1:%READY%/"
exit /b 0

:reuse
echo       服务已在运行，正在打开页面 ...
start "" "http://127.0.0.1:%RUNNING%/"
exit /b 0

:env_failed
echo.
echo [错误] 依赖安装失败，无法启动。
echo 请查看 setup.log，或手动运行 setup.bat 查看详细提示。
pause
exit /b 1

:import_failed
echo.
echo [错误] 程序导入失败，详细错误如下：
echo --------------------------------------------------
"%VENV_PY%" -c "import web_app"
echo --------------------------------------------------
pause
exit /b 1

:launch_failed
echo.
echo [错误] 服务没有启动起来，最近日志：
echo --------------------------------------------------
"%VENV_PY%" "%APP%" --log-tail 25
echo --------------------------------------------------
echo 完整日志：%~dp0web.log
pause
exit /b 1

:probe
set "RUNNING="
"%VENV_PY%" "%APP%" --probe > "%PROBE_FILE%" 2>nul
if not errorlevel 1 set /p RUNNING=<"%PROBE_FILE%"
del "%PROBE_FILE%" >nul 2>&1
exit /b 0
