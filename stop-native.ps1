$ErrorActionPreference='Stop'
$pidFile=Join-Path $PSScriptRoot '.local\webui\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host '本工程没有记录运行中的控制台。'; exit 0 }
$serverId=[int](Get-Content -LiteralPath $pidFile -Raw)
$server=Get-CimInstance Win32_Process -Filter "ProcessId=$serverId"
$consolePath=Join-Path $PSScriptRoot 'z62_webui\console.py'
if ($server -and $server.CommandLine.Contains($consolePath)) {
    Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8642/api/stop' -ContentType application/json -Body '{}' | Out-Null
    Stop-Process -Id $serverId
}
Remove-Item -LiteralPath $pidFile -Force
Write-Host '本工程的模拟器与控制台已停止。'
