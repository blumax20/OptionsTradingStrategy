# PushButtonMenu.ps1 — master control menu (PS 5.1 safe)

# ---- Paths ----
$Root         = "C:\Users\Administrator\code\OptionsTradingStrategy"
$StartScript  = Join-Path $Root "PushButtonStart.ps1"
$StopScript   = Join-Path $Root "PushButtonStop.ps1"
$TestScript   = Join-Path $Root "PushButtonTestBatch.ps1"
$HealthScript = Join-Path $Root "Health.ps1"
$TunnelDoc    = Join-Path $Root "PushButtonTunnelDoctor.ps1"
$PyExe        = Join-Path $Root ".venv\Scripts\python.exe"
$PlaceScript  = Join-Path $Root "InteractiveBrokersTrader\PlaceAnOrder.py"
$SwitchScript = Join-Path $Root "switch_trading_mode.py"

# ---- Central log folder ----
$LogRoot = "C:\OptionsHistory\logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null

function New-LogFile([string]$base) {
    $ts = Get-Date -Format "yyyyMMdd_HHmmss"
    Join-Path $LogRoot "$($base)_$ts.log"
}

function Pause-Enter {
    try {
        Write-Host ""
        [void](Read-Host "Press Enter to continue")
    } catch {
        # If stdin isn't interactive, don't crash — just continue
    }
}

function Run-With-RealTime {
    param(
        [Parameter(Mandatory)] [string]$Title,
        [Parameter(Mandatory)] [string]$ScriptPath,
        [array]$Arguments = @()
    )
    $log = New-LogFile ($Title -replace '\s+','-')
    Write-Host ("--- {0} (logging to {1}) ---" -f $Title, $log) -ForegroundColor Cyan
    Write-Host "Output streaming... (Ctrl+C to cancel)" -ForegroundColor Yellow

    try {
        # Stream output in real-time
        & powershell -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments 2>&1 |
            Tee-Object -FilePath $log |
            Write-Host

        $code = $LASTEXITCODE
        if ($code -eq 0) {
            Write-Host ("✅ {0} completed" -f $Title) -ForegroundColor Green
        } else {
            Write-Host ("❌ {0} finished with errors (exit {1})" -f $Title, $code) -ForegroundColor Yellow
        }
    } catch {
        Write-Host ("❌ {0} threw: {1}" -f $Title, $_.Exception.Message) -ForegroundColor Red
    }
    return $log
}

