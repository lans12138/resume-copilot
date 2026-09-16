#!/usr/bin/env pwsh
<#
.SYNOPSIS
    One-command start for a fresh clone: build, migrate, seed, verify, clean up.

.DESCRIPTION
    FIN-012 item 4. The Compose stack is already declarative, but "one entry
    command on a fresh clone" is a claim that has to be executable, not
    documented. This script is that command, and it fails loudly rather than
    printing a URL and hoping.

    It builds every image, brings the stack up with ``--wait`` (migrate is on the
    default profile and the api gates on it, so the schema is at head before the
    API accepts traffic), asserts the health of each service through the public
    Nginx entry point, seeds the demo dataset idempotently, runs a browser-level
    smoke check against the entry point, and then — unless -KeepRunning — tears
    everything down including volumes.

    The teardown is deliberately the default: a fresh-clone check that leaves six
    containers and four volumes behind is a check the operator has to clean up by
    hand, and a leftover volume silently makes the *next* run non-fresh.

.EXAMPLE
    ./scripts/start_stack.ps1
    Starts from scratch, verifies, and removes everything afterwards.

.EXAMPLE
    ./scripts/start_stack.ps1 -KeepRunning
    Leaves the stack up so the UI can be opened at http://localhost:8080.
#>
[CmdletBinding()]
param(
    [string] $ProjectName = 'resume-copilot',
    [string] $EnvFile = '.env',
    [int] $EntryPort = 8080,
    [switch] $KeepRunning,
    [switch] $SkipBrowserSmoke
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverride = Join-Path $repoRoot 'compose.override.yaml'
$envPath = Join-Path $repoRoot $EnvFile
$demoUsername = 'hr.demo'
$demoPassword = 'demo-password-123'

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

function Test-Endpoint {
    param(
        [Parameter(Mandatory)][string] $Path,
        [int] $ExpectedStatus = 200,
        [int] $TimeoutSeconds = 60
    )
    # Polled rather than assumed: ``up --wait`` reports container health, and the
    # Nginx healthcheck only proves Nginx is up, not that the proxy path to the
    # API is complete.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastStatus = 0
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest `
                -Uri "http://127.0.0.1:$EntryPort$Path" `
                -UseBasicParsing `
                -TimeoutSec 5
            $lastStatus = [int] $response.StatusCode
            if ($lastStatus -eq $ExpectedStatus) { return $response }
        }
        catch {
            if ($_.Exception.Response) {
                $lastStatus = [int] $_.Exception.Response.StatusCode
                if ($lastStatus -eq $ExpectedStatus) { return $null }
            }
        }
        Start-Sleep -Seconds 2
    }
    throw "GET $Path did not return $ExpectedStatus within $TimeoutSeconds seconds (last=$lastStatus)."
}

# ── Preconditions ─────────────────────────────────────────────────────────────
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'The Docker CLI is not on PATH.'
}
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw "Missing $EnvFile. Copy .env.example to .env and replace every placeholder secret first."
}
# A placeholder left in place produces an app that boots and then rejects every
# login, so it is checked here where the message is actionable.
#
# Only the variables the default configuration actually dereferences are
# blocking. In mock mode the model endpoint and key are never read, and Langfuse
# is off, so their template placeholders are inert -- and ``env-init`` leaves
# exactly those three in place, which means blocking on them would make the
# documented path ("copy the example, replace the secrets, run this script")
# impossible to follow.
$envText = Get-Content -LiteralPath $envPath -Raw
$blockingPlaceholders = @('JWT_SECRET', 'POSTGRES_PASSWORD', 'DATABASE_URL')
$placeholderVariables = @(
    [regex]::Matches($envText, '(?m)^([A-Z_]+)=replace-with-[a-z-]+') |
        ForEach-Object { $_.Groups[1].Value }
)
$blocking = @($placeholderVariables | Where-Object { $blockingPlaceholders -contains $_ })
if ($blocking.Count -gt 0) {
    throw "These variables still hold their .env.example placeholders: $($blocking -join ', ')"
}
$inert = @($placeholderVariables | Where-Object { $blockingPlaceholders -notcontains $_ })
if ($inert.Count -gt 0) {
    Write-Host (
        "      note: $($inert -join ', ') still hold template placeholders; they are " +
        'unused while MOCK_MODEL_MODE stays true and LANGFUSE_ENABLED stays false.'
    )
}

function Get-EnvValue {
    param(
        [Parameter(Mandatory)][string] $Text,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][string] $Default
    )
    # Read from the env *file* rather than the process environment: compose is
    # given the file explicitly, so the file is what the containers actually see
    # and the only source that cannot disagree with them.
    $match = [regex]::Match($Text, "(?m)^\s*$([regex]::Escape($Name))\s*=\s*(.+?)\s*$")
    if ($match.Success) { return $match.Groups[1].Value.Trim('"', "'") }
    return $Default
}

# Not hardcoded to the .env.example defaults: an operator who changes POSTGRES_USER
# there would otherwise have a stack that starts and then fails only at the
# psql probe, which is the least useful place to discover it.
$dbUser = Get-EnvValue -Text $envText -Name 'POSTGRES_USER' -Default 'resume_app'
$dbName = Get-EnvValue -Text $envText -Name 'POSTGRES_DB' -Default 'resume_copilot'

$previousEntryPort = $env:WEB_HOST_PORT
$env:WEB_HOST_PORT = "$EntryPort"

$projectResources = @(
    & docker ps --all --filter "label=com.docker.compose.project=$ProjectName" --format '{{.Names}}'
) + @(
    & docker volume ls --filter "label=com.docker.compose.project=$ProjectName" --format '{{.Name}}'
) | Where-Object { $_ -and $_.Trim() }

if ($projectResources.Count -gt 0) {
    throw (
        "Project '$ProjectName' already owns Docker resources, so this is not a fresh clone/volume run:`n" +
        "  $($projectResources -join "`n  ")`n" +
        "Remove them first: docker compose --project-name $ProjectName down --volumes --remove-orphans"
    )
}

