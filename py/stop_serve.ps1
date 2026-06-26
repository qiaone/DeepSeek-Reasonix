if (Test-Path .\serve.pid) {
    $pidVal = (Get-Content .\serve.pid | Select-Object -First 1).Trim()
    if ($pidVal) {
        try {
            Stop-Process -Id $pidVal -Force -ErrorAction Stop
            Write-Host "stopped PID=$pidVal"
        } catch {
            Write-Host "stop failed (maybe already gone): $($_.Exception.Message)"
        }
    }
    Remove-Item .\serve.pid -ErrorAction SilentlyContinue
} else {
    Write-Host "no serve.pid, nothing to stop"
}
Start-Sleep -Milliseconds 300
try {
    $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/status' -TimeoutSec 2
    Write-Host "WARN: still alive, STATUS_CODE=$($r.StatusCode)"
} catch {
    Write-Host "verified: port 8765 no longer responding"
}
