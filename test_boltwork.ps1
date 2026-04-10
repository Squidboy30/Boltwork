# Boltwork Full System Test
# Run: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#      .\test_boltwork.ps1

$API  = "https://parsebit.fly.dev"
$L402 = "https://parsebit-lnd.fly.dev"
$pass = 0
$fail = 0
$results = @()

function Test-Endpoint {
    param(
        [string]$Label,
        [string]$Url,
        [string]$Method = "GET",
        [string]$Body = "",
        [int[]]$Expect = @(200)
    )
    try {
        $params = @{
            Uri             = $Url
            Method          = $Method
            UseBasicParsing = $true
            ErrorAction     = "Stop"
            TimeoutSec      = 60
        }
        if ($Body -ne "") {
            $params.Body        = $Body
            $params.ContentType = "application/json"
        }
        $r      = Invoke-WebRequest @params
        $status = [int]$r.StatusCode
    } catch [System.Net.WebException] {
        $status = [int]$_.Exception.Response.StatusCode
    } catch {
        $status = 0
    }
    $ok     = $Expect -contains $status
    $icon   = if ($ok) { "PASS" } else { "FAIL" }
    $colour = if ($ok) { "Green" } else { "Red" }
    $script:pass += if ($ok) { 1 } else { 0 }
    $script:fail += if ($ok) { 0 } else { 1 }
    $script:results += [PSCustomObject]@{
        Result   = $icon
        Label    = $Label
        Status   = $status
        Expected = ($Expect -join "/")
    }
    Write-Host ("  [{0}] {1,-46} HTTP {2}" -f $icon, $Label, $status) -ForegroundColor $colour
}

Write-Host ""
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "  Boltwork System Test  $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan

# --- Free / info endpoints --------------------------------------------------
Write-Host "`n[ Direct API - Free endpoints ]" -ForegroundColor Yellow
Test-Endpoint "GET /health"                "$API/health"
Test-Endpoint "GET /agent-spec.md"         "$API/agent-spec.md"
Test-Endpoint "GET /.well-known/l402.json" "$API/.well-known/l402.json"
Test-Endpoint "GET /.well-known/mcp.json"  "$API/.well-known/mcp.json"
Test-Endpoint "GET /llms.txt"              "$API/llms.txt"
Test-Endpoint "GET /trial/info"            "$API/trial/info"
Test-Endpoint "GET /memory/info"           "$API/memory/info"
Test-Endpoint "GET /workflow/info"         "$API/workflow/info"

# --- Route presence ---------------------------------------------------------
Write-Host "`n[ Direct API - Route presence, empty body expects 422 or 400 ]" -ForegroundColor Yellow
Test-Endpoint "POST /summarise/url"   "$API/summarise/url"   POST "{}" @(422)
Test-Endpoint "POST /review/code"     "$API/review/code"     POST "{}" @(422)
Test-Endpoint "POST /review/url"      "$API/review/url"      POST "{}" @(422)
Test-Endpoint "POST /extract/webpage" "$API/extract/webpage" POST "{}" @(422)
Test-Endpoint "POST /extract/data"    "$API/extract/data"    POST "{}" @(422)
Test-Endpoint "POST /translate"       "$API/translate"       POST "{}" @(422)
Test-Endpoint "POST /analyse/tables"  "$API/analyse/tables"  POST "{}" @(422)
Test-Endpoint "POST /analyse/compare" "$API/analyse/compare" POST "{}" @(422)
Test-Endpoint "POST /analyse/explain" "$API/analyse/explain" POST "{}" @(400,422)
Test-Endpoint "POST /memory/store"    "$API/memory/store"    POST "{}" @(422)
Test-Endpoint "POST /memory/retrieve" "$API/memory/retrieve" POST "{}" @(422)
Test-Endpoint "POST /memory/delete"   "$API/memory/delete"   POST "{}" @(422)
Test-Endpoint "POST /trial/review"    "$API/trial/review"    POST "{}" @(422)
Test-Endpoint "POST /trial/summarise" "$API/trial/summarise" POST "{}" @(422)
Test-Endpoint "POST /workflow/run"    "$API/workflow/run"    POST "{}" @(422)

