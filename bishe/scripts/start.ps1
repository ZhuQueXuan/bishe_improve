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
$releaseFile = Join-Path $projectRoot 'data\natural_memory_v1\runtime_release_v1_1_0.json'
$settingsProfile = 'knowledge_graph_backend.settings'
if (Test-Path -LiteralPath $releaseFile) {
    $release = Get-Content -LiteralPath $releaseFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($release.ready -and $release.version -eq '1.1.0') { $settingsProfile = 'knowledge_graph_backend.release_settings' }
}
. (Join-Path $PSScriptRoot 'process-records.ps1')
$previousRecords = @()
if (Test-Path -LiteralPath $processFile) {
    # Windows PowerShell 5.1 emits a JSON array as one pipeline object. Avoid
    # wrapping that result in @(), which adds a nested array on that host.
    try { $previousRecords = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json }
    catch { throw 'Existing process record is invalid; no services started. Inspect temp\run\managed-processes.json.' }
}

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
$formalConfig = Join-Path $projectRoot 'data\neo4j_full_v1\conf\neo4j.conf'
$boltSetting = @(Get-Content -LiteralPath $formalConfig | Where-Object { $_ -match '^server\.bolt\.listen_address=127\.0\.0\.1:\d+$' })
if ($boltSetting.Count -ne 1) { throw 'Expected exactly one loopback Bolt address in the formal Neo4j config.' }
$boltPort = [int]($boltSetting[0].Split(':')[-1])

if (-not (Test-LocalPort -Port $boltPort)) {
    $formalDeployment = Join-Path $projectRoot 'data\neo4j_full_v1\deployment.json'
    if (-not (Test-Path -LiteralPath $formalDeployment)) { throw 'Missing formal graph deployment record.' }
    $deployment = Get-Content -LiteralPath $formalDeployment -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($deployment.status -ne 'ready') { throw 'Formal graph deployment is incomplete.' }
    $neo4j = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'neo4j-console.ps1')) `
        -RedirectStandardOutput (Join-Path $runDirectory 'neo4j.out.log') `
        -RedirectStandardError (Join-Path $runDirectory 'neo4j.err.log') `
        -WindowStyle Hidden -PassThru
    $processes += New-ManagedRecord 'neo4j' $neo4j.Id
} else {
    $processes += Get-ExistingManagedRecord 'neo4j' $boltPort $previousRecords $projectRoot
}

if (-not (Test-LocalPort -Port 8000)) {
    $backend = Start-Process -FilePath $pythonPath `
        -ArgumentList @($managePath, 'runserver', '127.0.0.1:8000', '--noreload', "--settings=$settingsProfile") `
        -WorkingDirectory $backendPath `
        -RedirectStandardOutput (Join-Path $runDirectory 'backend.out.log') `
        -RedirectStandardError (Join-Path $runDirectory 'backend.err.log') `
        -WindowStyle Hidden `
        -PassThru
    $processes += New-ManagedRecord 'backend' $backend.Id
} else {
    $processes += Get-ExistingManagedRecord 'backend' 8000 $previousRecords $projectRoot
}

if ($settingsProfile -eq 'knowledge_graph_backend.release_settings') {
    $workerRecord = $previousRecords | Where-Object { $_.Name -eq 'natural-memory-worker' } | Select-Object -First 1
    if ($workerRecord -and (Test-ManagedRecord $workerRecord $projectRoot)) {
        $processes += $workerRecord
    } else {
        $worker = Start-Process -FilePath $pythonPath `
            -ArgumentList @($managePath, 'run_natural_memory_background', '--loop', "--settings=$settingsProfile") `
            -WorkingDirectory $backendPath `
            -RedirectStandardOutput (Join-Path $runDirectory 'natural-memory-worker.out.log') `
            -RedirectStandardError (Join-Path $runDirectory 'natural-memory-worker.err.log') `
            -WindowStyle Hidden -PassThru
        $processes += New-ManagedRecord 'natural-memory-worker' $worker.Id
    }
}

if (-not (Test-LocalPort -Port 8080)) {
    $frontend = Start-Process -FilePath 'npm.cmd' `
        -ArgumentList @('run', 'serve', '--', '--host', '127.0.0.1') `
        -WorkingDirectory $frontendPath `
        -RedirectStandardOutput (Join-Path $runDirectory 'frontend.out.log') `
        -RedirectStandardError (Join-Path $runDirectory 'frontend.err.log') `
        -WindowStyle Hidden `
        -PassThru
    $processes += New-ManagedRecord 'frontend' $frontend.Id
} else {
    $processes += Get-ExistingManagedRecord 'frontend' 8080 $previousRecords $projectRoot
}

$processes | ConvertTo-Json | Set-Content -LiteralPath $processFile -Encoding utf8

Write-Output 'Project services are starting.'
Write-Output 'Frontend: http://127.0.0.1:8080/'
Write-Output 'Backend:  http://127.0.0.1:8000/'
Write-Output 'Neo4j:   http://127.0.0.1:7474/'
Write-Output "Logs:     $runDirectory"
Write-Output 'Run scripts\health-check.ps1 to verify readiness.'
