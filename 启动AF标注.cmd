@echo off
setlocal
cd /d "%~dp0"
set BATCH=pilot
call "%~dp0env.cmd"
set PORT=8710
if "%~1" neq "" set BATCH=%~1
if "%~2" neq "" set PORT=%~2

.local\af_venv\Scripts\python.exe work\af_ai\portcheck.py %PORT% >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath python -ArgumentList 'work/af_ai/label_ui.py','--manifest','.local/af_dataset/%BATCH%/manifest.jsonl','--labels','.local/af_dataset/%BATCH%/labels','--port','%PORT%' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '.local/af_dataset/label-%BATCH%.out.log' -RedirectStandardError '.local/af_dataset/label-%BATCH%.err.log'"
    timeout /t 2 /nobreak >nul
)

start "" "http://127.0.0.1:%PORT%/"
echo AF labeler %BATCH%  http://127.0.0.1:%PORT%/
endlocal
