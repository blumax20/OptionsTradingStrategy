<#  PushButtonTunnelDoctor.ps1 (PS 5.1 safe)
    - Verifies cloudflared config/creds for a given tunnel UUID
    - Checks Windows service, can optionally Restart it
    - Runs `cloudflared tunnel list/info`
    - Validates DNS (CNAME -> *.cfargotunnel.com) for the hostname
    - Probes local and public /health
    - Optional quick POST to /webhook_batch with a single message
#>

param(
    [string]$Hostname   = "signals.hyperbukit.com",
    [string]$TunnelId   = "a532f8e3-a13c-4dde-9e04-faf87a712053.cfargotunnel.com",
    [string]$CfgPath    = "C:\Users\Administrator\.cloudflared\config.yml",
    [string]$CredsPath  = "C:\Users\Administrator\.cloudflared\a532f8e3-a13c-4dde-9e04-faf87a712053.json",
    [string]$ExePath    = "C:\Users\Administrator\cloudflared.exe",  # adjust if installed elsewhere
    [switch]$Restart
)

$ErrorActionPreference = "Continue"

function Sect($t){ Write-Host "`n==== $t ====" }
function Ok($t){ Write-Host ("  OK  "+$t) -ForegroundColor Green }
function Warn($t){ Write-Host ("  WARN "+$t) -ForegroundColor Yellow }
function Err($t){ Write-Host ("  ERR "+$t) -ForegroundColor Red }

$exitCode = 0

Sect "Config & Files"
if (Test-Path $CfgPath) { Ok "config.yml: $CfgPath" } else { Err "Missing config: $CfgPath"; $exitCode = 1 }
if (Test-Path $CredsPath) { Ok "creds json: $CredsPath" } else { Err "Missing tunnel creds: $CredsPath"; $exitCode = 1 }
if (Test-Path $ExePath) { Ok "cloudflared.exe: $ExePath" } else { Err "cloudflared.exe not found: $ExePath"; $exitCode = 1 }

Sect "Windows Service"
$svc = Get-Service | Where-Object { $_.Name -match 'cloudflar|Cloudflare.*Tunnel|Argo.*Tunnel' }
if ($svc) {
    $svc | Select-Object Status,Name,DisplayName | Format-Table -AutoSize
    if ($Restart) {
        Write-Host "Restarting service: $($svc.Name) ..."
        try { Restart-Service -Name $svc.Name -Force -ErrorAction Stop; Ok "Service restarted" }
        catch { Err "Restart failed: $($_.Exception.Message)"; $exitCode = 1 }
    }
} else {
    Warn "No cloudflared service found. You can run in foreground:"
    Write-Host "    `"$ExePath`" --config `"$CfgPath`" tunnel run $TunnelId"
}

Sect "cloudflared tunnel list/info"
try {
    & $ExePath tunnel list 2>&1 | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
    & $ExePath tunnel info $TunnelId 2>&1 | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
} catch {
    Warn "cloudflared CLI calls failed: $($_.Exception.Message)"
    $exitCode = 1
}

Sect "DNS CNAME for $Hostname"
try {
    $cname = Resolve-DnsName -Name $Hostname -Type CNAME -ErrorAction Stop
    $target = ($cname | Select-Object -First 1).NameHost
    if ($target -and $target.ToLower().Contains("cfargotunnel.com")) {
        Ok ("CNAME -> "+$target)
    } else {
        Warn ("CNAME exists but not cfargotunnel.com: "+$target)
        $exitCode = 1
    }
} catch {
    Warn "No CNAME found; checking A/AAAA instead"
    try {
        $arec = Resolve-DnsName -Name $Hostname -Type A -ErrorAction Stop
        ($arec | Select-Object -First 3) | Format-Table -AutoSize | Out-String | Write-Host
        Warn "Host uses A/AAAA instead of CNAME; ensure Cloudflare DNS is proxied orange-cloud and matches your tunnel's DNS target."
        $exitCode = 1
    } catch {
        Err "DNS lookup failed: $($_.Exception.Message)"
        $exitCode = 1
    }
}

Sect "TCP 443 connectivity"
try {
    $tcp = Test-NetConnection -ComputerName $Hostname -Port 443 -InformationLevel Detailed
    Write-Host ("Ping={0} Tcp={1} Remote={2}" -f $tcp.PingSucceeded, $tcp.TcpTestSucceeded, $tcp.RemoteAddress)
    if (-not $tcp.TcpTestSucceeded) { $exitCode = 1 }
} catch {
    Err "tcp443 test failed: $($_.Exception.Message)"; $exitCode = 1
}

Sect "Local listener (/health)"
try {
    $loc = Invoke-WebRequest -UseBasicParsing -TimeoutSec 6 -Uri "http://127.0.0.1:5001/health"
    Ok ("local /health: "+$loc.StatusCode)
} catch {
    Err "local /health failed: $($_.Exception.Message)"; $exitCode = 1
}

Sect "Public listener (/health)"
try {
    $pub = Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 -Uri ("https://{0}/health" -f $Hostname)
    Ok ("public /health: "+$pub.StatusCode)
} catch {
    Warn "public /health failed: $($_.Exception.Message)"
    $exitCode = 1
}

Sect "Public /webhook_batch smoke"
try {
    $payload = @{ data = @(@{ ticker="PAYX"; message="order buy ... filled on PAYX. New strategy position is 1"}) } | ConvertTo-Json -Depth 5
    $resp = Invoke-RestMethod -Uri ("https://{0}/webhook_batch" -f $Hostname) -Method POST -Body $payload -ContentType "application/json" -TimeoutSec 8
    if ($resp -and $resp.results) { Ok "public /webhook_batch OK (received results)" }
    else { Warn "public /webhook_batch: no 'results' in response"; $exitCode = 1 }
} catch {
    Warn "public /webhook_batch failed: $($_.Exception.Message)"
    $exitCode = 1
}

Write-Host "`n==== Summary ===="
if ($exitCode -eq 0) { Ok "Tunnel looks healthy and routing appears good." }
else { Err "Issues detected. Review sections above (DNS CNAME, service, public probes)." }

exit $exitCode