# --- Real Claude calls ------------------------------------------------------
Write-Host "`n[ Direct API - Real calls, may take 15-30s each ]" -ForegroundColor Yellow
Test-Endpoint "POST /trial/review real"    "$API/trial/review"    POST '{"code":"def add(a,b): return a+b"}' @(200)
Test-Endpoint "POST /trial/summarise real" "$API/trial/summarise" POST '{"text":"Bitcoin is a decentralised digital currency."}' @(200)
Test-Endpoint "POST /memory/store real"    "$API/memory/store"    POST '{"agent_id":"syscheck","entries":{"ping":"pong"}}' @(200)
Test-Endpoint "POST /memory/retrieve real" "$API/memory/retrieve" POST '{"agent_id":"syscheck"}' @(200)
Test-Endpoint "POST /memory/delete real"   "$API/memory/delete"   POST '{"agent_id":"syscheck","key":"ping"}' @(200)
Test-Endpoint "POST /workflow/run 1-step"  "$API/workflow/run"    POST '{"label":"t1","steps":[{"service":"translate","input":{"text":"Hello world","target_language":"french"}}]}' @(200)
Test-Endpoint "POST /workflow/run 2-step"  "$API/workflow/run"    POST '{"label":"t2","steps":[{"service":"translate","input":{"text":"The Bitcoin network is a peer to peer payment system.","target_language":"french"}},{"service":"translate","input":{"text":{"$from":0},"target_language":"spanish"}}]}' @(200)

# --- Lightning gate paid ----------------------------------------------------
Write-Host "`n[ Lightning gate - Paid endpoints expect 402 ]" -ForegroundColor Yellow
Test-Endpoint "L402 /summarise/upload"  "$L402/summarise/upload"  POST "{}" @(402)
Test-Endpoint "L402 /summarise/url"     "$L402/summarise/url"     POST '{"url":"https://example.com/t.pdf"}' @(402)
Test-Endpoint "L402 /review/code"       "$L402/review/code"       POST '{"code":"def hello(): pass"}' @(402)
Test-Endpoint "L402 /review/url"        "$L402/review/url"        POST '{"url":"https://github.com/Squidboy30/Boltwork/blob/main/main.py"}' @(402)
Test-Endpoint "L402 /extract/webpage"   "$L402/extract/webpage"   POST '{"url":"https://example.com"}' @(402)
Test-Endpoint "L402 /extract/data"      "$L402/extract/data"      POST '{"url":"https://example.com/t.pdf"}' @(402)
Test-Endpoint "L402 /translate"         "$L402/translate"         POST '{"text":"hello","target_language":"spanish"}' @(402)
Test-Endpoint "L402 /analyse/tables"    "$L402/analyse/tables"    POST '{"url":"https://example.com/t.pdf"}' @(402)
Test-Endpoint "L402 /analyse/compare"   "$L402/analyse/compare"   POST '{"url_a":"https://a.pdf","url_b":"https://b.pdf"}' @(402)
Test-Endpoint "L402 /analyse/explain"   "$L402/analyse/explain"   POST '{"code":"def hello(): pass"}' @(402)
Test-Endpoint "L402 /memory/store"      "$L402/memory/store"      POST '{"agent_id":"gate","entries":{"ping":"pong"}}' @(402)
Test-Endpoint "L402 /memory/retrieve"   "$L402/memory/retrieve"   POST '{"agent_id":"gate"}' @(402)
Test-Endpoint "L402 /workflow/run"      "$L402/workflow/run"      POST '{"steps":[{"service":"translate","input":{"text":"hello","target_language":"french"}}]}' @(402)

# --- Lightning gate free pass-through ---------------------------------------
Write-Host "`n[ Lightning gate - Free pass-through expect 200 ]" -ForegroundColor Yellow
Test-Endpoint "L402 /health free"        "$L402/health"
Test-Endpoint "L402 /trial/info free"    "$L402/trial/info"
Test-Endpoint "L402 /memory/info free"   "$L402/memory/info"
Test-Endpoint "L402 /workflow/info free" "$L402/workflow/info"

# --- Summary ----------------------------------------------------------------
Write-Host "`n========================================"  -ForegroundColor Cyan
$c = if ($fail -eq 0) { "Green" } else { "Red" }
Write-Host ("  PASSED: {0}   FAILED: {1}   TOTAL: {2}" -f $pass, $fail, ($pass + $fail)) -ForegroundColor $c
Write-Host ""
