# Compare2FA.ps1 -- Compare 2FA predictions vs actual watchdog.log events
# Run after the weekend to see how predictions matched reality.
# Usage: powershell -File C:\OptionsHistory\bin\Compare2FA.ps1

$PredictionsFile = "C:\OptionsHistory\logs\2fa_predictions.txt"
$WatchdogLog     = "C:\OptionsHistory\logs\watchdog.log"
$IbcBackupDir    = "C:\OptionsHistory\logs\ibc_backup"

Write-Host ""
Write-Host "===== 2FA Prediction vs Actual Report =====" -ForegroundColor Cyan
Write-Host "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host ""

# --- Load predictions ---
$predictions = @()
if (Test-Path $PredictionsFile) {
    Get-Content $PredictionsFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(.+)') {
                $predictions += [PSCustomObject]@{
                    PredTime  = [DateTime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm', $null)
                    PredDesc  = $Matches[2].Trim()
                }
            }
        }
    }
} else {
    Write-Warning "Predictions file not found: $PredictionsFile"
}

# --- Load watchdog events ---
$watchdogEvents = @()
if (Test-Path $WatchdogLog) {
    Get-Content $WatchdogLog | ForEach-Object {
        if ($_ -match '^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.+)$') {
            $ts   = [DateTime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
            $msg  = $Matches[2].Trim()
            $type = switch -Regex ($msg) {
                'FAIL \(2FA\)'       { '2FA-DETECTED' }
                'ONLINE:'            { 'ONLINE' }
                'PREWARM:'           { 'PREWARM' }
                'RESTART: calling'   { 'RESTART' }
                '^OK$'               { 'OK' }
                default              { $null }
            }
            if ($type) {
                $watchdogEvents += [PSCustomObject]@{ Ts=$ts; Type=$type; Msg=$msg }
            }
        }
    }
} else {
    Write-Warning "watchdog.log not found: $WatchdogLog"
}

# --- Match predictions to actual events ---
Write-Host "--- Prediction vs Actual ---" -ForegroundColor Yellow
Write-Host ("{0,-22} {1,-16} {2,-22} {3,-10} {4}" -f "Predicted Time","Pred Type","Actual Time","Delta","Actual Event")
Write-Host ("-" * 100)

foreach ($pred in $predictions) {
    # Find the closest 2FA or RESTART event within +/- 3 hours of prediction
    $match = $watchdogEvents | Where-Object {
        ($_.Type -eq '2FA-DETECTED' -or $_.Type -eq 'RESTART') -and
        [Math]::Abs(($_.Ts - $pred.PredTime).TotalMinutes) -le 180
    } | Sort-Object { [Math]::Abs(($_.Ts - $pred.PredTime).TotalMinutes) } | Select-Object -First 1

    if ($match) {
        $deltaMin = [int](($match.Ts - $pred.PredTime).TotalMinutes)
        $deltaStr = if ($deltaMin -ge 0) { "+${deltaMin}min" } else { "${deltaMin}min" }
        Write-Host ("{0,-22} {1,-16} {2,-22} {3,-10} {4}" -f `
            $pred.PredTime.ToString('yyyy-MM-dd HH:mm'), `
            ($pred.PredDesc -split ' -- ')[1], `
            $match.Ts.ToString('yyyy-MM-dd HH:mm:ss'), `
            $deltaStr, `
            $match.Type)
    } else {
        Write-Host ("{0,-22} {1,-16} {2,-22} {3,-10} {4}" -f `
            $pred.PredTime.ToString('yyyy-MM-dd HH:mm'), `
            ($pred.PredDesc -split ' -- ')[1], `
            "(no match found)", `
            "---", `
            "(check ibc_backup)") -ForegroundColor DarkYellow
    }
}

# --- Authentication durations (FAIL 2FA -> next OK) ---
Write-Host ""
Write-Host "--- 2FA Authentication Durations ---" -ForegroundColor Yellow
Write-Host ("{0,-22} {1,-22} {2,-12} {3}" -f "2FA Detected","User Authenticated","Duration","Note")
Write-Host ("-" * 80)

$failEvents = $watchdogEvents | Where-Object { $_.Type -eq '2FA-DETECTED' }
foreach ($fail in $failEvents) {
    $online = $watchdogEvents | Where-Object {
        ($_.Type -eq 'ONLINE' -or $_.Type -eq 'OK') -and $_.Ts -gt $fail.Ts
    } | Select-Object -First 1

    if ($online) {
        $dur = $online.Ts - $fail.Ts
        $durStr = if ($dur.TotalHours -ge 1) { "$([int]$dur.TotalHours)h $($dur.Minutes)min" } else { "$($dur.Minutes)min" }
        Write-Host ("{0,-22} {1,-22} {2,-12} {3}" -f `
            $fail.Ts.ToString('yyyy-MM-dd HH:mm'), `
            $online.Ts.ToString('yyyy-MM-dd HH:mm'), `
            $durStr, `
            $(if ($online.Type -eq 'ONLINE') { 'ONLINE logged' } else { 'first OK' }))
    } else {
        Write-Host ("{0,-22} {1,-22} {2,-12}" -f `
            $fail.Ts.ToString('yyyy-MM-dd HH:mm'), "(still pending)", "---") -ForegroundColor Red
    }
}

# --- IBC backup log summary ---
Write-Host ""
Write-Host "--- IBC Log Backups (timestamps of 2FA events) ---" -ForegroundColor Yellow
if (Test-Path $IbcBackupDir) {
    $backups = Get-ChildItem $IbcBackupDir -Filter "*.txt" | Sort-Object LastWriteTime
    foreach ($bk in $backups) {
        $has2fa = Select-String -Path $bk.FullName -Pattern "Second Factor Authentication|autorestart file not found" -Quiet
        if ($has2fa) {
            $line = Select-String -Path $bk.FullName -Pattern "Second Factor Authentication|autorestart file not found" | Select-Object -First 1
            Write-Host "  $($bk.Name): $($line.Line.Trim())" -ForegroundColor DarkYellow
        }
    }
} else {
    Write-Host "  (ibc_backup directory not found -- hourly backup task may not have run yet)"
}

Write-Host ""
Write-Host "===== End Report =====" -ForegroundColor Cyan
