# DailyHealthCheck.ps1
$IB_PORT = 7496  # Fix DI: updated by switch_trading_mode.py (7497=paper, 7496=live)
$py = "C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
$root="C:\Users\Administrator\code\OptionsTradingStrategy"
$logDir="C:\OptionsHistory\logs"

$now = Get-Date
$stamp = $now.ToString("yyyyMMdd_HHmmss")
$report = Join-Path $logDir "health_$stamp.txt"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

"==== IB HEALTH CHECK $($now) ====" | Tee-Object -FilePath $report

# Services
"--- Services ---"        | Tee-Object -FilePath $report -Append
"IBGateway  : $((Get-Service IBGateway   -ErrorAction SilentlyContinue).Status)"   | Tee-Object -FilePath $report -Append
"IB_Listener: $((Get-Service IB_Listener -ErrorAction SilentlyContinue).Status)"   | Tee-Object -FilePath $report -Append

# Listening ports
"--- Listening Ports ---" | Tee-Object -FilePath $report -Append
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in $IB_PORT,5001 } |
  Format-Table -AutoSize LocalAddress,LocalPort,OwningProcess |
  Out-String | Tee-Object -FilePath $report -Append

# Listener probes
"--- Listener Probes ---" | Tee-Object -FilePath $report -Append
try {
  $h = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:5001/health
  "health: $($h.StatusCode) $($h.Content)" | Tee-Object -FilePath $report -Append
} catch { "health: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $report -Append }

try {
  $md = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:5001/mdtest?symbol=SPY&mdtype=4&timeout_ms=1200"
  "mdtest:  $($md.StatusCode) $($md.Content)" | Tee-Object -FilePath $report -Append
} catch { "mdtest:  ERROR $($_.Exception.Message)" | Tee-Object -FilePath $report -Append }

# Current positions
"--- Current Positions ---" | Tee-Object -FilePath $report -Append
$tmpPy = Join-Path $env:TEMP "ib_positions_$stamp.py"
$pyBlock = @"
from ib_insync import IB
ib = IB()
ok = ib.connect('127.0.0.1', $IB_PORT, clientId=888)
print("connected:", ok)
if ok:
    for p in ib.positions():
        c = p.contract
        sym = getattr(c, 'symbol', '')
        sec = getattr(c, 'secType', '')
        exp = getattr(c, 'lastTradeDateOrContractMonth', '')
        right = getattr(c, 'right', '')
        strike = getattr(c, 'strike', '')
        print(f"{sym} {sec} {exp} {right}{strike} qty={p.position} avg={p.avgCost}")
    ib.disconnect()
"@
$pyBlock | Set-Content -Encoding ASCII $tmpPy
try {
  & $py $tmpPy | Tee-Object -FilePath $report -Append | Out-Null
} catch {
  "positions: ERROR $($_.Exception.Message)" | Tee-Object -FilePath $report -Append
}

# Recent log tails
"--- Recent Logs ---" | Tee-Object -FilePath $report -Append
Get-ChildItem "C:\OptionsHistory\logs\*.log" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Desc | Select-Object -First 3 |
  ForEach-Object {
    "`n### $($_.FullName) (last 60 lines)" | Tee-Object -FilePath $report -Append
    Get-Content $_.FullName -Tail 60 | Tee-Object -FilePath $report -Append | Out-Null
  }

"==== END ====" | Tee-Object -FilePath $report -Append

# Optional email (fill in and uncomment):
# $smtp='smtp.yourhost.com'; $to='you@domain.com'; $from='noreply@domain.com'
# Send-MailMessage -To $to -From $from -SmtpServer $smtp -Subject "IB Health $stamp" -Body "See attached." -Attachments $report
