@echo off
setlocal
set PORT=8710
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
echo AF labeler stopped; labels stay under .local/af_dataset
timeout /t 1 /nobreak >nul
endlocal