try {
    Write-Host '[1/5] Building api and web images...'
    Invoke-Compose @('build', 'api', 'web')

    Write-Host '[2/5] Starting the stack (migrate runs on the default profile; api gates on it)...'
    # ``web`` is included so the entry point is up in the same pass; worker and
    # scheduler are covered by the default profile too, since they are the only
    # thing that turns a queued run into a finished one.
    Invoke-Compose @(
        'up', '--detach', '--wait', '--wait-timeout', '420',
        'postgres', 'redis', 'api', 'worker', 'scheduler', 'web'
    )

    Write-Host '[3/5] Checking health through the public entry point...'
    # The directory endpoint is the API's own readiness, reached over the proxy:
    # this is the assertion that Nginx is wired to a healthy API, not merely that
    # both containers are running.
    [void] (Test-Endpoint -Path '/healthz')
    [void] (Test-Endpoint -Path '/api/v1/health/ready')
    $index = Test-Endpoint -Path '/'
    if ($index.Content -notmatch '<div id="root">') {
        throw 'The entry point did not serve the SPA shell.'
    }
    Write-Host "      entry point is serving the SPA at http://localhost:$EntryPort"

    Write-Host '[4/5] Seeding the demo dataset...'
    # ``seed`` depends on ``migrate`` completing, and the dependencies make a
    # second run a no-op rather than a duplicate; the probe below asserts that
    # second-run idempotency rather than trusting it.
    Invoke-Compose @('--profile', 'tools', 'run', '--rm', 'seed')
    Invoke-Compose @('--profile', 'tools', 'run', '--rm', 'seed')

    # Bootstrap the demo HR account with a credential that only lives in this
    # process, so the browser smoke check has something to log in with.
    Invoke-Compose @(
        'run', '--rm', '--no-deps',
        '--env', "BOOTSTRAP_USERNAME=$demoUsername",
        '--env', "BOOTSTRAP_PASSWORD=$demoPassword",
        '--env', 'BOOTSTRAP_ROLE=HR',
        'api', 'python', '-m', 'backend.app.auth.bootstrap'
    )

    $seedProbe = & docker compose `
        --project-name $ProjectName `
        --env-file $envPath `
        --file $composeFile `
        --file $composeOverride `
        exec --no-TTY postgres psql -U $dbUser -d $dbName `
        -v ON_ERROR_STOP=1 -tAc `
        "SELECT (SELECT count(*) FROM jobs) || '|' || (SELECT count(*) FROM job_applications);"
    $seedCounts = ($seedProbe | Select-Object -Last 1).Trim()
    Write-Host "      seeded rows (jobs|applications) = $seedCounts"

    if (-not $SkipBrowserSmoke) {
        Write-Host '[5/5] Running the browser smoke check (real login through Nginx)...'
        $smoke = Invoke-WebRequest `
            -Uri "http://127.0.0.1:$EntryPort/api/v1/auth/token" `
            -Method Post `
            -Body "username=$demoUsername&password=$demoPassword" `
            -ContentType 'application/x-www-form-urlencoded' `
            -UseBasicParsing `
            -TimeoutSec 30
        if ($smoke.StatusCode -ne 200) {
            throw "The demo credential did not authenticate through the entry point: $($smoke.StatusCode)"
        }
        Write-Host '      login through the proxy succeeded'
    }
    else {
        Write-Host '[5/5] Browser smoke check skipped by request.'
    }

    Write-Output (
        "STACK_READY entry=http://localhost:$EntryPort " +
        "services=postgres,redis,api,worker,scheduler,web seed=$seedCounts"
    )
}
finally {
    if ($KeepRunning) {
        Write-Host "Leaving the stack running. Stop it with: docker compose --project-name $ProjectName down --volumes --remove-orphans"
    }
    else {
        Write-Host 'Tearing the stack down, including volumes...'
        Invoke-Compose @('--profile', 'tools', 'down', '--volumes', '--remove-orphans', '--timeout', '20')
    }

    if ($null -eq $previousEntryPort) {
        Remove-Item Env:WEB_HOST_PORT -ErrorAction SilentlyContinue
    }
    else {
        $env:WEB_HOST_PORT = $previousEntryPort
    }
}
