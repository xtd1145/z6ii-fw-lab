@echo off
setlocal
cd /d "%~dp0"
call "%~dp0env.cmd"
set PY=.local\af_venv\Scripts\python.exe

rem ============================================================
rem  Z6II 一键启动：AF 标注台（6 个批次）+ QEMU 实验台
rem  端口已占用就跳过，重复双击不会产生第二份服务
rem  停止：双击 停止全部.cmd
rem ============================================================

call :up pilot 8710
call :up review 8711
call :up review_video 8712
call :up review_spot 8713
call :up review_water 8714
call :up review_vessel 8715
call :up review_active 8716
call :up review_fill 8717
call :lab

echo.
echo   pilot 手标（飞机/车辆/船舶）  http://127.0.0.1:8710/
echo   预标复核（照片）              http://127.0.0.1:8711/
echo   视频轨迹复核                  http://127.0.0.1:8712/
echo   抽查复核                      http://127.0.0.1:8713/
echo   海岸场景复核                  http://127.0.0.1:8714/
echo   船舶档复核                    http://127.0.0.1:8715/
echo   QEMU 实验台                   http://127.0.0.1:8642/
echo.
start "" "http://127.0.0.1:8710/"
start "" "http://127.0.0.1:8715/"
start "" "http://127.0.0.1:8642/"
timeout /t 2 /nobreak >nul
endlocal
exit /b 0

:up
set BATCH=%~1
set PORT=%~2
%PY% work\af_ai\portcheck.py %PORT% >nul 2>&1
if errorlevel 1 (
    echo   [%PORT%] %BATCH% 已在运行，跳过
) else (
    powershell -NoProfile -Command "Start-Process -FilePath 'D:\z6ii_fw_lab\.local\af_venv\Scripts\python.exe' -ArgumentList 'work/af_ai/label_ui.py','--manifest','.local/af_dataset/%BATCH%/manifest.jsonl','--labels','.local/af_dataset/%BATCH%/labels','--port','%PORT%' -WorkingDirectory 'D:\z6ii_fw_lab' -WindowStyle Hidden -RedirectStandardOutput '.local/af_dataset/label-%BATCH%.out.log' -RedirectStandardError '.local/af_dataset/label-%BATCH%.err.log'" >nul 2>&1
    echo   [%PORT%] %BATCH% 已启动
)
exit /b 0

:lab
%PY% work\af_ai\portcheck.py 8642 >nul 2>&1
if errorlevel 1 (
    echo   [8642] 实验台已在运行，跳过
) else (
    if not exist .local\webui mkdir .local\webui
    powershell -NoProfile -Command "Start-Process -FilePath python -ArgumentList 'z62_webui\console.py','--port','8642' -WorkingDirectory 'D:\z6ii_fw_lab' -WindowStyle Hidden -RedirectStandardOutput '.local\webui\server.out.log' -RedirectStandardError '.local\webui\server.err.log'" >nul 2>&1
    echo   [8642] 实验台已启动
)
exit /b 0
