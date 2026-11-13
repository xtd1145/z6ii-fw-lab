@echo off
rem ============================================================
rem  Pin this project's execution environment to the D volume.
rem  Call it before any manual python run:
rem      call env.cmd
rem      python work\af_ai\enrich_batch.py --batch pilot --boxes
rem  The batch launchers (qi dong quan bu) already call it.
rem ============================================================
set "Z6II_ROOT=%~dp0"
set "TMP=%Z6II_ROOT%.local\cache\tmp"
set "TEMP=%Z6II_ROOT%.local\cache\tmp"
set "PIP_CACHE_DIR=%Z6II_ROOT%.local\cache\pip"
set "TORCH_HOME=%Z6II_ROOT%.local\cache\torch"
set "HF_HOME=%Z6II_ROOT%.local\cache\hf"
set "XDG_CACHE_HOME=%Z6II_ROOT%.local\cache"
if not exist "%TMP%" mkdir "%TMP%"
if not exist "%PIP_CACHE_DIR%" mkdir "%PIP_CACHE_DIR%"
if not exist "%TORCH_HOME%" mkdir "%TORCH_HOME%"
if not exist "%HF_HOME%" mkdir "%HF_HOME%"
set "PATH=%Z6II_ROOT%.local\af_venv\Scripts;%PATH%"
rem  Outbound HTTPS from this box only works through the local proxy: a direct
rem  commons.wikimedia.org request times out, through 12450 it answers in ~2s.
rem  urllib picks this up automatically; the registry proxy is disabled.
if not defined HTTPS_PROXY set "HTTPS_PROXY=http://127.0.0.1:12450"
if not defined HTTP_PROXY set "HTTP_PROXY=http://127.0.0.1:12450"
echo [z6ii env] caches + venv on D: %Z6II_ROOT%.local
