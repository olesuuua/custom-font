[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$ReplaceProjectServer
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$healthUrl = "http://127.0.0.1:$Port/api/bold/health"

function Get-QaHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
    } catch {
        return $null
    }
}

function Test-LegacyProjectServer {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/bold/report" -TimeoutSec 2 -UseBasicParsing
        $remote = $response.Content | ConvertFrom-Json
        $local = Get-Content (Join-Path $projectRoot 'qa\assets\bold-report.json') -Raw | ConvertFrom-Json
        return ($response.Headers['Server'] -like 'OlesuasQA/*' -and
            $remote.meta.bold_sfd_hash -eq $local.bold_sfd_hash -and
            $remote.meta.regular_sfd_hash -eq $local.regular_sfd_hash)
    } catch {
        return $false
    }
}

$health = Get-QaHealth
if ($health) {
    if ($health.project -ne 'olesuas-hand-bold-qa') {
        throw "Port $Port is serving another application. Nothing was stopped."
    }
    if ($health.server_version -eq 'OlesuasQA/4.1' -and $health.metrics_version -eq 'bold-metrics-v4.1') {
        Write-Host "Bold QA v4.1 is already running at http://127.0.0.1:$Port/"
        exit 0
    }
    if (-not $ReplaceProjectServer) {
        throw "An older Olesuas QA server is using port $Port. Re-run with -ReplaceProjectServer to replace this verified project server."
    }
    $listener = netstat -ano | Select-String "127.0.0.1:$Port\s+.*LISTENING"
    if (-not $listener) { throw "The verified QA server responded, but its listener could not be identified." }
    $listenerPid = [int](($listener.Line -split '\s+')[-1])
    $process = Get-Process -Id $listenerPid
    if ($process.ProcessName -notlike 'python*') {
        throw "The verified QA listener is not a Python process. Nothing was stopped."
    }
    Stop-Process -Id $listenerPid
    Start-Sleep -Milliseconds 400
} elseif ((netstat -ano | Select-String "127.0.0.1:$Port\s+.*LISTENING")) {
    if (-not (Test-LegacyProjectServer)) {
        throw "Port $Port is occupied but does not identify itself as this QA project. Nothing was stopped."
    }
    if (-not $ReplaceProjectServer) {
        throw "A verified legacy Olesuas QA server is using port $Port. Re-run with -ReplaceProjectServer."
    }
    $listener = netstat -ano | Select-String "127.0.0.1:$Port\s+.*LISTENING"
    $listenerPid = [int](($listener.Line -split '\s+')[-1])
    $process = Get-Process -Id $listenerPid
    if ($process.ProcessName -notlike 'python*') { throw "The verified legacy QA listener is not Python. Nothing was stopped." }
    Stop-Process -Id $listenerPid
    Start-Sleep -Milliseconds 400
}

$pythonExe = (Get-Command python).Source
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $pythonExe
$startInfo.ArgumentList.Add((Join-Path $projectRoot 'tools\serve_qa.py'))
$startInfo.ArgumentList.Add('--host')
$startInfo.ArgumentList.Add('127.0.0.1')
$startInfo.ArgumentList.Add('--port')
$startInfo.ArgumentList.Add([string]$Port)
$startInfo.WorkingDirectory = $projectRoot
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
[void][Diagnostics.Process]::Start($startInfo)

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    $health = Get-QaHealth
    if ($health -and $health.server_version -eq 'OlesuasQA/4.1' -and $health.metrics_version -eq 'bold-metrics-v4.1') {
        Write-Host "Bold QA v4.1 started at http://127.0.0.1:$Port/"
        exit 0
    }
}
throw 'The QA server was launched but did not become healthy.'
