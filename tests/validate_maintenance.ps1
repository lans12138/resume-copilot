[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-maintenance-probe'
$backendDevelopmentImage = 'resume-copilot-backend-development:local'
# The probe container mounts no compose volume, so point storage at a path that
# always exists and is writable inside the image.
$storageRoot = '/tmp/resume-maintenance-e2e'

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    # Merge native stderr as data, not as a terminating error. Under Windows
    # PowerShell 5.1 every stderr line of a native command merged with ``2>&1``
    # becomes an ErrorRecord, and ``$ErrorActionPreference = 'Stop'`` would then
    # abort the probe mid-build (docker writes build progress to stderr).
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & docker @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
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
    # Build the development image (carries pytest + backend + tests).
    Invoke-Docker -Arguments @(
        'build', '--file', (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'),
        '--target', 'development', '--tag', $backendDevelopmentImage, $repoRoot
    )

    # Start the real dependencies: PostgreSQL is the only one these tests need —
    # the sweeps are exercised through the SQL repositories, and the broker
    # failure path is modelled explicitly rather than by stopping Redis.
    Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'postgres')

    # Apply the full migration chain so every table the sweeps touch exists.
    Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate')

    # NOTE: precompute the network name into its own variable. Inlining
    # ``"$projectName" + '_backend'`` inside an array literal makes PowerShell emit
    # two separate arguments, which docker then reads as an invalid image reference.
    $networkName = "$projectName" + '_backend'
    $pytestArgs = @(
        'run', '--rm',
        '--network', $networkName,
        '--env-file', $envFile,
        '--env', "STORAGE_ROOT=$storageRoot",
        $backendDevelopmentImage,
        'pytest', '-q', 'tests/integration/test_maintenance_postgres.py'
    )
    & docker @pytestArgs
    if ($LASTEXITCODE -ne 0) {
        throw "FIN-006 maintenance integration tests failed (exit $LASTEXITCODE)"
    }
    Write-Host 'FIN-006 maintenance integration tests passed.'
}
finally {
    Invoke-Compose -Arguments @('down', '--volumes', '--remove-orphans')
}
