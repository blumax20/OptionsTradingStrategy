# Fix AB8: IB Watchdog -- Auto-Restart During Trading Hours
# Fix AK:  Added CloudflareTunnel service check (tunnel-only restart, no IB disruption)
# Fix CV:  Soft-fail re-check for "IB not connected": wait 2 min before full restart.
# Fix DH:  On SOFT-FAIL, restart OptionsListener only if port still UP (IBGateway alive).
# Fix DJ:  On port-DOWN FAIL, check IBC log for 2FA dialog before calling BounceServices.
#          If 2FA detected: log FAIL (2FA) and exit -- user logs in; watchdog retries in 15min.
#          Detects: "Second Factor Authentication" (manual restart) or "Restart in progress" (5AM timer).
#          No IBGateway kill, no 2FA required. Escalate to BounceServices only if listener
#          cannot reconnect within 60s after RestartListener.cmd.
#          Port-down is a hard failure (restart immediately).
#          "IB not connected" (port up, listener responding) is transient - listener
#          auto-reconnects. Immediately calling BounceServices kills IBGateway and
#          creates a 15-minute crash cascade matching the watchdog interval.
# Fix DL:  Extended 2FA detection window from 20 min to 90 min so the 5 AM IBC log
#          (written at 5:01 AM, checked at 6:07 AM = 66 min old) is correctly detected.
# Fix DN:  Add "autorestart file not found" to 2FA detection pattern. When IBGateway
#          starts a new session after a BounceServices restart, IBC writes this text
#          immediately -- before showing the 2FA dialog. The new session file becomes
#          the most-recently-modified IBC log, overshadowing the prior session's
#          "Restart in progress" entry. Without this pattern, Fix DJ would read the
#          new session file, find no 2FA text, and call BounceServices again.
# Fix DO:  Remove -Tail 50 from IBC log read. "autorestart file not found" appears at
#          line ~42, followed by ~156 lines of JVM system properties. -Tail 50 only
#          reads the JVM dump at the end, never reaching line 42. Confirmed from
#          ibc_backup: MONDAY_0605.txt has 198 lines, pattern at line 42, tail reads
#          lines 149-198 (miss). Fix: read entire file (IBC logs are small, <5k lines/day).
# Fix DM:  On FAIL(2FA) or RESTART: write prewarm flag file. On OK after flag exists:
#          run PrewarmConnections.cmd to register all API clientIds with IBGateway,
#          then log ONLINE/PREWARM. Prevents 3 PM approval dialogs after restarts.
# Runs every 15 min via Task Scheduler (daily 6:07AM-8:07PM).
# Checks IB Gateway port, listener /health, and CloudflareTunnel service.
# Tunnel-only failure: restart just CloudflareTunnel (no BounceServices).
# IB Gateway or Listener failure: BounceServices (restarts all three).
# 10-minute cooldown prevents restart loops.
#
# To switch between paper and live trading, update IB_GW_PORT below
# (must match IB_PORT in InteractiveBrokersTrader\ib_config.py):
#   Paper trading : $IB_GW_PORT = 7497
#   Live trading  : $IB_GW_PORT = 7496
$IB_GW_PORT = 7496

$LogDir = "C:\OptionsHistory\logs"
$Log    = Join-Path $LogDir "watchdog.log"
$CooldownFile  = Join-Path $LogDir "watchdog_last_restart.txt"
$PrewarmFlag   = Join-Path $LogDir "watchdog_prewarm_needed.txt"  # Fix DM
$PrewarmCmd    = "C:\OptionsHistory\bin\PrewarmConnections.cmd"   # Fix DM
$CooldownMinutes = 10
$HealthUrl = "http://127.0.0.1:5001/health"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$ts] $msg" | Out-File -Append -FilePath $Log -Encoding ASCII
}

$needFullRestart   = $false
$needTunnelRestart = $false

# ===== CHECK 1: IB Gateway port =====
$gw = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $IB_GW_PORT }
# Get PID from TCP connection (works regardless of process name -- IBGateway runs as javaw.exe on Windows)
$gwPid = if ($gw) { ($gw | Select-Object -First 1).OwningProcess } else { "not-running" }
if (-not $gw) {
    Write-Log "FAIL: port $IB_GW_PORT not listening (IBGateway DOWN) PID=$gwPid"
    # Fix DJ: Check IBC log for 2FA dialog before triggering BounceServices.
    # When IBGateway restarts and shows a 2FA screen, port is DOWN but IBGateway is running.
    # Calling BounceServices kills the login screen; user must restart the whole cascade.
    # Instead: log it and exit (skip BounceServices) -- user authenticates, port comes back.
    $ibcLogDir = "C:\IBC\Logs"
    $twoFaDetected = $false
    $ibcLog = Get-ChildItem $ibcLogDir -Filter "IBC-*.txt" -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($ibcLog -and $ibcLog.LastWriteTime -gt (Get-Date).AddMinutes(-90)) {  # Fix DL: was -20; 5AM log is 66min old at 6:07AM
        $recent = Get-Content $ibcLog.FullName -ErrorAction SilentlyContinue  # Fix DO: was -Tail 50; "autorestart file not found" is at line ~42 but buried under 150+ JVM property lines that follow it
        if ($recent -match "2FA dialog|Second Factor Authentication|Exit Session Setting|Restart in progress|autorestart file not found") {
            $twoFaDetected = $true
        }
    }
    if ($twoFaDetected) {
        Write-Log "FAIL (2FA): IBGateway waiting for authentication -- skipping BounceServices, login manually"
        # Fix DM: flag pre-warm needed; runs automatically on next OK after user authenticates
        Get-Date -Format "yyyy-MM-dd HH:mm:ss" | Set-Content -Path $PrewarmFlag -Encoding ASCII
        exit 0
    }
    $needFullRestart = $true
}

