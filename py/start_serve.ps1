if (Test-Path .\serve.log)     { Remove-Item .\serve.log }
if (Test-Path .\serve.err.log) { Remove-Item .\serve.err.log }
$p = Start-Process `
    -FilePath 'C:\Users\Admin\miniforge3\envs\py312\python.exe' `
    -ArgumentList '-m','reasonix_py.serve','--host','127.0.0.1','--port','8765' `
    -WorkingDirectory (Get-Location).Path `
    -RedirectStandardOutput .\serve.log `
    -RedirectStandardError  .\serve.err.log `
    -PassThru `
    -WindowStyle Hidden
$p.Id | Out-File -Encoding ascii .\serve.pid
Write-Host "PID=$($p.Id)"
