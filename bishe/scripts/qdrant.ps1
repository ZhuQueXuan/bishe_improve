param(
    [ValidateSet('Install', 'Start', 'Stop', 'Status')]
    [string]$Action = 'Status'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$composeFile = Join-Path $projectRoot 'docker-compose.qdrant.yml'
$logDirectory = Join-Path $workspaceRoot 'temp\qdrant'
$composeArgs = @('compose', '-p', 'medical-rag-qdrant', '-f', $composeFile)

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker Desktop is not installed or docker.exe is not on PATH.'
    }
    $probe = Invoke-DockerProbe -Arguments @('info', '--format', '{{.ServerVersion}}')
    if ($probe.ExitCode -ne 0) {
        throw 'Docker engine is unavailable. Open Docker Desktop first. For WSL2/virtualization errors, see the project deployment note (document 14). No project database was changed.'
    }
}

function Invoke-DockerProbe {
    param([string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & docker @Arguments 2>$null
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    return [pscustomobject]@{ ExitCode = $code; Output = $output }
}

function Assert-OwnedResources {
    $container = Invoke-DockerProbe -Arguments @('inspect', '--type', 'container', 'medical-rag-qdrant-v1')
    if ($container.ExitCode -eq 0) {
        $item = @($container.Output | ConvertFrom-Json)[0]
        if ($item.Config.Labels.'com.docker.compose.project' -ne 'medical-rag-qdrant' -or
            $item.Config.Labels.'com.docker.compose.service' -ne 'qdrant') {
            throw 'Container name is already used by another service. Nothing was replaced.'
        }
    }
    $volume = Invoke-DockerProbe -Arguments @('volume', 'inspect', 'medical-rag-qdrant-storage-v1')
    if ($volume.ExitCode -eq 0) {
        $item = @($volume.Output | ConvertFrom-Json)[0]
        if ($item.Labels.'com.docker.compose.project' -ne 'medical-rag-qdrant') {
            throw 'Storage volume belongs to another service. Nothing was replaced.'
        }
    }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $logFile = Join-Path $logDirectory ('service-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker @composeArgs @Arguments 2>&1 | ForEach-Object { $_.ToString() } | Tee-Object -FilePath $logFile -Append | Out-Host
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0) { throw "Docker Compose failed. Log: $logFile" }
}

function Wait-Ready {
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        try {
            $ready = Invoke-WebRequest -Uri 'http://127.0.0.1:16333/readyz' -UseBasicParsing -TimeoutSec 2
            $root = Invoke-RestMethod -Uri 'http://127.0.0.1:16333/' -TimeoutSec 2
            if ($ready.StatusCode -eq 200 -and $root.title -eq 'qdrant - vector search engine') {
                Write-Host "Qdrant ready: version $($root.version)"
                Write-Host 'Dashboard: http://127.0.0.1:16333/dashboard'
                Write-Host 'Persistent Docker volume: medical-rag-qdrant-storage-v1'
                return
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    throw 'Qdrant did not become ready in 60 seconds. Check Docker Desktop and temp/qdrant logs.'
}

Assert-Docker
Assert-OwnedResources
switch ($Action) {
    'Install' {
        Invoke-Compose -Arguments @('pull', 'qdrant')
        Invoke-Compose -Arguments @('up', '-d', '--no-build', '--pull', 'never', 'qdrant')
        Wait-Ready
    }
    'Start' {
        Invoke-Compose -Arguments @('up', '-d', '--no-build', '--pull', 'never', 'qdrant')
        Wait-Ready
    }
    'Stop' {
        Invoke-Compose -Arguments @('stop', 'qdrant')
        Write-Host 'Qdrant stopped. Its container and stored data are preserved.'
    }
    'Status' {
        Invoke-Compose -Arguments @('ps', '-a')
        $ready = Invoke-WebRequest -Uri 'http://127.0.0.1:16333/readyz' -UseBasicParsing -TimeoutSec 3
        $root = Invoke-RestMethod -Uri 'http://127.0.0.1:16333/' -TimeoutSec 3
        if ($ready.StatusCode -ne 200 -or $root.title -ne 'qdrant - vector search engine') {
            throw 'The local endpoint is not a ready Qdrant instance.'
        }
        Write-Host "Qdrant ready: version $($root.version)"
    }
}
