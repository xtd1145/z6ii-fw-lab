@echo off
setlocal
cd /d "%~dp0"
rem 只关我们自己的标注台/实验台进程，不动训练和其它 python
.local\af_venv\Scripts\python.exe work\af_ai\stop_services.py
echo 标注台与实验台已停止；已存的标签在 .local\af_dataset 下，不会丢
timeout /t 2 /nobreak >nul
endlocal
