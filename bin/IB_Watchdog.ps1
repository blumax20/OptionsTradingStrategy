# Fix AB8: IB Watchdog — Auto-Restart During Trading Hours
# Fix AK:  Added CloudflareTunnel service check (tunnel-only restart, no IB disruption)
# Runs every 15 min via Task Scheduler (Mon-Fri 6AM-8PM).
# Checks IB Gateway port, listener /health, and CloudflareTunnel service.
# Tunnel-only failure → restart just CloudflareTunnel (no BounceServices).
# IB Gateway or Listener failure → BounceServices (restarts all three).
# 10-minute cooldown prevents restart loops.
#
# To switch between paper and live trading, update IB_GW_PORT below
# (must match IB_PORT in InteractiveBrokersTrader\ib_config.py):
#   Paper trading : $IB_GW_PORT = 7497
#   Live trading  : $IB_GW_PORT = 7496
$IB_GW_PORT = 7497

$LogDir = "C:\OptionsHistory\logs"
$Log    = Join-Path $LogDir "watchdog.log"
$CooldownFile = Join-Path $LogDir "watchdog_last_restart.txt"
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
if (-not $gw) {
    Write-Log "FAIL: port $IB_GW_PORT not listening (IBGateway DOWN)"
    $needFullRestart = $true
}

# ===== CHECK 2: Listener /health HTTP 200 =====
if (-not $needFullRestart) {
    $httpOk = $false
    try {
        $resp = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $httpOk = $true }
    } catch {
        # connection refused, timeout, etc.
    }
    if (-not $httpOk) {
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
        # corrupt file, parse failure — proceed with restart
    }
}

# --- Record restart timestamp ---
Get-Date -Format "yyyy-MM-dd HH:mm:ss" | Set-Content -Path $CooldownFile -Encoding ASCII

if ($needTunnelRestart) {
    # ===== TUNNEL-ONLY RESTART =====
    # Start-Service is targeted — does not touch IBGateway or OptionsListener.
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
