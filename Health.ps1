<# =======================================================================
 OneShotHealth.ps1
 - Verifies IBGateway, OptionsListener, and CloudflareTunnel
 - Checks local and public listener endpoints (no quick-tunnel)
 - Confirms IB API ports, today’s CSV, and mdtest path
 - Summarizes IBKR P/L (day, YTD baseline)
 - Tails recent logs
 ======================================================================= #>

# ---------- Settings ----------
# IB API port. The single source of truth is IB_PORT in
# InteractiveBrokersTrader\ib_config.py, which this script reads below, so a
# paper/live switch cannot leave the health report probing a dead port.
#   Paper trading : 7497        Live trading : 7496
# The literal is only the fallback for when that file cannot be read. It is
# also what switch_trading_mode.py rewrites, so that tool still works as-is.
$IB_PORT = 7497
try {
  $_ibCfg = "C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\ib_config.py"
  $_ibM = Select-String -Path $_ibCfg -Pattern '^\s*IB_PORT\s*:\s*int\s*=\s*(\d{4,5})' -ErrorAction Stop |
          Select-Object -First 1
  if ($_ibM) { $IB_PORT = [int]$_ibM.Matches[0].Groups[1].Value }
} catch { }

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

# --- Fix EI: SYSTEM STATE banner ---
# Surfaces the system_stopped.txt sentinel flag at the top of every health report
# so it's obvious when reviewing the daily output that the system is intentionally
# stopped (watchdog and scheduled tasks honor the flag and skip their work).
$StoppedFlag = "C:\OptionsHistory\logs\system_stopped.txt"
if (Test-Path $StoppedFlag) {
  $stoppedItem = Get-Item $StoppedFlag -ErrorAction SilentlyContinue
  $stoppedSince = if ($stoppedItem) { $stoppedItem.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss") } else { "unknown" }
  $stoppedAge   = if ($stoppedItem) { [int]((Get-Date) - $stoppedItem.LastWriteTime).TotalMinutes } else { 0 }
  "**********************************************************************" | Tee-Object -FilePath $Report -Append
  "*  SYSTEM STOPPED via PushButton (since $stoppedSince, $stoppedAge min ago)  *" | Tee-Object -FilePath $Report -Append
  "*  Watchdog and scheduled tasks are SKIPPING -- run PushButton menu 2 to resume.  *" | Tee-Object -FilePath $Report -Append
  "**********************************************************************" | Tee-Object -FilePath $Report -Append
  " " | Tee-Object -FilePath $Report -Append
}


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
    '\IB_PlaceSkippedOpens_1000',    # Fix CP: 10:00 AM retry of skipped OPEN orders
    '\IB_RiskExits_Retry_1030',     # Fix AC1: 10:30 AM risk-exit retry
    '\IB_Watchdog_Every15Min'        # Fix AC1: 15-min IB Gateway watchdog
  )

  # For any task that SHOULD invoke DailyCycleManagement.py, list it here:
  $expectDaily = @(
    '\IB_AfterHours_PlaceFromWebhook_1700',
    '\IB_ForceClose_MarketOrders_1500',
    '\IB_Open_PlaceMissing_0935',
    '\IB_PlaceSkippedOpens_1000',    # Fix CP: 10:00 AM retry of skipped OPEN orders
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

$WebhookSecret = ""
try { $WebhookSecret = (Get-Content "C:\Users\Administrator\.secrets\webhook.key" -Raw -ErrorAction Stop).Trim() } catch {}

function Hide-Secret([string]$t) {
  # Exception messages echo the request URI; keep the secret out of the report
  # file and the menu's Start-Transcript log.
  if ($null -eq $t) { return "" }
  return ($t -replace '/s/[^/\s"]+', '/s/***')
}

function Get-HttpStatus {
  # Windows PowerShell 5.1's Invoke-WebRequest -MaximumRedirection 0 throws a
  # bare InvalidOperationException on a redirect and exposes no Response object,
  # so the status code is unrecoverable -- it surfaces only as "Operation is not
  # valid due to the current state of the object". Use HttpWebRequest directly.
  param([string]$Uri, [int]$TimeoutSec = 8)
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
  $req = [Net.HttpWebRequest]::Create($Uri)
  $req.AllowAutoRedirect = $false
  $req.Timeout           = $TimeoutSec * 1000
  $req.UserAgent         = "HyperOS-Health"
  $resp = $null
  try { $resp = $req.GetResponse() }
  catch [Net.WebException] { $resp = $_.Exception.Response; if (-not $resp) { throw } }
  $code = 0; $loc = ""; $body = ""
  try { $code = [int]$resp.StatusCode } catch {}
  try { $loc  = "$($resp.Headers['Location'])" } catch {}
  try {
    $sr = New-Object IO.StreamReader($resp.GetResponseStream())
    $body = $sr.ReadToEnd(); $sr.Close()
  } catch {}
  try { $resp.Close() } catch {}
  return [pscustomobject]@{ Code = $code; Location = $loc; Body = $body }
}

function Test-PublicEndpoint {
  # Two probes, because they prove different things:
  #   authed   /s/<secret>/... exercises the whole machine plane end to end:
  #            DNS -> Cloudflare -> Access bypass -> tunnel -> origin -> app.
  #            This is the probe that says the tunnel is actually carrying
  #            traffic; a redirect off the bare path says only that Cloudflare
  #            answered, which stays true even when the origin is down.
  #   unauth   the bare path must NOT return 200. A 302 to cloudflareaccess.com
  #            confirms SSO is still enforcing on the human plane.
  param([string]$Route, [string]$Label)
  if ($WebhookSecret) {
    try {
      $a = Get-HttpStatus -Uri ("https://{0}/s/{1}{2}" -f $PublicHost, $WebhookSecret, $Route)
      if ($a.Code -eq 200) {
        $c = "$($a.Body)"
        if ($c.Length -gt 200) { $c = $c.Substring(0,200) + "..." }
        ("{0}: 200 {1}" -f $Label, (Hide-Secret $c))
      } else {
        ("{0}: UNHEALTHY HTTP {1} on authenticated path {2}" -f $Label, $a.Code, $a.Location)
      }
    } catch { ("{0}: ERROR {1}" -f $Label, (Hide-Secret $_.Exception.Message)) }
  } else {
    ("{0}: SKIPPED (webhook.key unreadable)" -f $Label)
  }
  try {
    $b = Get-HttpStatus -Uri ("https://{0}{1}" -f $PublicHost, $Route)
    if ((@(301,302,303,307,308) -contains $b.Code) -and ($b.Location -match 'cloudflareaccess\.com')) {
      ("{0} [unauth]: OK {1} -> Cloudflare Access login (SSO enforcing)" -f $Label, $b.Code)
    } elseif ($b.Code -eq 200) {
      ("{0} [unauth]: WARNING 200 without SSO - Access is NOT enforcing" -f $Label)
    } else {
      ("{0} [unauth]: HTTP {1} {2}" -f $Label, $b.Code, $b.Location)
    }
  } catch { ("{0} [unauth]: ERROR {1}" -f $Label, (Hide-Secret $_.Exception.Message)) }
}

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
Test-PublicEndpoint -Route "/health" -Label "public health" |
  Tee-Object -FilePath $Report -Append
Test-PublicEndpoint -Route "/mdtest?symbol=SPY&mdtype=4&timeout_ms=1200" -Label "public mdtest" |
  Tee-Object -FilePath $Report -Append
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

def _deposits_since(base_ts):
    """Sum external cash flows dated on/after the baseline date.

    A deposit raises NetLiq without being performance, so the raw
    "NetLiq - baseline" delta counts it as a gain. Returns 0.0 when the ledger
    is absent or unreadable, which reproduces the old behaviour exactly.
    """
    try:
        import json as _json
        with open(r"C:\OptionsHistory\deposits.json") as _fh:
            _d = _json.load(_fh)
        _rows = _d.get("flows", []) if isinstance(_d, dict) else _d
        _cut = str(base_ts or "")[:10]
        _tot = 0.0
        for _it in _rows or []:
            if str(_it.get("date", ""))[:10] >= _cut:
                _tot += float(_it.get("amount") or 0.0)
        return round(_tot, 2)
    except Exception:
        return 0.0

def main():
    out = {"ok": False}
    ib = IB()
    cid = 830 + random.randint(0,19)
    try:
        ib.connect('127.0.0.1', $IB_PORT, clientId=cid)
        ib.sleep(1.0)  # Wait for managed accounts list to populate after connect
    except Exception as e:
        out["error"] = f"connect: {e}"
        print(json.dumps(out)); return

    acct = ib.managedAccounts()[0] if ib.managedAccounts() else None
    vals = {}
    try:
        if acct:
            ib.client.reqAccountUpdates(True, acct)   # non-blocking subscribe
            ib.sleep(4.0)                               # wait for IB to push account values
            avs = ib.accountValues(acct)
            ib.client.reqAccountUpdates(False, acct)   # unsubscribe
            for av in avs:  # prefer BASE (consolidated), fall back to USD
                if av.currency == 'BASE' or (av.currency == 'USD' and av.tag not in vals):
                    vals[av.tag] = av.value
    except Exception: pass

    # Fix FD (H4): realized/unrealized via reqPnL (cleaner than accountValues tags), falling
    # back to the RealizedPnL/UnrealizedPnL tags if the stream does not populate. Day Total is
    # realized + unrealized (so it always adds up); all values rounded to 2dp.
    day_real = None; day_unrl = None
    try:
        if acct:
            _pnl = ib.reqPnL(acct)
            ib.sleep(2.5)
            def _fin(x):
                try:
                    xv = float(x)
                    return xv if xv == xv else None   # drop nan
                except Exception:
                    return None
            day_unrl = _fin(getattr(_pnl, "unrealizedPnL", None))
            day_real = _fin(getattr(_pnl, "realizedPnL", None))
            try: ib.cancelPnL(acct)
            except Exception: pass
    except Exception: pass

    if day_real is None: day_real = f(vals.get("RealizedPnL"))
    if day_unrl is None: day_unrl = f(vals.get("UnrealizedPnL"))
    netliq   = f(vals.get("NetLiquidation"))

    day_real = round(day_real, 2) if day_real is not None else None
    day_unrl = round(day_unrl, 2) if day_unrl is not None else None
    out["day_realized"]   = day_real
    out["day_unrealized"] = day_unrl
    out["day_total"]      = round((day_real or 0) + (day_unrl or 0), 2)

    basef = r"C:\OptionsHistory\ytd_baseline.json"
    now   = datetime.now(ZoneInfo("America/New_York"))
    if not os.path.exists(basef):
        base = {"year": now.year, "acct": acct, "netliq": netliq or 0.0, "ts": now.isoformat()}
        os.makedirs(os.path.dirname(basef), exist_ok=True)
        with open(basef,"w") as fh: json.dump(base, fh)
    else:
        base = json.load(open(basef))
        # Reset if year changed OR account changed (e.g. paper→live switch)
        if int(base.get("year",0)) != now.year or base.get("acct") != acct:
            base = {"year": now.year, "acct": acct, "netliq": netliq or 0.0, "ts": now.isoformat()}
            json.dump(base, open(basef,"w"))

    if netliq is not None:
        out["ytd_change"] = round(netliq - float(base.get("netliq", 0.0)), 2)
        # Deposits inflate NetLiq without being performance. Keep ytd_change raw
        # (historical series depend on it) and report the adjusted figure beside it.
        _dep = _deposits_since(base.get("ts"))
        out["deposits_since_base"] = _dep
        out["perf_ex_deposits"] = round(out["ytd_change"] - _dep, 2)
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
  # Fix FD (H4): P/L formatter — 2dp, negatives shown in parentheses e.g. (645.88)
  function _fmtpl($v) {
    if ($null -eq $v) { return '-' }
    $d = 0.0
    if (-not [double]::TryParse([string]$v, [ref]$d)) { return "$v" }
    $s = ('{0:N2}' -f [math]::Abs($d))
    if ($d -lt 0) { return "($s)" } else { return $s }
  }
  if ($obj -and $obj.ok) {
    ("Realized (day)   : {0}" -f (_fmtpl $obj.day_realized))   | Tee-Object -FilePath $Report -Append
    ("Unrealized (open): {0}" -f (_fmtpl $obj.day_unrealized)) | Tee-Object -FilePath $Report -Append
    ("Day Total        : {0}" -f (_fmtpl $obj.day_total))      | Tee-Object -FilePath $Report -Append
    if ($obj.PSObject.Properties.Name -contains 'ytd_change') {
      ("YTD change NetLiq: {0}" -f (_fmtpl $obj.ytd_change))   | Tee-Object -FilePath $Report -Append
    }
    if ($obj.PSObject.Properties.Name -contains 'deposits_since_base') {
      ("Deposits (since baseline): {0}" -f (_fmtpl $obj.deposits_since_base)) | Tee-Object -FilePath $Report -Append
      ("Performance ex-deposits  : {0}" -f (_fmtpl $obj.perf_ex_deposits))    | Tee-Object -FilePath $Report -Append
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

# ---------- Last 20 Orders Submitted (from attempts CSV; survives IBGateway restarts) ----------
"--- Last 20 Orders Submitted (attempts CSV; [working]=live open order) ---" | Tee-Object -FilePath $Report -Append
$tmpPlacedPy = Join-Path $env:TEMP ("placed_" + [guid]::NewGuid().ToString('N') + ".py")
@"
from ib_insync import IB
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import csv, os

NY = ZoneInfo("America/New_York")
OPEN_ACT  = {"open_call", "open_put"}
CLOSE_ACT = {"close", "force_close", "close_call", "close_put", "close_individual_leg"}
OK_STATUS = {"placed", "submitted", "success"}
WORKING   = {"presubmitted", "submitted", "pendingsubmit", "inactive"}

def main():
    # Live annotation: symbols with a currently-working order. Fix FD (H1): reqAllOpenOrders +
    # reqOpenOrders (Fix CZ/DA reclaim) so prior-session clientId=101 orders are visible.
    working = {}
    ib = IB()
    try:
        ib.connect('127.0.0.1', $IB_PORT, clientId=900, timeout=6)
        try:
            ib.reqAllOpenOrders(); ib.sleep(1.5)
            ib.reqOpenOrders();    ib.sleep(0.5)
        except Exception:
            pass
        for tr in (ib.openTrades() or []):
            st = (getattr(tr.orderStatus, "status", "") or "")
            if st.lower() not in WORKING:
                continue
            sym = getattr(tr.contract, "symbol", "") or ""
            if sym:
                working[sym.upper()] = st
    except Exception:
        pass
    finally:
        try: ib.disconnect()
        except: pass

    # Primary source: persistent attempts CSVs (never wiped by the daily IBGateway restart).
    rows = []
    today = datetime.now(NY)
    for back in range(0, 4):
        folder = (today - timedelta(days=back)).strftime("%y_%m_%d")
        path = r"C:\OptionsHistory\{0}\attempts_{0}.csv".format(folder)
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="") as fh:
                for r in csv.DictReader(fh):
                    act = (r.get("action") or "").strip().lower()
                    stt = (r.get("status") or "").strip().lower()
                    if act not in OPEN_ACT and act not in CLOSE_ACT:
                        continue
                    if stt not in OK_STATUS:
                        continue
                    try:
                        tdt = datetime.fromisoformat((r.get("ts") or "").strip())
                    except Exception:
                        continue
                    sym = (r.get("symbol") or "").strip().upper()
                    oa  = (r.get("order_action") or "").strip()
                    ot  = (r.get("order_type") or "").strip()
                    lim = (r.get("limit") or "").strip()
                    rsn = (r.get("reason") or "").strip()
                    oatype = (oa + "/" + ot).strip("/") or "-"
                    lims = ("limit=" + lim) if lim else "MKT"
                    wk = "  [working]" if sym in working else ""
                    line = "{0}  {1:<6} {2:<9} {3:<9} {4:<11}  ({5}){6}".format(
                        tdt.strftime("%Y-%m-%d %H:%M"), sym, act, oatype, lims, rsn, wk)
                    rows.append((tdt, line))
        except Exception:
            continue

    rows.sort(key=lambda x: x[0], reverse=True)
    rows = rows[:20]
    if not rows:
        print("(no placements in recent attempts CSVs)")
        return
    for _, line in rows:
        print(line)

if __name__ == '__main__':
    main()
"@ | Set-Content -Encoding ASCII $tmpPlacedPy

try {
  $placedOut = & $Py $tmpPlacedPy 2>$null
  if ($LASTEXITCODE -ne 0) { "placed-orders: ERROR (exit $LASTEXITCODE)" | Tee-Object -FilePath $Report -Append }
  if ($placedOut) {
    ($placedOut -split "`r?`n") | ForEach-Object { $_ | Tee-Object -FilePath $Report -Append }
  } else {
    "(no output from placed-orders probe)" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "placed-orders: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpPlacedPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Rejected / Errored Orders (today) ----------
# Fix FD (H2): surface IB async rejections (bs_rejected_bag_e201 = margin/PDT/permission) and
# hard-failure skips from today's attempts CSV. Read-only; no IB connection.
"--- Rejected / Errored Orders (today) ---" | Tee-Object -FilePath $Report -Append
try {
  $errFile = Get-ChildItem -Path ("C:\OptionsHistory\{0}" -f $dated) -Filter 'attempts_*.csv' -File -EA SilentlyContinue |
             Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $errFile) {
    "  (no attempts CSV today)" | Tee-Object -FilePath $Report -Append
  } else {
    $erows = @(); try { $erows = Import-Csv $errFile.FullName -EA Stop } catch { $erows = @() }
    $bad = $erows | Where-Object {
      $_.status -eq 'error' -or
      ($_.status -eq 'skipped' -and $_.reason -match 'bs_rejected|no_viable_limit|opposite_unwind_failed|place_failed|qualify_failed')
    }
    if (-not $bad -or @($bad).Count -eq 0) {
      "  (no rejected/errored orders today)" | Tee-Object -FilePath $Report -Append
    } else {
      @($bad) | Sort-Object { $_.ts } | Select-Object -Last 20 | ForEach-Object {
        $t = "$($_.ts)"; try { $t = ([datetime]$_.ts).ToString('yyyy-MM-dd HH:mm') } catch {}
        $rs = "$($_.reason)"; if ($rs.Length -gt 70) { $rs = $rs.Substring(0,70) }
        $lm = if ("$($_.limit)".Trim() -ne '') { "limit=$("$($_.limit)".Trim())" } else { "" }
        ("  {0}  {1,-6} {2,-10} {3,-8} {4}  {5}" -f $t, $_.symbol, $_.action, $_.status, $rs, $lm) |
          Tee-Object -FilePath $Report -Append
      }
    }
  }
} catch {
  ("  Rejected/Errored section error: {0}" -f $_.Exception.Message) | Tee-Object -FilePath $Report -Append
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
    ib=IB()
    try:
        ib.connect('127.0.0.1',$IB_PORT,clientId=897,timeout=6)
        ib.sleep(1.5)
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
            print("(no current option positions)"); return

        for (sym,exp,right), legs in items:
            strikes = sorted(legs.keys())
            if len([k for k,(q,_) in legs.items() if abs(q)>1e-9]) < 2:
                continue
            print(f"{sym} {exp} {right} Vertical (positions)")
            for k in strikes:
                q, avg = legs[k]
                if abs(q) < 1e-9:
                    continue
                side = "LONG " if q>0 else "SHORT"
                print(f"  {side:<5} strike={k:<7} exp={exp:<8} right={right}  posQty={q:<6} avgCost={avg:.2f}")
    except Exception as e:
        print(f"(open-positions) error: {e}")
    finally:
        try: ib.disconnect()
        except: pass

if __name__=='__main__': main()
"@ | Set-Content -Encoding ASCII $tmpOpenPosPy

try {
  $openposOut = & $Py $tmpOpenPosPy 2>$null
  if ($LASTEXITCODE -ne 0) { "open-positions: ERROR (exit $LASTEXITCODE)" | Tee-Object -FilePath $Report -Append }
  if ($openposOut) {
    ($openposOut -split "`r?`n") | ForEach-Object { $_ | Tee-Object -FilePath $Report -Append }
  } else {
    "(no output from open-positions probe)" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "open-positions: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpOpenPosPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Closes Attempted But Still Open (unfilled) ----------
# Flags symbols that had a close attempt in today's attempts CSV but are STILL held
# (close placed but never filled -- e.g. low demand). Read-only; may need manual intervention.
"--- Closes Attempted But Still Open (today; likely unfilled) ---" | Tee-Object -FilePath $Report -Append
$tmpUnfilledPy = Join-Path $env:TEMP ("unfilledclose_" + [guid]::NewGuid().ToString('N') + ".py")
@"
import csv, os
from datetime import datetime
from zoneinfo import ZoneInfo
from ib_insync import IB

NY = ZoneInfo("America/New_York")

def main():
    folder = datetime.now(NY).strftime("%y_%m_%d")
    path = r"C:\OptionsHistory\{0}\attempts_{0}.csv".format(folder)
    close_actions = {"close", "force_close", "close_individual_leg"}
    ok_status = {"placed", "submitted"}
    attempted = {}
    if os.path.exists(path):
        try:
            with open(path, newline="") as fh:
                for row in csv.DictReader(fh):
                    act = (row.get("action") or "").strip().lower()
                    stt = (row.get("status") or "").strip().lower()
                    if act in close_actions and stt in ok_status:
                        sym = (row.get("symbol") or "").strip().upper()
                        if sym:
                            attempted[sym] = (row.get("limit") or "", row.get("reason") or "")
        except Exception as e:
            print("(unfilled-close) csv error: {0}".format(e)); return
    if not attempted:
        print("(no close attempts in today's attempts CSV)"); return
    ib = IB()
    held = set()
    try:
        ib.connect('127.0.0.1',$IB_PORT,clientId=885,timeout=6)
        ib.sleep(1.5)
        for p in ib.positions():
            c = p.contract
            if getattr(c,"secType","")=="OPT" and abs(float(p.position or 0.0))>1e-9:
                held.add((c.symbol or "").upper())
    except Exception as e:
        print("(unfilled-close) IB error: {0}".format(e)); return
    finally:
        try: ib.disconnect()
        except: pass
    flagged = sorted(s for s in attempted if s in held)
    if not flagged:
        print("(all of today's close attempts are no longer held -- none stuck)"); return
    for s in flagged:
        lim, reason = attempted[s]
        lim_s = lim if lim else "?"
        print("{0:<6} close placed limit={1} ({2}) -- STILL OPEN, likely unfilled (low demand); consider manual close".format(s, lim_s, reason))

if __name__=='__main__': main()
"@ | Set-Content -Encoding ASCII $tmpUnfilledPy

try {
  $unfilledOut = & $Py $tmpUnfilledPy 2>$null
  if ($LASTEXITCODE -ne 0) { "unfilled-close: ERROR (exit $LASTEXITCODE)" | Tee-Object -FilePath $Report -Append }
  if ($unfilledOut) {
    ($unfilledOut -split "`r?`n") | ForEach-Object { $_ | Tee-Object -FilePath $Report -Append }
  } else {
    "(no output from unfilled-close probe)" | Tee-Object -FilePath $Report -Append
  }
} catch {
  "unfilled-close: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $Report -Append
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmpUnfilledPy
}
" " | Tee-Object -FilePath $Report -Append

# ---------- Last 20 Orders Closed (with P/L) ----------
"--- Last 20 Orders Closed (est. P/L from limits) ---" | Tee-Object -FilePath $Report -Append
# Attempts-CSV-based closed P/L — no IB connection needed, survives IBGateway restarts.
# Scans last 30 days of C:\OptionsHistory\YY_MM_DD\attempts_*.csv.
# OPEN  rows: action=open_call/open_put,  status=placed, reason=success, limit+longK present.
# CLOSE rows: action=force_close/close_*, status=placed, order_action=SELL, limit+longK present.
# Match key: symbol+right+longK. P/L = (close_limit - open_limit) * 100 * qty.
$allAttempts = @()
$today_dt = Get-Date
for ($dOff = 0; $dOff -le 30; $dOff++) {
    $d = $today_dt.AddDays(-$dOff)
    $folder = "C:\OptionsHistory\{0}" -f $d.ToString("yy_MM_dd")
    $csvFile = Get-ChildItem -Path $folder -Filter "attempts_*.csv" -File -EA SilentlyContinue |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($csvFile) {
        $rows = Import-Csv $csvFile.FullName -EA SilentlyContinue
        if ($rows) { $allAttempts += $rows }
    }
}

# Pre-parse timestamps so we can compare in Where-Object (try/catch not valid in PS5 filter blocks)
$csvOpens = @()
foreach ($r in $allAttempts) {
    if ($r.action -notin @('open_call','open_put')) { continue }
    if ($r.status -ne 'placed' -or $r.reason -ne 'success') { continue }
    if ($r.limit -eq '' -or $r.longK -eq '') { continue }
    $ts = $null; try { $ts = [datetime]$r.ts } catch {}
    if (-not $ts) { continue }
    $r | Add-Member -NotePropertyName PsTs -NotePropertyValue $ts -Force
    $csvOpens += $r
}
$csvCloses = @()
foreach ($r in $allAttempts) {
    if ($r.action -notin @('force_close','close_call','close_put','close')) { continue }
    if ($r.status -ne 'placed') { continue }
    # Fix FD (H3): reconcile/roll closes leave order_action blank but are always SELL-to-close.
    $oa = "$($r.order_action)".Trim()
    if ($oa -ne '' -and $oa -ne 'SELL') { continue }
    if ($r.limit -eq '') { continue }   # longK may be blank on reconcile rows -> symbol fallback below
    # Fix FD (H3): include signal-driven / reconcile / roll closes, not just success|worthless.
    if ($r.reason -notmatch 'success|worthless|close_signal|reconcile_close_signal|roll_mkt_close') { continue }
    $ts = $null; try { $ts = [datetime]$r.ts } catch {}
    if (-not $ts) { continue }
    $r | Add-Member -NotePropertyName PsTs -NotePropertyValue $ts -Force
    $csvCloses += $r
}

$closedMatched = @()
foreach ($cl in $csvCloses) {
    # Trim fields before building key — CSV values may have surrounding spaces
    $clSym = "$($cl.symbol)".Trim()
    $clRt  = "$($cl.right)".Trim()
    $clLk  = "$($cl.longK)".Trim()
    if ($clRt -ne '' -and $clLk -ne '') {
        # Precise pairing: symbol|right|longK
        $op = $csvOpens | Where-Object {
            "$($_.symbol)".Trim() -eq $clSym -and "$($_.right)".Trim() -eq $clRt -and "$($_.longK)".Trim() -eq $clLk -and $_.PsTs -lt $cl.PsTs
        } | Sort-Object PsTs -Descending | Select-Object -First 1
    } else {
        # Fix FD (H3): reconcile/roll close rows lack right/longK -> symbol-only fallback
        # to the most recent prior open for that symbol.
        $op = $csvOpens | Where-Object {
            "$($_.symbol)".Trim() -eq $clSym -and $_.PsTs -lt $cl.PsTs
        } | Sort-Object PsTs -Descending | Select-Object -First 1
    }
    if (-not $op) { continue }
    $qty = if ("$($cl.qty)".Trim() -match '^\d+$') { [int]$cl.qty } else { 1 }
    $pnl = ([double]$cl.limit - [double]$op.limit) * 100 * $qty
    # Fill display fields from the close row, else the matched open row.
    $dLk  = if ($clLk -ne '') { $clLk } else { "$($op.longK)".Trim() }
    $dSk  = if ("$($cl.shortK)".Trim() -ne '') { "$($cl.shortK)".Trim() } else { "$($op.shortK)".Trim() }
    $dRt  = if ($clRt -ne '') { $clRt } else { "$($op.right)".Trim() }
    $dExp = if ("$($cl.exp)".Trim() -ne '') { "$($cl.exp)".Trim() } else { "$($op.exp)".Trim() }
    $closedMatched += [pscustomobject]@{
        CloseTs     = $cl.PsTs
        Symbol      = $clSym
        Exp         = $dExp
        Right       = $dRt
        Spread      = "$dLk/$dSk"
        OpenLimit   = [double]$op.limit
        CloseLimit  = [double]$cl.limit
        PnL         = $pnl
        CloseReason = "$($cl.close_reason)".Trim()
    }
}

if (-not $closedMatched) {
    "  (no matched open->close pairs in last 30 days of attempts CSVs)" | Tee-Object -FilePath $Report -Append
} else {
    $closedMatched | Sort-Object CloseTs -Descending | Select-Object -First 20 | ForEach-Object {
        $ts   = $_.CloseTs.ToString("yyyy-MM-dd HH:mm")
        $rsn  = if ($_.CloseReason) { "  [$($_.CloseReason)]" } else { "" }
        # Fix FD: losses shown in parentheses e.g. ($106.00); gains as $14.00
        $pnlStr = if ($_.PnL -lt 0) { "(`${0:N2})" -f [Math]::Abs($_.PnL) } else { "`${0:N2}" -f $_.PnL }
        ("  {0}  {1,-6} {2} {3}  {4,-11}  open=`${5:N2} -> close=`${6:N2}  P/L={7}{8}" -f `
            $ts, $_.Symbol, $_.Exp, $_.Right, $_.Spread,
            $_.OpenLimit, $_.CloseLimit, $pnlStr, $rsn) | Tee-Object -FilePath $Report -Append
    }
}
" " | Tee-Object -FilePath $Report -Append

"==== END ===="                             | Tee-Object -FilePath $Report -Append
" " | Tee-Object -FilePath $Report -Append
# ---------- Order Attempts Summary (from today's day folder) ----------
"--- Order Attempts Summary (today's attempts_*.csv) ---" | Tee-Object -FilePath $Report -Append

$attemptDir = "C:\OptionsHistory\$dated"
try {
  # $dated = yy_MM_dd (today's folder) — all attempts_*.csv here are today's
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
    $enc = [System.Text.Encoding]::UTF8
    # Seek from end: ~300 bytes/line avg; 3x buffer to be safe
    $bytesToRead = [Math]::Min($fs.Length, $tail * 900)
    $startPos    = $fs.Length - $bytesToRead
    if ($startPos -gt 0) { [void]$fs.Seek($startPos, [System.IO.SeekOrigin]::Begin) }
    $buf  = New-Object byte[] ($fs.Length - $fs.Position)
    $read = $fs.Read($buf, 0, $buf.Length)
    $text = $enc.GetString($buf, 0, $read)
    $lines = $text -split "`r?`n"
    # Drop the first (potentially partial) line when we started mid-file
    if ($startPos -gt 0 -and $lines.Count -gt 0) { $lines = $lines[1..($lines.Count - 1)] }
    if ($lines.Count -gt $tail) { $lines = $lines[-$tail..-1] }
    return $lines
  } finally { $fs.Dispose() }
}

$lines = Read-SharedFile $logPath 2500

# Scope stats to today + yesterday to avoid multi-day accumulation from the cumulative log
$_todayStr  = (Get-Date).ToString('yyyy-MM-dd')
$_yesterStr = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
$lines = $lines | Where-Object { $_ -match "^($_todayStr|$_yesterStr)" }

# Buckets
# Filter to 'orderStatus:' lines only — each order also fires an 'openOrder:' broadcast
# that would double-count every submission. 'orderStatus:' = the real status update.
$placed  = $lines | Select-String -Pattern "orderStatus:.*status='(Submitted|PreSubmitted)'" | Select-Object -ExpandProperty Line
# Order-level action comes after 'permId=NNN, action=' in Order(); comboLegs also have
# action= but appear earlier in the line — anchoring on permId= avoids false matches.
$closed  = $lines | Select-String -Pattern "orderStatus:.*permId=\d+, action='SELL'.*status='(Submitted|PreSubmitted)'" | Select-Object -ExpandProperty Line
$weekly  = $lines | Select-String -Pattern '[Ff]orce.close|[Ss]tage\s+2[^0-9]|force_close' | Select-Object -ExpandProperty Line
$failed  = $lines | Select-String -Pattern '\bERROR\b|Exception:|no_viable_limit|SKIPPING order' | Select-Object -ExpandProperty Line
$skipped = $lines | Select-String -Pattern 'skipped|defer_to_force|no_spread_in_positions' | Select-Object -ExpandProperty Line

# Compact summary counts
$buyCount = $placed.Count - $closed.Count
("Submitted (open/close) : {0}  [{1} SELL / {2} BUY]" -f $placed.Count, $closed.Count, $buyCount) | Tee-Object -FilePath $Report -Append
("Force-close activity   : {0}" -f $weekly.Count)                                                   | Tee-Object -FilePath $Report -Append
("Failures            : {0}" -f $failed.Count)                           | Tee-Object -FilePath $Report -Append
("Skips               : {0}" -f $skipped.Count)                          | Tee-Object -FilePath $Report -Append

# Parse a raw ib_insync orderStatus line into a compact one-line summary
$formatOrder = {
  param($line)
  $ts  = if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})') { $Matches[1] } else { '?' }
  $sym = if ($line -match "symbol='([A-Z][A-Z\.]*)'")           { $Matches[1] } else { '?' }
  # Order-level action anchored after permId= in Order() — not a comboLeg action
  $act = if ($line -match "permId=\d+, action='([A-Z]+)'")      { $Matches[1] } else { '?' }
  $ot  = if ($line -match "orderType='([^']+)'")                 { $Matches[1] } else { '?' }
  $lmt = if ($line -match "lmtPrice=([\d\.]+)")                  { $Matches[1] } else { '-' }
  "{0}  {1,-6}  {2,-4}  {3,-3}  @`${4}" -f $ts, $sym, $act, $ot, $lmt
}

# Fix FD (H5): source the submitted list from the persistent attempts CSV — the ib_cycle.log
# tail window often does not reach back to the 5 PM batch, leaving $placed empty.
"`nLast 10 submitted orders (attempts CSV):" | Tee-Object -FilePath $Report -Append
$subRows = @()
foreach ($bk in 0..2) {
  $fld = (Get-Date).AddDays(-$bk).ToString('yy_MM_dd')
  $ap  = "C:\OptionsHistory\$fld\attempts_$fld.csv"
  if (Test-Path $ap) {
    $rws = @(); try { $rws = Import-Csv $ap -EA Stop } catch { $rws = @() }
    foreach ($r in $rws) {
      if ($r.action -notin @('open_call','open_put','close','force_close','close_call','close_put','close_individual_leg')) { continue }
      if ($r.status -notin @('placed','submitted','success')) { continue }
      $ts = $null; try { $ts = [datetime]$r.ts } catch {}
      if (-not $ts) { continue }
      $oatype = (("$($r.order_action)".Trim() + "/" + "$($r.order_type)".Trim()).Trim('/')); if (-not $oatype) { $oatype = '-' }
      $lm = if ("$($r.limit)".Trim() -ne '') { "limit=$("$($r.limit)".Trim())" } else { "MKT" }
      $subRows += [pscustomobject]@{ Ts = $ts; Line = ("{0}  {1,-6} {2,-9} {3,-9} {4,-11}  ({5})" -f $ts.ToString('yyyy-MM-dd HH:mm'), "$($r.symbol)".Trim(), "$($r.action)".Trim(), $oatype, $lm, "$($r.reason)".Trim()) }
    }
  }
}
if (@($subRows).Count -eq 0) {
  "  (no recent placements in attempts CSVs)" | Tee-Object -FilePath $Report -Append
} else {
  $subRows | Sort-Object Ts -Descending | Select-Object -First 10 | ForEach-Object { $_.Line } |
    Tee-Object -FilePath $Report -Append | Out-Null
}

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
  # Limit to 1-5 char symbols; excludes [ERROR], [WARNING], [INFO], [DEBUG] (6+ chars)
  $excluded = @('ERROR','WARNING','INFO','DEBUG','CRITICAL')
  if ($line -match '\[(?<sym>[A-Z][A-Z\.]{0,4})\]') {
    if ($matches.sym -notin $excluded) { return $matches.sym }
  }
  if ($line -match "symbol='(?<sym>[A-Z][A-Z\.]+)'") { return $matches.sym }
  return $null
}
$allTagged = @()
# Use $placed only (not $placed + $closed) — $closed is a subset of $placed and would double-count SELL orders
foreach ($l in $placed)  { $s = & $extractSym $l; if ($s){ $allTagged += [pscustomobject]@{Sym=$s; Kind='placed'} } }
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

# ---------- Recent Logs ----------
"--- Recent Logs ---"                      | Tee-Object -FilePath $Report -Append
# Fix FD (H6): always include ib_cycle.log (the real recent activity), and exclude the Health
# self-log (Health_*.log — position dumps) which is always the newest file right after this run
# and otherwise crowds out ib_cycle.log from the top-3-most-recent selection.
$recentLogFiles = @()
$iclFile = Get-Item 'C:\OptionsHistory\logs\ib_cycle.log' -ErrorAction SilentlyContinue
if ($iclFile) { $recentLogFiles += $iclFile }
Get-ChildItem "$LogDir\*.log" -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -notlike 'Health_*' -and $_.Name -ne 'ib_cycle.log' } |
  Sort-Object LastWriteTime -Desc | Select-Object -First 2 |
  ForEach-Object { $recentLogFiles += $_ }
$recentLogFiles |
  ForEach-Object {
    $logFile = $_
    "`n### $($logFile.FullName) (last 60 lines)" | Tee-Object -FilePath $Report -Append
    $logLines = Get-Content $logFile.FullName -Tail 60
    # Suppress verbose ib_insync position/portfolio/openOrder dumps (already shown in Positions);
    # applies to ib_cycle.log and listener.err.log alike.
    $logLines = $logLines | Where-Object {
      $_ -notmatch 'ib_insync\.wrapper:(position|updatePortfolio|openOrder):' -and
      $_ -notmatch 'INFO (position|updatePortfolio|openOrder):' -and
      $_ -notmatch 'ib_insync\.ib:Synchronization'
    }
    $logLines | Tee-Object -FilePath $Report -Append
  }

" " | Tee-Object -FilePath $Report -Append
# ---- Minimal terminal summary only ----
Write-Host ""
Write-Host "--- SUMMARY ---"
try {
  $svc = Get-Service IBGateway, OptionsListener -ErrorAction SilentlyContinue
  foreach ($s in $svc) { Write-Host ("{0,-16} : {1}" -f $s.Name,$s.Status) }
} catch {}

try {
  # Fix FD: echo ONLY the P/L Summary section (not a whole-file grep) so stale P/L lines that
  # a Recent-Logs tail may surface from a prior run can't leak into this end-of-run summary.
  $inPL = $false
  Get-Content $Report | ForEach-Object {
    if ($_ -match '^--- P/L Summary') { $inPL = $true; return }
    if ($inPL -and $_ -match '^--- ') { $inPL = $false }
    if ($inPL -and $_ -match '^(Realized|Unrealized|Day Total|YTD)') { Write-Host $_ }
  }
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

# Re-encode the saved report from UTF-16 LE (PS 5.1 Tee-Object default) to UTF-8 without BOM
# so the .txt file is readable in Notepad, VSCode, etc.
try {
    $content = [System.IO.File]::ReadAllText($Report, [System.Text.Encoding]::Unicode)
    [System.IO.File]::WriteAllText($Report, $content, [System.Text.UTF8Encoding]::new($false))
} catch {}

Write-Output "`nPress Enter to return to menu: "
try { [void](Read-Host) } catch {}