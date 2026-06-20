param(
  [switch]$KillIfStuck = $false,
  [int]$PortWaitSec = 90
)

# -------------------------------------------
# PushButtonStop.ps1 — stop full trading system (no restarts)
# Stops processes: listener, DailyCycleManagement, PlaceAnOrder (if running)
# Stops services if running: OptionsListener (by name), IB Gateway, Cloudflare Tunnel
# -------------------------------------------

$ErrorActionPreference = "SilentlyContinue"

# --- Fix EI: Write the sentinel FIRST, before stopping anything ---
# If we wait until after services are stopped, there is a race window where the
# watchdog (15-min cycle) or a scheduled .cmd task can see services down and call
# BounceServices.cmd, restoring everything before the flag is written. By writing
# the flag first, those guards skip their work for the entire duration of the stop.
$StoppedFlag = "C:\OptionsHistory\logs\system_stopped.txt"
try {
  if (-not (Test-Path "C:\OptionsHistory\logs")) {
    New-Item -ItemType Directory -Force -Path "C:\OptionsHistory\logs" | Out-Null
  }
  $stopTs = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  "stopped_at=$stopTs" | Set-Content -Path $StoppedFlag -Encoding ASCII -Force
  Write-Host "[STOP] Sentinel flag written FIRST: $StoppedFlag (watchdog and scheduled tasks will skip during shutdown)"
} catch {
  Write-Warning "Failed to write sentinel flag $StoppedFlag : $($_.Exception.Message)"
  Write-Warning "Watchdog may attempt to restart services during this stop!"
}

$Root    = "C:\Users\Administrator\code\OptionsTradingStrategy"
$Runtime = Join-Path $Root "runtime"
$Names   = @("listener","DailyCycleManagement","PlaceAnOrder")

# Add your listener/agent service names here (no disabling; just Stop-Service)
$ListenerServiceNames = @('OptionsListener')  # add more if needed

function Get-PortOwners {
  param([int[]]$Ports = @(7497,7496,4002,4001))
  Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $Ports -contains $_.LocalPort } |
    ForEach-Object {
      $procId = $_.OwningProcess
      $p      = Get-Process -Id $procId -ErrorAction SilentlyContinue
      $cmd    = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
      [PSCustomObject]@{
        Port    = $_.LocalPort
        PID     = $procId
        Process = $p.ProcessName
        Path    = $p.Path
        CmdLine = $cmd
      }
    } | Sort-Object Port
}

function Wait-Ports-Down {
  param([int]$TimeoutSec = 90, [int[]]$Ports = @(7497,7496,4002,4001))
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  do {
    $owners = Get-PortOwners -Ports $Ports
    if (-not $owners) { return $true }
    Start-Sleep -Milliseconds 500
  } while ((Get-Date) -lt $deadline)
  return $false
}

function Stop-ServiceSafe {
  param([string]$Name,[string]$Display)
  $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
  if ($svc -and $svc.Status -eq 'Running') {
    Write-Host "Stopping $Display service ($Name)..."
    try {
      Stop-Service -Name $svc.Name -ErrorAction Stop
      $svc.WaitForStatus('Stopped','00:00:15') | Out-Null
      Write-Host "✅ $Display stopped."
    } catch {
      Write-Warning ("Failed to stop {0}: {1}" -f $Display, $_.Exception.Message)
    }
  } else {
    Write-Host "$Display service already Stopped or not found — ok."
  }
}

