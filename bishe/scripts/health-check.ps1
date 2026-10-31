$ErrorActionPreference = 'Stop'

$checks = @(
    @{ Name = 'Frontend'; Url = 'http://127.0.0.1:8080/' },
    @{ Name = 'Backend'; Url = 'http://127.0.0.1:8000/admin/login/' },
    @{ Name = 'Graph API'; Url = 'http://127.0.0.1:8000/api/statistics/' },
    @{ Name = 'Neo4j'; Url = 'http://127.0.0.1:7474/' },
    @{ Name = 'Qdrant'; Url = 'http://127.0.0.1:16333/readyz' }
)

$failed = $false
$results = foreach ($check in $checks) {
    try {
        $response = Invoke-WebRequest -Uri $check.Url -UseBasicParsing -TimeoutSec 5
        [pscustomobject]@{
            Service = $check.Name
            Status = $response.StatusCode
            Url = $check.Url
        }
    } catch {
        $failed = $true
        [pscustomobject]@{
            Service = $check.Name
            Status = 'FAILED'
            Url = $check.Url
        }
    }
}

$results | Format-Table -AutoSize
if ($failed) {
    exit 1
}
exit 0
