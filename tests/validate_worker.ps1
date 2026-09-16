[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-worker-probe'
$backendRuntimeImage = 'resume-copilot-api:local'

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

function Dump-Logs {
    param([Parameter(Mandatory)][string] $Service)
    Write-Host "----- docker compose logs $Service (diagnostic) -----"
    try {
        $out = (Invoke-Compose -Arguments @('logs', $Service) | Out-String)
        Write-Host $out
    }
    catch {
        Write-Host "failed to fetch logs for $Service : $_"
    }
    Write-Host "------------------------------------------------------"
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    # Build the runtime image (carries the backend, the Celery app, and the
    # worker/scheduler entry points).
    Invoke-Docker -Arguments @(
        'build', '--file', (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'),
        '--target', 'runtime', '--tag', $backendRuntimeImage, $repoRoot
    )

    # Start the dependencies and wait for health.
    Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis')

    # Apply migrations so the worker can run maintenance tasks against a live schema.
    Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate')

    # Start the worker and the beat scheduler.
    Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'worker', 'scheduler')

    # Surface startup output immediately so any boot crash is visible in CI.
    Start-Sleep -Seconds 8
    Dump-Logs -Service 'worker'
    Dump-Logs -Service 'scheduler'

    # Wait for the worker to finish booting and register with the broker.
    $workerReady = $false
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        $log = (Invoke-Compose -Arguments @('logs', 'worker') | Out-String)
        if ($log -match 'celery@.*ready' -or $log -match 'Connected to redis') {
            $workerReady = $true
            break
        }
        Start-Sleep -Seconds 3
    }
    if (-not $workerReady) {
        Dump-Logs -Service 'worker'
        throw "Worker did not become ready within the timeout."
    }

    # Enqueue a maintenance sweep; the worker must consume it from Redis.
    Invoke-Compose -Arguments @(
        'run', '--rm', 'worker', 'python', '-c',
        "from backend.app.infrastructure.celery import app; app.send_task('maintenance.expire_approvals', args=[None]); print('ENQUEUED')"
    )

    # The worker should log a successful execution of the consumed task.
    $consumed = $false
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        $log = (Invoke-Compose -Arguments @('logs', 'worker') | Out-String)
        if ($log -match 'Task maintenance\.expire_approvals.*succeeded') {
            $consumed = $true
            break
        }
        Start-Sleep -Seconds 3
    }
    if (-not $consumed) {
        Dump-Logs -Service 'worker'
        throw "Worker did not consume maintenance.expire_approvals from Redis."
    }

    # Beat must publish the expire schedule within its 60s interval.
    $beatFired = $false
    $deadline = (Get-Date).AddSeconds(80)
    while ((Get-Date) -lt $deadline) {
        $log = (Invoke-Compose -Arguments @('logs', 'scheduler') | Out-String)
        # Celery beat logs the dispatch as:
        #   "Scheduler: Sending due task <schedule-key> (<task-name>)"
        # i.e. "Sending due task expire-pending-approvals (maintenance.expire_approvals)".
        # The task name appears in parentheses, not immediately after "Sending due task",
        # so match with a wildcard between the two anchors.
        if ($log -match 'Sending due task.*maintenance\.expire_approvals') {
            $beatFired = $true
            break
        }
        Start-Sleep -Seconds 3
    }
    if (-not $beatFired) {
        Dump-Logs -Service 'scheduler'
        throw "Celery Beat did not publish maintenance.expire_approvals within the interval."
    }

    Write-Host 'FIN-002 worker/scheduler Redis integration probe passed.'
}
finally {
    Invoke-Compose -Arguments @('--profile', 'tools', 'down', '--volumes', '--remove-orphans', '--timeout', '15')
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Worker probe left Docker resources behind for project: $projectName"
}
