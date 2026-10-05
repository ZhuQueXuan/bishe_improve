$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$runDirectory = Join-Path $workspaceRoot 'temp\run'
$backendPath = Join-Path $projectRoot 'knowledge_graph_backend'
$pythonPath = Join-Path $backendPath '.venv\Scripts\python.exe'
$managePath = Join-Path $backendPath 'manage.py'
$frontendPath = Join-Path $projectRoot 'knowledge-graph-frontend'
$envPath = Join-Path $backendPath '.env'
$processFile = Join-Path $runDirectory 'managed-processes.json'

if (-not (Test-Path -LiteralPath $envPath)) {
    throw 'Missing backend .env. Copy knowledge_graph_backend\.env.example to .env and configure it first.'
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Missing backend Python environment. Create knowledge_graph_backend\.venv and install its requirements.txt first.'
}
if (-not (Test-Path -LiteralPath $runDirectory)) {
    New-Item -ItemType Directory -Path $runDirectory | Out-Null
}

function Test-LocalPort {
    param([int]$Port)
    return $null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
}

$processes = @()

if (-not (Test-LocalPort -Port 7687)) {
    $formalDeployment = Join-Path $projectRoot 'data\neo4j_full_v1\deployment.json'
    if (-not (Test-Path -LiteralPath $formalDeployment)) { throw 'Missing formal graph deployment record.' }
    $deployment = Get-Content -LiteralPath $formalDeployment -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($deployment.status -ne 'ready') { throw 'Formal graph deployment is incomplete.' }
    $neo4j = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'neo4j-console.ps1')) `
        -RedirectStandardOutput (Join-Path $runDirectory 'neo4j.out.log') `
        -RedirectStandardError (Join-Path $runDirectory 'neo4j.err.log') `
        -WindowStyle Hidden -PassThru
    $processes += [pscustomobject]@{ Name = 'neo4j'; Id = $neo4j.Id; Managed = $true }
} else {
    $processes += [pscustomobject]@{ Name = 'neo4j'; Id = $null; Managed = $false }
}

if (-not (Test-LocalPort -Port 8000)) {
    $backend = Start-Process -FilePath $pythonPath `
        -ArgumentList @($managePath, 'runserver', '127.0.0.1:8000', '--noreload') `
        -WorkingDirectory $backendPath `
        -RedirectStandardOutput (Join-Path $runDirectory 'backend.out.log') `
        -RedirectStandardError (Join-Path $runDirectory 'backend.err.log') `
        -WindowStyle Hidden `
        -PassThru
    $processes += [pscustomobject]@{ Name = 'backend'; Id = $backend.Id; Managed = $true }
} else {
    $processes += [pscustomobject]@{ Name = 'backend'; Id = $null; Managed = $false }
}

if (-not (Test-LocalPort -Port 8080)) {
    $frontend = Start-Process -FilePath 'npm.cmd' `
        -ArgumentList @('run', 'serve', '--', '--host', '127.0.0.1') `
        -WorkingDirectory $frontendPath `
        -RedirectStandardOutput (Join-Path $runDirectory 'frontend.out.log') `
        -RedirectStandardError (Join-Path $runDirectory 'frontend.err.log') `
        -WindowStyle Hidden `
        -PassThru
    $processes += [pscustomobject]@{ Name = 'frontend'; Id = $frontend.Id; Managed = $true }
} else {
    $processes += [pscustomobject]@{ Name = 'frontend'; Id = $null; Managed = $false }
}

$processes | ConvertTo-Json | Set-Content -LiteralPath $processFile -Encoding utf8

Write-Output 'Project services are starting.'
Write-Output 'Frontend: http://127.0.0.1:8080/'
Write-Output 'Backend:  http://127.0.0.1:8000/'
Write-Output 'Neo4j:   http://127.0.0.1:7474/'
Write-Output "Logs:     $runDirectory"
Write-Output 'Run scripts\health-check.ps1 to verify readiness.'