# ===== CHECK 2: Listener /health HTTP 200 + IB connection status =====
if (-not $needFullRestart) {
    $httpOk = $false
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) {
            # /health always returns 200 ??? parse JSON to verify IB is actually connected.
            # When IB is down, the endpoint returns {"positions_error": "not connected", ...}.
            try {
                $body = $resp.Content | ConvertFrom-Json
                if ($body.positions_error) {
                    # Fix CV: Soft failure - port is up, listener is responding.
                    # Listener auto-reconnects; give it 2 minutes before triggering BounceServices.
                    # Immediate BounceServices kills IBGateway and causes a 15-min crash cascade.
                    Write-Log "SOFT-FAIL: /health=200 but IB not connected: $($body.positions_error) - waiting 2 min for auto-reconnect"
                    Start-Sleep -Seconds 120
                    try {
                        $resp2 = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
                        if ($resp2.StatusCode -eq 200) {
                            try {
                                $body2 = $resp2.Content | ConvertFrom-Json
                                if ($body2.positions_error) {
                                    Write-Log "FAIL: IB still not connected after 2 min: $($body2.positions_error)"
                                    # Fix DH: Port still UP means IBGateway is running but listener lost IB connection.
                                    # Restart OptionsListener only -- no IBGateway kill, no 2FA required.
                                    # Only escalate to BounceServices if listener cannot reconnect within 60s.
                                    if ($gw) {
                                        Write-Log "SOFT-FAIL-RETRY: port $IB_GW_PORT still UP -- restarting OptionsListener only (no IBGateway kill)"
                                        try {
                                            Start-Process -FilePath cmd.exe `
                                                -ArgumentList '/c', 'C:\OptionsHistory\bin\RestartListener.cmd' `
                                                -WorkingDirectory 'C:\OptionsHistory\bin' -Wait -NoNewWindow
                                        } catch {
                                            Write-Log "SOFT-FAIL-RETRY ERROR: RestartListener.cmd failed: $_"
                                        }
                                        $recovered = $false
                                        for ($i = 0; $i -lt 12; $i++) {
                                            Start-Sleep -Seconds 5
                                            try {
                                                $rResp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
                                                if ($rResp.StatusCode -eq 200) {
                                                    try {
                                                        $rBody = $rResp.Content | ConvertFrom-Json
                                                        if (-not $rBody.positions_error) {
                                                            Write-Log "RECOVERED: listener reconnected after RestartListener -- no 2FA needed"
                                                            $recovered = $true; break
                                                        }
                                                    } catch { $recovered = $true; break }
                                                }
                                            } catch {}
                                        }
                                        if (-not $recovered) {
                                            Write-Log "FAIL: still not connected after RestartListener (60s) -- escalating to BounceServices"
                                            $needFullRestart = $true
                                        } else {
                                            $httpOk = $true  # Fix DH2: recovery succeeded -- prevent outer check from firing BounceServices
                                        }
                                    } else {
                                        Write-Log "FAIL: port went DOWN during 2-min wait -- IBGateway crashed, escalating to BounceServices"
                                        $needFullRestart = $true
                                    }
                                } else {
                                    Write-Log "RECOVERED: IB reconnected on its own ??? no restart needed"
                                    $httpOk = $true
                                }
                            } catch {
                                $httpOk = $true  # JSON parse failed ??? treat as healthy
                            }
                        } else {
                            Write-Log "FAIL: /health not 200 on re-check after 2 min"
                            $needFullRestart = $true
                        }
                    } catch {
                        Write-Log "FAIL: /health unreachable on re-check after 2 min"
                        $needFullRestart = $true
                    }
                } else {
                    $httpOk = $true
                }
            } catch {
                # JSON parse failed ??? treat as healthy to avoid unnecessary bounces
                $httpOk = $true
            }
        }
    } catch {
        # connection refused, timeout, HTTP error
    }
    if (-not $httpOk -and -not $needFullRestart) {
        Write-Log "FAIL: /health did not return HTTP 200 (Listener DOWN or unhealthy)"
        $needFullRestart = $true
    }
}

