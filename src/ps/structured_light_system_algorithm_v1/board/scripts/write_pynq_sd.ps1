# Explicit, guarded invocation of the official signed image writer.
# Default is read-only preflight. -Write requires an elevated process.
[CmdletBinding()]
param(
    [switch]$Write,
    [int]$ExpectedDisk = 1,
    [long]$ExpectedSize = 31914983424,
    [ValidatePattern('^[A-Z]$')][string]$DriveLetter = 'E'
)
$ErrorActionPreference = 'Stop'
$sdWorkspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$sdImagePath = Join-Path $sdWorkspace 'build/toolchain/pynq_sd_materials/pynq_z2_v3.1.1.img'
$sdExpectedHash = '4a6ac1cdbb413d768fcb059b7182e1f53aa7f728b898b7f5061cbb0b7cc93aab'
$sdImagerPath = 'D:\Tools\pynq_imager\rpi-imager.exe'
$sdLogDirectory = Join-Path $sdWorkspace 'build/toolchain/pynq_imager'
$sdReportPath = Join-Path $sdWorkspace 'board/results/p2/sd_write.json'
function Save-SdWriteRecord {
    [IO.File]::WriteAllText($sdReportPath,($sdRecord | ConvertTo-Json -Depth 8)+"`n",[Text.UTF8Encoding]::new($false))
}

function Get-ConfirmedSd {
    $sdPartition = Get-Partition -DriveLetter $DriveLetter
    $sdDisk = Get-Disk -Number $sdPartition.DiskNumber
    $sdVolume = Get-Volume -DriveLetter $DriveLetter
    if ($ExpectedDisk -eq 0 -or $sdDisk.Number -ne $ExpectedDisk -or
        $sdDisk.Size -ne $ExpectedSize -or $sdDisk.BusType -ne 'USB' -or
        $sdDisk.IsBoot -or $sdDisk.IsSystem -or $sdDisk.IsReadOnly -or
        $sdVolume.DriveType -ne 'Removable') { throw 'SD identity mismatch; no write permitted' }
    $sdEntries = @(Get-ChildItem -LiteralPath ($DriveLetter + ':\') -Force)
    if (@($sdEntries | Where-Object {$_.Name -ne 'System Volume Information'}).Count -ne 0) {
        throw 'Card contains user files or existing boot files; no write permitted'
    }
    if (@(Get-Partition -DiskNumber $sdDisk.Number).Count -ne 1 -or $sdVolume.FileSystem -ne 'FAT32') {
        throw 'SD partition layout changed; no write permitted'
    }
    return $sdDisk
}

$sdInitialDisk = Get-ConfirmedSd
$sdSignature = Get-AuthenticodeSignature -LiteralPath $sdImagerPath
if ($sdSignature.Status -ne 'Valid' -or $sdSignature.SignerCertificate.Subject -notmatch 'Raspberry Pi Limited') {
    throw 'Installed imager signature invalid'
}
$sdImage = Get-Item -LiteralPath $sdImagePath
if ($sdImage.Length -ne 8337309696 -or $sdImage.Length -gt $sdInitialDisk.Size -or
    (Get-FileHash -LiteralPath $sdImagePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sdExpectedHash) {
    throw 'Image size or SHA-256 mismatch'
}
# Recheck immediately before invoking the writer; do not rely on old disk IDs.
$sdFinalDisk = Get-ConfirmedSd
$sdDestination = '\\.\PhysicalDrive' + $sdFinalDisk.Number
$sdRecord = [ordered]@{
    board='pynq_z2'; image_version='3.1.1'; disk_number=$sdFinalDisk.Number
    disk_size_bytes=$sdFinalDisk.Size; drive_letter=$DriveLetter
    disk_unique_id=$sdFinalDisk.UniqueId; disk_serial_number=$sdFinalDisk.SerialNumber
    image_sha256=$sdExpectedHash; image_size_bytes=$sdImage.Length
    writer='raspberry_pi_imager'; writer_version=(Get-Item -LiteralPath $sdImagerPath).VersionInfo.FileVersion
    writer_sha256=(Get-FileHash -LiteralPath $sdImagerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    signer=$sdSignature.SignerCertificate.Subject; signature_valid=$true
    started_utc=[DateTime]::UtcNow.ToString('o'); sd_written=$false
    verification_enabled=$true; hardware_runtime_verified=$false
    stage='preflight_passed'
}
if (-not $Write) { $sdRecord | ConvertTo-Json -Depth 5; exit 0 }
$sdPrincipal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $sdPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Windows administrator authorization is required; no write performed'
}
$sdRecord.stage = 'writing'
Save-SdWriteRecord
# No --disable-verify and no --enable-writing-system-drives. No OS customization.
$sdWriterArgs = @('--cli','--disable-eject','--sha256',$sdExpectedHash,$sdImagePath,$sdDestination)
$sdWriter = Start-Process -FilePath $sdImagerPath -ArgumentList $sdWriterArgs -WindowStyle Hidden -Wait -PassThru `
    -RedirectStandardOutput (Join-Path $sdLogDirectory 'write_stdout.txt') `
    -RedirectStandardError (Join-Path $sdLogDirectory 'write_stderr.txt')
$sdRecord['writer_exit_code'] = $sdWriter.ExitCode
if ($sdWriter.ExitCode -ne 0) {
    $sdRecord.stage = 'writer_failed'
    Save-SdWriteRecord
    throw 'Official image writer failed; retain logs and do not boot this card'
}
$sdRecord.sd_written = $true
$sdRecord.stage = 'independent_readback'
Save-SdWriteRecord
# Shared read-only verifier uses an explicitly unsigned native handle and
# Int64 chunk arithmetic, including on Windows PowerShell 5.
& (Join-Path $PSScriptRoot 'verify_pynq_sd.ps1') -ExpectedDisk $ExpectedDisk -ExpectedSize $ExpectedSize
Get-Content -LiteralPath $sdReportPath -Raw
