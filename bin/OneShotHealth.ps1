param(
  [string]$PublicHost = $env:CF_PUBLIC_HOST,  # your public tunnel host
  [int]$TimeoutSec = 8
)
if (-not $PublicHost) { $PublicHost = 'signals.hyperbukit.com' }

Write-Host "Running one-shot health…"

# --- Local listener (/health) ---
$localOk = $false; $localMsg = ""
try {
  $resp = Invoke-RestMethod -Uri "http://127.0.0.1:5001/health" -TimeoutSec $TimeoutSec -Method GET
  $localOk = $true
  $localMsg = "version=$($resp.version) csv=$($resp.combined_csv_path)"
} catch { $localMsg = "ERROR: $($_.Exception.Message)" }

# --- Public listener via tunnel (/health) ---
$pubOk = $false; $pubMsg = ""
try {
  $ph = Invoke-WebRequest -Uri ("https://{0}/health" -f $PublicHost) -TimeoutSec $TimeoutSec -UseBasicParsing
  $pubOk = ($ph.StatusCode -eq 200)
  $pubMsg = "status=$($ph.StatusCode)"
} catch { $pubMsg = "ERROR: $($_.Exception.Message)" }

# --- Gateway ports ---
$gwPaper = $false; $gwLive = $false; $gwOwner = ""
try {
  $paper = netstat -ano | Select-String ":7497"
  $gwPaper = [bool]$paper
  $live  = netstat -ano | Select-String ":7496"
  $gwLive = [bool]$live
  if ($paper) {
    $pid = ($paper -split '\s+')[-1]
    $gwOwner = (Get-CimInstance Win32_Process -Filter "ProcessId=$pid").CommandLine
  }
} catch {}

# --- mdtest (dry API path) ---
$mdOk = $false; $mdMsg = ""
try {
  $md = Invoke-WebRequest -UseBasicParsing -TimeoutSec $TimeoutSec "http://127.0.0.1:5001/mdtest?symbol=SPY&mdtype=4&timeout_ms=1500"
  $mdOk = ($md.StatusCode -eq 200)
  $mdMsg = $md.Content
} catch { $mdMsg = "ERROR: $($_.Exception.Message)" }

# --- Today’s CSV presence ---
$ny     = (Get-Date).ToUniversalTime().AddHours(-4)
$dated  = $ny.ToString('yy_MM_dd')
$csv    = "C:\OptionsHistory\$dated\combined_listener_spreads.csv"
$csvOk  = Test-Path $csv
$csvRows= 0
if ($csvOk) { try { $csvRows = (Import-Csv $csv).Count } catch {} }

# --- Summary ---
$summary = @(
  "Local:    " + ($(if($localOk){'UP'})else{'DOWN'}) + "  " + $localMsg
  "Public:   " + ($(if($pubOk){'UP'})else{'DOWN'})  + "  " + $pubMsg
  "Gateway:  " + ($(if($gwPaper -or $gwLive){'UP'})else{'DOWN'}) + "  paper=$gwPaper live=$gwLive"
  (if ($gwOwner) { "         owner: $gwOwner" } else { "" })
  "mdtest:   " + ($(if($mdOk){'OK'})else{'FAIL'})) + "  " + $mdMsg
  "CSV:      " + ($(if($csvOk){'OK'})else{'MISSING'})) + ($(if($csvOk){" rows=$csvRows path=$csv"}else{""}))
) -join "`r`n"

Write-Host "---------------------------------------------"
Write-Host ($summary)
Write-Host "---------------------------------------------"

# Write a small report next to the script
$Report = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "OneShotHealth.report.txt"
$reportBody = @(
  "When:  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
  "Host:  $PublicHost"
  $summary
) -join "`r`n"
$reportBody | Set-Content -Path $Report -Encoding ASCII
Write-Host "Wrote OneShotHealth report: $Report"

# Exit codes (optional; keep simple)
if (-not $localOk -or -not ($gwPaper -or $gwLive)) { exit 2 }
exit 0