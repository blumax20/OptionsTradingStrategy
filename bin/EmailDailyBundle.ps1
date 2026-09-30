<#
  EmailDailyBundle.ps1 — end-of-day summary + attachments to max@hyperbukit.com.

  Restored 2026-08-25. The previous version was deleted in commit de3bfab and the scheduled
  task IB_Email_Daily_Bundle kept firing at a path that no longer existed, returning
  4294770688 daily. Four things were wrong with it beyond being missing:

    1. It used the `??` null-coalescing operator, which is PowerShell 7 only. The task runs
       powershell.exe (Windows PowerShell 5.1), so it would have failed to PARSE even once
       restored. Do not reintroduce `??`, `?.` or ternaries in this file.
    2. It connected to IB on port 7497 (paper). This account trades live on 7496.
    3. It read RealizedPnL / UnrealizedPnL from accountSummary(), which returns them empty.
       They come from portfolio() instead.
    4. Its day-folder calculation was UtcNow.AddHours(-4), hardcoding EDT. That silently
       picks the wrong folder from the first Sunday in November until March.

  Mail path: Google Workspace SMTP relay, IP-allowlisted to this host (45.43.28.226). No
  credential is stored on this box by design — the box already holds enough secrets. The
  relay is configured "Only addresses in my domains", so From must stay @hyperbukit.com.
#>

param(
  [string]$SmtpServer = 'smtp-relay.gmail.com',
  [int]   $SmtpPort   = 587,
  [string]$From       = 'noreply@hyperbukit.com',
  [string]$To         = 'max@hyperbukit.com',
  [switch]$WhatIfNoSend
)

$ErrorActionPreference = 'Stop'
$LOG = 'C:\OptionsHistory\logs\EmailDailyBundle.log'

function W([string]$m) {
  $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  "$ts $m" | Tee-Object -FilePath $LOG -Append | Out-Null
  Write-Host "$ts $m"
}

# Same formatter Health.ps1 uses (Health.ps1 _fmtpl), so the 17:15 email and the
# 17:15 health report are directly comparable: 2dp, negatives in parentheses, '-'
# when IB gave us nothing. Defined at script scope so every section - and the
# catch block - can reach it.
function _fmtpl($v) {
  if ($null -eq $v) { return '-' }
  $d = 0.0
  if (-not [double]::TryParse([string]$v, [ref]$d)) { return "$v" }
  $s = ('{0:N2}' -f [math]::Abs($d))
  if ($d -lt 0) { return "($s)" } else { return $s }
}

