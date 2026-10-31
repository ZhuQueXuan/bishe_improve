param([switch]$Elevated, [switch]$NoPause)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    if ($Elevated) { throw 'Administrator access was not granted.' }
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Elevated'
    if ($NoPause) { $arguments += ' -NoPause' }
    $windowStyle = 'Normal'
    if ($NoPause) { $windowStyle = 'Hidden' }
    $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle $windowStyle -ArgumentList $arguments -Wait -PassThru
    exit $process.ExitCode
}
$workspaceRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logDirectory = Join-Path $workspaceRoot 'temp\qdrant'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$reportPath = Join-Path $logDirectory ('prerequisites-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
$report = [ordered]@{ timestamp = (Get-Date -Format o); success = $false; automatic_restart = $false; features = @() }
try {
    Write-Host 'Enabling Windows Subsystem for Linux and Virtual Machine Platform.'
    Write-Host 'No Linux distribution will be downloaded. This script will NOT restart the computer.'
    foreach ($featureName in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $before = Get-WindowsOptionalFeature -Online -FeatureName $featureName
        $result = Enable-WindowsOptionalFeature -Online -FeatureName $featureName -All -NoRestart
        $after = Get-WindowsOptionalFeature -Online -FeatureName $featureName
        $report.features += [ordered]@{ name = $featureName; before = [string]$before.State; after = [string]$after.State; restart_needed = $result.RestartNeeded }
    }
    $report.success = $true
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host 'Save your work and restart Windows manually, then open Docker Desktop.'
    Write-Host 'After Docker is running, use qdrant-install.cmd in this scripts directory.'
} catch {
    $report.error = $_.Exception.Message
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host $_.Exception.Message -ForegroundColor Red
    if (-not $NoPause) { Read-Host 'Press Enter to close' }
    exit 1
}
if (-not $NoPause) { Read-Host 'Press Enter to close' }
