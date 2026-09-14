@echo off
setlocal
set "SCRIPT_DIR=C:\RetroBat\plugins\BackglassViewer\"

REM Touching this file tells backglass_viewer_file_v9.py to quit.
REM Use a temp file then move it over the signal path so the viewer sees one clean change.
set TARGET=%SCRIPT_DIR%quit.signal
set TEMP=%TARGET%.tmp

> "%TEMP%" echo %date% %time%
move /Y "%TEMP%" "%TARGET%" >nul

endlocal