try {
  W "==== EmailDailyBundle start ===="

  # Proper timezone conversion, so this stays correct across the DST boundary.
  $ny = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Eastern Standard Time')
  $dateStr   = $ny.ToString('yyyy-MM-dd')
  $dayFolder = 'C:\OptionsHistory\{0}' -f $ny.ToString('yy_MM_dd')
  W "Day folder: $dayFolder"

  # ---------------------------------------------------------------- signals
  $signalCount = 0
  $wh = Join-Path $dayFolder 'webhooks.jsonl'
  if (Test-Path $wh) { $signalCount = @(Get-Content $wh | Where-Object { $_.Trim() }).Count }

  $pricedRows = 0
  $combined = Join-Path $dayFolder 'combined_listener_spreads.csv'
  if (Test-Path $combined) { $pricedRows = [Math]::Max(0, (@(Get-Content $combined).Count - 1)) }

  # ---------------------------------------------------------------- orders
  $placed = @(); $skipReasons = @{}
  $attempts = Join-Path $dayFolder ("attempts_{0}.csv" -f $ny.ToString('yy_MM_dd'))
  if (Test-Path $attempts) {
    foreach ($r in (Import-Csv $attempts)) {
      if ($r.status -eq 'placed') {
        $placed += ("{0} {1} {2} {3} @ {4}" -f $r.ts.Substring(11,8), $r.symbol, $r.action, $r.right, $r.limit)
      } elseif ($r.status -eq 'skipped' -and $r.reason) {
        if (-not $skipReasons.ContainsKey($r.reason)) { $skipReasons[$r.reason] = 0 }
        $skipReasons[$r.reason]++
      }
    }
  }

  # ---------------------------------------------------------------- P/L
  $py = 'C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe'
  $pl = $null
  if (Test-Path $py) {
    $tmp = Join-Path $env:TEMP ('pl_' + [guid]::NewGuid().ToString('N') + '.py')
    @'
from ib_insync import IB
import contextlib, io as _io, json, os, random, sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Hardcoded on purpose: this heredoc is single-quoted, so PowerShell does not
# interpolate and every path must be a literal here.
REPO  = r"C:\Users\Administrator\code\OptionsTradingStrategy"
BASEF = r"C:\OptionsHistory\ytd_baseline.json"
NY    = ZoneInfo("America/New_York")

# Read the port from ib_config rather than hardcoding it. This file used to
# hardcode 7497, then 7496; because Fix DI recorded it as deleted it also fell out
# of switch_trading_mode.py's update loop, so a paper/live switch would silently
# leave the email pointed at a dead port.
IB_PORT = 7496
try:
    sys.path.insert(0, os.path.join(REPO, "InteractiveBrokersTrader"))
    from ib_config import IB_PORT as _CFG_PORT
    IB_PORT = int(_CFG_PORT)
except Exception:
    pass

def F(x):
    try: return float(x)
    except: return None

def FIN(x):
    # float, but reject NaN - IB sends NaN for a tag it has not computed yet
    try:
        xv = float(x)
        return xv if xv == xv else None
    except Exception:
        return None

def read_ytd_baseline():
    # Same file Health.ps1 derives "YTD change NetLiq" from (Health.ps1 lines 415-429),
    # so the 17:15 email and the 17:15 health report cannot disagree.
    #
    # READ ONLY. Health.ps1 owns creating this file and resetting it on a year or
    # account change. If we created it here we would silently rebase YTD to today's
    # NetLiq for Health.ps1 and MonthlyReview as well. On a missing or stale baseline
    # we report nothing rather than guessing.
    try:
        with open(BASEF) as fh:
            return json.load(fh)
    except Exception:
        return None


def month_context(today):
    # Reuse MonthlyReview.load_health() instead of re-parsing the health reports:
    # section A of the monthly review IS the definition of the month figure, and its
    # read_txt() already handles both the UTF-16LE and UTF-8 reports on disk.
    # Returns (prior_month_last_ytd, realized_sum_before_today, n_days), or all None.
    #
    # stdout is redirected because this process's contract is "last line is the JSON";
    # anything MonthlyReview ever prints must not be able to break that.
    try:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        with contextlib.redirect_stdout(_io.StringIO()):
            import MonthlyReview as MR
            daily, _snaps, base = MR.load_health(today[:6])   # load_health wants YYYYMM
        prior = base[1] if base else None
        rsum = 0.0
        ndays = 0
        for row in daily:
            # Strictly before today: IB_EndOfDay_Health_1715 and this task both fire at
            # 17:15, so today's report may or may not exist yet. Today's realized is
            # added from the live reqPnL figure instead - race-free, never double counted.
            if row[0] >= today:
                continue
            if row[1] is not None:
                rsum += row[1]
                ndays += 1
        return prior, rsum, ndays
    except Exception:
        return None, None, None


ib = IB(); out = {"ok": False}

# File-derived, so these survive an IB outage and the MONTH TO DATE block still
# renders when ACCOUNT says unavailable.
_today = datetime.now(NY).strftime("%Y%m%d")
out["today"] = _today
_base = read_ytd_baseline()
out["ytd_base_netliq"] = F(_base.get("netliq")) if _base else None
out["ytd_base_year"]   = _base.get("year") if _base else None
out["ytd_base_acct"]   = _base.get("acct") if _base else None
out["ytd_base_ts"]     = (_base.get("ts") or "")[:10] if _base else None
_prior_ytd, _mtd_real_prior, _mtd_days = month_context(_today)
out["mtd_prior_ytd"]      = _prior_ytd
out["mtd_realized_prior"] = round(_mtd_real_prior, 2) if _mtd_real_prior is not None else None
out["mtd_report_days"]    = _mtd_days

try:
    ib.connect("127.0.0.1", IB_PORT, clientId=850 + random.randint(0, 29), timeout=8)
    ib.sleep(1.0)
    accts = ib.managedAccounts()
    acct = accts[0] if accts else None

    # accountValues(), NOT accountSummary(): the RealizedPnL / UnrealizedPnL tags come back
    # empty from accountSummary but are populated here. Prefer BASE (consolidated) over USD.
    vals = {}
    try:
        if acct:
            ib.client.reqAccountUpdates(True, acct)
            ib.sleep(4.0)
            avs = ib.accountValues(acct)
            ib.client.reqAccountUpdates(False, acct)
            for av in avs:
                if av.currency == "BASE" or (av.currency == "USD" and av.tag not in vals):
                    vals[av.tag] = av.value
    except Exception:
        pass

    # Day P/L comes from IB's own P&L stream, matching Health.ps1.
    #
    # Do NOT go back to summing PortfolioItem over ib.portfolio(). That was the original
    # implementation and it reported Realized P/L = 0.00 every single day, for two
    # compounding reasons: PortfolioItem.realizedPNL is 0.0 on every open leg (verified
    # across all 38 legs on 2026-08-27), and closing a spread removes its legs from
    # portfolio() altogether - so the one event that creates realized P/L is the same
    # event that deletes it from the sum. The figure was structurally unreachable.
    day_real = None; day_unrl = None
    try:
        if acct:
            pnl = ib.reqPnL(acct)
            ib.sleep(2.5)
            day_unrl = FIN(getattr(pnl, "unrealizedPnL", None))
            day_real = FIN(getattr(pnl, "realizedPnL", None))
            try: ib.cancelPnL(acct)
            except Exception: pass
    except Exception:
        pass

    # Fall back to the account tags if the P&L stream did not populate in time.
    if day_real is None: day_real = F(vals.get("RealizedPnL"))
    if day_unrl is None: day_unrl = F(vals.get("UnrealizedPnL"))

    # Group the option legs into verticals the same way the health report's Current
    # Positions probe does (Health.ps1 lines 626-649): key (symbol, expiry, right),
    # LONG/SHORT from the sign of the position, strikes ascending, entry debit =
    # avgCost(long) - avgCost(short) in whole dollars (IB option avgCost already
    # includes the x100 multiplier). MonthlyReview.section_ledger defines it the same.
    #
    # Unrealized per leg is PortfolioItem.unrealizedPNL, which IS populated - it is only
    # realizedPNL that is structurally 0.00 on an open leg (see the note above). This
    # reuses the ib.portfolio() call that already feeds legs=, so it costs no round trip.
    #
    # Unlike Health.ps1, legs that are not a clean 1x1 vertical are reported rather than
    # skipped: a naked leg left by a half-filled close is exactly what this table is for.
    spreads = []
    orphans = []
    try:
        groups = {}
        for it in ib.portfolio():
            c = it.contract
            if getattr(c, "secType", "") != "OPT":
                continue
            q = float(it.position or 0.0)
            if abs(q) < 1e-9:
                continue
            k = (c.symbol, getattr(c, "lastTradeDateOrContractMonth", ""),
                 getattr(c, "right", ""))
            groups.setdefault(k, []).append(
                (float(getattr(c, "strike", 0.0)), q, F(it.averageCost),
                 FIN(it.unrealizedPNL)))
        for k in sorted(groups.keys()):
            legs = sorted(groups[k], key=lambda L: L[0])
            longs  = [L for L in legs if L[1] > 0]
            shorts = [L for L in legs if L[1] < 0]
            unrl = 0.0
            unrl_ok = True
            for L in legs:
                if L[3] is None:
                    unrl_ok = False
                else:
                    unrl += L[3]
            if len(longs) == 1 and len(shorts) == 1:
                lc, sc = longs[0][2], shorts[0][2]
                debit = round(lc - sc, 2) if (lc is not None and sc is not None) else None
                spreads.append({
                    "sym": k[0], "exp": k[1], "right": k[2],
                    # Preformatted here so the PowerShell side never casts a null.
                    "strikes": "%g/%g" % (longs[0][0], shorts[0][0]),
                    "qty": int(abs(longs[0][1])),
                    "debit": debit,
                    "unrealized": round(unrl, 2) if unrl_ok else None,
                })
            else:
                for L in legs:
                    orphans.append({"sym": k[0], "exp": k[1], "right": k[2],
                                    "strike": "%g" % L[0], "qty": "%g" % L[1],
                                    "avg_cost": L[2], "unrealized": L[3]})
    except Exception:
        pass

    # YTD: identical arithmetic to Health.ps1 line 429, against the same baseline file.
    netliq = F(vals.get("NetLiquidation"))
    ytd_chg = None
    ytd_note = None
    if out["ytd_base_netliq"] is None:
        ytd_note = "no ytd_baseline.json"
    elif int(out["ytd_base_year"] or 0) != datetime.now(NY).year:
        ytd_note = "baseline year %s" % out["ytd_base_year"]
    elif out["ytd_base_acct"] is not None and acct is not None and out["ytd_base_acct"] != acct:
        ytd_note = "baseline account %s" % out["ytd_base_acct"]
    elif netliq is not None:
        ytd_chg = round(netliq - float(out["ytd_base_netliq"]), 2)
    out["ytd_change"] = ytd_chg
    out["ytd_note"] = ytd_note

    # MTD: MonthlyReview section A's authoritative number - the current change minus
    # the last change strictly before the month started.
    out["mtd_netliq_delta"] = None
    if ytd_chg is not None and _prior_ytd is not None:
        out["mtd_netliq_delta"] = round(ytd_chg - float(_prior_ytd), 2)
    out["mtd_realized"] = None
    if _mtd_real_prior is not None:
        out["mtd_realized"] = round(_mtd_real_prior + (day_real or 0.0), 2)

    out.update(ok=True,
               legs=len(ib.portfolio()),
               realized=round(day_real, 2) if day_real is not None else None,
               unrealized=round(day_unrl, 2) if day_unrl is not None else None,
               day_total=round((day_real or 0.0) + (day_unrl or 0.0), 2),
               net_liq=netliq,
               excess_liq=F(vals.get("ExcessLiquidity")),
               spreads=spreads,
               orphan_legs=orphans)
except Exception as e:
    out["error"] = "%s: %s" % (type(e).__name__, e)
finally:
    try: ib.disconnect()
    except Exception: pass
print(json.dumps(out))
'@ | Set-Content -Encoding ASCII $tmp
    try {
      # Last line that looks like JSON, not simply the last line: a stray warning on
      # stdout would otherwise take the whole ACCOUNT block down with it.
      #
      # ErrorActionPreference is dropped to Continue for just this call. In PS 5.1 a
      # native exe writing ANY stderr raises NativeCommandError, which under 'Stop'
      # is terminating -- so on an IB outage (ib_insync logs the refused connection to
      # stderr) we lost $pl entirely and with it the file-derived MONTH TO DATE values,
      # even though the probe had already printed perfectly good JSON to stdout.
      $eapSave = $ErrorActionPreference
      $ErrorActionPreference = 'Continue'
      try {
        $lines = @(& $py $tmp 2>$null)
      } finally {
        $ErrorActionPreference = $eapSave
      }
      $raw   = ($lines | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
      $pl = $raw | ConvertFrom-Json
    } catch { W "WARN: P/L query failed: $($_.Exception.Message)" }
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  }

  # ---------------------------------------------------------------- task health
  $failedTasks = @()
  foreach ($t in (Get-ScheduledTask -TaskPath '\' | Where-Object { $_.TaskName -like 'IB*' })) {
    $i = $t | Get-ScheduledTaskInfo
    if ($i.LastTaskResult -ne 0 -and $i.LastRunTime -gt $ny.Date) {
      $failedTasks += ("{0} -> {1}" -f $t.TaskName, $i.LastTaskResult)
    }
  }

  # ---------------------------------------------------------------- body
  $b = New-Object Collections.Generic.List[string]
  $b.Add("HyperOS daily bundle - $dateStr")
  $b.Add("")
  if ($pl -and $pl.ok) {
    $b.Add("ACCOUNT")
    $b.Add(("  Net liquidation : {0}" -f (_fmtpl $pl.net_liq)))
    $b.Add(("  Realized (day)  : {0}" -f (_fmtpl $pl.realized)))
    $b.Add(("  Unrealized (open): {0}" -f (_fmtpl $pl.unrealized)))
    $b.Add(("  Day total       : {0}" -f (_fmtpl $pl.day_total)))
    $b.Add(("  Excess liquidity: {0}" -f (_fmtpl $pl.excess_liq)))
    # Labelled for what it actually measures: the baseline in ytd_baseline.json was
    # captured mid-year, so this is "change since <that date>", not calendar YTD.
    if ($null -ne $pl.ytd_change) {
      $since = ''
      if ($pl.ytd_base_ts) { $since = ' (since ' + $pl.ytd_base_ts + ')' }
      $b.Add(("  YTD change NetLiq: {0}{1}" -f (_fmtpl $pl.ytd_change), $since))
    } else {
      $note = 'unavailable'
      if ($pl.ytd_note) { $note = $pl.ytd_note }
      $b.Add("  YTD change NetLiq: - ($note)")
    }
  } else {
    $msg = 'unavailable'
    if ($pl -and $pl.error) { $msg = $pl.error }
    $b.Add("ACCOUNT: $msg")
  }
  # ---------------------------------------------------------------- month to date
  # MonthlyReview.py section A's authoritative number: the current YTD change minus
  # the last YTD change strictly before the month started. The prior-month figure
  # comes from MonthlyReview.load_health() itself, so the email and
  # `python MonthlyReview.py --month <YY_MM>` cannot drift apart.
  #
  # $null -ne, not if($x): a real delta of exactly 0.00 is falsy in PowerShell and
  # would silently vanish on the 1st of the month.
  $b.Add("")
  $b.Add("MONTH TO DATE")
  try {
    if ($null -ne $pl.mtd_netliq_delta) {
      $b.Add(("  NetLiq delta    : {0}" -f (_fmtpl $pl.mtd_netliq_delta)))
    } else {
      $b.Add("  NetLiq delta    : - (no prior-month health report carrying a YTD line)")
    }
    if ($null -ne $pl.mtd_realized) {
      $b.Add(("  Realized (sum)  : {0}" -f (_fmtpl $pl.mtd_realized)))
    } else {
      $b.Add("  Realized (sum)  : -")
    }
    if ($null -ne $pl.mtd_prior_ytd) {
      $b.Add(("  Month opened at : YTD {0}  ({1} health-report days since)" -f (_fmtpl $pl.mtd_prior_ytd), $pl.mtd_report_days))
    }
  } catch {
    $b.Add(("  MONTH TO DATE render error: {0}" -f $_.Exception.Message))
  }

  # ---------------------------------------------------------------- open positions
  # Grouped into verticals in the probe above, keyed exactly like the health report's
  # Current Positions section so the two can be diffed. debit is per spread (IB
  # avgCost already includes the x100 multiplier); unreal is the position total,
  # which is why qty is shown.
  $b.Add("")
  try {
    $sp = $null
    if ($pl) { $sp = $pl.spreads }
    if ($pl -and $pl.ok -and $sp) {
      # @() only AFTER the -and $sp guard: @($null).Count is 1, not 0, so counting an
      # unguarded null would report a phantom row.
      $spArr = @($sp)
      $FMT = "  {0,-6} {1,-8} {2,-1} {3,-13} {4,3} {5,10} {6,10}"
      $b.Add(("OPEN POSITIONS ({0} verticals, {1} legs)" -f $spArr.Count, $pl.legs))
      $b.Add(($FMT -f 'sym','exp','R','strikes','qty','debit','unreal'))
      $tDebit = 0.0
      $tUnrl  = 0.0
      foreach ($s in $spArr) {
        $b.Add(($FMT -f $s.sym, $s.exp, $s.right, $s.strikes, $s.qty, (_fmtpl $s.debit), (_fmtpl $s.unrealized)))
        if ($null -ne $s.debit)      { $tDebit += [double]$s.debit }
        if ($null -ne $s.unrealized) { $tUnrl  += [double]$s.unrealized }
      }
      $b.Add(($FMT -f 'TOTAL','','','','', (_fmtpl $tDebit), (_fmtpl $tUnrl)))
      $b.Add("  TOTAL debit is max loss on the book - compare against Net liquidation.")
      $orph = $pl.orphan_legs
      if ($orph) {
        $b.Add("  unpaired legs (not a 1x1 vertical - check for a half-filled close):")
        foreach ($o in @($orph)) {
          $b.Add(("    {0,-6} {1,-8} {2} {3,-8} qty={4,-5} avgCost={5,9} unreal={6,9}" -f $o.sym, $o.exp, $o.right, $o.strike, $o.qty, (_fmtpl $o.avg_cost), (_fmtpl $o.unrealized)))
        }
      }
    } elseif ($pl -and $pl.ok) {
      $b.Add(("OPEN POSITIONS: none reported ({0} legs in portfolio)" -f $pl.legs))
    } else {
      $msg2 = 'unavailable'
      if ($pl -and $pl.error) { $msg2 = $pl.error }
      $b.Add("OPEN POSITIONS: $msg2")
    }
  } catch {
    $b.Add(("  OPEN POSITIONS render error: {0}" -f $_.Exception.Message))
  }

  $b.Add("")
  $b.Add("SIGNALS")
  $b.Add("  Webhooks received : $signalCount")
  $b.Add("  Priced rows       : $pricedRows")
  $b.Add("")
  $b.Add("ORDERS PLACED ($($placed.Count))")
  if ($placed.Count) { foreach ($p in $placed) { $b.Add("  $p") } } else { $b.Add("  none") }
  if ($skipReasons.Count) {
    $b.Add("")
    $b.Add("NOT PLACED")
    foreach ($k in ($skipReasons.Keys | Sort-Object)) { $b.Add(("  {0,-40} {1}" -f $k, $skipReasons[$k])) }
  }
  $b.Add("")
  if ($failedTasks.Count) {
    $b.Add("TASKS WITH NON-ZERO RESULT TODAY")
    foreach ($f in $failedTasks) { $b.Add("  $f") }
  } else {
    $b.Add("TASKS: all IB* tasks returned 0 today")
  }
  $b.Add("")
  $b.Add("Sent from $env:COMPUTERNAME via Google Workspace SMTP relay.")
  $body = ($b -join "`r`n")

  # ---------------------------------------------------------------- attachments
  # Size matters here. DailyCycle.log is ~617 MB and ib_cycle.log ~545 MB — neither is
  # rotated. The previous version of this script attached DailyCycle.log whole, so it would
  # have failed with Google's 552 "message exceeded size limits" even once it had a working
  # credential. Small files go whole; anything large is tailed to a sidecar first.
  $MAX_WHOLE  = 2MB      # attach as-is below this
  $TAIL_LINES = 2000     # otherwise ship this many trailing lines
  $TOTAL_CAP  = 20MB     # Google's ceiling is 25MB; leave room for base64 overhead

  $attached = @()
  $running  = 0
  foreach ($cand in @($combined, $attempts, 'C:\OptionsHistory\logs\DailyCycle.log')) {
    if (-not $cand -or -not (Test-Path $cand)) { continue }
    $item = Get-Item $cand
    $use  = $cand

    if ($item.Length -gt $MAX_WHOLE) {
      $tail = Join-Path 'C:\OptionsHistory\logs' ("bundle_tail_" + $item.BaseName + ".log")
      W ("{0} is {1} MB - attaching last {2} lines instead" -f $item.Name, [Math]::Round($item.Length/1MB,1), $TAIL_LINES)
      $header = "# tail of $($item.FullName) ($([Math]::Round($item.Length/1MB,1)) MB) - last $TAIL_LINES lines, $dateStr"
      $header | Set-Content -Path $tail -Encoding UTF8
      Get-Content $item.FullName -Tail $TAIL_LINES | Add-Content -Path $tail -Encoding UTF8
      $use = $tail
    }

    $sz = (Get-Item $use).Length
    if (($running + $sz) -gt $TOTAL_CAP) {
      W ("skipping {0} - would exceed the {1} MB attachment cap" -f (Split-Path $use -Leaf), ($TOTAL_CAP/1MB))
      continue
    }
    $attached += $use
    $running  += $sz
  }
  W ("attachments: {0}, total {1} KB" -f $attached.Count, [Math]::Round($running/1KB))

  if ($WhatIfNoSend) {
    W "WhatIfNoSend set - not sending. Body follows:"
    Write-Host ""
    Write-Host $body
    Write-Host ""
    W ("Would attach {0}: {1}" -f $attached.Count, ($attached -join '; '))
    return
  }

  # ---------------------------------------------------------------- send
  # Sending goes through bin\send_mail.py, NOT System.Net.Mail.SmtpClient. SmtpClient hangs
  # during STARTTLS against smtp-relay.gmail.com from this host and dies on its 100s timeout;
  # the identical raw SMTP conversation completes in well under a second from smtplib, so the
  # fault is the client. SmtpClient is also formally obsolete. Do not "simplify" this back.
  #
  # The job goes over stdin as JSON so the body and paths never pass through PowerShell's
  # native-command argument quoting.
  W ("Sending via {0}:{1} from {2} to {3} (attachments={4})" -f $SmtpServer, $SmtpPort, $From, $To, $attached.Count)

  $sender = 'C:\OptionsHistory\bin\send_mail.py'
  if (-not (Test-Path $sender)) { throw "missing $sender" }
  $sysPy = 'C:\Program Files\Python312\python.exe'
  if (-not (Test-Path $sysPy)) { $sysPy = $py }

  $job = @{
    from        = $From
    to          = $To
    subject     = "HyperOS daily bundle $dateStr"
    body        = $body
    attachments = @($attached)
  } | ConvertTo-Json -Depth 4 -Compress

  $out = $job | & $sysPy $sender 2>&1
  if ($LASTEXITCODE -ne 0) { throw ("send_mail.py failed: " + ($out -join ' | ')) }
  foreach ($line in $out) { W "  $line" }
  W "Sent."
}
catch {
  W ("ERROR: {0}" -f $_.Exception.Message)
  exit 1
}
finally {
  W "==== EmailDailyBundle end ===="
}
