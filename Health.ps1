<# =======================================================================
 OneShotHealth.ps1
 - Verifies IBGateway, OptionsListener, and CloudflareTunnel
 - Checks local and public listener endpoints (no quick-tunnel)
 - Confirms IB API ports, today’s CSV, and mdtest path
 - Summarizes IBKR P/L (day, YTD baseline)
 - Tails recent logs
 ======================================================================= #>

# ---------- Settings ----------
# To switch between paper and live trading, update $IB_PORT below
# (must match IB_PORT in InteractiveBrokersTrader\ib_config.py):
#   Paper trading : $IB_PORT = 7497
#   Live trading  : $IB_PORT = 7496
$IB_PORT = 7496

$PublicHost = $env:CF_PUBLIC_HOST
if (-not $PublicHost -or -not $PublicHost.Trim()) { $PublicHost = 'signals.hyperbukit.com' }

$Py     = "C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python.exe" }  # fallback

$LogDir = "C:\OptionsHistory\logs"
$Now    = Get-Date
$Stamp  = $Now.ToString('yyyyMMdd_HHmmss')
$Report = Join-Path $LogDir "health_$Stamp.txt"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

"==== IB HEALTH CHECK $($Now) ===="        | Tee-Object -FilePath $Report
"public host: $PublicHost"                 | Tee-Object -FilePath $Report -Append
" "                                        | Tee-Object -FilePath $Report -Append


function Get-TaskPrimaryFile([string]$action) {
  if (-not $action) { return $null }
  # cmd.exe /c "C:\path\file.cmd"
  if ($action -match 'cmd(\.exe)?\s+/c\s+"?([^"]+?\.(cmd|bat|ps1))"?') { return $matches[2] }
  # powershell.exe -File C:\path\file.ps1
  if ($action -match 'powershell(\.exe)?\s+[^"]*-File\s+"?([^"]+?\.ps1)"?') { return $matches[2] }
  # direct .cmd/.bat/.ps1
  if ($action -match '((?:[A-Za-z]:)?\\[^"]+\.(?:cmd|bat|ps1))') { return $matches[1] }
  return $null
}

