# Read-only verification of the exact image extent on the confirmed SD card.
# Windows raw disk handles require CreateFileW, not a filename FileStream.
[CmdletBinding()]
param([int]$ExpectedDisk=1,[long]$ExpectedSize=31914983424)
$ErrorActionPreference='Stop'
$sdVerifyRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$sdVerifyReport=Join-Path $sdVerifyRoot 'board/results/p2/sd_write.json'
$sdVerifyLog=Join-Path $sdVerifyRoot 'build/toolchain/pynq_imager/readback.txt'
$sdVerifyHash='4a6ac1cdbb413d768fcb059b7182e1f53aa7f728b898b7f5061cbb0b7cc93aab'
$sdVerifyLength=[long]8337309696
function Save-SdVerification {
    [IO.File]::WriteAllText($sdVerifyReport,($sdVerifyRecord | ConvertTo-Json -Depth 8)+"`n",[Text.UTF8Encoding]::new($false))
}
$sdVerifyRecord=Get-Content -LiteralPath $sdVerifyReport -Raw | ConvertFrom-Json
$sdVerifyDevice=Get-Disk -Number $ExpectedDisk
if ($ExpectedDisk -eq 0 -or $sdVerifyDevice.Size -ne $ExpectedSize -or
    $sdVerifyDevice.UniqueId -ne $sdVerifyRecord.disk_unique_id -or
    $sdVerifyDevice.BusType -ne 'USB' -or $sdVerifyDevice.IsBoot -or $sdVerifyDevice.IsSystem -or
    $sdVerifyRecord.writer_exit_code -ne 0 -or $sdVerifyRecord.image_sha256 -ne $sdVerifyHash -or
    $sdVerifyRecord.image_size_bytes -ne $sdVerifyLength) {throw 'Confirmed SD/image identity mismatch'}
$sdVerifyPrincipal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $sdVerifyPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'Raw disk read requires Windows administrator authorization'}
$sdVerifyRecord | Add-Member -NotePropertyName readback_bytes -NotePropertyValue 0 -Force
$sdVerifyRecord | Add-Member -NotePropertyName independent_readback_passed -NotePropertyValue $false -Force
$sdVerifyRecord.stage='independent_readback'
Save-SdVerification
$sdVerifyStream=$null
$sdVerifyHasher=$null
try {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class PynqSdReadHandle {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern SafeFileHandle CreateFileW(string name, uint access,
        uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
}
'@
    $sdVerifyPath='\\.\PhysicalDrive'+$ExpectedDisk
    # GENERIC_READ only. No write, volume format, eject, or device reset.
    # Windows PowerShell 5 interprets hexadecimal 0x80000000 as signed.
    $sdVerifyHandle=[PynqSdReadHandle]::CreateFileW($sdVerifyPath,[uint32]2147483648,3,[IntPtr]::Zero,3,0,[IntPtr]::Zero)
    if ($sdVerifyHandle.IsInvalid) {throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())}
    $sdVerifyStream=[IO.FileStream]::new($sdVerifyHandle,[IO.FileAccess]::Read,4*1024*1024,$false)
    $sdVerifyHasher=[Security.Cryptography.SHA256]::Create()
    $sdVerifyBuffer=[byte[]]::new(4*1024*1024)
    $sdVerifyRemaining=$sdVerifyLength
    $sdVerifyLastLog=[long]0
    [IO.File]::WriteAllText($sdVerifyLog,"Read-only raw device opened; disk=$ExpectedDisk; bytes=$sdVerifyLength`n")
    while ($sdVerifyRemaining -gt 0) {
        $sdVerifyCount=$sdVerifyStream.Read($sdVerifyBuffer,0,[int][Math]::Min([long]$sdVerifyBuffer.Length,[long]$sdVerifyRemaining))
        if ($sdVerifyCount -eq 0) {throw 'Unexpected end of SD image extent'}
        [void]$sdVerifyHasher.TransformBlock($sdVerifyBuffer,0,$sdVerifyCount,$sdVerifyBuffer,0)
        $sdVerifyRemaining-=$sdVerifyCount
        $sdVerifyRecord.readback_bytes=$sdVerifyLength-$sdVerifyRemaining
        if ($sdVerifyRecord.readback_bytes-$sdVerifyLastLog -ge 256*1024*1024) {
            Save-SdVerification
            [IO.File]::AppendAllText($sdVerifyLog,"Read $($sdVerifyRecord.readback_bytes) / $sdVerifyLength bytes`n")
            $sdVerifyLastLog=$sdVerifyRecord.readback_bytes
        }
    }
    [void]$sdVerifyHasher.TransformFinalBlock([byte[]]::new(0),0,0)
    $sdVerifyActual=[BitConverter]::ToString($sdVerifyHasher.Hash).Replace('-','').ToLowerInvariant()
    $sdVerifyRecord | Add-Member -NotePropertyName readback_sha256 -NotePropertyValue $sdVerifyActual -Force
    $sdVerifyRecord.independent_readback_passed=($sdVerifyActual -eq $sdVerifyHash)
    if (-not $sdVerifyRecord.independent_readback_passed) {throw 'Independent SD read-back SHA-256 mismatch'}
    $sdVerifyRecord.stage='written_and_verified'
    $sdVerifyRecord | Add-Member -NotePropertyName completed_utc -NotePropertyValue ([DateTime]::UtcNow.ToString('o')) -Force
    Save-SdVerification
    [IO.File]::AppendAllText($sdVerifyLog,"PASS SHA-256 $sdVerifyActual`n")
} catch {
    $sdVerifyRecord.stage='readback_failed'
    $sdVerifyRecord | Add-Member -NotePropertyName readback_error -NotePropertyValue $_.Exception.Message -Force
    Save-SdVerification
    [IO.File]::AppendAllText($sdVerifyLog,"ERROR $($_.Exception.Message)`n")
    throw
} finally {
    if ($null -ne $sdVerifyStream) {$sdVerifyStream.Dispose()}
    if ($null -ne $sdVerifyHasher) {$sdVerifyHasher.Dispose()}
}
