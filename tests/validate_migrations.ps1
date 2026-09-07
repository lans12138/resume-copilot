[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-imp004-probe'

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

    return Invoke-Docker -Arguments (@(
        'compose',
        '--project-name', $projectName,
        '--env-file', $envFile,
        '--file', $composeFile
    ) + $Arguments)
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

function Get-ApiResponse {
    $pythonProbe = (
        "import http.client; " +
        "connection=http.client.HTTPConnection('127.0.0.1',8000,timeout=10); " +
        "connection.request('GET','/api/v1/health/ready'); " +
        "response=connection.getresponse(); " +
        "print(response.status); print(response.read().decode('utf-8'))"
    )
    $output = @(Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'api', 'python', '-c', $pythonProbe
    ))
    if ($output.Count -lt 2) {
        throw "API readiness probe returned incomplete output: $($output -join ' ')"
    }
    return [PSCustomObject]@{
        Status = [int] $output[-2].Trim()
        Body = $output[-1] | ConvertFrom-Json
        RawBody = $output[-1]
    }
}

if (@(Get-ProjectContainers).Count -gt 0 -or @(Get-ProjectVolumes).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    $config = (
        Invoke-Compose -Arguments @('--profile', 'tools', 'config', '--format', 'json')
        | Out-String
    ) | ConvertFrom-Json
    foreach ($serviceName in @('api', 'postgres', 'redis', 'storage-init', 'migrate')) {
        if ($null -eq $config.services.$serviceName) {
            throw "Compose config is missing IMP-004 service: $serviceName"
        }
    }
    if ($config.services.migrate.command -join ' ' -ne 'alembic upgrade head') {
        throw 'The migrate service must run only alembic upgrade head.'
    }

    [void] (Invoke-Compose -Arguments @('build', 'api'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis'
    ))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'storage-init'))

    # Running the same migration twice proves that bootstrap is repeatable.
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    [void] (Invoke-Compose -Arguments @(
        '--profile', 'tools', 'run', '--rm', 'migrate', 'alembic', 'check'
    ))
    # The new workflow migration is reversible and can be reapplied cleanly.
    [void] (Invoke-Compose -Arguments @(
        '--profile', 'tools', 'run', '--rm', 'migrate',
        'alembic', 'downgrade', '0007_add_parsed_json'
    ))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    [void] (Invoke-Compose -Arguments @(
        '--profile', 'tools', 'run', '--rm', 'migrate', 'alembic', 'check'
    ))

    $revision = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc', 'SELECT version_num FROM alembic_version;'
        ) | Select-Object -Last 1
    ).Trim()
    if ($revision -ne '0008_create_workflow_tables') {
        throw "Unexpected Alembic revision: $revision"
    }

    $expectedTablesSql = @"
SELECT count(*)
FROM (VALUES
    ('agent_events'),
    ('agent_runs'),
    ('application_runs'),
    ('application_status_history'),
    ('approvals'),
    ('candidate_profiles'),
    ('candidates'),
    ('claim_evidences'),
    ('evidence_chunks'),
    ('interviews'),
    ('job_applications'),
    ('job_assignments'),
    ('job_versions'),
    ('jobs'),
    ('match_reports'),
    ('match_run_candidates'),
    ('match_runs'),
    ('report_claims'),
    ('resume_documents'),
    ('users')
) AS expected(name)
WHERE to_regclass('public.' || name) IS NULL;
"@
    $missingTableCount = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc', $expectedTablesSql
        ) | Select-Object -Last 1
    ).Trim()
    if ($missingTableCount -ne '0') {
        throw "Migration left $missingTableCount expected ORM tables missing."
    }

    $vectorDimension = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            'CREATE TEMP TABLE vector_probe (embedding vector(1024)); SELECT atttypmod FROM pg_attribute WHERE attrelid = ''vector_probe''::regclass AND attname = ''embedding'';'
        ) | Select-Object -Last 1
    ).Trim()
    if ($vectorDimension -ne '1024') {
        throw "pgvector dimension probe returned: $vectorDimension"
    }

    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'api'))
    $readyResponse = Get-ApiResponse
    if ($readyResponse.Status -ne 200 -or $readyResponse.Body.status -ne 'ready') {
        throw "API did not become ready: status=$($readyResponse.Status) body=$($readyResponse.RawBody)"
    }
    foreach ($dependency in @('postgres', 'redis', 'storage')) {
        if ($readyResponse.Body.dependencies.$dependency.status -ne 'up') {
            throw "Readiness did not report $dependency as up."
        }
    }

    [void] (Invoke-Compose -Arguments @('stop', 'redis'))
    $degradedResponse = Get-ApiResponse
    if (
        $degradedResponse.Status -ne 503 -or
        $degradedResponse.Body.code -ne 'DEPENDENCY_UNAVAILABLE' -or
        $degradedResponse.Body.details.dependencies.redis.status -ne 'down'
    ) {
        throw "API did not fail safely when Redis stopped: $($degradedResponse.RawBody)"
    }
    if ($degradedResponse.RawBody -match '(?i)(redis://|postgresql\+asyncpg://|/data/resumes)') {
        throw 'Readiness error exposed an internal connection string or storage path.'
    }

    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'redis'
    ))
    $recoveredResponse = Get-ApiResponse
    if ($recoveredResponse.Status -ne 200 -or $recoveredResponse.Body.status -ne 'ready') {
        throw "API readiness did not recover: $($recoveredResponse.RawBody)"
    }

    Write-Output (
        'MIGRATION_VALIDATION_OK ' +
        "revision=$revision tables=20 vector_dimension=$vectorDimension readiness=ready degradation=503 recovery=ready"
    )
}
finally {
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

if (@(Get-ProjectContainers).Count -gt 0 -or @(Get-ProjectVolumes).Count -gt 0) {
    throw "Migration probe left Docker resources behind for project: $projectName"
}