function Read-TextSafe([string]$path, [int]$max=4000) {
  try {
    if (!(Test-Path $path)) { return $null }
    $fs = [System.IO.File]::Open($path,'Open','Read','ReadWrite')
    try {
      $sr = New-Object System.IO.StreamReader($fs)
      $txt = $sr.ReadToEnd()
      if ($txt.Length -gt $max) { return $txt.Substring(0,$max) } else { return $txt }
    } finally { $sr.Dispose(); $fs.Dispose() }
  } catch { return $null }
}
# ---------- Services (compact) ----------
"--- Services ---" | Tee-Object -FilePath $Report -Append
try {
  $list = @()
  $svc = Get-Service IBGateway -ErrorAction SilentlyContinue
  if ($svc) { $list += [pscustomobject]@{ Name='IBGateway'; Status=$svc.Status; StartType=$svc.StartType } }
  $svc = Get-Service OptionsListener -ErrorAction SilentlyContinue
  if ($svc) { $list += [pscustomobject]@{ Name='OptionsListener'; Status=$svc.Status; StartType=$svc.StartType } }
  $cf  = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '(cloudflared|Cloudflare.*Tunnel|Argo.*Tunnel)' }
  if ($cf) { $list += $cf | ForEach-Object { [pscustomobject]@{ Name=$_.Name; Status=$_.Status; StartType=$_.StartType } } }

  if ($list) {
    $list | Format-Table -AutoSize Name,Status,StartType | Out-String |
      Tee-Object -FilePath $Report -Append | Out-Null
  } else {
    "No target services found." | Tee-Object -FilePath $Report -Append
  }
} catch {
  "Services: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Scheduled Tasks (matching "IB") ----------
"--- Scheduled Tasks (matching 'IB') ---" | Tee-Object -FilePath $Report -Append
try {
  # Tasks you care about, in your preferred order
  $wanted = @(
    '\IB_AfterHours_PlaceFromWebhook_1700',
    '\IB_DailyHealth_0830',
    '\IB_ForceClose_MarketOrders_1500',
    '\IB_Health_0715',
    '\IB_Midday_Health_1200',
    '\IB_Open_PlaceMissing_0935',
    '\IB_PreClose_RestartListener_1530',
    '\IB_PreMarket_StartListener',
    '\IB_RiskExits_Retry_1030',     # Fix AC1: 10:30 AM risk-exit retry
    '\IB_Watchdog_Every15Min'        # Fix AC1: 15-min IB Gateway watchdog
  )

  # For any task that SHOULD invoke DailyCycleManagement.py, list it here:
  $expectDaily = @(
    '\IB_AfterHours_PlaceFromWebhook_1700',
    '\IB_ForceClose_MarketOrders_1500',
    '\IB_Open_PlaceMissing_0935',
    '\IB_RiskExits_Retry_1030'       # Fix AC1: also calls DCM --risk-exits-only
  )

  $all = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskName -match 'IB' -or $_.TaskPath -match 'IB'
  }

  $rows = @()
  foreach ($name in $wanted) {
    $task = $all | Where-Object { ($_.TaskPath + $_.TaskName) -eq $name } | Select-Object -First 1
    if (-not $task) { continue }
    $ti = Get-ScheduledTaskInfo -TaskPath $task.TaskPath -TaskName $task.TaskName -ErrorAction SilentlyContinue
    $lastRes = $ti.LastTaskResult
    switch ($lastRes) {
      0          { $lastNote = "Success" }
      5          { $lastNote = "AccessDenied/General" }
      9          { $lastNote = "Path/Script error" }
      267009     { $lastNote = "Missed" }
      2147942402 { $lastNote = "FileNotFound" }
      2147943645 { $lastNote = "AccessDenied" }
      default    { $lastNote = "" }
    }

    $action = ($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" } | Select-Object -First 1)
    $cwd    = ($task.Actions | Where-Object { $_.WorkingDirectory } | Select-Object -ExpandProperty WorkingDirectory -First 1)

    # Check whether Action looks like it's calling DailyCycleManagement.py
    $callsDaily = $false
    if ($action -match 'DailyCycleManagement\.py') { $callsDaily = $true }

    $rows += [pscustomobject]@{
      Task       = $name
      State      = $task.State
      LastRun    = if ($ti.LastRunTime) { $ti.LastRunTime } else { "(never)" }
      NextRun    = if ($ti.NextRunTime) { $ti.NextRunTime } else { "(not scheduled)" }
      LastResult = if ($lastNote) { "$lastRes $lastNote" } else { "$lastRes" }
      Action     = $action
      CWD        = $cwd
      NeedsDaily = ($expectDaily -contains $name)
      CallsDaily = $callsDaily
    }
  }

  if ($rows) {
    # Compact table
    $rows | Select-Object Task,State,LastRun,NextRun,LastResult | Format-Table -AutoSize | Out-String |
      Tee-Object -FilePath $Report -Append | Out-Null
    # Per-row action + verification
    # Per-row action + verification (detect indirect DCM inside scripts)
    foreach ($r in $rows) {
      if ($r.Action) { ("    Action: {0}" -f $r.Action) | Tee-Object -FilePath $Report -Append | Out-Null }
      if ($r.CWD)    { ("    CWD   : {0}" -f $r.CWD)     | Tee-Object -FilePath $Report -Append | Out-Null }

      $indirect = $false
      $target = Get-TaskPrimaryFile $r.Action
      if ($target) {
        $src = Read-TextSafe $target
        if ($src -and ($src -match 'DailyCycleManagement\.py')) {
          $indirect = $true
          ("    ✓ Indirect DCM detected via: {0}" -f $target) |
            Tee-Object -FilePath $Report -Append | Out-Null
        }
      }

      if ($r.NeedsDaily -and -not ($r.CallsDaily -or $indirect)) {
        "    ⚠ Expected: DailyCycleManagement.py (task is not calling it)" |
          Tee-Object -FilePath $Report -Append | Out-Null
      }
    }
  } else {
    "No scheduled tasks matched 'IB'." | Tee-Object -FilePath $Report -Append
  }
} catch {
  "Scheduled Tasks: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Listening Ports ----------
"--- Listening Ports (5001, 7497/paper, 7496/live) ---" | Tee-Object -FilePath $Report -Append
try {
  Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 5001,7497,7496 } |
    Sort-Object LocalPort |
    Format-Table -AutoSize LocalAddress,LocalPort,OwningProcess |
    Out-String | Tee-Object -FilePath $Report -Append | Out-Null
} catch { "ports: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
" "                                        | Tee-Object -FilePath $Report -Append

# ---------- Local listener probes ----------
"--- Local Listener Probes ---"            | Tee-Object -FilePath $Report -Append
try {
  $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:5001/health"
  $c = $h.Content; if ($c.Length -gt 240) { $c = $c.Substring(0,240) + "..." }
  "health: $($h.StatusCode) $c"            | Tee-Object -FilePath $Report -Append
} catch { "health: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
try {
  $md = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:5001/mdtest?symbol=SPY&mdtype=4&timeout_ms=1200"
  "mdtest: $($md.StatusCode) $($md.Content)" | Tee-Object -FilePath $Report -Append
} catch { "mdtest: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
" "                                        | Tee-Object -FilePath $Report -Append

# ---------- Cloudflare / public probes ----------
"--- Cloudflare Tunnel ---"                | Tee-Object -FilePath $Report -Append
try {
  $cfdProc = Get-Process cloudflared -ErrorAction SilentlyContinue
  if ($cfdProc) {
    "cloudflared: running (count=$($cfdProc.Count))" | Tee-Object -FilePath $Report -Append
    $cfdProc | Select-Object Id,StartTime,Path | Format-Table -AutoSize | Out-String |
      Tee-Object -FilePath $Report -Append | Out-Null
  } else {
    "cloudflared: NOT running (service may manage it under a different name)" | Tee-Object -FilePath $Report -Append
  }
} catch { "cloudflared: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
try {
  $dns = Resolve-DnsName -Name $PublicHost -ErrorAction Stop
  $ip  = ($dns | Where-Object { $_.QueryType -in 'A','AAAA' } | Select-Object -First 1).IPAddress
  "dns: $PublicHost -> $ip"                | Tee-Object -FilePath $Report -Append
} catch { "dns: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
try {
  $tcp = Test-NetConnection -ComputerName $PublicHost -Port 443 -InformationLevel Detailed -WarningAction SilentlyContinue -ErrorAction SilentlyContinue -InformationAction Ignore
  ("tcp443: Ping={0} Tcp={1} Remote={2}" -f $tcp.PingSucceeded, $tcp.TcpTestSucceeded, $tcp.RemoteAddress) |
    Tee-Object -FilePath $Report -Append | Out-Null
} catch { "tcp443: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
try {
  $ph = Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 -Uri ("https://{0}/health" -f $PublicHost)
  $hc = $ph.Content; if ($hc.Length -gt 240) { $hc = $hc.Substring(0,240) + "..." }
  "public health: $($ph.StatusCode) $hc"   | Tee-Object -FilePath $Report -Append
} catch { "public health: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
try {
  $pm = Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 -Uri ("https://{0}/mdtest?symbol=SPY&mdtype=4&timeout_ms=1200" -f $PublicHost)
  "public mdtest: $($pm.StatusCode) $($pm.Content)" | Tee-Object -FilePath $Report -Append
} catch { "public mdtest: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append }
" "                                        | Tee-Object -FilePath $Report -Append

# ---------- Daily CSV presence ----------
"--- Daily CSV Presence ---"               | Tee-Object -FilePath $Report -Append
$ny     = (Get-Date).ToUniversalTime().AddHours(-4)
$dated  = $ny.ToString('yy_MM_dd')
$csvPath= "C:\OptionsHistory\$dated\combined_listener_spreads.csv"
if (Test-Path $csvPath) {
  $rows = (Import-Csv $csvPath).Count
  "Found $rows rows at $csvPath"           | Tee-Object -FilePath $Report -Append
} else {
  "MISSING: $csvPath"                      | Tee-Object -FilePath $Report -Append
}
" "                                        | Tee-Object -FilePath $Report -Append
# ---------- DailyCycleManagement runtime probe ----------
"--- DailyCycleManagement runtime probe ---" | Tee-Object -FilePath $Report -Append
try {
  $p = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
       Where-Object { $_.CommandLine -match 'DailyCycleManagement\.py' -or $_.Name -match 'python(\.exe)?' -and $_.CommandLine -match 'DailyCycleManagement\.py' }
  if ($p) {
    $p | Select-Object ProcessId, Name, CreationDate, CommandLine |
      Format-Table -AutoSize | Out-String |
      Tee-Object -FilePath $Report -Append | Out-Null
  } else {
    "DailyCycleManagement.py: not detected as a running process." |
      Tee-Object -FilePath $Report -Append
  }
} catch {
  "DailyCycleManagement probe: ERROR $($_.Exception.Message)" |
    Tee-Object -FilePath $Report -Append
}
" " | Tee-Object -FilePath $Report -Append
# ---------- P/L Summary (day & YTD) ----------
"--- P/L Summary (day & YTD) ---"          | Tee-Object -FilePath $Report -Append
$tmpPy = Join-Path $env:TEMP ("pl_check_" + [guid]::NewGuid().ToString('N') + ".py")
@"
from ib_insync import IB
from datetime import datetime
from zoneinfo import ZoneInfo
import json, os, random

def f(x):
    try: return float(x)
    except: return None

def main():
    out = {"ok": False}
    ib = IB()
    cid = 830 + random.randint(0,19)
    try:
        ib.connect('127.0.0.1', $IB_PORT, clientId=cid)
    except Exception as e:
        out["error"] = f"connect: {e}"
        print(json.dumps(out)); return

    acct = ib.managedAccounts()[0] if ib.managedAccounts() else None
    vals = {}
    try:
        summ = ib.accountSummary(acct)
        vals = {s.tag: s.value for s in summ}
    except Exception: pass

    day_real = f(vals.get("RealizedPnL"))
    day_unrl = f(vals.get("UnrealizedPnL"))
    netliq   = f(vals.get("NetLiquidation"))

    out["day_realized"]   = day_real
    out["day_unrealized"] = day_unrl
    out["day_total"]      = (day_real or 0) + (day_unrl or 0)

    basef = r"C:\OptionsHistory\ytd_baseline.json"
    now   = datetime.now(ZoneInfo("America/New_York"))
    if not os.path.exists(basef):
        base = {"year": now.year, "netliq": netliq or 0.0, "ts": now.isoformat()}
        os.makedirs(os.path.dirname(basef), exist_ok=True)
        with open(basef,"w") as fh: json.dump(base, fh)
    else:
        base = json.load(open(basef))
        if int(base.get("year",0)) != now.year:
            base = {"year": now.year, "netliq": netliq or 0.0, "ts": now.isoformat()}
            json.dump(base, open(basef,"w"))

    if netliq is not None:
        out["ytd_change"] = netliq - float(base.get("netliq", 0.0))
    out["ok"] = True
    print(json.dumps(out))
if __name__ == "__main__":
    main()
"@ | Set-Content -Encoding ASCII $tmpPy

try {
  $plJson = & $Py $tmpPy
  $obj = $null
  try { $obj = $plJson | ConvertFrom-Json -ErrorAction Stop } catch {}
  # helper for PS 5.1: null/empty coalesce
  function _co([object]$v, [string]$fallback) {
    if ($null -eq $v) { return $fallback }
    if ($v -is [string] -and $v.Trim() -eq '') { return $fallback }
    return $v
  }
  if ($obj -and $obj.ok) {
    ("Day Realized   : {0}" -f (_co $obj.day_realized   '-')) | Tee-Object -FilePath $Report -Append
    ("Day Unrealized : {0}" -f (_co $obj.day_unrealized '-')) | Tee-Object -FilePath $Report -Append
    ("Day Total      : {0}" -f (_co $obj.day_total      '-')) | Tee-Object -FilePath $Report -Append
    if ($obj.PSObject.Properties.Name -contains 'ytd_change') {
      ("YTD Δ NetLiq  : {0}" -f (_co $obj.ytd_change '-'))    | Tee-Object -FilePath $Report -Append
    }
  } else {
    "P/L: $plJson" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "P/L ERROR: $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Last 20 Orders Submitted (open) ----------
"--- Last 20 Orders Submitted (open) ---" | Tee-Object -FilePath $Report -Append
$tmpPlacedPy = Join-Path $env:TEMP ("placed_" + [guid]::NewGuid().ToString('N') + ".py")
@"
from ib_insync import IB, util, Contract
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import collections

NY, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")

def parse_ib_time(s):
    try:
        dt = util.parseIBDatetime(s)
        if dt.tzinfo is None: return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except: return None

def first_submitted_time(tr):
    # Prefer first "Submitted" / "PreSubmitted" in the trade log
    for L in (tr.log or []):
        st = (L.status or "").lower()
        if "submitted" in st:
            t = parse_ib_time(L.time)
            if t: return t
    # Fallbacks on status timestamps (if any)
    s = getattr(tr.orderStatus, "lastUpdateTime", "") or getattr(tr.orderStatus, "lastFillTime", "")
    return parse_ib_time(s) if s else None

def legs_for_combo(ib: IB, combo, cache: dict):
    # Only resolve leg contracts (no market data)
    out=[]
    for leg in (getattr(combo,"comboLegs",None) or []):
        conId = getattr(leg, "conId", None)
        if not conId: 
            continue
        if conId not in cache:
            try:
                cds = ib.reqContractDetails(Contract(conId=conId)) or []
                cache[conId] = cds[0].contract if cds else None
            except Exception:
                cache[conId] = None
        c = cache.get(conId)
        if c and getattr(c, "secType", "") == "OPT":
            out.append(dict(
                action=getattr(leg,"action",""),
                exp=getattr(c,"lastTradeDateOrContractMonth",""),
                strike=float(getattr(c,"strike",0.0)),
                right=getattr(c,"right","")
            ))
    return out

def main():
    ib = IB()
    try:
        ib.connect('127.0.0.1', $IB_PORT, clientId=900, timeout=6)
    except Exception as e:
        print(f"(submitted) error: connect: {e}")
        return

    try:
        # Make sure we see current open orders; harmless if not supported
        try:
            ib.reqAllOpenOrders(); ib.reqAutoOpenOrders(True)
        except Exception:
            pass

        trades = ib.trades() or []
        rows=[]
        for tr in trades:
            t_utc = first_submitted_time(tr)
            if not t_utc: 
                continue
            if t_utc < datetime.now(UTC) - timedelta(days=14):
                continue
            rows.append((t_utc,tr))

        rows.sort(key=lambda r:r[0], reverse=True)
        rows = rows[:20]

        if not rows:
            print("(no placed orders in recent window)")
            return

        cache={}
        for t_utc,tr in rows:
            c,o = tr.contract, tr.order
            tny = t_utc.astimezone(NY)
            tag = " [~17:00]" if (tny.hour==17 and abs(tny.minute-0)<=10) else ""
            sym = getattr(c,"symbol","")
            spread = "MKT" if (getattr(o,"orderType","") or "").upper()=="MKT" else (f"LMT {o.lmtPrice:.2f}" if getattr(o,"lmtPrice",None) else "-")

            if getattr(c,"secType","")=="BAG":
                print(f"{tny:%Y-%m-%d %H:%M:%S %Z}{tag}  {sym}  Vertical  spread={spread}")
                for L in legs_for_combo(ib,c,cache):
                    print(f"  {L['action']:<4} strike={L['strike']:<7} exp={L['exp']:<8} right={L['right']}")
            elif getattr(c,"secType","")=="OPT":
                exp=getattr(c,"lastTradeDateOrContractMonth",""); st=getattr(c,"strike",""); rt=getattr(c,"right","")
                print(f"{tny:%Y-%m-%d %H:%M:%S %Z}{tag}  {sym} {exp} {rt} Vertical  spread={spread}")
                print(f"  {o.action:<4} strike={st:<7} exp={exp:<8} right={rt}")
    except Exception as e:
        print(f"(submitted) error: {e}")
    finally:
        try: ib.disconnect()
        except: pass

if __name__=='__main__':
    main()
"@ | Set-Content -Encoding ASCII $tmpPlacedPy

try {
  $placedOut = & $Py $tmpPlacedPy 2>&1
  if ($LASTEXITCODE -ne 0) { "placed-orders: ERROR (exit $LASTEXITCODE)" | Tee-Object -FilePath $Report -Append }
  if ($placedOut) {
    ($placedOut -split "`r?`n") | ForEach-Object { $_ | Tee-Object -FilePath $Report -Append | Out-Null }
  } else {
    "(no output from placed-orders probe)" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "placed-orders: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpPlacedPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Current Positions (grouped by verticals; positions data only) ----------
"--- Current Positions (verticals; positions data only) ---" | Tee-Object -FilePath $Report -Append
$tmpOpenPosPy = Join-Path $env:TEMP ("openpos_" + [guid]::NewGuid().ToString('N') + ".py")
@"
from ib_insync import IB
from collections import defaultdict
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

def main():
    ib=IB(); ib.connect('127.0.0.1',$IB_PORT,clientId=897,timeout=6)
    poss = ib.positions()

    # Build verticals: (sym, exp, right) -> {strike: (qty, avgCost)}
    groups = defaultdict(dict)
    for p in poss:
        c = p.contract
        if getattr(c,"secType","") != "OPT":
            continue
        sym   = c.symbol
        exp   = getattr(c,"lastTradeDateOrContractMonth","")
        right = getattr(c,"right","")
        k     = float(getattr(c,"strike",0.0))
        groups[(sym,exp,right)][k] = (float(p.position or 0.0), float(p.avgCost or 0.0))

    # Print last 50 verticals (by symbol name, no market data)
    items = sorted(groups.items(), key=lambda kv: kv[0][0])[:50]
    if not items:
        print("(no current option positions)"); ib.disconnect(); return

    for (sym,exp,right), legs in items:
        strikes = sorted(legs.keys())
        if len([k for k,(q,_) in legs.items() if abs(q)>1e-9]) < 2:
            # not a 2-leg vertical currently open
            continue
        print(f"{sym} {exp} {right} Vertical (positions)")
        for k in strikes:
            q, avg = legs[k]
            if abs(q) < 1e-9: 
                continue
            side = "LONG " if q>0 else "SHORT"
            print(f"  {side:<5} strike={k:<7} exp={exp:<8} right={right}  posQty={q:<6} avgCost={avg:.2f}")
    ib.disconnect()

if __name__=='__main__': main()
"@ | Set-Content -Encoding ASCII $tmpOpenPosPy

try {
  $openposOut = & $Py $tmpOpenPosPy 2>&1
  if ($LASTEXITCODE -ne 0) { "open-positions: ERROR (exit $LASTEXITCODE)" | Tee-Object -FilePath $Report -Append }
  if ($openposOut) {
    ($openposOut -split "`r?`n") | ForEach-Object { $_ | Tee-Object -FilePath $Report -Append | Out-Null }
  } else {
    "(no output from open-positions probe)" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "open-positions: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpOpenPosPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Last 20 Orders Closed (with P/L) ----------
"--- Last 20 Orders Closed (with P/L) ---" | Tee-Object -FilePath $Report -Append
$tmpClosedPy = Join-Path $env:TEMP ("closed_" + [guid]::NewGuid().ToString('N') + ".py")
@"
from ib_insync import IB, util, ExecutionFilter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import collections

NY, UTC = ZoneInfo("America/New_York"), ZoneInfo("UTC")

def parse_ib_time(s):
    try:
        dt = util.parseIBDatetime(s)
        if dt.tzinfo is None: return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except: return None

def main():
    ib=IB()
    try:
        ib.connect('127.0.0.1',$IB_PORT,clientId=901,timeout=6)
    except Exception as e:
        print(f"(closed) error: connect: {e}")
        return

    try:
        since = (datetime.now(UTC)-timedelta(days=7)).strftime("%Y%m%d-%H:%M:%S")
        fills = ib.reqExecutions(ExecutionFilter(time=since)) or []

        # Collect leg-level CLOSE fills (no market data)
        legs=[]
        for f in fills:
            c,e=f.contract,f.execution
            if getattr(c,"secType","")!="OPT": continue
            if getattr(e,"openClose","")!="C": continue
            t_utc=parse_ib_time(getattr(e,"time","")); t_ny=t_utc.astimezone(NY) if t_utc else None
            pnl=0.0
            try:
                if getattr(e,"execId",None):
                    cr=ib.reqCommissionReport(e.execId)
                    if cr and cr.realizedPNL is not None:
                        pnl=float(cr.realizedPNL)
            except Exception:
                # Commission may be unavailable — treat as 0 and continue
                pnl=0.0
            legs.append(dict(sym=c.symbol,
                             exp=getattr(c,"lastTradeDateOrContractMonth",""),
                             right=getattr(c,"right",""),
                             strike=float(getattr(c,"strike",0.0)),
                             side=e.side, fill=float(e.price or 0.0),
                             t_utc=t_utc, t_ny=t_ny, pnl=pnl))

        if not legs:
            print("(no closed executions in last 7 days)")
            return

        # Group by (sym,exp,right,time bucket ~1m)
        def bucket(t): 
            ny=t.astimezone(NY); 
            return ny.replace(second=0, microsecond=0) if ny else None

        groups=collections.defaultdict(list)
        for L in legs:
            key=(L['sym'],L['exp'],L['right'], bucket(L['t_utc']) if L['t_utc'] else None)
            groups[key].append(L)

        items=sorted(groups.items(), key=lambda kv: max([(l['t_utc'] or datetime.min.replace(tzinfo=UTC)) for l in kv[1]]), reverse=True)[:20]
        for (sym,exp,right,tb), g in items:
            tny = (tb.astimezone(NY).strftime("%Y-%m-%d %H:%M:%S %Z") if tb else "-")
            tag = " [~17:00]" if (tb and tb.astimezone(NY).hour==17 and abs(tb.astimezone(NY).minute-0)<=10) else ""
            spread_pnl=sum(l['pnl'] for l in g)
            print(f"{tny}{tag}  {sym} {exp} {right} Vertical  spreadP/L={spread_pnl:.2f}")
            for l in sorted(g, key=lambda z:z['strike']):
                print(f"  {l['side']:<4} strike={l['strike']:<7} exp={l['exp']:<8} right={l['right']}  fillPx={l['fill']:.2f}  legPnL={l['pnl']:.2f}")
    except Exception as e:
        print(f"(closed) error: {e}")
    finally:
        try: ib.disconnect()
        except: pass

if __name__=='__main__': main()
"@ | Set-Content -Encoding ASCII $tmpClosedPy

try {
  $closedOut = & $Py $tmpClosedPy 2>&1
  if ($LASTEXITCODE -ne 0) { "closed-orders: ERROR (exit $LASTEXITCODE)" | Tee-Object -FilePath $Report -Append }
  if ($closedOut) {
    ($closedOut -split "`r?`n") | ForEach-Object { $_ | Tee-Object -FilePath $Report -Append | Out-Null }
  } else {
    "(no output from closed-orders probe)" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "closed-orders: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpClosedPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Recent Logs ----------
"--- Recent Logs ---"                      | Tee-Object -FilePath $Report -Append
Get-ChildItem "$LogDir\*.log" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Desc | Select-Object -First 3 |
  ForEach-Object {
    "`n### $($_.FullName) (last 60 lines)" | Tee-Object -FilePath $Report -Append
    Get-Content $_.FullName -Tail 60       | Tee-Object -FilePath $Report -Append | Out-Null
  }

"==== END ===="                             | Tee-Object -FilePath $Report -Append
# ---------- Order Attempts Summary (from latest attempts_*.csv) ----------
"--- Order Attempts Summary (latest attempts_*.csv) ---" | Tee-Object -FilePath $Report -Append

$attemptDir = 'C:\OptionsHistory\logs'
try {
  $latestAttempts = Get-ChildItem -Path $attemptDir -Filter 'attempts_*.csv' -File -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $latestAttempts) {
    "No attempts_*.csv found in $attemptDir (run PlaceAnOrder first)." | Tee-Object -FilePath $Report -Append
    " " | Tee-Object -FilePath $Report -Append
  } else {
    ("Using attempts file: {0}" -f $latestAttempts.FullName) | Tee-Object -FilePath $Report -Append

    # Import robustly
    $rows = @()
    try { $rows = Import-Csv -Path $latestAttempts.FullName -ErrorAction Stop } catch { $rows = @() }

    if (-not $rows -or $rows.Count -eq 0) {
      "Attempts file is empty or unreadable." | Tee-Object -FilePath $Report -Append
      " " | Tee-Object -FilePath $Report -Append
    } else {
      # Successful placements
      $placed  = $rows | Where-Object { $_.status -eq 'placed' }
      $skipped = $rows | Where-Object { $_.status -ne 'placed' }  # includes 'skipped' and 'error'

      ("Orders placed (count): {0}" -f ($placed.Count)) | Tee-Object -FilePath $Report -Append

      # Group non-placed by reason
      if ($skipped.Count -gt 0) {
        "`nNot placed (grouped by reason):" | Tee-Object -FilePath $Report -Append
        $skipped | Group-Object reason | Sort-Object Count -Descending |
          ForEach-Object {
            $rn = if ($_.Name) { $_.Name } else { '(unknown)' }
            ("  {0,-28} : {1}" -f $rn, $_.Count) |
              Tee-Object -FilePath $Report -Append | Out-Null
          }

        # Show a concise table of recent non-placed with key context
        "`nRecent not-placed details (last 40):" | Tee-Object -FilePath $Report -Append
        $skipped |
          Sort-Object { $_.ts } |
          Select-Object -Last 40 ts, symbol, action, status, reason, exp, right, atm, oth, limit |
          Format-Table -AutoSize |
          Out-String |
          Tee-Object -FilePath $Report -Append | Out-Null
      } else {
        "No not-placed entries found in latest attempts file." | Tee-Object -FilePath $Report -Append
      }
      " " | Tee-Object -FilePath $Report -Append
    }
  }
} catch {
  ("Attempts summary error: {0}" -f $_.Exception.Message) | Tee-Object -FilePath $Report -Append
  " " | Tee-Object -FilePath $Report -Append
}
# ---------- Recent PlaceAnOrder activity (from ib_cycle.log) ----------
"--- Recent PlaceAnOrder activity (ib_cycle.log) ---" | Tee-Object -FilePath $Report -Append

$logPath = 'C:\OptionsHistory\logs\ib_cycle.log'
function Read-SharedFile([string]$path, [int]$tail = 2000) {
  if (!(Test-Path $path)) { return @() }
  $fs = [System.IO.File]::Open($path,'Open','Read','ReadWrite')
  try {
    $sr = New-Object System.IO.StreamReader($fs)
    $lines = @()
    while(-not $sr.EndOfStream){ $lines += $sr.ReadLine() }
    if ($lines.Count -gt $tail) { $lines = $lines[-$tail..-1] }
    return $lines
  } finally { $sr.Dispose(); $fs.Dispose() }
}

$lines = Read-SharedFile $logPath 2500

# Buckets
$placed  = $lines | Select-String -SimpleMatch ' Placed ' | Select-Object -ExpandProperty Line
$closed  = $lines | Select-String -SimpleMatch ' Submitted CLOSE ' | Select-Object -ExpandProperty Line
$weekly  = $lines | Select-String -Pattern 'Weekly-enforce|FORCE-CLOSE' | Select-Object -ExpandProperty Line
$failed  = $lines | Select-String -Pattern 'Failed to place|Failed to qualify|ERROR:' | Select-Object -ExpandProperty Line
$skipped = $lines | Select-String -Pattern 'Skipping;|No matching spread quantity|limit below min|OI ' | Select-Object -ExpandProperty Line

# Compact summary counts
("Placed (open/close) : {0}" -f ($placed.Count + $closed.Count))        | Tee-Object -FilePath $Report -Append
("Weekly/Force close  : {0}" -f $weekly.Count)                           | Tee-Object -FilePath $Report -Append
("Failures            : {0}" -f $failed.Count)                           | Tee-Object -FilePath $Report -Append
("Skips               : {0}" -f $skipped.Count)                          | Tee-Object -FilePath $Report -Append

# Top recent examples
"`nLast 10 placed/submitted:" | Tee-Object -FilePath $Report -Append
($placed + $closed | Select-Object -Last 10) |
  Tee-Object -FilePath $Report -Append | Out-Null

"`nLast 10 failures:" | Tee-Object -FilePath $Report -Append
$failed | Select-Object -Last 10 |
  Tee-Object -FilePath $Report -Append | Out-Null

"`nLast 10 skipped/why:" | Tee-Object -FilePath $Report -Append
$skipped | Select-Object -Last 10 |
  Tee-Object -FilePath $Report -Append | Out-Null

# Per-symbol quick stats (placed vs failed vs skipped)
"`nPer-symbol activity (last window):" | Tee-Object -FilePath $Report -Append
$extractSym = {
  param($line)
  if ($line -match '\[(?<sym>[A-Z\.]+)\]') { $matches.sym } else { $null }
}
$allTagged = @()
foreach ($l in ($placed + $closed)) { $s = & $extractSym $l; if ($s){ $allTagged += [pscustomobject]@{Sym=$s; Kind='placed'} } }
foreach ($l in $failed)            { $s = & $extractSym $l; if ($s){ $allTagged += [pscustomobject]@{Sym=$s; Kind='failed'} } }
foreach ($l in $skipped)           { $s = & $extractSym $l; if ($s){ $allTagged += [pscustomobject]@{Sym=$s; Kind='skipped'} } }

$allTagged |
  Group-Object Sym |
  ForEach-Object {
    $p = ($_.Group | Where-Object Kind -eq 'placed').Count
    $f = ($_.Group | Where-Object Kind -eq 'failed').Count
    $k = ($_.Group | Where-Object Kind -eq 'skipped').Count
    "{0,-6}  placed={1,-3} failed={2,-3} skipped={3,-3}" -f $_.Name,$p,$f,$k
  } |
  Sort-Object -Descending |
  Tee-Object -FilePath $Report -Append | Out-Null

" " | Tee-Object -FilePath $Report -Append
# ---- Minimal terminal summary only ----
Write-Host ""
Write-Host "--- SUMMARY ---"
try {
  $svc = Get-Service IBGateway, OptionsListener -ErrorAction SilentlyContinue
  foreach ($s in $svc) { Write-Host ("{0,-16} : {1}" -f $s.Name,$s.Status) }
} catch {}

try {
  $lastPL = Select-String -Path $Report -Pattern '^Day Realized|^Day Unrealized|^Day Total|^YTD' -SimpleMatch
  if ($lastPL) { $lastPL | ForEach-Object { $_.Line | Write-Host } }
} catch {}

Write-Host ("Report   : {0}" -f $Report)
# ---------- Email Report (optional) ----------
try {
  $smtpServer = "smtp.office365.com"; $smtpPort = 587
  $from       = "noreply@hyperbukit.com"
  $to         = "max@hyperbukit.com"
  $subject    = "IB Health Report $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
  $body       = Get-Content -Raw $Report
  # Send-MailMessage is deprecated in newer PS; if you have an SMTP module use that.
  # Send-MailMessage -SmtpServer $smtpServer -Port $smtpPort -UseSsl -From $from -To $to -Subject $subject -Body $body
} catch {
  Write-Warning "Email send failed: $($_.Exception.Message)"
}

Write-Host "Wrote OneShotHealth report: $Report"