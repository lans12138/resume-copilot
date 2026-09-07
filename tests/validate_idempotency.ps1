[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-idempotency-probe'
$backendDevelopmentImage = 'resume-copilot-backend-development:local'

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')`n$($output -join "`n")"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]] $Arguments)
    return Invoke-Docker -Arguments (@(
        'compose',
        '--project-name', $projectName,
        '--env-file', $envFile,
        '--file', $composeFile
    ) + $Arguments)
}

function Get-ProjectResources {
    $containers = Invoke-Docker -Arguments @(
        'ps', '--all',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Names}}'
    ) | Where-Object { $_.Trim() }
    $volumes = Invoke-Docker -Arguments @(
        'volume', 'ls',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Name}}'
    ) | Where-Object { $_.Trim() }
    return @($containers) + @($volumes)
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    # Build the development image (carries pytest + backend code).
    Invoke-Docker -Arguments @(
        'build', '--file', (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'),
        '--target', 'development', '--tag', $backendDevelopmentImage, $repoRoot
    )

    # Start only the dependencies.
    Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis')

    # Validate migration 0011 (idempotency_records) applies against a live database.
    Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate')

    # Run the PostgreSQL integration suite inside the dev container, attached to the
    # compose network so DATABASE_URL (host ``postgres``) resolves.
    $pytestArgs = @(
        'run', '--rm',
        '--network', "$projectName" + '_backend',
        '--env-file', $envFile,
        $backendDevelopmentImage,
        'pytest', '-q', 'tests/integration/test_idempotency_postgres.py'
    )
    & docker @pytestArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Idempotency PostgreSQL integration tests failed (exit $LASTEXITCODE)"
    }
    Write-Host 'FIN-001 idempotency PostgreSQL integration tests passed.'
}
finally {
    Invoke-Compose -Arguments @('down', '--volumes', '--remove-orphans')
}
