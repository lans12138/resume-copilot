[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverrideFile = Join-Path $repoRoot 'compose.override.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-imp003-probe'
$expectedVolumes = @(
    'postgres_data',
    'redis_data',
    'resume_storage',
    'test_artifacts'
)
$stackStarted = $false

function Invoke-Docker {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')`n$($output -join "`n")"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $composeArguments = @(
        'compose',
        '--project-name', $projectName,
        '--env-file', $envFile,
        '--file', $composeFile
    ) + $Arguments
    return Invoke-Docker -Arguments $composeArguments
}

function Get-DevelopmentComposeConfig {
    $arguments = @(
        'compose',
        '--project-name', $projectName,
        '--env-file', $envFile,
        '--file', $composeFile,
        '--file', $composeOverrideFile,
        '--profile', 'tools',
        'config', '--format', 'json'
    )
    return (Invoke-Docker -Arguments $arguments | Out-String) | ConvertFrom-Json
}

function Get-ProjectContainers {
    return Invoke-Docker -Arguments @(
        'ps', '--all',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Names}}'
    ) | Where-Object { $_.Trim() }
}

function Get-ProjectVolumes {
    return Invoke-Docker -Arguments @(
        'volume', 'ls',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Name}}'
    ) | Where-Object { $_.Trim() }
}

if (@(Get-ProjectContainers).Count -gt 0 -or @(Get-ProjectVolumes).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    $config = (
        Invoke-Compose -Arguments @('--profile', 'tools', 'config', '--format', 'json')
        | Out-String
    ) | ConvertFrom-Json

    foreach ($serviceName in @('postgres', 'redis', 'storage-init')) {
        if ($null -eq $config.services.$serviceName) {
            throw "Compose config is missing service: $serviceName"
        }
    }
    if ($null -eq $config.services.postgres.healthcheck) {
        throw 'PostgreSQL healthcheck is missing.'
    }
    if ($null -eq $config.services.redis.healthcheck) {
        throw 'Redis healthcheck is missing.'
    }
    if ($config.services.postgres.image -notmatch '@sha256:[0-9a-f]{64}$') {
        throw 'PostgreSQL image must be pinned by digest.'
    }
    if ($config.services.redis.image -notmatch '@sha256:[0-9a-f]{64}$') {
        throw 'Redis image must be pinned by digest.'
    }
    $postgresProperties = @($config.services.postgres.PSObject.Properties.Name)
    $redisProperties = @($config.services.redis.PSObject.Properties.Name)
    if ('ports' -in $postgresProperties -or 'ports' -in $redisProperties) {
        throw 'The base Compose file must not publish database or Redis ports.'
    }

    $configuredVolumes = @($config.volumes.PSObject.Properties.Name)
    foreach ($volumeName in $expectedVolumes) {
        if ($volumeName -notin $configuredVolumes) {
            throw "Compose config is missing volume: $volumeName"
        }
    }

    $developmentConfig = Get-DevelopmentComposeConfig
    foreach ($serviceName in @('postgres', 'redis')) {
        $ports = @($developmentConfig.services.$serviceName.ports)
        if ($ports.Count -ne 1 -or $ports[0].host_ip -ne '127.0.0.1') {
            throw "Development port for $serviceName must bind only to 127.0.0.1."
        }
    }

    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis'
    ))
    $stackStarted = $true
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'storage-init'))

    $redisPing = (
        Invoke-Compose -Arguments @('exec', '--no-TTY', 'redis', 'redis-cli', 'ping')
        | Select-Object -Last 1
    ).Trim()
    if ($redisPing -ne 'PONG') {
        throw "Redis health probe returned: $redisPing"
    }

    $postgresVersion = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc', 'SHOW server_version;'
        ) | Select-Object -Last 1
    ).Trim()
    $pgvectorVersion = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname = 'vector';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($pgvectorVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "pgvector probe returned an unexpected version: $pgvectorVersion"
    }

    [void] (Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'postgres',
        'psql', '-U', 'resume_app', '-d', 'resume_copilot',
        '-v', 'ON_ERROR_STOP=1', '-c',
        "CREATE TABLE imp003_persistence_probe (value text PRIMARY KEY); INSERT INTO imp003_persistence_probe VALUES ('postgres-ok');"
    ))
    [void] (Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'redis', 'redis-cli', 'SET', 'imp003:persistence', 'redis-ok'
    ))
    [void] (Invoke-Compose -Arguments @('exec', '--no-TTY', 'redis', 'redis-cli', 'SAVE'))

    [void] (Invoke-Compose -Arguments @('stop', 'postgres', 'redis'))
    [void] (Invoke-Compose -Arguments @('rm', '--force', 'postgres', 'redis'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis'
    ))

    $postgresMarker = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            'SELECT value FROM imp003_persistence_probe;'
        ) | Select-Object -Last 1
    ).Trim()
    $redisMarker = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'redis', 'redis-cli', 'GET', 'imp003:persistence'
        ) | Select-Object -Last 1
    ).Trim()
    if ($postgresMarker -ne 'postgres-ok' -or $redisMarker -ne 'redis-ok') {
        throw "Persistence probe failed: postgres=$postgresMarker redis=$redisMarker"
    }

    $projectVolumes = @(Get-ProjectVolumes)
    foreach ($volumeName in $expectedVolumes) {
        if (-not ($projectVolumes | Where-Object { $_ -like "*_$volumeName" })) {
            throw "Docker did not create the expected project volume: $volumeName"
        }
    }

    Write-Output (
        'COMPOSE_STACK_VALIDATION_OK ' +
        "postgres=$postgresVersion " +
        "pgvector=$pgvectorVersion " +
        "redis=$redisPing " +
        "volumes=$($expectedVolumes.Count) persistence=ok"
    )
}
finally {
    if ($stackStarted) {
        & docker compose `
            --project-name $projectName `
            --env-file $envFile `
            --file $composeFile `
            --profile tools `
            down --volumes --remove-orphans --timeout 15 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Failed to fully remove probe project: $projectName"
        }
    }
}

if (@(Get-ProjectContainers).Count -gt 0 -or @(Get-ProjectVolumes).Count -gt 0) {
    throw "Compose probe left Docker resources behind for project: $projectName"
}
