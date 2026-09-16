[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Decode container output as UTF-8 regardless of the host's console codepage: a
# child pwsh inherits its parent's, which on a Chinese Windows host is cp936, and
# the stack's own logging is UTF-8. CI runs with a UTF-8 console, so without this
# a local run can disagree with CI for reasons that have nothing to do with the
# stack.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# FIN-012 item 4: "one entry command on a fresh clone/volume".
#
# The claim under test is not "the script exits 0" — it is that a *fresh* project
# can be brought to a serving state and then removed without leftovers. So the
# probe drives scripts/start_stack.ps1 itself, on a dedicated project name and
# volume set, and then asserts that nothing survives.
#
# Running the real entry script (rather than re-implementing its steps) is the
# point: a probe that duplicated the sequence would keep passing after the script
# rotted.

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverride = Join-Path $repoRoot 'compose.override.yaml'
$projectName = 'resume-copilot-fin012-onecommand-probe'
$entryPort = 18081
$apiPort = 18082
$postgresPort = 18083
$redisPort = 18084
$envFile = Join-Path $repoRoot '.env.example'
$probeEnvFile = Join-Path $repoRoot '.env'

# The root launcher is the operator-facing daily path. Keep its safe defaults
# under a static contract here; the live probe below exercises the fresh-start
# implementation that it delegates to.
$dailyLauncher = Join-Path $repoRoot 'start.ps1'
$windowsLauncher = Join-Path $repoRoot 'start.cmd'
$windowsLauncherText = [System.IO.File]::ReadAllText($windowsLauncher, [System.Text.Encoding]::ASCII)
if ($windowsLauncherText -notmatch 'powershell\.exe' -or
    $windowsLauncherText -notmatch '-ExecutionPolicy Bypass' -or
    $windowsLauncherText -notmatch 'start\.ps1' -or
    $windowsLauncherText -notmatch '%\*') {
    throw 'start.cmd must invoke start.ps1 with a process-scoped execution-policy bypass and forward arguments.'
}
$launcherTokens = $null
$launcherErrors = $null
$launcherText = [System.IO.File]::ReadAllText($dailyLauncher, [System.Text.Encoding]::UTF8)
[void][System.Management.Automation.Language.Parser]::ParseInput(
    $launcherText,
    [ref] $launcherTokens,
    [ref] $launcherErrors
)
if ($launcherErrors.Count -gt 0) {
    $details = $launcherErrors | ForEach-Object {
        "line $($_.Extent.StartLineNumber): $($_.Message)"
    }
    throw "start.ps1 has PowerShell parse errors:`n$($details -join "`n")"
}
$launcherContracts = @{
    'creates the local environment on first use' = 'scripts/initialize_local_env\.ps1'
    'delegates clean starts to the verified launcher' = 'scripts/start_stack\.ps1'
    'requires an explicit fresh switch before volume deletion' = 'if \(\$Fresh\)[\s\S]+?--volumes'
    'supports an explicit demo-data reset' = '\$ResetDemo[\s\S]+?--reset'
    'checks API readiness through the public entry' = '/api/v1/health/ready'
    'reports a stable ready marker' = 'DEMO_READY'
}
foreach ($contract in $launcherContracts.GetEnumerator()) {
    if ($launcherText -notmatch $contract.Value) {
        throw "start.ps1 no longer $($contract.Key)."
    }
}

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    # ``$LASTEXITCODE`` is initialised before use: under
    # ``Set-StrictMode -Version Latest`` a read of the automatic variable before
    # any native command has run is a fatal error, not a failure check.
    $exitCode = 1
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
    $networks = Invoke-Docker -Arguments @(
        'network', 'ls',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Name}}'
    ) | Where-Object { $_.Trim() }
    return @($containers) + @($volumes) + @($networks)
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

# The entry script reads a real .env, and refuses to run while it still holds the
# template placeholders. Generate one from the example with the placeholders
# replaced by throwaway values, so the probe never touches, and never depends on,
# the developer's own .env.
$existingEnv = $null
if (Test-Path -LiteralPath $probeEnvFile -PathType Leaf) {
    $existingEnv = Get-Content -LiteralPath $probeEnvFile -Raw
}
$previousApiPort = $env:API_HOST_PORT
$previousPostgresPort = $env:POSTGRES_HOST_PORT
$previousRedisPort = $env:REDIS_HOST_PORT
$env:API_HOST_PORT = "$apiPort"
$env:POSTGRES_HOST_PORT = "$postgresPort"
$env:REDIS_HOST_PORT = "$redisPort"
# A deterministic, obviously synthetic secret; not used anywhere but this probe.
$syntheticSecret = ('probe' + ('0' * 44))
$probeEnvText = (Get-Content -LiteralPath $envFile -Raw) `
    -replace 'replace-with-at-least-48-random-characters-x+', $syntheticSecret `
    -replace 'replace-with-local-database-password', 'probe-database-password' `
    -replace 'replace-with-local-api-key', 'probe-model-key' `
    -replace 'replace-with-[a-z-]+', 'probe-placeholder'

try {
    Write-Host '[probe] Writing a synthetic .env for the entry-point run...'
    [System.IO.File]::WriteAllText($probeEnvFile, $probeEnvText, (New-Object System.Text.UTF8Encoding($false)))

    Write-Host '[probe] Invoking scripts/start_stack.ps1 on a fresh project...'
    $entryScript = Join-Path $repoRoot 'scripts/start_stack.ps1'
    # Read as UTF-8 and run the text as a script block, so the host's console
    # codepage cannot garble the script's own non-ASCII text.
    #
    # That choice costs ``$PSScriptRoot``: a script block created from text has no
    # script path, and the entry script derives the repository root from it. It is
    # therefore handed ``-RepoRoot`` explicitly — without that the entry command
    # failed on its very first line with "Cannot bind argument to parameter 'Path'
    # because it is an empty string", before doing anything observable.
    $scriptBlock = [scriptblock]::Create(
        [System.IO.File]::ReadAllText($entryScript, [System.Text.Encoding]::UTF8)
    )
    $output = & $scriptBlock `
        -ProjectName $projectName `
        -EnvFile '.env' `
        -EntryPort $entryPort `
        -RepoRoot $repoRoot `
        *>&1 | ForEach-Object { $_.ToString() }

    $output | ForEach-Object { Write-Host $_ }

    $joined = $output -join "`n"
    if ($joined -notmatch 'STACK_READY') {
        throw "The entry command did not report STACK_READY:`n$joined"
    }
    if ($joined -notmatch "entry=http://localhost:$entryPort") {
        throw "The entry command reported an unexpected entry point:`n$joined"
    }
    # The seeded counts are asserted, not just the exit status: a stack that comes
    # up with an empty database has still failed the operator's actual goal.
    $seedMatch = [regex]::Match($joined, 'seed=(\d+)\|(\d+)')
    if (-not $seedMatch.Success) {
        throw "The entry command did not report seed counts:`n$joined"
    }
    $jobs = [int] $seedMatch.Groups[1].Value
    $applications = [int] $seedMatch.Groups[2].Value
    if ($jobs -le 0 -or $applications -le 0) {
        throw "The demo dataset was not seeded: jobs=$jobs applications=$applications"
    }

    Write-Host "[probe] Entry command succeeded: jobs=$jobs applications=$applications"

    # ── Cleanup actually happened ─────────────────────────────────────────────
    # start_stack.ps1 tears down in its ``finally`` and does not use -KeepRunning
    # here, so any surviving resource is a real leak, not a policy choice.
    $leftover = @(Get-ProjectResources)
    if ($leftover.Count -gt 0) {
        throw (
            "The entry command left Docker resources behind:`n  " +
            ($leftover -join "`n  ")
        )
    }

    # A volume that survives is the worst case, because the *next* run would no
    # longer be fresh. Check the named volumes directly as well.
    $residualVolumes = Invoke-Docker -Arguments @(
        'volume', 'ls', '--format', '{{.Name}}'
    ) | Where-Object { $_ -match [regex]::Escape($projectName) }
    if (@($residualVolumes).Count -gt 0) {
        throw "Residual volumes survived the entry command: $($residualVolumes -join ', ')"
    }

    Write-Output (
        'ONE_COMMAND_UP_VALIDATION_OK ' +
        "project=$projectName entry_port=$entryPort " +
        "seed=$jobs|$applications " +
        'cleanup=containers+volumes+networks-removed'
    )
}
catch {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Write-Host '--- compose logs: web, api, worker (tail 100, context only) ---'
        & docker compose `
            --project-name $projectName `
            --env-file $envFile `
            --file $composeFile `
            --file $composeOverride `
            logs --no-color --tail 100 web api worker 2>&1 |
            ForEach-Object { $_.ToString() } | Out-Host
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    throw
}
finally {
    # Belt and braces: if start_stack.ps1 threw before its own cleanup ran, this
    # removes the probe project so the next run starts fresh.
    & docker compose `
        --project-name $projectName `
        --env-file $envFile `
        --file $composeFile `
        --file $composeOverride `
        --profile tools `
        down --volumes --remove-orphans --timeout 15 2>&1 | Out-Null

    if ($null -ne $existingEnv) {
        [System.IO.File]::WriteAllText($probeEnvFile, $existingEnv, (New-Object System.Text.UTF8Encoding($false)))
    }
    else {
        Remove-Item -LiteralPath $probeEnvFile -Force -ErrorAction SilentlyContinue
    }

    foreach ($portVariable in @(
        @{ Name = 'API_HOST_PORT'; Previous = $previousApiPort },
        @{ Name = 'POSTGRES_HOST_PORT'; Previous = $previousPostgresPort },
        @{ Name = 'REDIS_HOST_PORT'; Previous = $previousRedisPort }
    )) {
        if ($null -eq $portVariable.Previous) {
            Remove-Item "Env:$($portVariable.Name)" -ErrorAction SilentlyContinue
        }
        else {
            Set-Item "Env:$($portVariable.Name)" $portVariable.Previous
        }
    }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "One-command probe left Docker resources behind for project: $projectName"
}
