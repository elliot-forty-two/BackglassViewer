param(
    [Parameter(Position=0)] [string] $System = "",
    [Parameter(Position=1)] [string] $Rom = "",
    [Parameter(Position=2, ValueFromRemainingArguments=$true)] [string[]] $NameParts
)

$ErrorActionPreference = "Stop"

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$outFile = Join-Path $baseDir "current_selection.json"
$tmpFile = Join-Path $baseDir ("current_selection." + [System.Diagnostics.Process]::GetCurrentProcess().Id + ".tmp")
$name = ($NameParts -join " ").Trim()

$msg = [ordered]@{
    ts     = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.fff")
    system = $System
    rom    = $Rom
    name   = $name
}

$json = $msg | ConvertTo-Json -Compress
[System.IO.File]::WriteAllText($tmpFile, $json, [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $tmpFile -Destination $outFile -Force
