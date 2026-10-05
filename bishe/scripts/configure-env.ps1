$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendPath = Join-Path $projectRoot 'knowledge_graph_backend'
$envPath = Join-Path $backendPath '.env'
$examplePath = Join-Path $backendPath '.env.example'

if (-not (Test-Path -LiteralPath $envPath)) {
    if (-not (Test-Path -LiteralPath $examplePath)) {
        throw 'Neither .env nor .env.example exists.'
    }
    Copy-Item -LiteralPath $examplePath -Destination $envPath
}

Write-Output 'Enter the new DeepSeek API Key. Input is hidden; paste it and press Enter:'
$secureKey = Read-Host -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        throw 'API Key cannot be empty.'
    }
    if (-not $apiKey.StartsWith('sk-')) {
        throw 'Invalid API Key format: the value must start with sk-.'
    }

    $lines = Get-Content -LiteralPath $envPath
    $found = $false
    $updatedLines = foreach ($line in $lines) {
        if ($line -match '^DEEPSEEK_API_KEY=') {
            $found = $true
            "DEEPSEEK_API_KEY=$apiKey"
        } else {
            $line
        }
    }
    if (-not $found) {
        $updatedLines += "DEEPSEEK_API_KEY=$apiKey"
    }

    [IO.File]::WriteAllLines(
        $envPath,
        [string[]]$updatedLines,
        [Text.UTF8Encoding]::new($false)
    )
    Write-Output 'Updated DEEPSEEK_API_KEY in .env without displaying the value.'
} finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    $apiKey = $null
}
