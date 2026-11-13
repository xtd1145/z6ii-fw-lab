@echo off
setlocal
set PORT=8642
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
)
taskkill /IM qemu-system-arm.exe /F >nul 2>&1
echo 实验台已停止
timeout /t 2 /nobreak >nul
endlocal
