# -------------------------------------------
# PushButtonStart.ps1 — start full trading system (no restarts)
# Starts services if stopped: Cloudflare Tunnel, IB Gateway, OptionsListener (if present)
# Starts processes if not running: listener.py (only if NO service), DailyCycleManagement.py, PlaceAnOrder watcher
# Creates: runtime\*.pid and logs\*.log
# Idempotent: skips a component if already running
# -------------------------------------------

param(
  [int]$IBListenPort = 0,        # 0 = auto-detect from ib_config.py (Fix DK)
  [int]$IBWarmupSec  = 35,       # wait window for LISTEN socket to appear
  [switch]$Summary = $false
)


$ErrorActionPreference = "Stop"
$script:ExitCode = 0
try {
    # (everything that already exists in your script goes here)

# --- Fix EI: Clear system-stopped sentinel flag FIRST ---
# PushButtonStop creates C:\OptionsHistory\logs\system_stopped.txt; the watchdog and
# all scheduled-task .cmd launchers skip their work while it exists. Delete it before
# any service start so they resume normally on the next cycle.
$StoppedFlag = "C:\OptionsHistory\logs\system_stopped.txt"
if (Test-Path $StoppedFlag) {
    try {
        Remove-Item $StoppedFlag -Force -ErrorAction Stop
        Write-Host "[START] Cleared sentinel flag $StoppedFlag -- watchdog and scheduled tasks will resume."
    } catch {
        Write-Warning "Failed to clear sentinel flag $StoppedFlag : $($_.Exception.Message)"
    }
}

# --- Paths ---
$Root = "C:\Users\Administrator\code\OptionsTradingStrategy"
$Py   = Join-Path $Root ".venv\Scripts\python.exe"

# Fix DK: read IB_PORT from ib_config.py (single source of truth, updated by switch_trading_mode.py)
if ($IBListenPort -eq 0) {
    $ibConfigPath = Join-Path $Root "InteractiveBrokersTrader\ib_config.py"
    $ibPortLine = Get-Content $ibConfigPath -ErrorAction SilentlyContinue |
                  Where-Object { $_ -match "^IB_PORT\s*[:=]" } | Select-Object -First 1
    $IBListenPort = if ($ibPortLine -match "(\d+)") { [int]$Matches[1] } else { 7496 }
    Write-Host "Fix DK: IB_PORT=$IBListenPort (from ib_config.py)"
}

$Listener = Join-Path $Root "InteractiveBrokersTrader\listener.py"
$Daily    = Join-Path $Root "InteractiveBrokersTrader\DailyCycleManagement.py"
$Order    = Join-Path $Root "InteractiveBrokersTrader\PlaceAnOrder.py"   # optional resident watcher

$Runtime = Join-Path $Root "runtime"
$Logs    = Join-Path $Root "logs"

New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
New-Item -ItemType Directory -Force -Path $Logs    | Out-Null

# --- Known listener service names (prefer service if present) ---
$ListenerServiceNames = @('OptionsListener')  # add more aliases here if you have them

# --- Helpers ---
function Test-IBGListening {
    $p = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
         Where-Object { $_.LocalPort -eq $IBListenPort }
    return $null -ne $p
}

function Test-AlreadyRunning($name) {
    $pidFile = Join-Path $Runtime "$name.pid"
    if (Test-Path $pidFile) {
        $savedPid = Get-Content $pidFile | ForEach-Object { $_.Trim() } | Select-Object -First 1
        if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) { return $true }
        Remove-Item $pidFile -ErrorAction SilentlyContinue
    }
    return $false
}

