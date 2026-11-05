@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

where python >nul 2>nul
if errorlevel 1 goto :no_python

rem --- Step 1: is the existing virtual environment actually usable? ---
set "VENV_OK=0"
if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import sys" >nul 2>nul
    if not errorlevel 1 set "VENV_OK=1"
)

if "%VENV_OK%"=="1" goto :venv_ready

echo [1/3] Virtual environment missing or damaged. Rebuilding ...
if exist ".venv" (
    echo       Removing the broken .venv ...
    rmdir /s /q ".venv"
)
if exist ".venv" goto :venv_locked
echo       Creating .venv with the system Python ...
python -m venv .venv
if errorlevel 1 goto :venv_failed
if not exist "%VENV_PY%" goto :venv_failed
"%VENV_PY%" -c "import sys" >nul 2>nul
if errorlevel 1 goto :venv_failed

echo [2/3] Installing dependencies ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
goto :done

:venv_ready
echo [1/3] Virtual environment is ready: %VENV_PY%
"%VENV_PY%" -V
echo [2/3] Checking dependencies ...
"%VENV_PY%" -c "import yt_dlp, websocket" >nul 2>nul
if not errorlevel 1 goto :deps_ready
echo       Dependencies missing. Installing ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :pip_failed
goto :done

:deps_ready
echo       Dependencies already installed. Nothing to do.

:done
echo [3/3] Verifying ...
"%VENV_PY%" -c "import yt_dlp, websocket; print('yt-dlp', yt_dlp.version.__version__)"
if errorlevel 1 goto :verify_failed
echo.
echo Setup complete. Double-click douyin.bat to start.
pause
exit /b 0

:venv_failed
echo.
echo Failed to create the virtual environment with: python -m venv .venv
goto :error

:venv_locked
echo.
echo Could not remove .venv. Close any program using it, then run this script again.
goto :error

:pip_failed
echo.
echo Failed to install dependencies. Check your network, then run:
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
goto :error

:verify_failed
echo.
echo Dependencies do not import correctly. Reinstall them with:
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
goto :error

:no_python
echo Python was not found.
echo Install Python 3.10 or newer and enable "Add Python to PATH".
pause
exit /b 1

:error
pause
exit /b 1
