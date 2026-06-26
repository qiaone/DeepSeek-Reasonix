Start-Sleep -Seconds 2
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/status' -TimeoutSec 5
    Write-Host "STATUS_CODE=$($r.StatusCode)"
    Write-Host '---BODY---'
    Write-Host $r.Content
} catch {
    Write-Host 'PROBE_FAILED:'
    Write-Host $_.Exception.Message
}
Write-Host '---serve.err.log---'
if (Test-Path .\serve.err.log) { Get-Content .\serve.err.log } else { Write-Host '<no err log>' }
Write-Host '---serve.log---'
if (Test-Path .\serve.log) { Get-Content .\serve.log } else { Write-Host '<no log>' }