function Start-PyProc($displayName, $scriptPath, $args = "") {
    if (-not (Test-Path $scriptPath)) { Write-Host "X $displayName not found: $scriptPath"; return }
    if (Test-AlreadyRunning $displayName) { Write-Host "$displayName already running; skipping."; return }

    $pidFile = Join-Path $Runtime "$displayName.pid"

    # Fix DX: Use Start-Process (fully detached) instead of .NET BeginOutputReadLine().
    # BeginOutputReadLine() creates async IO threads tied to the parent PowerShell host.
    # Since DCM.py never exits, threads never complete, parent PS cannot exit -- causing
    # Run-And-Log in PushButtonMenu.ps1 to hang indefinitely (menu 2->2 blocked).
    # Start-Process -WindowStyle Hidden creates a truly detached process; parent exits immediately.
    # DCM.py writes output to ib_cycle.log via Python's logging file handler -- no stdout capture needed.
    $p = Start-Process -FilePath $Py `
        -ArgumentList ("`"$scriptPath`"" + $(if ($args) { " $args" } else { "" })) `
        -WorkingDirectory (Split-Path $scriptPath) `
        -WindowStyle Hidden `
        -PassThru
    $p.Id | Out-File -Encoding ascii -FilePath $pidFile -Force
    Write-Host "Started $displayName (PID $($p.Id))."
}

function Start-ServiceSafe {
    param([string]$Name,[string]$Display,[int]$WaitSec=10)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $svc) { Write-Host "$Display service not found ($Name)."; return $false }
    if ($svc.Status -ne 'Running') {
        Write-Host "Starting $Display service ($Name)..."
        Start-Service -Name $Name -ErrorAction Stop
        $svc.WaitForStatus('Running', ("00:00:{0}" -f $WaitSec)) | Out-Null
        Write-Host "✅ $Display is Running."
    } else {
        Write-Host "$Display already Running — ok."
    }
    return $true
}

# --- Ensure venv python exists ---
if (-not (Test-Path $Py)) { throw "Python venv not found at $Py. Activate or (re)create the .venv first." }

# --- Detect Services ---
$CloudflareSvc = Get-Service -Name CloudflareTunnel -ErrorAction SilentlyContinue
$IBSvc         = Get-Service -Name IBGateway       -ErrorAction SilentlyContinue

# Try to find a listener service by known names
$ListenerSvc = $null
foreach ($n in $ListenerServiceNames) {
    $s = Get-Service -Name $n -ErrorAction SilentlyContinue
    if ($s) { $ListenerSvc = $s; break }
}

# --- Start Cloudflare Tunnel if stopped ---
if ($CloudflareSvc) {
    [void](Start-ServiceSafe -Name $CloudflareSvc.Name -Display "Cloudflare Tunnel" -WaitSec 10)
} else {
    Write-Warning "Cloudflare Tunnel service not found (CloudflareTunnel)."
}

# --- Start IB Gateway if stopped; wait for $IBListenPort LISTENING ---
if ($IBSvc) {
    if ($IBSvc.Status -ne 'Running') {
        Write-Host "Starting IB Gateway service ($($IBSvc.Name))..."
        Start-Service -Name $IBSvc.Name -ErrorAction Stop
        $IBSvc.WaitForStatus('Running','00:00:12') | Out-Null
    } else {
        Write-Host "IB Gateway service already Running — ok."
    }

    # Wait up to $IBWarmupSec for socket
    Write-Host "Waiting for IBGateway port $IBListenPort (up to ${IBWarmupSec}s)..." -NoNewline
    for ($i=0; $i -lt $IBWarmupSec; $i++) {
        if (Test-IBGListening) { break }
        Start-Sleep 1
        Write-Host "." -NoNewline
    }
    Write-Host ""
    if (Test-IBGListening) {
        Write-Host "✅ IB Gateway listening on port $IBListenPort."
    } else {
        Write-Warning "IB Gateway not LISTENING on port $IBListenPort after ${IBWarmupSec}s (orders may fail until it finishes booting)."
    }
} else {
    Write-Warning "IB Gateway service not found (IBGateway)."
}

# --- Start Listener (prefer service if present; else launch python) ---
if ($ListenerSvc) {
    [void](Start-ServiceSafe -Name $ListenerSvc.Name -Display $ListenerSvc.DisplayName -WaitSec 10)
} else {
    Start-PyProc "listener" $Listener
}

