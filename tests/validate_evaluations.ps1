[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-evaluations-probe'
$backendDevelopmentImage = 'resume-copilot-backend-development:local'

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

    # PostgreSQL is the only dependency these tests need: the evaluation tables, the
    # duplicate-guard index, and the metric uniqueness constraint are all
    # database behaviours. The `evaluations.execute` task is driven through
    # ``celery_app.tasks[...].run(...)`` so no broker is required, and the built-in
    # suites are model-free by construction (FakeModel / recorded responses).
    Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'postgres')

    # Apply the full migration chain so dataset_versions / evaluation_runs /
    # metric_snapshots exist before the tests touch them.
    Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate')

    # NOTE: precompute the network name into its own variable. Inlining
    # ``"$projectName" + '_backend'`` inside an array literal makes PowerShell emit
    # two separate arguments, which docker then reads as an invalid image reference.
    $networkName = "$projectName" + '_backend'
    $pytestArgs = @(
        'run', '--rm',
        '--network', $networkName,
        '--env-file', $envFile,
        $backendDevelopmentImage,
        'pytest', '-q', 'tests/integration/test_evaluations_postgres.py'
    )
    & docker @pytestArgs
    if ($LASTEXITCODE -ne 0) {
        throw "FIN-007 evaluation integration tests failed (exit $LASTEXITCODE)"
    }
    Write-Host 'FIN-007 evaluation integration tests passed.'
}
finally {
    Invoke-Compose -Arguments @('down', '--volumes', '--remove-orphans')
}
