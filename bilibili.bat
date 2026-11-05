@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :check_global
".venv\Scripts\python.exe" -c "import yt_dlp, websocket" >nul 2>nul
if not errorlevel 1 goto :run_venv
echo Virtual environment is missing dependencies. Running setup ...
call setup.bat
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" goto :run_venv

:check_global
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import yt_dlp, websocket" >nul 2>nul
if not errorlevel 1 goto :run_global

echo Installing dependencies for the first run ...
call setup.bat
if errorlevel 1 exit /b 1
if exist ".venv\Scripts\python.exe" goto :run_venv
python -c "import yt_dlp, websocket" >nul 2>nul
if not errorlevel 1 goto :run_global
echo Dependencies are still missing. Run setup.bat manually.
pause
exit /b 1

:run_global
python bilibili_downloader.py %*
goto :done

:run_venv
".venv\Scripts\python.exe" bilibili_downloader.py %*
goto :done

:no_python
echo Python was not found.
echo Install Python 3.10 or newer and enable "Add Python to PATH".
pause
exit /b 1

:done
set "RC=%errorlevel%"
if "%~1"=="" pause
exit /b %RC%