# --- Start Python components (idempotent) ---
Start-PyProc "DailyCycleManagement" $Daily
Start-PyProc "PlaceAnOrder"         $Order "--watch"   # optional resident watcher

Write-Host "🎬 All requested components started."

if ($Summary) {
    Write-Host ""
    Write-Host "—— Account/Positions Snapshot ——" -ForegroundColor Cyan
    $pyCode = @"
import os, sys, glob, csv, datetime as dt
from ib_insync import IB, util

host = '127.0.0.1'
port = int(os.environ.get('IB_PORT', '7497'))
clientId = 9876

ib = IB()
try:
    ib.connect(host, port, clientId=clientId, timeout=6)
except Exception as e:
    print(f"❌ Could not connect to IB Gateway on {host}:{port}: {e}")
    sys.exit(0)

# Account summary (fast, no subscriptions)
acct = None
try:
    # pick the first account if multiple returned
    accts = ib.managedAccounts() or []
    acct = accts[0] if accts else None
except Exception:
    acct = None

def acct_value(tag, default='—'):
    try:
        v = ib.accountSummary()  # pulls for all accounts
        for a in v:
            if a.tag == tag and (acct is None or a.account == acct):
                return a.value
    except Exception:
        pass
    return default

netliq = acct_value('NetLiquidation')
unrlzd = acct_value('UnrealizedPnL')  # may be '—' on some accounts; ok

print(f"NetLiquidation : {netliq}")
print(f"UnrealizedPnL  : {unrlzd}")

# Open positions
try:
    poss = ib.positions()
    if not poss:
        print("Positions      : (none)")
    else:
        print("Positions      :")
        for p in poss:
            sym = f"{p.contract.symbol}"
            if getattr(p.contract, 'localSymbol', None):
                sym = p.contract.localSymbol
            print(f"  - {sym:<12} qty={p.position:>8}  avgCost={p.avgCost:.2f}")
except Exception as e:
    print(f"Positions      : (error: {e})")

# Optional YTD realized P/L from local CSV logs (best-effort)
root = os.environ.get('OTS_ROOT')
y = dt.date.today().year
candidates = []
for pattern in [
    os.path.join(root or '.', 'logs', 'trades', f'*{y}*.csv'),
    os.path.join(root or '.', 'logs', f'*{y}*.csv'),
    os.path.join(root or '.', 'runtime', f'*{y}*.csv'),
]:
    candidates.extend(glob.glob(pattern))

ytd = 0.0
found = False
cols_guess = {'realized','realized_pnl','realizedPnl','RealizedPnL','pnl','PnL'}

def to_float(s):
    try:
        # strip commas/dollar signs
        return float(str(s).replace(',','').replace('$','').strip())
    except Exception:
        return None

for fp in candidates:
    try:
        with open(fp, newline='') as f:
            r = csv.DictReader(f)
            # pick the first column name that looks like realized pnl
            pnl_col = None
            for c in r.fieldnames or []:
                if c in cols_guess:
                    pnl_col = c; break
            if not pnl_col:
                continue
            found = True
            for row in r:
                val = to_float(row.get(pnl_col))
                if val is not None:
                    ytd += val
    except Exception:
        pass

if found:
    print(f"YTD RealizedPnL: {ytd:,.2f}")
else:
    print("YTD RealizedPnL: (no trade logs found — IBKR FLEX or local fills CSV needed)")

ib.disconnect()
"@

    $tf = Join-Path $env:TEMP 'ib_snapshot.py'
    Set-Content -Path $tf -Value $pyCode -Encoding ASCII
    $env:OTS_ROOT = $Root
    $env:IB_PORT  = "$IBListenPort"

    & $Py $tf
    Write-Host "—— End Snapshot ——" -ForegroundColor Cyan
}

} catch {
    Write-Error $_
    $script:ExitCode = 1
} finally {
    # VS Code gets grumpy if you don’t be explicit
    exit $script:ExitCode
}