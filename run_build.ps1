# Usage (PowerShell, from C:\forko-worker, with the worker running):
#   .\run_build.ps1 https://github.com/owner/repo
#   .\run_build.ps1 https://github.com/owner/repo -OS linux
#   .\run_build.ps1 https://github.com/owner/repo -Api https://your.server.name -ApiKey YOUR_KEY
# If PowerShell blocks the script, run once in this window:
#   Set-ExecutionPolicy -Scope Process Bypass
param(
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [string]$OS = "windows",
    [string]$Api = "http://localhost:8000",
    [string]$ApiKey = ""
)

$headers = @{}
if ($ApiKey) { $headers["X-API-Key"] = $ApiKey }

$body = @{ repo_url = $RepoUrl; os = $OS } | ConvertTo-Json
try {
    $start = Invoke-RestMethod -Method Post -Uri "$Api/api/builds" -ContentType "application/json" -Body $body -Headers $headers
} catch {
    Write-Host "Could not start the build:"
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message } else { Write-Host $_.Exception.Message }
    return
}

$id = $start.build_id
Write-Host "Build started: $id"
$final = @("verified", "partially_verified", "failed")
$sw = [Diagnostics.Stopwatch]::StartNew()
do {
    Start-Sleep -Seconds 10
    $r = Invoke-RestMethod "$Api/api/builds/$id" -Headers $headers
    Write-Host ("[{0:n0}s] status: {1}" -f $sw.Elapsed.TotalSeconds, $r.status)
} while (($final -notcontains $r.status) -and ($sw.Elapsed.TotalSeconds -lt 1500))

Write-Host ""
Write-Host "Status:  $($r.status)"
Write-Host "Message: $($r.message)"
if ($r.note) { Write-Host "Note:    $($r.note)" }
Write-Host ""
if ($r.fixes.Count -gt 0) {
    Write-Host "Fixes:"
    $r.fixes | ForEach-Object { Write-Host (" - {0} -> {1} (worked: {2}, original error gone: {3})" -f $_.what_broke, $_.what_changed, $_.worked, $_.cleared) }
    Write-Host ""
} else {
    Write-Host "Fixes: none needed or none matched"
    Write-Host ""
}
$r.steps | Select-Object name, status, duration_s | Format-Table
$failed = $r.steps | Where-Object { $_.status -eq "failed" }
if ($failed) {
    Write-Host "Log of the failed step ($($failed.name)):"
    Write-Host $failed.log
}
$file = "build-$id.json"
$r | ConvertTo-Json -Depth 8 | Set-Content $file -Encoding UTF8
Write-Host "Full result saved to $file"