function Get-LatestHealthReport {
    $cand = Get-ChildItem -Path $LogRoot -Filter 'health_*.txt' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($cand) { return $cand.FullName }
    $cand = Get-ChildItem -Path $Root -Filter 'health_*.txt' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($cand) { return $cand.FullName }
    $fallback = Join-Path $Root 'OneShotHealth.report.txt'
    if (Test-Path $fallback) { return $fallback }
    return $null
}
function Get-LatestAttemptsCsv {
    # Build NY "today" and "yesterday" folder names by asking Python (which uses tz NY in DCM)
    $py = @"
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
ny = ZoneInfo("America/New_York")
d1 = datetime.now(ny).strftime("%y_%m_%d")
d0 = (datetime.now(ny)-timedelta(days=1)).strftime("%y_%m_%d")
print(d1+";"+d0)
"@
    $yy = (& $PyExe -c $py 2>$null).Trim()
    $pieces = $yy -split ';'
    $nyToday = $pieces[0]
    $nyYday  = $pieces[1]

    $dirs = @()
    $dirs += Join-Path 'C:\OptionsHistory' $nyToday
    $dirs += Join-Path 'C:\OptionsHistory' $nyYday
    $dirs += 'C:\OptionsHistory\logs'

    $cands = @()
    foreach ($d in $dirs) {
        if (Test-Path $d) {
            $cands += Get-ChildItem -Path $d -Filter 'attempts_*.csv' -File -ErrorAction SilentlyContinue
        }
    }
    $cands | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
function Run-And-Log {
    param(
        [Parameter(Mandatory)] [string]$Title,
        [Parameter(Mandatory)] [scriptblock]$Action
    )
    $log = New-LogFile ($Title -replace '\s+','-')
    Write-Host ("--- {0} (logging to {1}) ---" -f $Title, $log) -ForegroundColor Cyan
    $global:LASTEXITCODE = 0
    try {
        & { & $Action *>&1 } | Tee-Object -FilePath $log -Append | Write-Host
        $code = $LASTEXITCODE
        if ($code -eq $null) { $code = 0 }
        if ($code -eq 0) {
            Write-Host ("✅ {0} completed (exit {1})" -f $Title, $code) -ForegroundColor Green
        } else {
            Write-Host ("❌ {0} finished with errors (exit {1})" -f $Title, $code) -ForegroundColor Yellow
        }
    } catch {
        ($_ | Out-String) | Tee-Object -FilePath $log -Append | Write-Host
        Write-Host ("❌ {0} threw: {1}" -f $Title, $_.Exception.Message) -ForegroundColor Red
    }
    return $log
}

function Show-Menu {
    Clear-Host
    Write-Host "============================="
    Write-Host "   OPTIONS TRADING MENU"
    Write-Host "============================="
    Write-Host "1)  System Health Check (write report)"
    Write-Host "2)  Start trading system"
    Write-Host "3)  Stop trading system"
    Write-Host "4)  Restart trading system"
    Write-Host "5)  Test batch endpoint (/webhook_batch)"
    Write-Host "6)  Tunnel Doctor (diagnose/restart tunnel)"
    Write-Host "7)  Switch trading mode (paper <-> live)"
    Write-Host "8)  Place today's orders (from CSV)"
    Write-Host "9)  Exit"
    Write-Host "10) Reboot system"
    Write-Host "============================="
}

# Optional transcript for the menu session
if (-not (Get-Variable -Name __TranscriptOn -Scope Script -ErrorAction SilentlyContinue)) {
    $menuLog = New-LogFile "menu_session"
    try { Start-Transcript -Path $menuLog -Append -ErrorAction SilentlyContinue | Out-Null } catch {}
    Set-Variable -Name __TranscriptOn -Value $true -Scope Script
}

while ($true) {
    Show-Menu
    $choice = Read-Host "Choose an option (1-10)"

    switch ($choice) {

        1 {
            if (Test-Path $HealthScript) {
                Run-With-RealTime -Title "Health" -ScriptPath $HealthScript
                $last = Get-LatestHealthReport
                if ($last) { Write-Host "`nLatest report:`n$last" }
            } else {
                Write-Host ("Health script not found: {0}" -f $HealthScript)
            }
        }

        2 {
            Write-Host ""
            Write-Host "  1) Pre-warm API clientIds (register after IBGateway restart/login)"
            Write-Host "  2) Start full system"
            $sub = Read-Host "  Choose (1 or 2)"
            switch ($sub.Trim()) {
                '1' {
                    $prewarmCmd = "C:\OptionsHistory\bin\PrewarmConnections.cmd"
                    if (Test-Path $prewarmCmd) {
                        Write-Host "Running pre-warm to register all API clientIds with IBGateway..." -ForegroundColor Cyan
                        & cmd.exe /c $prewarmCmd
                        $prewarmFlag = "C:\OptionsHistory\logs\watchdog_prewarm_needed.txt"
                        if (Test-Path $prewarmFlag) { Remove-Item $prewarmFlag -ErrorAction SilentlyContinue }
                        Write-Host "Pre-warm complete. ClientIds registered -- no 3 PM approval dialogs expected." -ForegroundColor Green
                    } else {
                        Write-Host "PrewarmConnections.cmd not found at $prewarmCmd" -ForegroundColor Red
                    }
                }
                '2' {
                    if (Test-Path $StartScript) {
                        $doSum = Read-Host "Show account/positions summary after start? (y/N)"
                        $args = @()
                        if ($doSum -and $doSum.ToLower().StartsWith("y")) { $args += "-Summary" }
                        Run-And-Log -Title "Start" -Action { powershell -NoProfile -ExecutionPolicy Bypass -File $StartScript @args }
                    } else {
                        Write-Host ("Start script not found: {0}" -f $StartScript)
                    }
                }
                Default {
                    Write-Host "Invalid selection." -ForegroundColor Yellow
                }
            }
            Pause-Enter
        }

        3 {
            if (Test-Path $StopScript) {
                Run-And-Log -Title "Stop" -Action { powershell -NoProfile -ExecutionPolicy Bypass -File $StopScript }
            } else {
                Write-Host ("Stop script not found: {0}" -f $StopScript)
            }
            Pause-Enter
        }

        4 {
            if (Test-Path $StopScript) {
                Run-And-Log -Title "Stop"  -Action { powershell -NoProfile -ExecutionPolicy Bypass -File $StopScript }
                Write-Host "Waiting 5 seconds for services to fully stop..." -ForegroundColor DarkCyan
                Start-Sleep -Seconds 5
            }
            if (Test-Path $StartScript) {
                Run-And-Log -Title "Start" -Action { powershell -NoProfile -ExecutionPolicy Bypass -File $StartScript }
            }
            Pause-Enter
        }

        5 {
            $base = Read-Host "Enter base URL (default http://localhost:5001)"
            if ([string]::IsNullOrWhiteSpace($base)) { $base = "http://localhost:5001" }
            if (Test-Path $TestScript) {
                Run-And-Log -Title "TestBatch" -Action { powershell -NoProfile -ExecutionPolicy Bypass -File $TestScript -BaseUrl $base }
            } else {
                Write-Host ("Batch test script not found: {0}" -f $TestScript)
            }
            Pause-Enter
        }

        6 {
            if (Test-Path $TunnelDoc) {
                $doRestart = Read-Host "Restart the CloudflareTunnel service too? (y/N)"
                $args = @()
                if ($doRestart -and $doRestart.ToLower().StartsWith("y")) { $args += "-Restart" }
                Run-And-Log -Title "TunnelDoctor" -Action { powershell -NoProfile -ExecutionPolicy Bypass -File $TunnelDoc @args }
            } else {
                Write-Host ("Tunnel Doctor script not found: {0}" -f $TunnelDoc)
            }
            Pause-Enter
        }

        7 {
            # === Switch trading mode (paper <-> live) ===
            if (-not (Test-Path $SwitchScript)) {
                Write-Host ("switch_trading_mode.py not found: {0}" -f $SwitchScript) -ForegroundColor Red
                Pause-Enter; continue
            }
            if (-not (Test-Path $PyExe)) {
                Write-Host ("Python venv not found: {0}" -f $PyExe) -ForegroundColor Red
                Pause-Enter; continue
            }

            # Show current mode
            Write-Host ""
            $statusRaw = (& $PyExe $SwitchScript status 2>&1) | Out-String
            $statusLine = $statusRaw.Trim()
            if ($statusLine -match 'LIVE') {
                Write-Host ("Current: {0}" -f $statusLine) -ForegroundColor Green
                $targetMode = "paper"
                $targetLabel = "PAPER (port 7497)"
            } elseif ($statusLine -match 'PAPER') {
                Write-Host ("Current: {0}" -f $statusLine) -ForegroundColor Yellow
                $targetMode = "live"
                $targetLabel = "LIVE (port 7496)"
            } else {
                Write-Host ("Current mode unknown: {0}" -f $statusLine) -ForegroundColor Red
                $targetMode = $null
            }

            Write-Host ""
            if ($targetMode) {
                $confirm = Read-Host ("Switch to {0}? (y/N)" -f $targetLabel)
            } else {
                $confirm = Read-Host "Enter target mode to switch to (paper / live), or Enter to cancel"
                if ($confirm -match '^(paper|live)$') {
                    $targetMode = $confirm.Trim().ToLower()
                    $confirm = "y"
                } else {
                    $confirm = ""
                }
            }

            if (-not ($confirm -and $confirm.Trim().ToLower().StartsWith("y"))) {
                Write-Host "Switch cancelled." -ForegroundColor DarkCyan
                Pause-Enter; continue
            }

            Write-Host ""
            Write-Host ("Switching to {0}..." -f $targetMode.ToUpper()) -ForegroundColor Cyan
            Write-Host "(IBGateway will be restarted - this takes about 30 seconds)" -ForegroundColor DarkCyan
            Write-Host ""

            # Run the switch script and stream output
            $switchLog = New-LogFile "switch_trading_mode"
            & $PyExe $SwitchScript $targetMode 2>&1 | Tee-Object -FilePath $switchLog | ForEach-Object {
                if ($_ -match '^\s*\[OK\]') {
                    Write-Host $_ -ForegroundColor Green
                } elseif ($_ -match '^\s*\[FAIL\]|\[WARN\]') {
                    Write-Host $_ -ForegroundColor Yellow
                } elseif ($_ -match 'CHECKLIST|verify') {
                    Write-Host $_ -ForegroundColor Cyan
                } else {
                    Write-Host $_
                }
            }
            $switchExit = $LASTEXITCODE

            Write-Host ""
            if ($switchExit -eq 0) {
                Write-Host ("Switch to {0} completed." -f $targetMode.ToUpper()) -ForegroundColor Green
            } else {
                Write-Host ("Switch exited with code {0}. Check output above." -f $switchExit) -ForegroundColor Red
                Pause-Enter; continue
            }

            # Wait for IBGateway to come back up before running Health
            Write-Host ""
            Write-Host "Waiting 20 seconds for IBGateway to restart..." -ForegroundColor DarkCyan
            Start-Sleep -Seconds 20

            # Run Health.ps1 to confirm new mode and show positions
            Write-Host ""
            Write-Host "--- Running Health check to confirm new mode ---" -ForegroundColor Cyan
            if (Test-Path $HealthScript) {
                Run-With-RealTime -Title "Health" -ScriptPath $HealthScript
                $last = Get-LatestHealthReport
                if ($last) { Write-Host ("`nHealth report saved: {0}" -f $last) -ForegroundColor DarkCyan }
            } else {
                Write-Host ("Health script not found: {0}" -f $HealthScript) -ForegroundColor Yellow
            }

            Pause-Enter
        }
        # --- Place today's orders (from CSV) ---
        # --- Place today's orders (via DailyCycleManagement) ---
        8 {
            #
            # DailyCycleManagement launcher (direct) with selectable flows.
            # This uses the new __main__ CLI in DailyCycleManagement.py.
            #
            $env:PYTHONUNBUFFERED = "1"
            $PyExe  = "C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
            $DCMSrc = "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\DailyCycleManagement.py"

            if (-not (Test-Path $PyExe))  { Write-Host ("Python venv not found: {0}" -f $PyExe);  Pause-Enter; break }
            if (-not (Test-Path $DCMSrc)) { Write-Host ("DailyCycleManagement.py not found: {0}" -f $DCMSrc); Pause-Enter; break }

            # --- flow menu ---
            Write-Host ""
            Write-Host "Choose DCM flow:"
            Write-Host "  1) Place OPENs from latest CSVs (JOIN limits, DTE >= 20)"
            Write-Host "  2) Pre-close sweep (~3:00 pm ET) - delegate CLOSE and convert stubborn limits to MKT; flattens STK on CLOSE"
            Write-Host "  3) Reconcile held positions vs latest signals (21d) - outside RTH"
            Write-Host "  4) After-hours batch (OPENs plus CLOSE delegates 7d/21d)"
            Write-Host "  5) Enforce CLOSES from last N days (fallback) → enter N"
            Write-Host "  6) OI cleanup + risk exits retry (Fix CN/CO) -- cancel low-OI orders, then run risk exits"
            Write-Host "  7) Place skipped OPEN orders from prior day (10 AM retry -- Fix CP/CQ)"
            $flow = Read-Host "Select [1-7]"

            $argList = @("`"$DCMSrc`"")
            $skipGenericLaunch = $false
            $wantVerbose = Read-Host "Verbose logs? (y/N)"
            if ($wantVerbose -and $wantVerbose.ToLower().StartsWith('y')) { $argList += "--verbose" }

            switch ($flow) {
                '1' { $argList += "--place-opens" }
                '2' { $argList += "--preclose" }
                '3' { $argList += "--reconcile" }
                '4' { $argList += "--after-hours" }
                '5' {
                    $n = Read-Host "Enter lookback days (e.g., 7)"
                    if ([string]::IsNullOrWhiteSpace($n)) { $n = "7" }
                    $argList += @("--enforce-closes", $n)
                }
                '6' {
                    $skipGenericLaunch = $true
                    & powershell.exe -NonInteractive -File "C:\OptionsHistory\bin\OiRiskRetry.ps1"
                    Pause-Enter
                }
                '7' {
                    $skipGenericLaunch = $true
                    Write-Host "Running PlaceSkippedOpens.cmd ..." -ForegroundColor Cyan
                    & cmd.exe /c "C:\OptionsHistory\bin\PlaceSkippedOpens.cmd"
                    Pause-Enter
                }
                Default {
                    Write-Host "Invalid selection." -ForegroundColor Yellow
                    Pause-Enter; break
                }
            }

            # --- logging setup ---
            $logDir = "C:\OptionsHistory\logs"
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
            $stamp  = (Get-Date).ToString("yyyyMMdd_HHmmss")
            $log    = Join-Path $logDir ("DailyCycleManagement_session_{0}.log" -f $stamp)

            Write-Host ("--- DailyCycleManagement {0} ---" -f ($argList -join ' ')) -ForegroundColor Cyan
            Write-Host ("Logging to: {0}" -f $log) -ForegroundColor DarkCyan
            Write-Host "Press Ctrl+C to cancel if hung..." -ForegroundColor Yellow
            # # ---- Submit OPENs from today's CSV via PlaceAnOrder (live JOIN pricing) ----
            # try {
            #     Write-Host ""
            #     Write-Host "Submitting OPEN limit orders from today's CSV (JOIN pricing)..." -ForegroundColor Cyan

            #     $poLogOut = New-LogFile "PlaceAnOrder_OpenFromSignal_out"
            #     $poLogErr = New-LogFile "PlaceAnOrder_OpenFromSignal_err"
            #     $poArgs = @(
            #         "`"$PlaceScript`"",
            #         "--mode","from-signal",
            #         "--min-limit","0.05",
            #         "--bump-to-min",
            #         "--use-live-open","join",
            #         "--use-live-close","off",
            #         "--quiet"
            #     )

            #     # Run from the script directory to avoid relative-path issues
            #     $poWorkDir = Split-Path $PlaceScript -Parent
            #     $po = $null
            #     try {
            #         $po = Start-Process -FilePath $PyExe `
            #                             -ArgumentList $poArgs `
            #                             -WorkingDirectory $poWorkDir `
            #                             -NoNewWindow `
            #                             -RedirectStandardOutput $poLogOut `
            #                             -RedirectStandardError  $poLogErr `
            #                             -PassThru
            #     } catch {
            #         Write-Host ("Failed to start PlaceAnOrder: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
            #     }
            #     if ($po) {
            #         $po.WaitForExit()
            #         Write-Host ("PlaceAnOrder exited with {0}. Out: {1} Err: {2}" -f $po.ExitCode, $poLogOut, $poLogErr)
            #     } else {
            #         Write-Host ("PlaceAnOrder did not start. See: {0}, {1}" -f $poLogOut, $poLogErr) -ForegroundColor Yellow
            #     }
            # } catch {
            #     Write-Host ("Failed to submit OPEN orders: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
            # }

            # # ---- Re-summarize attempts after OPEN submission ----
            # try {
            #     $latestAttempts2 = Get-LatestAttemptsCsv
            #     if ($latestAttempts2) {
            #         Write-Host ""
            #         Write-Host ("Attempts CSV (post-OPEN): {0}" -f $latestAttempts2.FullName) -ForegroundColor Cyan
            #         $rows2   = Import-Csv -Path $latestAttempts2.FullName
            #         $placed2 = ($rows2 | Where-Object { $_.status -eq 'placed' }).Count
            #         $non2    =  $rows2 | Where-Object { $_.status -ne 'placed' }
            #         Write-Host ("Placed count (post-OPEN): {0}" -f $placed2)
            #         if ($non2.Count -gt 0) {
            #             Write-Host "Not-placed by reason (post-OPEN):"
            #             $groups2 = $non2 | Group-Object reason | Sort-Object Count -Desc
            #             foreach ($g2 in $groups2) {
            #                 $name2 = if ($g2.Name) { $g2.Name } else { '(unknown)' }
            #                 Write-Host ("  {0,-32} : {1}" -f $name2, $g2.Count)
            #             }
            #         }
            #     } else {
            #         Write-Host "No attempts CSV found post-OPEN (checked dated folder and logs)." -ForegroundColor Yellow
            #     }
            # } catch {
            #     Write-Host ("Attempts summary (post-OPEN) error: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
            # }
            if ($skipGenericLaunch) { break }  # option 6 already handled above; skip generic launch
            # Fix AA8: Redirect stdout/stderr to session log for post-mortem analysis
            $logErr = $log -replace '\.log$', '.err.log'
            try {
                $proc = Start-Process -FilePath $PyExe `
                                      -ArgumentList $argList `
                                      -WorkingDirectory (Split-Path $DCMSrc -Parent) `
                                      -NoNewWindow `
                                      -PassThru `
                                      -Wait `
                                      -RedirectStandardOutput $log `
                                      -RedirectStandardError $logErr

                Write-Host ("Done (pid {0}, exit {1}). Log: {2}" -f $proc.Id, $proc.ExitCode, $log)
            } catch {
                Write-Host ("Failed to start or complete DCM: {0}" -f $_.Exception.Message) -ForegroundColor Red
                if ($proc -and -not $proc.HasExited) {
                    try { Stop-Process -Id $proc.Id -Force } catch {}
                }
                Pause-Enter; break
            }
            # Show DCM output to user after completion
            try {
                if (Test-Path $log) {
                    Write-Host "--- DCM Output ---" -ForegroundColor DarkCyan
                    Get-Content $log | ForEach-Object { Write-Host $_ }
                    $hint = Select-String -Path $log -Pattern 'Attempt CSV path resolved to:' -SimpleMatch | Select-Object -Last 1
                    if ($hint) { Write-Host ("DCM attempts folder hint: {0}" -f $hint.Line) -ForegroundColor DarkCyan }
                }
                if (Test-Path $logErr) {
                    $errContent = Get-Content $logErr
                    if ($errContent) {
                        Write-Host "--- DCM Errors ---" -ForegroundColor Yellow
                        $errContent | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
                    }
                }
            } catch {}

            # ---- Attempts CSV summary (latest) ----
            try {
                $latestAttemptsX = Get-LatestAttemptsCsv
                if ($latestAttemptsX) {
                    Write-Host ""
                    Write-Host ("Attempts CSV: {0}" -f $latestAttemptsX.FullName) -ForegroundColor Cyan
                    $rows   = Import-Csv -Path $latestAttemptsX.FullName
                    $placed = ($rows | Where-Object { $_.status -eq 'placed' }).Count
                    $non    =  $rows | Where-Object { $_.status -ne 'placed' }
                    Write-Host ("Placed count: {0}" -f $placed)
                    if ($non.Count -gt 0) {
                        Write-Host "Not-placed by reason:"
                        $groups = $non | Group-Object reason | Sort-Object Count -Desc
                        foreach ($g in $groups) {
                            $name = if ($g.Name) { $g.Name } else { '(unknown)' }
                            Write-Host ("  {0,-32} : {1}" -f $name, $g.Count)
                        }
                        Write-Host "`nLast 20 not-placed rows:"
                        $tbl = $non | Select-Object -Last 20 ts, symbol, action, exp, right, limit, reason | Format-Table -AutoSize | Out-String
                        Write-Host $tbl
                    } else {
                        Write-Host "No not-placed entries."
                    }
                } else {
                    Write-Host "No attempts_*.csv found (checked dated folder and logs)." -ForegroundColor Yellow
                }
            } catch {
                Write-Host ("Attempts summary error: {0}" -f $_.Exception.Message) -ForegroundColor Yellow
            }

            # ---- Tail convenience logs ----
            try {
                $dcLog = Join-Path $logDir 'DailyCycle.log'
                if (Test-Path $dcLog) {
                    Write-Host ""
                    Write-Host ("DailyCycle.log (tail 120): {0}" -f $dcLog) -ForegroundColor Cyan
                    (Get-Content $dcLog -Tail 120 | Out-String) | Write-Host
                }
            } catch {}

            try {
                $poLogs = Get-ChildItem -Path $logDir -Filter 'PlaceAnOrder_*.log' -File -ErrorAction SilentlyContinue |
                          Sort-Object LastWriteTime -Desc | Select-Object -First 2
                foreach ($l in $poLogs) {
                    Write-Host ""
                    Write-Host ("PlaceAnOrder log (tail 80): {0}" -f $l.FullName) -ForegroundColor Cyan
                    (Get-Content $l.FullName -Tail 80 | Out-String) | Write-Host
                }
            } catch {}

            try {
                $ibCycle = Join-Path $logDir 'ib_cycle.log'
                if (Test-Path $ibCycle) {
                    Write-Host ""
                    Write-Host ("ib_cycle.log (tail 120): {0}" -f $ibCycle) -ForegroundColor Cyan
                    (Get-Content $ibCycle -Tail 120 | Out-String) | Write-Host
                }
            } catch {}

            Pause-Enter
        }

        9 {
            Write-Host "Exiting menu..."
            Start-Sleep -Milliseconds 400
            try { Stop-Transcript | Out-Null } catch {}
            [Environment]::Exit(0)   # end the host entirely (works in PS 5.1 and in VS Code terminal)
        }

        10 {
            Write-Host "Rebooting system..."
            Start-Sleep -Seconds 1
            Restart-Computer -Force

        }

        Default {
            Write-Host "Invalid choice."
            Start-Sleep -Milliseconds 800
        }
    }
}

#try { Stop-Transcript | Out-Null } catch {}