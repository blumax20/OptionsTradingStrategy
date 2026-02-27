param(
    [string]$BaseUrl = "http://localhost:5001",
    [int]$TimeoutSec = 20
)

$Endpoint = "$BaseUrl/webhook_batch"

# Quick pre-check
try {
    $pre = Invoke-WebRequest -Uri ($BaseUrl + "/health") -Method GET -TimeoutSec 5 -UseBasicParsing
    Write-Host "Health pre-check: OK"
} catch {
    Write-Host "Health pre-check failed (listener may be down). Proceeding with tests..."
}

function Get-FirstSymbol($obj) {
    if ($null -ne $obj) {
        if ($obj.PSObject.Properties.Name -contains 'symbol' -and $obj.symbol) { return $obj.symbol }
        if ($obj.PSObject.Properties.Name -contains 'Symbol' -and $obj.Symbol) { return $obj.Symbol }
    }
    return "<n/a>"
}

function Test-Payload {
    param(
        [string]$Name,
        [object]$Body
    )
    Write-Host ("-> {0}" -f $Name)
    try {
        $json =
            if ($Body -is [string]) { $Body }
            else { $Body | ConvertTo-Json -Depth 6 }
        $resp = Invoke-RestMethod -Uri $Endpoint -Method POST -Body $json -ContentType "application/json" -TimeoutSec $TimeoutSec
        if ($null -ne $resp -and $resp.PSObject.Properties.Name -contains 'results' -and $resp.results) {
            $first = $resp.results | Select-Object -First 1
            $sym = Get-FirstSymbol $first
            Write-Host "   OK"
            Write-Host ("   first result symbol: {0}" -f $sym)
        } else {
            Write-Host "   Warning: no 'results' in response"
            if ($null -ne $resp) { $resp | ConvertTo-Json -Depth 6 }
        }
    }
    catch {
        Write-Host "   FAILED"
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            Write-Host ("   HTTP {0}" -f [int]$_.Exception.Response.StatusCode)
        }
        Write-Host ("   {0}" -f $_.Exception.Message)
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            Write-Host ("   Body: {0}" -f $_.ErrorDetails.Message)
        }
    }
    Write-Host ""
}

Write-Host ("Batch endpoint: {0}" -f $Endpoint)
Write-Host ""

# 1) {"tickers":[...]}
Test-Payload -Name 'tickers:list' -Body @{ tickers = @("KO","PEP","SPY") }

# 2) {"symbols":[...]}
Test-Payload -Name 'symbols:list' -Body @{ symbols = @("AAPL","MSFT","NVDA") }

# 3) {"data":[...]} mixed strings + objects
Test-Payload -Name 'data:mixed' -Body @{ data = @("INTC", @{ ticker="TSM"; text="order buy ... filled on TSM. New strategy position is 1" }) }

# 4) top-level array [...]
Test-Payload -Name 'array:top' -Body @("META","AMD","AVGO")

# 5) comma-separated string
Test-Payload -Name 'tickers:csv-string' -Body @{ tickers = "PAYX,ADP,CRM" }

# 6) {"payload":[...]} alternate key
Test-Payload -Name 'payload:list' -Body @{ payload = @("ORCL","SAP") }

Write-Host "Done."