# --- Stop Python processes tracked by pid files ---
foreach ($name in $Names) {
  $pidFile = Join-Path $Runtime "$name.pid"
  if (Test-Path $pidFile) {
    $savedPid = Get-Content $pidFile | ForEach-Object { $_.Trim() } | Select-Object -First 1
    if ($savedPid) {
      $p = Get-Process -Id $savedPid -ErrorAction SilentlyContinue
      if ($p) {
        Write-Host "🛑 Stopping $name (PID $savedPid)..."
        try { $p.CloseMainWindow() | Out-Null } catch {}
        Start-Sleep -Seconds 2
        if (Get-Process -Id $savedPid -ErrorAction SilentlyContinue) {
          try { Stop-Process -Id $savedPid -Force -ErrorAction SilentlyContinue } catch {}
          Start-Sleep -Seconds 1
        }
      }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
  }
}

# --- Stop your listener services first (no disabling; just stop now) ---
foreach ($svcName in $ListenerServiceNames) {
  $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
  if ($svc) { Stop-ServiceSafe -Name $svc.Name -Display $svc.DisplayName }
}

# --- Detect Services ---
$IBSvc         = Get-Service -Name IBGateway        -ErrorAction SilentlyContinue
$CloudflareSvc = Get-Service -Name CloudflareTunnel -ErrorAction SilentlyContinue

# --- Stop IB Gateway (graceful) ---
if ($IBSvc -and $IBSvc.Status -eq 'Running') {
  Write-Host "Stopping IB Gateway service ($($IBSvc.Name))..."

  try {
    # 1) Ask IBC to STOP
    $stopBat = 'C:\IBC\Stop.bat'
    if (Test-Path $stopBat) {
      Write-Host "→ Sending STOP to IBC via Stop.bat..."
      & $stopBat | Out-Null
    } elseif (Test-Path 'C:\IBC\SendCommand.bat') {
      Write-Host "→ Sending STOP to IBC via SendCommand.bat..."
      & 'C:\IBC\SendCommand.bat' STOP | Out-Null
    } else {
      Write-Warning "No Stop.bat or SendCommand.bat found in C:\IBC; skipping graceful stop."
    }

    # 2) Wait up to half the timeout; retry STOP once; then wait remainder
    $half = [Math]::Max([int]([double]$PortWaitSec/2), 10)
    $ok = Wait-Ports-Down -TimeoutSec $half
    if (-not $ok) {
      Write-Host "→ Ports still listening; re-sending STOP and waiting remaining $($PortWaitSec - $half)s..."
      if (Test-Path $stopBat) {
        & $stopBat | Out-Null
      } elseif (Test-Path 'C:\IBC\SendCommand.bat') {
        & 'C:\IBC\SendCommand.bat' STOP | Out-Null
      }
      $ok = Wait-Ports-Down -TimeoutSec ($PortWaitSec - $half)
    }

    # 3) Final check + diagnostics
    if (-not $ok) {
      Write-Warning "Ports still listening after $PortWaitSec s:"
      $owners = Get-PortOwners
      if ($owners) {
        Write-Host "`nActive port owners (still bound):"
        $owners | Format-Table -AutoSize | Out-String | Write-Host
      } else {
        Write-Warning "No specific processes detected on IB ports — may be a transient socket hang."
      }

      if ($KillIfStuck) {
        Write-Warning "KillIfStuck is set — terminating only safe processes holding API ports..."
        foreach ($o in $owners) {
          $safe =
            ($o.Path -match '\\Jts\\ibgateway') -or
            ($o.CmdLine -match '\\Jts\\ibgateway') -or
            ($o.Process -match 'javaw?$') -or
            ($o.Process -match 'python') -or
            ($o.Process -match 'listener')
          if ($safe) {
            try {
              Write-Warning "Killing PID $($o.PID) ($($o.Process)) holding port $($o.Port)"
              Stop-Process -Id $o.PID -Force -ErrorAction SilentlyContinue
            } catch {}
          }
        }
        Start-Sleep -Seconds 2
      } else {
        Write-Warning "Skipping kill (use -KillIfStuck to force-close port owners)."
      }
    } else {
      Write-Host "✅ IB Gateway API sockets closed cleanly."
    }

    # 4) Stop the service wrapper
    Stop-Service -Name $IBSvc.Name -ErrorAction Stop
    $IBSvc.WaitForStatus('Stopped','00:00:15') | Out-Null
    Write-Host "✅ IB Gateway service stopped."

    if (-not $ok) {
      Write-Warning "Ports still listening after $PortWaitSec s:"
      $owners = Get-PortOwners
      if ($owners) { $owners | Format-Table -AutoSize | Out-String | Write-Host }

      if ($KillIfStuck) {
        Write-Warning "KillIfStuck is set — terminating only the processes actually holding API ports..."
        foreach ($o in $owners) {
          # be strict about what we kill
          $safe =
            ($o.Path -match '\\Jts\\ibgateway') -or
            ($o.CmdLine -match '\\Jts\\ibgateway') -or
            ($o.Process -match 'javaw?$') -or
            ($o.Process -match 'python') -or
            ($o.Process -match 'listener')
          if ($safe) {
            try {
              Write-Warning "Killing PID $($o.PID) ($($o.Process)) holding port $($o.Port)"
              Stop-Process -Id $o.PID -Force -ErrorAction SilentlyContinue
            } catch {}
          }
        }
        Start-Sleep -Seconds 2
      } else {
        Write-Warning "Skipping kill (use -KillIfStuck to force-close port owners)."
      }
    } else {
      Write-Host "✅ IB Gateway API sockets closed."
    }

    # 3) Stop the service (wrapper should exit now)
    Stop-Service -Name $IBSvc.Name -ErrorAction Stop
    $IBSvc.WaitForStatus('Stopped','00:00:15') | Out-Null
    Write-Host "✅ IB Gateway service stopped."
  } catch {
    Write-Warning "Failed to stop IB Gateway: $($_.Exception.Message)"
  }
} else {
  Write-Host "IB Gateway service already Stopped or not found — ok."
}

# --- Stop Cloudflare Tunnel last ---
if ($CloudflareSvc -and $CloudflareSvc.Status -eq 'Running') {
  Stop-ServiceSafe -Name $CloudflareSvc.Name -Display "Cloudflare Tunnel"
} else {
  Write-Host "Cloudflare Tunnel service already Stopped or not found — ok."
}

# Fix EI: Sentinel flag was written at the TOP of this script (before stopping anything)
# to prevent a race with the watchdog. If you re-test, the flag is already in place
# and watchdog/scheduled-task guards have already been active for the full stop duration.
if (Test-Path $StoppedFlag) {
  Write-Host "[OK] Sentinel flag confirmed in place: $StoppedFlag -- system will stay stopped until PushButtonStart runs."
} else {
  Write-Warning "[WARN] Sentinel flag NOT in place at end of stop -- watchdog may restart things."
}

Write-Host "🧹 All requested components stopped (if they were running)."