@echo off
setlocal
set "SCRIPT_DIR=C:\RetroBat\plugins\BackglassViewer\"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%write_selection.ps1" %*
