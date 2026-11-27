@echo off
setlocal
title Video Downloader Studio v5.1 - Update
set "APPDIR=%LOCALAPPDATA%\VideoDownloaderStudio"
set "PYTHON=%APPDIR%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Existing Video Downloader Studio installation not found:
  echo "%PYTHON%"
  pause
  exit /b 1
)
echo Close the old Video Downloader Studio window before continuing.
echo Updating libraries...
"%PYTHON%" -m pip install --disable-pip-version-check -U "yt-dlp[default]" "curl_cffi" "imageio-ffmpeg" "PySide6"
if errorlevel 1 (
  echo ERROR: Could not update Python libraries.
  pause
  exit /b 1
)
if exist "%APPDIR%\app.py" copy /Y "%APPDIR%\app.py" "%APPDIR%\app_v5_backup.py" >nul
copy /Y "%~dp0app.py" "%APPDIR%\app.py" >nul
if errorlevel 1 (
  echo ERROR: Could not update app.py. Close the old application and retry.
  pause
  exit /b 1
)
echo Update completed. Launching version 5.1...
"%PYTHON%" "%APPDIR%\app.py"
if errorlevel 1 (
  echo ERROR: Application exited with an error.
  pause
)
