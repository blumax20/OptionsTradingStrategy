# OiRiskRetry.ps1
# Run OI cleanup + risk exits retry (DailyCycleManagement --risk-exits-only)
# Called from PushButtonMenu option 8-6

$cmdScript = "C:\OptionsHistory\bin\RiskExitsRetry.cmd"
$ibLog     = "C:\OptionsHistory\logs\ib_cycle.log"

Write-Host "Running OI cleanup + risk exits retry via RiskExitsRetry.cmd..." -ForegroundColor Cyan
Write-Host "(Output streaming from ib_cycle.log -- this takes 2-5 minutes)" -ForegroundColor DarkCyan

# Remember log size before we start so we only show new lines
$startPos = 0
if (Test-Path $ibLog) {
    $startPos = (Get-Item $ibLog).Length
}

# Launch the cmd asynchronously so we can tail the log while it runs
$proc = Start-Process -FilePath "cmd.exe" `
                      -ArgumentList @("/c", "`"$cmdScript`"") `
                      -WorkingDirectory "C:\OptionsHistory\bin" `
                      -NoNewWindow -PassThru

# Tail the log in real time while the process runs
Write-Host ""
while (-not $proc.HasExited) {
    Start-Sleep -Milliseconds 800
    if (Test-Path $ibLog) {
        $f = [System.IO.File]::Open($ibLog, 'Open', 'Read', 'ReadWrite')
        $f.Seek($startPos, 'Begin') | Out-Null
        $reader = New-Object System.IO.StreamReader($f)
        $chunk = $reader.ReadToEnd()
        $reader.Close(); $f.Close()
        if ($chunk.Length -gt 0) {
            Write-Host $chunk -NoNewline
            $startPos += [System.Text.Encoding]::Default.GetByteCount($chunk)
        }
    }
}

# Flush any last lines written after process exit
Start-Sleep -Milliseconds 500
if (Test-Path $ibLog) {
    $f = [System.IO.File]::Open($ibLog, 'Open', 'Read', 'ReadWrite')
    $f.Seek($startPos, 'Begin') | Out-Null
    $reader = New-Object System.IO.StreamReader($f)
    $chunk = $reader.ReadToEnd()
    $reader.Close(); $f.Close()
    if ($chunk.Length -gt 0) { Write-Host $chunk -NoNewline }
}

Write-Host ""
Write-Host ("Done (exit {0})" -f $proc.ExitCode) -ForegroundColor Cyan
