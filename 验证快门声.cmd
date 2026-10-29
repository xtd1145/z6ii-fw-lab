@echo off
chcp 65001 >nul
cd /d "%~dp0"
set Z62_NVME_TRACE=1
python -X utf8 -B tools\z62_packaged_current.py --package .local\integration-20261029\input-r31 --qemu runtime\qemu-system-arm.exe shutter-preview
pause
