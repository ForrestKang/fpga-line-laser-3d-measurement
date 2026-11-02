param(
    [Parameter(Mandatory=$true)][ValidatePattern('^b[0-9]+[a-z0-9_]*$')][string]$Build,
    [Parameter(Mandatory=$true)][ValidateSet(2,4,8)][int]$Lanes,
    [Parameter(Mandatory=$true)][ValidateSet(0,1)][int]$Dispatch,
    [Parameter(Mandatory=$true)][ValidateSet(0,1)][int]$Banked,
    [Parameter(Mandatory=$true)][ValidateSet(0,1)][int]$BankedResults,
    [Parameter(Mandatory=$true)][ValidateSet(0,1)][int]$BalancedDispatch
)
$ErrorActionPreference = 'Stop'
$r43Root = Split-Path -Parent $PSScriptRoot
$r43Output = Join-Path (Join-Path $r43Root 'build') $Build
if (Test-Path -LiteralPath $r43Output) { throw 'Output exists; use a new build ID.' }
$env:TEMP = Join-Path $r43Root 'tmp'
$env:TMP = $env:TEMP
$env:PROCESSOR_ARCHITECTURE = 'AMD64'
Push-Location $r43Root
try {
    & 'D:\Vavido\2025.2\Vivado\bin\vivado.bat' -mode batch -source scripts/build_r43.tcl -log (Join-Path $r43Root "build/$Build.log") -journal (Join-Path $r43Root "build/$Build.jou") -tclargs $r43Output $Lanes $Dispatch $Banked $BankedResults $BalancedDispatch
    if ($LASTEXITCODE -ne 0) { throw "Vivado failed: $LASTEXITCODE. Evidence retained." }
} finally { Pop-Location }
