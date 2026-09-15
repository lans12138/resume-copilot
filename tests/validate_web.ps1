[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverride = Join-Path $repoRoot 'compose.override.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$webDirectory = Join-Path $repoRoot 'apps/web'
$projectName = 'resume-copilot-imp007-web-probe'
$testPassword = 'synthetic-password-123'
$previousApiPort = $env:API_HOST_PORT
$previousProxyTarget = $env:VITE_API_PROXY_TARGET

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    # Merge native stderr as data, not as a terminating error. Under Windows
    # PowerShell 5.1 every stderr line of a native command that is merged with
    # ``2>&1`` becomes an ErrorRecord, and ``$ErrorActionPreference = 'Stop'``
    # then aborts the probe mid-build (docker writes its build progress to
    # stderr). Scoping the preference keeps the failure contract below intact
    # while behaving identically under pwsh.
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
        'compose', '--project-name', $projectName, '--env-file', $envFile,
        '--file', $composeFile, '--file', $composeOverride
    ) + $Arguments)
}

function Get-ProjectResources {
    return @(Invoke-Docker -Arguments @(
        'ps', '--all', '--filter', "label=com.docker.compose.project=$projectName", '--format', '{{.Names}}'
    ) | Where-Object { $_.Trim() })
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    $env:API_HOST_PORT = '18007'
    $env:VITE_API_PROXY_TARGET = 'http://127.0.0.1:18007'
    [void] (Invoke-Compose -Arguments @('build', 'api'))
    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'storage-init'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'seed'))
    [void] (Invoke-Compose -Arguments @(
        'run', '--rm', '--no-deps', '--env', 'BOOTSTRAP_USERNAME=hr-web-demo',
        '--env', "BOOTSTRAP_PASSWORD=$testPassword", '--env', 'BOOTSTRAP_ROLE=HR',
        'api', 'python', '-m', 'backend.app.auth.bootstrap'
    ))
    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'api'))
    # The browser flow uploads a synthetic resume and waits for it to be parsed, so
    # the worker that consumes documents.parse has to be part of this stack.
    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'worker'))

    # The worker has no Docker healthcheck, so `up --wait` only confirms the
    # container started, not that it has imported the app and connected to the
    # Redis broker. Without this gate the browser can enqueue a match run while
    # the worker is still booting, the task sits unacked, the run never leaves
    # CREATED within the e2e poll budget, and recruitment-flow.spec.ts reports 0
    # candidates. Mirror validate_worker.ps1: poll the worker log for readiness.
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
        & docker compose --project-name $projectName --env-file $envFile `
            --file $composeFile --file $composeOverride logs --no-color --tail 120 api worker 2>&1 | Out-Host
        throw "Web probe worker did not become ready within the timeout."
    }

    Push-Location $webDirectory
    try {
        # Playwright clears its own output directory on start, but that clear can be
        # refused as a bulk delete once a few runs have accumulated (measurements,
        # traces and screenshots land there per failure), and the refusal then shows
        # up inside the Playwright run itself. Clearing first keeps the run clean;
        # it is best-effort because the same refusal must never fail the probe.
        try {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
                (Join-Path $webDirectory 'test-results'), (Join-Path $webDirectory 'playwright-report')
        }
        catch {
            Write-Host "Could not clear the previous Playwright artefacts: $($_.Exception.Message)"
        }
        & npm run e2e
        if ($LASTEXITCODE -ne 0) { throw "Playwright failed with exit code $LASTEXITCODE" }
    }
    finally { Pop-Location }

    Write-Output 'WEB_VALIDATION_OK browser=chromium flow=login-match-application-dual-approval-interview flow=upload-review-readiness storage=session-only'
}
catch {
    # A browser failure is usually an API-side 500, and the stack trace only lives
    # in the detached container's log, which the ``finally`` block below destroys.
    # Dump it here or the next debugging round starts blind — which is exactly how
    # the confirm-with-evidence failure was first reported as a bare 500.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $logs = @('compose', '--project-name', $projectName, '--env-file', $envFile,
            '--file', $composeFile, '--file', $composeOverride)
        Write-Host '--- compose logs: api, worker (tail 120, context only) ---'
        & docker @logs logs --no-color --tail 120 api worker 2>&1 |
            ForEach-Object { $_.ToString() } | Out-Host
        # The traceback is far above any useful tail: a full browser run emits
        # thousands of request lines, so search the whole log for the handler that
        # logged the failure instead of guessing a bigger window.
        Write-Host '--- compose logs: unhandled exceptions in the whole run ---'
        & docker @logs logs --no-color --no-log-prefix api worker 2>&1 |
            Select-String -Pattern 'unhandled_exception' -SimpleMatch |
            ForEach-Object { $_.Line } | Out-Host
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    throw
}
finally {
    & docker compose --project-name $projectName --env-file $envFile --file $composeFile --file $composeOverride --profile tools down --volumes --remove-orphans --timeout 15 | Out-Host
    if ($null -eq $previousApiPort) { Remove-Item Env:API_HOST_PORT -ErrorAction SilentlyContinue } else { $env:API_HOST_PORT = $previousApiPort }
    if ($null -eq $previousProxyTarget) { Remove-Item Env:VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue } else { $env:VITE_API_PROXY_TARGET = $previousProxyTarget }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Web probe left Docker resources behind for project: $projectName"
}
