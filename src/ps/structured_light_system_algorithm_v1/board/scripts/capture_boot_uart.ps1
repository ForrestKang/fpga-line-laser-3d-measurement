param(
    [string]$Port='COM3',
    [string]$ExpectedSerial='1234-TUL',
    [ValidateRange(10,600)][int]$Seconds=180
)
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$taskDevices=@(Get-PnpDevice -PresentOnly -Class Ports | Where-Object {
    $_.FriendlyName -match "\($([regex]::Escape($Port))\)$" -and
    $_.InstanceId -like "FTDIBUS\VID_0403+PID_6010+$($ExpectedSerial)B\*"
})
if ($taskDevices.Count -ne 1) { throw 'COM port does not match the explicitly selected PYNQ-Z2 TUL FTDI UART' }
$taskOutput=Join-Path $taskRoot ('board/results/p2/uart/'+[DateTime]::UtcNow.ToString('yyyyMMdd_HHmmss'))
New-Item -ItemType Directory -Path $taskOutput | Out-Null
$taskRecord=[ordered]@{target='PYNQ-Z2';port=$Port;device=$taskDevices[0].InstanceId;baud=115200;
    data_bits=8;parity='None';stop_bits=1;flow_control='None';listen_only=$true;
    dtr=$false;rts=$false;started_utc=[DateTime]::UtcNow.ToString('o');
    fpga_loaded=$false;boot_verified=$false;runtime_verified=$false}
$taskLog=[IO.File]::Open((Join-Path $taskOutput 'boot_uart.bin'),[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
$taskSerial=New-Object System.IO.Ports.SerialPort $Port,115200,([System.IO.Ports.Parity]::None),8,([System.IO.Ports.StopBits]::One)
$taskSerial.Handshake=[System.IO.Ports.Handshake]::None
$taskSerial.DtrEnable=$false
$taskSerial.RtsEnable=$false
$taskSerial.ReadTimeout=200
$taskClock=[Diagnostics.Stopwatch]::StartNew()
$taskBytes=0
$taskBuffer=New-Object byte[] 4096
try {
    $taskSerial.Open()
    Write-Output "UART_READER_READY $Port 115200 8N1 listen_only seconds=$Seconds output=$taskOutput"
    while ($taskClock.Elapsed.TotalSeconds -lt $Seconds) {
        try {
            if (-not $taskSerial.IsOpen) { $taskSerial.Open() }
            $taskAvailable=$taskSerial.BytesToRead
            if ($taskAvailable -gt 0) {
                $taskCount=$taskSerial.Read($taskBuffer,0,[Math]::Min(4096,$taskAvailable))
                $taskLog.Write($taskBuffer,0,$taskCount)
                $taskLog.Flush()
                $taskBytes+=$taskCount
                Write-Output ([Text.Encoding]::ASCII.GetString($taskBuffer,0,$taskCount))
            }
        } catch [System.IO.IOException] {
            $taskSerial.Close()
        } catch [System.UnauthorizedAccessException] {
            $taskSerial.Close()
        }
        Start-Sleep -Milliseconds 100
    }
} finally {
    $taskSerial.Close()
    $taskSerial.Dispose()
    $taskLog.Dispose()
    $taskRecord['bytes_received']=$taskBytes
    $taskRecord['status']=if ($taskBytes -gt 0) {'uart_bytes_observed'} else {'no_uart_bytes_observed'}
    $taskRecord['completed_utc']=[DateTime]::UtcNow.ToString('o')
    $taskText=[Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes((Join-Path $taskOutput 'boot_uart.bin')))
    [IO.File]::WriteAllText((Join-Path $taskOutput 'boot_uart.txt'),$taskText,(New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText((Join-Path $taskOutput 'summary.json'),($taskRecord|ConvertTo-Json -Depth 6),(New-Object Text.UTF8Encoding($false)))
    Write-Output "UART_CAPTURE_COMPLETE bytes=$taskBytes output=$taskOutput"
}
if ($taskBytes -eq 0) { exit 1 }
