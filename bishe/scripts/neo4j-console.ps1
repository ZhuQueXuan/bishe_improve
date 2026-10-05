param([string]$ConfigDirectory)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$deploymentPath = Join-Path $projectRoot 'data\neo4j_full_v1\deployment.json'
if (-not (Test-Path -LiteralPath $deploymentPath)) { throw 'No verified formal database deployment record.' }
$deployment = Get-Content -LiteralPath $deploymentPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($deployment.status -ne 'ready') { throw 'Formal database has not completed verification.' }
if (-not $ConfigDirectory) { $ConfigDirectory = Join-Path $projectRoot 'data\neo4j_full_v1\conf' }
$env:JAVA_HOME = $deployment.java_home
$env:NEO4J_CONF = $ConfigDirectory
$env:NEO4J_CONFIG_FILE_CHARSET = 'UTF-8'
& (Join-Path $deployment.neo4j_home 'bin\neo4j.ps1') console
exit $LASTEXITCODE