# ===== CHECK 3: CloudflareTunnel service =====
# Fix AK: tunnel crash does not affect IB Gateway or local listener, so watchdog
# previously missed it. A targeted service restart avoids disrupting IB connections.
if (-not $needFullRestart) {
    $tunnelSvc = Get-Service -Name "CloudflareTunnel" -ErrorAction SilentlyContinue
    if (-not $tunnelSvc -or $tunnelSvc.Status -ne "Running") {
        Write-Log "FAIL: CloudflareTunnel service not Running (tunnel DOWN)"
        $needTunnelRestart = $true
    }
}

if (-not $needFullRestart -and -not $needTunnelRestart) {
    # Fix DM: if pre-warm flag exists, IBGateway just came back up after a restart or 2FA.
    # Run PrewarmConnections.cmd to register all API clientIds before trading starts.
    if (Test-Path $PrewarmFlag) {
        $flagAge = (Get-Date) - (Get-Item $PrewarmFlag).LastWriteTime
        Write-Log "ONLINE: IBGateway port back up after FAIL(2FA) or RESTART -- running pre-warm (flag was $([int]$flagAge.TotalMinutes)min old)"
        Remove-Item $PrewarmFlag -ErrorAction SilentlyContinue
        if (Test-Path $PrewarmCmd) {
            try {
                Start-Process -FilePath cmd.exe -ArgumentList '/c', $PrewarmCmd `
                    -WorkingDirectory 'C:\OptionsHistory\bin' -Wait -NoNewWindow
                Write-Log "PREWARM: registered all clientIds with IBGateway."
            } catch {
                Write-Log "PREWARM ERROR: PrewarmConnections.cmd failed: $_"
            }
        } else {
            Write-Log "PREWARM SKIP: $PrewarmCmd not found."
        }
    }
    Write-Log "OK"
    exit 0
}

# ===== COOLDOWN CHECK (shared) =====
if (Test-Path $CooldownFile) {
    try {
        $lastRestart = [DateTime]::Parse((Get-Content $CooldownFile -ErrorAction Stop))
        $elapsed = ((Get-Date) - $lastRestart).TotalMinutes
        if ($elapsed -lt $CooldownMinutes) {
            Write-Log "SKIP: cooldown active (last restart: $($lastRestart.ToString('HH:mm:ss')), ${elapsed:.0}min ago). Not restarting within ${CooldownMinutes}min."
            exit 0
        }
    } catch {
        # corrupt file, parse failure ??? proceed with restart
    }
}

# --- Record restart timestamp ---
Get-Date -Format "yyyy-MM-dd HH:mm:ss" | Set-Content -Path $CooldownFile -Encoding ASCII

if ($needTunnelRestart) {
    # ===== TUNNEL-ONLY RESTART =====
    # Start-Service is targeted ??? does not touch IBGateway or OptionsListener.
    Write-Log "TUNNEL-RESTART: starting CloudflareTunnel service ..."
    try {
        Start-Service -Name "CloudflareTunnel" -ErrorAction Stop
    } catch {
        Write-Log "TUNNEL-RESTART ERROR: $_"
    }

    Start-Sleep -Seconds 15

    $tunnelPost = (Get-Service -Name "CloudflareTunnel" -ErrorAction SilentlyContinue).Status
    if ($tunnelPost -eq "Running") {
        Write-Log "TUNNEL-RESTART OK: CloudflareTunnel is Running."
        exit 0
    } else {
        Write-Log "TUNNEL-RESTART WARN: CloudflareTunnel still not Running after restart. Manual check needed."
        exit 1
    }

} else {
    # ===== FULL RESTART (IB Gateway or Listener down) =====
    # Fix DM: flag pre-warm needed after restart so clientIds are registered when IBGateway comes back
    Get-Date -Format "yyyy-MM-dd HH:mm:ss" | Set-Content -Path $PrewarmFlag -Encoding ASCII
    Write-Log "RESTART: calling BounceServices.cmd ..."
    $bounceProc = Start-Process -FilePath cmd.exe -ArgumentList '/c', 'C:\OptionsHistory\bin\BounceServices.cmd' `
        -WorkingDirectory 'C:\OptionsHistory\bin' -Wait -PassThru -NoNewWindow
    $bounceRc = $bounceProc.ExitCode

    # --- Wait for services to stabilize ---
    Start-Sleep -Seconds 30

    # --- Post-restart health check ---
    $postOk = $false
    try {
        $postResp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($postResp.StatusCode -eq 200) { $postOk = $true }
    } catch {}

    if ($postOk) {
        Write-Log "RESTART OK: BounceServices rc=$bounceRc, /health=200 after restart."
        exit 0
    } else {
        Write-Log "RESTART WARN: BounceServices rc=$bounceRc, /health not 200 after restart. May need manual check."
        exit 1
    }
}

