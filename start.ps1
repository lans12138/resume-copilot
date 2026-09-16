#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Start the local demo stack and keep it running.

.DESCRIPTION
    This is the daily-use launcher. It creates .env on first use, builds and
    starts the Docker Compose services, seeds the demo data idempotently, and
    verifies the public entry point. Existing containers and volumes are kept
    unless -Fresh is supplied explicitly.

.EXAMPLE
    ./start.ps1
    Start or refresh the stack without deleting existing data.

.EXAMPLE
    ./start.ps1 -Fresh -OpenBrowser
    Delete this project's Docker volumes, create a clean demo, and open it.
#>
[CmdletBinding()]
param(
    [string] $ProjectName = 'resume-copilot',
    [ValidateRange(1, 65535)]
    [int] $EntryPort = 8080,
    [ValidateRange(0, 65535)]
    [int] $ApiPort = 0,
    [ValidateRange(0, 65535)]
    [int] $PostgresPort = 0,
    [ValidateRange(0, 65535)]
    [int] $RedisPort = 0,
    [switch] $Fresh,
    [switch] $ResetDemo,
    [switch] $OpenBrowser
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($Fresh -and $ResetDemo) {
    throw 'Choose either -Fresh or -ResetDemo. A fresh start already recreates all demo data.'
}

$repoRoot = $PSScriptRoot
$envPath = Join-Path $repoRoot '.env'
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverride = Join-Path $repoRoot 'compose.override.yaml'
$initializer = Join-Path $repoRoot 'scripts/initialize_local_env.ps1'
$freshLauncher = Join-Path $repoRoot 'scripts/start_stack.ps1'
$entryUrl = "http://localhost:$EntryPort"

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]] $Arguments)

    & docker compose `
        --project-name $ProjectName `
        --env-file $envPath `
        --file $composeFile `
        --file $composeOverride `
        @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Wait-Endpoint {
    param(
        [Parameter(Mandatory)][string] $Path,
        [int] $TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = 'no response'
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest `
                -Uri "http://127.0.0.1:$EntryPort$Path" `
                -UseBasicParsing `
                -TimeoutSec 5
            if ([int] $response.StatusCode -eq 200) {
                return $response
            }
            $lastError = "HTTP $($response.StatusCode)"
        }
        catch {
            $lastError = $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }
    throw "GET $Path was not ready within $TimeoutSeconds seconds: $lastError"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker is not available on PATH. Start Docker Desktop and try again.'
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Write-Host '[setup] Creating .env with local random secrets...'
    & $initializer -OutputPath $envPath
}

$previousHostPorts = @{
    WEB_HOST_PORT = $env:WEB_HOST_PORT
    API_HOST_PORT = $env:API_HOST_PORT
    POSTGRES_HOST_PORT = $env:POSTGRES_HOST_PORT
    REDIS_HOST_PORT = $env:REDIS_HOST_PORT
}
$env:WEB_HOST_PORT = "$EntryPort"
$env:API_HOST_PORT = "$ApiPort"
$env:POSTGRES_HOST_PORT = "$PostgresPort"
$env:REDIS_HOST_PORT = "$RedisPort"
try {
    if ($Fresh) {
        Write-Host '[start] Removing this project stack and its volumes...'
        Invoke-Compose @('--profile', 'tools', 'down', '--volumes', '--remove-orphans', '--timeout', '20')

        Write-Host '[start] Building and verifying a clean demo stack...'
        & $freshLauncher `
            -ProjectName $ProjectName `
            -EnvFile '.env' `
            -EntryPort $EntryPort `
            -RepoRoot $repoRoot `
            -KeepRunning
        if ($LASTEXITCODE -ne 0) {
            throw "Fresh startup failed with exit code $LASTEXITCODE"
        }
        $mode = 'fresh'
    }
    else {
        Write-Host '[1/3] Building and starting the demo stack (existing data is preserved)...'
        Invoke-Compose @(
            'up', '--detach', '--wait', '--wait-timeout', '420', '--build',
            'postgres', 'redis', 'api', 'worker', 'scheduler', 'web'
        )

        if ($ResetDemo) {
            Write-Host '[2/3] Resetting synthetic demo data...'
            Invoke-Compose @('--profile', 'tools', 'run', '--rm', 'seed', '--', '--reset')
            $mode = 'reset-demo'
        }
        else {
            Write-Host '[2/3] Seeding synthetic demo data idempotently...'
            Invoke-Compose @('--profile', 'tools', 'run', '--rm', 'seed')
            $mode = 'preserve'
        }

        Write-Host '[3/3] Checking the public entry point...'
        [void] (Wait-Endpoint -Path '/healthz')
        [void] (Wait-Endpoint -Path '/api/v1/health/ready')
    }

    Write-Output "DEMO_READY entry=$entryUrl mode=$mode"
    Write-Host 'Demo login: hr.demo / demo-password-123'
    if ($ApiPort -eq 0 -or $PostgresPort -eq 0 -or $RedisPort -eq 0) {
        Write-Host 'Debug ports were assigned automatically to avoid local port conflicts.'
    }
    Write-Host "Stop without deleting data: docker compose --project-name $ProjectName stop"

    if ($OpenBrowser) {
        Start-Process $entryUrl
    }
}
finally {
    foreach ($variableName in $previousHostPorts.Keys) {
        $previousValue = $previousHostPorts[$variableName]
        if ($null -eq $previousValue) {
            Remove-Item "Env:$variableName" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$variableName" $previousValue
        }
    }
}
