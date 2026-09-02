[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$dockerfile = Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'
$imageName = 'resume-copilot-api:local'
$containerName = "resume-copilot-imp001-api-probe-$PID"
$containerId = $null

function Invoke-Docker {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $output = & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code ${LASTEXITCODE}: docker $($Arguments -join ' ')"
    }

    return $output
}

try {
    Invoke-Docker @(
        'build',
        '--file', $dockerfile,
        '--target', 'runtime',
        '--tag', $imageName,
        $repoRoot
    ) | Out-Host

    $containerId = (
        Invoke-Docker @(
            'run',
            '--detach',
            '--name', $containerName,
            '--label', 'resume-copilot.probe=imp001-api',
            '--publish', '127.0.0.1::8000',
            '--env', 'APP_ENV=test',
            '--env', "JWT_SECRET=$('x' * 48)",
            '--env', 'DATABASE_URL=postgresql+asyncpg://test:test@postgres/test',
            '--env', 'REDIS_URL=redis://redis:6379/0',
            '--env', 'CELERY_BROKER_URL=redis://redis:6379/1',
            '--env', 'STORAGE_ROOT=/tmp/resumes',
            '--env', 'MOCK_MODEL_MODE=true',
            $imageName
        ) | Select-Object -Last 1
    ).Trim()

    if (-not $containerId) {
        throw 'Docker did not return an API probe container ID.'
    }

    $portOutput = (Invoke-Docker @('port', $containerId, '8000/tcp') | Select-Object -Last 1).Trim()
    if ($portOutput -notmatch ':(?<port>[0-9]+)$') {
        throw "Could not parse the API probe port from: $portOutput"
    }

    $probeUri = "http://127.0.0.1:$($Matches.port)/api/v1/health/live"
    $response = $null
    for ($attempt = 1; $attempt -le 40; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri $probeUri -TimeoutSec 2
            break
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }

    if ($null -eq $response -or $response.status -ne 'ok' -or -not $response.request_id) {
        Invoke-Docker @('logs', $containerId) | Out-Host
        throw "API runtime probe did not return the expected response from $probeUri"
    }

    Write-Output "API_RUNTIME_VALIDATION_OK image=$imageName status=$($response.status)"
}
finally {
    if ($containerId) {
        & docker rm --force $containerId | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Could not remove API probe container: $containerId"
        }
    }
}
