$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
$processFile = Join-Path $workspaceRoot 'temp\run\managed-processes.json'
. (Join-Path $PSScriptRoot 'process-records.ps1')

if (-not (Test-Path -LiteralPath $processFile)) {
    Write-Output 'No managed process record was found. Nothing was stopped.'
    exit 0
}

function Stop-ProcessTree {
    param([int]$ProcessId)

    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-ProcessTree -ProcessId $child.ProcessId
    }
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        try { Stop-Process -Id $ProcessId -Force -ErrorAction Stop }
        catch {
            # A parent may exit naturally as its last child stops. Suppress only
            # that completed outcome; a still-running process remains an error.
            if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { throw }
        }
    }
}

$records = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
foreach ($record in $records) {
    if ($record.Managed -and $record.Id) {
        if (-not (Test-ManagedRecord $record $projectRoot)) {
            Write-Output "Skipped $($record.Name): process identity is missing, expired or changed. Nothing stopped; run start.ps1 to verify live legacy records."
            continue
        }
        if ($record.Name -eq 'neo4j') {
            if (Get-Process -Id ([int]$record.Id) -ErrorAction SilentlyContinue) {
                $helper = Start-Process -FilePath 'powershell.exe' `
                    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'neo4j-stop-console.ps1'), '-ProcessId', $record.Id) `
                    -WindowStyle Hidden -PassThru -Wait
                if ($helper.ExitCode -ne 0) { throw 'Neo4j graceful stop failed; database was not force-killed.' }
                $deadline = (Get-Date).AddSeconds(30)
                while ((Get-Process -Id ([int]$record.Id) -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
                if (Get-Process -Id ([int]$record.Id) -ErrorAction SilentlyContinue) { throw 'Neo4j is still stopping; no forced kill was issued.' }
            }
            Write-Output 'Neo4j stopped gracefully.'
            continue
        }
        Stop-ProcessTree -ProcessId ([int]$record.Id)
        Write-Output "Stopped $($record.Name)."
    } else {
        Write-Output "Skipped $($record.Name): it was already running before start.ps1."
    }
}
