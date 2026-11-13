@echo off
setlocal
cd /d "%~dp0"
set PORT=8642

netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
    if not exist .local\webui mkdir .local\webui
    powershell -NoProfile -Command "Start-Process -FilePath python -ArgumentList 'z62_webui\console.py','--port','%PORT%' -WorkingDirectory '%~dp0' -WindowStyle Hidden -RedirectStandardOutput '.local\webui\server.out.log' -RedirectStandardError '.local\webui\server.err.log'"
    timeout /t 3 /nobreak >nul
)

start "" "http://127.0.0.1:%PORT%/"
endlocal
