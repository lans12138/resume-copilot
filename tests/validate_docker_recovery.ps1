$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $repoRoot 'scripts\recover_docker_desktop.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw 'Docker Desktop recovery script is missing.'
}

$content = Get-Content -Raw -LiteralPath $scriptPath
$requiredFragments = @(
    "'Docker Desktop', 'com.docker.backend', 'docker-desktop', 'docker'",
    'wsl.exe --shutdown',
    'Assert-ChildPath -Root $env:LOCALAPPDATA',
    'Move-Item -LiteralPath $socketRoot -Destination $backupPath',
    'Copy-Item -LiteralPath $settingsPath -Destination $settingsBackup',
    'EnableDockerAI = $false',
    'AnalyticsEnabled = $false',
    'EnableIntegrationWithDefaultWslDistro = $true',
    '/mnt/wsl/docker-desktop/cli-tools/usr/bin/docker',
    'Start-Process -FilePath $DockerDesktopPath -WindowStyle Hidden',
    'DOCKER_DESKTOP_READY recovery=completed windows=ok wsl=ok'
)
$missing = @($requiredFragments | Where-Object { -not $content.Contains($_) })
if ($missing.Count -gt 0) {
    throw "Docker recovery script is missing safety contracts: $($missing -join ', ')"
}

foreach ($forbidden in @('Remove-Item', 'Reset to factory defaults', 'docker system prune')) {
    if ($content.Contains($forbidden)) {
        throw "Docker recovery script contains a destructive operation: $forbidden"
    }
}

$tokens = $null
$syntaxErrors = $null
[void] [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath,
    [ref] $tokens,
    [ref] $syntaxErrors
)
if ($syntaxErrors.Count -gt 0) {
    throw "Docker recovery script has syntax errors: $($syntaxErrors.Message -join '; ')"
}

Write-Output "DOCKER_RECOVERY_VALIDATION_OK contracts=$($requiredFragments.Count) destructive=none"
