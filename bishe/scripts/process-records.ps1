# Shared by start/stop; record identities rather than trusting reusable numeric PIDs.
function Get-ProcessIdentity {
    param($Process)
    if (-not $Process -or -not $Process.CreationDate -or -not $Process.CommandLine) { return $null }
    $bytes = [Text.Encoding]::UTF8.GetBytes([string]$Process.CommandLine)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $hash = ([BitConverter]::ToString($hasher.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose() }
    return [pscustomobject]@{ Created = $Process.CreationDate.ToUniversalTime().ToString('o'); CommandHash = $hash }
}

function Test-ProjectProcess {
    param($Process, [string]$Name, [string]$ProjectRoot)
    if (-not $Process) { return $false }
    $command = [string]$Process.CommandLine
    switch ($Name) {
        'backend' { return ($command -match [regex]::Escape((Join-Path $ProjectRoot 'knowledge_graph_backend\manage.py')) -and $command -match 'runserver\s+127\.0\.0\.1:8000') }
        'natural-memory-worker' { return ($command -match [regex]::Escape((Join-Path $ProjectRoot 'knowledge_graph_backend\manage.py')) -and $command -match 'run_natural_memory_background\s+--loop' -and $command -match '--settings=knowledge_graph_backend\.release_settings') }
        'neo4j' { return ($command -match [regex]::Escape((Join-Path $ProjectRoot 'scripts\neo4j-console.ps1'))) }
        'frontend' { return ($command -match 'npm(?:\.cmd|[-/]cli\.js)' -and $command -match 'run\s+serve') }
    }
    return $false
}

function New-ManagedRecord {
    param([string]$Name, [int]$ProcessId)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    $identity = Get-ProcessIdentity $process
    if (-not $identity) { throw "Cannot record identity for newly started $Name. Inspect dedicated logs." }
    return [pscustomobject]@{ Name=$Name; Id=$ProcessId; Managed=$true; Created=$identity.Created; CommandHash=$identity.CommandHash }
}

function Get-CanonicalCreationStamp {
    param($Value)
    # ConvertFrom-Json can return DateTime instead of the stored ISO string.
    # Keep exact UTC precision across Windows PowerShell 5.1 and PowerShell 7.
    if ($Value -is [datetimeoffset]) { return $Value.UtcDateTime.ToString('o') }
    if ($Value -is [datetime]) {
        if ($Value.Kind -eq [DateTimeKind]::Unspecified) { return $null }
        return $Value.ToUniversalTime().ToString('o')
    }
    if ($Value -isnot [string] -or $Value -notmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,7})?(Z|[+-]\d{2}:\d{2})$') { return $null }
    $parsed = [datetime]::MinValue
    $formats = [string[]]@('o', "yyyy-MM-dd'T'HH:mm:ss.FFFFFFFK", "yyyy-MM-dd'T'HH:mm:ssK")
    if (-not [datetime]::TryParseExact($Value, $formats, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind, [ref]$parsed)) { return $null }
    return $parsed.ToUniversalTime().ToString('o')
}

function Test-ManagedRecord {
    param($Record, [string]$ProjectRoot)
    if (-not $Record -or -not $Record.Managed -or -not $Record.Id -or -not $Record.Created -or -not $Record.CommandHash) { return $false }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.Id)" -ErrorAction SilentlyContinue
    $identity = Get-ProcessIdentity $process
    $created = Get-CanonicalCreationStamp $Record.Created
    return ($identity -and $created -and $identity.Created -eq $created -and $identity.CommandHash -eq $Record.CommandHash -and
            (Test-ProjectProcess $process $Record.Name $ProjectRoot))
}

function Get-ExistingManagedRecord {
    param([string]$Name, [int]$Port, $Records, [string]$ProjectRoot)
    $unmanaged = [pscustomobject]@{ Name=$Name; Id=$null; Managed=$false }
    $prior = @($Records | Where-Object { $_.Name -eq $Name -and $_.Managed -and $_.Id })
    if ($prior.Count -ne 1) { return $unmanaged }
    $record = $prior[0]
    # A legacy entry may be upgraded only when its still-live project process
    # is an ancestor of the current listener. Stop never trusts legacy entries.
    if ($record.Created -or $record.CommandHash) {
        if (-not (Test-ManagedRecord $record $ProjectRoot)) { return $unmanaged }
    }
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $currentId = [int]$listener.OwningProcess
        $seen = @{}
        for ($depth=0; $depth -lt 12 -and $currentId -gt 0 -and -not $seen.ContainsKey($currentId); $depth++) {
            $seen[$currentId]=$true
            $process=Get-CimInstance Win32_Process -Filter "ProcessId=$currentId" -ErrorAction SilentlyContinue
            if (-not $process) { break }
            if ($currentId -eq [int]$record.Id) {
                if (Test-ProjectProcess $process $Name $ProjectRoot) { return (New-ManagedRecord $Name $currentId) }
                break
            }
            $currentId=[int]$process.ParentProcessId
        }
    }
    return $unmanaged
}
