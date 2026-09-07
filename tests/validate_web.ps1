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
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
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

    Push-Location $webDirectory
    try {
        & npm run e2e
        if ($LASTEXITCODE -ne 0) { throw "Playwright failed with exit code $LASTEXITCODE" }
    }
    finally { Pop-Location }

    Write-Output 'WEB_VALIDATION_OK browser=chromium flow=login-match-application-dual-approval-interview storage=session-only'
}
finally {
    & docker compose --project-name $projectName --env-file $envFile --file $composeFile --file $composeOverride --profile tools down --volumes --remove-orphans --timeout 15 | Out-Host
    if ($null -eq $previousApiPort) { Remove-Item Env:API_HOST_PORT -ErrorAction SilentlyContinue } else { $env:API_HOST_PORT = $previousApiPort }
    if ($null -eq $previousProxyTarget) { Remove-Item Env:VITE_API_PROXY_TARGET -ErrorAction SilentlyContinue } else { $env:VITE_API_PROXY_TARGET = $previousProxyTarget }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Web probe left Docker resources behind for project: $projectName"
}
