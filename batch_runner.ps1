# Runs many repos through the Forko worker, one after another, and saves a results table.
#
# Usage (PowerShell, from C:\forko-worker, worker running in another window):
#   .\batch_runner.ps1 -ListFile repos.txt
#
# repos.txt: one GitHub URL per line. Blank lines and lines starting with # are ignored.
# Results are appended to results.csv after EVERY repo, so nothing is lost if you stop.
# Re-running skips repos already in results.csv, so you can resume.
# If PowerShell blocks the script, run once in this window:
#   Set-ExecutionPolicy -Scope Process Bypass
param(
    [string]$ListFile = "repos.txt",
    [string]$OutFile = "results.csv",
    [string]$OS = "linux",
    [string]$Api = "http://localhost:8000",
    [int]$TimeoutMin = 20
)

function Get-Cause([string]$step, [string]$log) {
    if ($log -match 'Timed out after') { return "timeout" }
    if ($log -match 'No space left on device|Cannot allocate memory|MemoryError|\bKilled\b') { return "resource_limit" }
    if ($log -match 'network connectivity|ReadTimeoutError|Connection (reset|broken|aborted)|Temporary failure in name resolution|IncompleteRead') { return "network_error" }
    if ($step -eq "Clone repository") { return "clone_failed" }
    if ($step -eq "Run tests") {
        if ($log -match 'Name or service not known|Connection refused|could not connect to server|OperationalError|ConnectionError') { return "needs_external_service" }
        if ($log -match 'ModuleNotFoundError|ImportError|Cannot find module') { return "tests_missing_dev_dependency" }
        return "repo_tests_failed"
    }
    if ($step -eq "Build") { return "build_failed" }
    if ($log -match 'No matching distribution found|Could not find a version that satisfies|ETARGET|notarget') { return "version_not_found" }
    if ($log -match 'pg_config|gcc|cc1|Failed building wheel|could not build wheels|error: command|gyp ERR|make: not found|fatal error: .*\.h') { return "needs_system_build_tools" }
    if ($log -match 'Failed to build|getting requirements to build wheel') { return "package_build_failed" }
    if ($log -match 'requires a different Python|Requires-Python|python_requires') { return "python_version" }
    if ($log -match 'ERESOLVE') { return "npm_peer_conflict" }
    if ($log -match 'ModuleNotFoundError|ImportError|Cannot find module') { return "missing_module" }
    if ($log -match 'E404|404 Not Found') { return "package_not_found" }
    return "other"
}

function Get-Tail([string]$log) {
    if (-not $log) { return "" }
    $lines = $log -split "`n" | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 3
    $t = ($lines -join " | ") -replace "[\r\n]+", " "
    if ($t.Length -gt 300) { $t = $t.Substring(0, 300) }
    return $t
}

function Save-Row($url, $status, $step, $cause, $fixes, $seconds, $message, $tail) {
    $row = [pscustomobject]@{
        repo = $url; build_id = $script:currentId; os = $OS; status = $status; failed_step = $step; cause = $cause
        fixes_applied = $fixes; seconds = $seconds; message = $message; error_tail = $tail
    }
    $row | Export-Csv -Path $OutFile -Append -NoTypeInformation -Encoding UTF8
    Write-Host ("  -> {0} {1} {2}" -f $status, $step, $cause)
}

if (-not (Test-Path $ListFile)) { Write-Host "List file not found: $ListFile"; return }
$urls = Get-Content $ListFile | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" -and -not $_.StartsWith("#") }
$done = @{}
if (Test-Path $OutFile) { Import-Csv $OutFile | ForEach-Object { $done[$_.repo] = $true } }
$final = @("verified", "partially_verified", "failed")
$i = 0

foreach ($url in $urls) {
    $i++
    if ($done.ContainsKey($url)) { Write-Host "[$i/$($urls.Count)] skip (already done): $url"; continue }
    Write-Host "[$i/$($urls.Count)] $url"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $script:currentId = ""

    $id = $null
    for ($attempt = 1; $attempt -le 3 -and -not $id; $attempt++) {
        try {
            $body = @{ repo_url = $url; os = $OS } | ConvertTo-Json
            $id = (Invoke-RestMethod -Method Post -Uri "$Api/api/builds" -ContentType "application/json" -Body $body).build_id
        } catch {
            $code = 0
            if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
            $detail = $_.Exception.Message
            if ($_.ErrorDetails.Message) { try { $detail = ($_.ErrorDetails.Message | ConvertFrom-Json).detail } catch { $detail = $_.ErrorDetails.Message } }
            if ($code -eq 429 -or $code -eq 503) { Write-Host "  waiting 60s ($detail)"; Start-Sleep -Seconds 60; continue }
            $st = "rejected"
            if ($code -eq 404) { $st = "not_found" }
            if ($code -eq 422) { $st = "unsupported" }
            Save-Row $url $st "" $st "" ([int]$sw.Elapsed.TotalSeconds) $detail ""
            break
        }
    }
    if ($id) { $script:currentId = $id }
    if (-not $id) {
        if (-not (Test-Path $OutFile) -or -not ((Import-Csv $OutFile | Where-Object { $_.repo -eq $url }))) {
            Save-Row $url "rate_limited" "" "rate_limited" "" ([int]$sw.Elapsed.TotalSeconds) "Could not start the build" ""
        }
        continue
    }

    do { Start-Sleep -Seconds 5; $r = Invoke-RestMethod "$Api/api/builds/$id" }
    while (($final -notcontains $r.status) -and ($sw.Elapsed.TotalMinutes -lt $TimeoutMin))

    $fixIds = ""
    if ($r.fixes) { $fixIds = (($r.fixes | ForEach-Object { "{0}(worked={1},cleared={2})" -f $_.id, $_.worked, $_.cleared }) -join ";") }
    $step = ""; $cause = ""; $tail = ""
    if ($final -notcontains $r.status) {
        Save-Row $url "timeout_waiting" "" "timeout" $fixIds ([int]$sw.Elapsed.TotalSeconds) "Build did not finish in $TimeoutMin minutes" ""
        continue
    }
    if ($r.status -eq "failed") {
        $f = $r.steps | Where-Object { $_.status -eq "failed" } | Select-Object -First 1
        if ($f) { $step = $f.name; $cause = Get-Cause $f.name $f.log; $tail = Get-Tail $f.log }
        else { $cause = "worker_error" }
    }
    Save-Row $url $r.status $step $cause $fixIds ([int]$sw.Elapsed.TotalSeconds) $r.message $tail
}

Write-Host ""
Write-Host "=== Summary from $OutFile ==="
$all = @(Import-Csv $OutFile)
Write-Host "Repos: $($all.Count)"
$all | Group-Object status | Sort-Object Count -Descending | ForEach-Object { Write-Host (" {0,3}  {1}" -f $_.Count, $_.Name) }
Write-Host "Causes of failure:"
$all | Where-Object { $_.cause -ne "" } | Group-Object cause | Sort-Object Count -Descending | ForEach-Object { Write-Host (" {0,3}  {1}" -f $_.Count, $_.Name) }
$withFix = @($all | Where-Object { $_.fixes_applied -ne "" }).Count
Write-Host "Repos where a fix was applied: $withFix"