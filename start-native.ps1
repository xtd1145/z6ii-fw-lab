param([switch]$NoBrowser)
$ErrorActionPreference='Stop'
$consolePath=Join-Path $PSScriptRoot 'z62_webui\console.py'
$logs=Join-Path $PSScriptRoot '.local\webui'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$url='http://127.0.0.1:8642'
$ready=$false
try { $state=Invoke-RestMethod ($url+'/api/state') -TimeoutSec 2; $ready=$null -ne $state.inputs } catch {}
if (-not $ready) {
    $python=(Get-Command python.exe -ErrorAction Stop).Source
    $process=Start-Process -FilePath $python -ArgumentList @('-X','utf8',('"'+$consolePath+'"'),'--port','8642') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'server.out.log') -RedirectStandardError (Join-Path $logs 'server.err.log') -PassThru
    $process.Id | Set-Content -LiteralPath (Join-Path $logs 'server.pid') -Encoding ascii
    for ($i=0;$i -lt 40;$i++) {
        Start-Sleep -Milliseconds 250
        try { $state=Invoke-RestMethod ($url+'/api/state') -TimeoutSec 2; $ready=$null -ne $state.inputs; if ($ready) { break } } catch {}
        if ($process.HasExited) { throw '控制台未启动，请查看 .local\webui\server.err.log' }
    }
}
if (-not $ready) { throw '控制台启动超时，请查看 .local\webui 中的日志。' }
Write-Host ('操作页面：'+$url)
if (-not $NoBrowser) {
    $ego=Get-Command ego-browser -ErrorAction Stop
    $module=([uri](Join-Path $PSScriptRoot 'open-egolite.mjs')).AbsoluteUri
    & $ego.Source nodejs -e "globalThis.z62ConsoleUrl='$url'; await import('$module')"
}
