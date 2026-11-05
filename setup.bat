@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem 参数 /quiet：不暂停，供 start.bat / bilibili.bat / douyin.bat 自动调用。
set "QUIET=0"
if /i "%~1"=="/quiet" set "QUIET=1"
if /i "%~1"=="-q" set "QUIET=1"

set "ROOT=%~dp0"
set "VENV_DIR=%ROOT%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQ=%ROOT%requirements.txt"
set "SETUP_LOG=%ROOT%setup.log"

rem 安装期间的临时目录放在项目内：系统 %TEMP% 被权限或清理策略挡住时 pip 会失败。
set "TEMP=%ROOT%.tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%" >nul 2>&1

if not exist "%REQ%" (
    echo [错误] 找不到 requirements.txt
    goto :failed
)

echo [1/3] 检查虚拟环境
if not exist "%VENV_PY%" (
    echo       未找到 .venv，正在用系统 Python 创建 ...
    where python >nul 2>nul
    if errorlevel 1 goto :no_python
    if exist "%VENV_DIR%" rmdir /s /q "%VENV_DIR%" >nul 2>&1
    python -m venv "%VENV_DIR%" >>"%SETUP_LOG%" 2>&1
)
if not exist "%VENV_PY%" goto :venv_failed

"%VENV_PY%" -c "import sys" >nul 2>&1
if errorlevel 1 goto :venv_failed

rem 半损坏的 .venv（缺 pip）装不了依赖：先尝试修复，再整份重建。
"%VENV_PY%" -c "import pip" >nul 2>&1
if not errorlevel 1 goto :deps_check
echo       虚拟环境缺少 pip，正在修复 ...
"%VENV_PY%" -m ensurepip --upgrade >>"%SETUP_LOG%" 2>&1
"%VENV_PY%" -c "import pip" >nul 2>&1
if not errorlevel 1 goto :deps_check
echo       修复失败，正在重建 .venv ...
where python >nul 2>nul
if errorlevel 1 goto :no_python
rmdir /s /q "%VENV_DIR%" >nul 2>&1
python -m venv "%VENV_DIR%" >>"%SETUP_LOG%" 2>&1
if not exist "%VENV_PY%" goto :venv_failed
"%VENV_PY%" -c "import pip" >nul 2>&1
if errorlevel 1 goto :venv_failed

:deps_check
echo [2/3] 检查依赖 yt-dlp / websocket-client
"%VENV_PY%" -c "import yt_dlp, websocket" >nul 2>&1
if not errorlevel 1 goto :verify

echo       正在安装依赖，首次可能需要 1-2 分钟 ...
"%VENV_PY%" -m pip install --disable-pip-version-check -r "%REQ%" >>"%SETUP_LOG%" 2>&1
if not errorlevel 1 goto :verify
echo       安装失败，正在重试一次 ...
"%VENV_PY%" -m pip install --disable-pip-version-check --no-input -r "%REQ%" >>"%SETUP_LOG%" 2>&1
if errorlevel 1 goto :pip_failed

:verify
echo [3/3] 验证依赖
"%VENV_PY%" -c "import yt_dlp, websocket; print('      yt-dlp', yt_dlp.version.__version__, '| websocket-client', websocket.__version__)"
if errorlevel 1 goto :deps_broken

echo.
echo 环境就绪：%VENV_PY%
echo 之后双击 start.bat 即可打开本地下载页面。
if "%QUIET%"=="0" pause
exit /b 0

:no_python
echo.
echo Python was not found. 未找到 Python。
echo 请安装 Python 3.10 或更高版本，并勾选 "Add Python to PATH"。
goto :failed

:venv_failed
echo.
echo 创建或使用虚拟环境失败：%VENV_DIR%
echo 请关闭正在使用 .venv 的程序后重试，或手工执行：
echo   python -m venv .venv
goto :failed

:pip_failed
echo.
echo 依赖安装失败，请检查网络后重试。安装输出见：
echo   %SETUP_LOG%
goto :failed

:deps_broken
echo.
echo 依赖安装后仍无法导入，请重装：
echo   "%VENV_PY%" -m pip install -r "%REQ%"
echo 安装输出见：%SETUP_LOG%
goto :failed

:failed
if "%QUIET%"=="0" pause
exit /b 1
