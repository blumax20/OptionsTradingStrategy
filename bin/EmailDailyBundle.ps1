param(
  [string]$SmtpServer = 'smtp.office365.com',
  [int]   $SmtpPort   = 587,
  [string]$From       = 'noreply@hyperbukit.com',
  [string]$To         = 'max@hyperbukit.com',
  [string]$CredPath   = 'C:\OptionsHistory\secrets\smtp_cred.xml'
)

$LOG = 'C:\OptionsHistory\logs\EmailDailyBundle.log'
function W($m){ $ts=Get-Date -Format 'yyyy-MM-dd HH:mm:ss'; "$ts $m" | Tee-Object -FilePath $LOG -Append }

try {
  W "==== EmailDailyBundle start ===="

  if (-not (Test-Path $CredPath)) { throw "Credential missing at $CredPath" }
  $cred = Import-Clixml $CredPath

  $ny = (Get-Date).ToUniversalTime().AddHours(-4)
  $dayFolder = "C:\OptionsHistory\{0}" -f $ny.ToString('yy_MM_dd')
  if (-not (Test-Path $dayFolder)) { throw "Missing folder $dayFolder" }

  # Collect typical files for the day
  $attached = @()
  $candidates = @(
    (Join-Path $dayFolder 'combined_listener_spreads.csv'),
    'C:\OptionsHistory\logs\DailyCycle.log'
  )
  # Latest attempts CSV
  $latestAttempts = Get-ChildItem 'C:\OptionsHistory\logs' -Filter 'attempts_*.csv' -File -ErrorAction SilentlyContinue |
                    Sort-Object LastWriteTime -Desc | Select-Object -First 1
  if ($latestAttempts) { $candidates += $latestAttempts.FullName }

  foreach ($p in $candidates) { if (Test-Path $p) { $attached += $p } }

  # Quick P/L summary reusing the same inline helper as Health.ps1
  $py = "C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts\python.exe"
  $pl = $null
  if (Test-Path $py) {
    $tmp = Join-Path $env:TEMP ("pl_" + [guid]::NewGuid().ToString('N') + ".py")
    @'
from ib_insync import IB
from datetime import datetime
from zoneinfo import ZoneInfo
import json, random
ib=IB()
out={"ok":False}
try:
  ib.connect('127.0.0.1',7497,clientId=750+random.randint(0,49),timeout=5)
  acct = ib.managedAccounts()[0] if ib.managedAccounts() else None
  vals = {s.tag:s.value for s in ib.accountSummary(acct)}
  def F(x):
    try: return float(x)
    except: return None
  day_real=F(vals.get("RealizedPnL")); day_unrl=F(vals.get("UnrealizedPnL"))
  out.update(day_realized=day_real, day_unrealized=day_unrl, day_total=(day_real or 0)+(day_unrl or 0), ok=True)
finally:
  try: ib.disconnect()
  except: pass
print(json.dumps(out))
'@ | Set-Content -Encoding ASCII $tmp
    $json = & $py $tmp
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    try { $pl = $json | ConvertFrom-Json } catch {}
  }

  $subject = "Daily bundle $(Get-Date -Format 'yyyy-MM-dd')"
  $body = @()
  $body += "Daily files for $(Get-Date -Format 'yyyy-MM-dd')"
  if ($pl -and $pl.ok) {
    $body += ""
    $body += "P/L:"
    $body += ("  Realized   : {0}" -f ($pl.day_realized ?? '-'))
    $body += ("  Unrealized : {0}" -f ($pl.day_unrealized ?? '-'))
    $body += ("  Total      : {0}" -f ($pl.day_total ?? '-'))
  }
  $body = ($body -join "`r`n")

  W ("Sending via {0}:{1} to {2} (attachments={3})" -f $SmtpServer,$SmtpPort,$To,$attached.Count)
  $smtp = [System.Net.Mail.SmtpClient]::new($SmtpServer,$SmtpPort)
  $smtp.EnableSsl = $true
  $smtp.Credentials = New-Object System.Net.NetworkCredential($cred.UserName, $cred.GetNetworkCredential().Password)
  $mail = New-Object System.Net.Mail.MailMessage($From,$To,$subject,$body)

  foreach ($a in $attached) {
    try {
      $att = New-Object System.Net.Mail.Attachment($a)
      [void]$mail.Attachments.Add($att)
    } catch {
      W ("WARN: could not attach {0}: {1}" -f $a,$_.Exception.Message)
    }
  }

  $smtp.Send($mail)
  foreach ($att in $mail.Attachments) { $att.Dispose() }
  $mail.Dispose()
  W "Sent."
}
catch {
  W ("ERROR: {0}" -f $_.Exception.Message)
  exit 1
}
finally {
  W "==== EmailDailyBundle end ===="
}