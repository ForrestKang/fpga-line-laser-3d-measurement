param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$tempRoot = 'D:\Workspace\CodexTemp\final_timingfix_20261030_01'
$env:TEMP = $tempRoot
$env:TMP = $tempRoot
$env:PATH = 'D:\Anaconda3\Library\bin;D:\Anaconda3\DLLs;' + $env:PATH
$env:PYNQ_SOURCE_IFINDEX = '4'
. D:\Workspace\CodexWork\scan3d_r40_pl_cloud_view_20261027_01\load_pynq_password.ps1
if (-not $env:PYNQ_PASSWORD) {
    throw 'Board credential unavailable in the process or secure loader'
}
try {
    Push-Location $taskRoot
    try {
        & D:\Anaconda3\python.exe -B board/request.py $RequestPath
        $requestExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item Env:PYNQ_PASSWORD -ErrorAction SilentlyContinue
}
exit $requestExit
