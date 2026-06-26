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
}
Start-Sleep -Milliseconds 500
& powershell -NoProfile -ExecutionPolicy Bypass -File .\start_serve.ps1
