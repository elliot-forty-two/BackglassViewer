@echo off
setlocal
set "SCRIPT_DIR=C:\RetroBat\plugins\BackglassViewer\"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path '%SCRIPT_DIR%' 'current_selection.json'; $tmp = Join-Path '%SCRIPT_DIR%' 'current_selection.clear.tmp'; $json = @{ts=(Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fff'); system=''; rom=''; name=''} | ConvertTo-Json -Compress; [IO.File]::WriteAllText($tmp, $json, [Text.UTF8Encoding]::new($false)); Move-Item -LiteralPath $tmp -Destination $p -Force"
