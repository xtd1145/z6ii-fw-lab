@echo off
rem ============================================================
rem  Ingest a camera card: find the DCIM root, carve NEFs to JPEG,
rem  triage both pools with the newest checkpoint.
rem  The card is only ever READ - NEFs are copied to D: first and
rem  only the D: copy is deleted afterwards.
rem    新卡入库.cmd            auto-detect the card
rem    新卡入库.cmd  G:        treat G: as the card
rem ============================================================
setlocal
call "%~dp0env.cmd"
cd /d "%~dp0"

if "%~1"=="" (
  "%~dp0.local\af_venv\Scripts\python.exe" work\af_ai\card_in.py
) else (
  "%~dp0.local\af_venv\Scripts\python.exe" work\af_ai\card_in.py --src "%~1/DCIM"
)
echo.
echo [z6ii] reports and shots are under .local\af_real\card2
endlocal
