[CmdletBinding()]
param(
    [Parameter()]
    [string] $DockerDesktopPath = 'D:\develop\Docker\Docker Desktop.exe',

    [Parameter()]
    [string] $WslDistribution = 'Ubuntu-24.04',

    [Parameter()]
    [ValidateRange(15, 300)]
    [int] $StartTimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-DockerReady {
    & docker info --format '{{.OSType}}' *> $null
    return $LASTEXITCODE -eq 0
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory)]
        [string] $Root,

        [Parameter(Mandatory)]
        [string] $Candidate
    )

    $resolvedRoot = [System.IO.Path]::GetFullPath($Root) +
        [System.IO.Path]::DirectorySeparatorChar
    $resolvedCandidate = [System.IO.Path]::GetFullPath($Candidate) +
        [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedCandidate.StartsWith(
        $resolvedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to move a path outside the Docker local-data root: $Candidate"
    }
}

function Test-WslDockerCli {
    & wsl.exe -d $WslDistribution -u root -- sh -lc (
        'if [ ! -e /usr/bin/docker ]; then ' +
        'ln -s /mnt/wsl/docker-desktop/cli-tools/usr/bin/docker /usr/bin/docker; fi'
    )
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    & wsl.exe -d $WslDistribution -- sh -lc (
        'docker info --format "{{.OSType}}" >/dev/null 2>&1 && ' +
        'docker compose version >/dev/null 2>&1'
    )
    return $LASTEXITCODE -eq 0
}

if (Test-DockerReady) {
    $integrationDeadline = [DateTimeOffset]::Now.AddSeconds($StartTimeoutSeconds)
    do {
        if (Test-WslDockerCli) {
            Write-Output 'DOCKER_DESKTOP_READY recovery=not-required windows=ok wsl=ok'
            exit 0
        }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::Now -lt $integrationDeadline)
    throw "Docker Desktop WSL integration did not become ready within $StartTimeoutSeconds seconds."
}

if (-not (Test-Path -LiteralPath $DockerDesktopPath -PathType Leaf)) {
    throw "Docker Desktop executable not found: $DockerDesktopPath"
}

$dockerProcessNames = @('Docker Desktop', 'com.docker.backend', 'docker-desktop', 'docker')
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $dockerProcessNames -contains $_.ProcessName } |
    Stop-Process -Force
& wsl.exe --shutdown
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to stop WSL before Docker socket recovery.'
}
Start-Sleep -Seconds 2

$localDataRoot = Join-Path $env:LOCALAPPDATA 'Docker'
$socketRoots = @(
    (Join-Path $localDataRoot 'run'),
    (Join-Path $env:LOCALAPPDATA 'docker-secrets-engine')
)
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
foreach ($socketRoot in $socketRoots) {
    Assert-ChildPath -Root $env:LOCALAPPDATA -Candidate $socketRoot
    if (Test-Path -LiteralPath $socketRoot) {
        $backupPath = "$socketRoot.backup-$timestamp"
        if (Test-Path -LiteralPath $backupPath) {
            throw "Docker socket backup path already exists: $backupPath"
        }
        Move-Item -LiteralPath $socketRoot -Destination $backupPath
        Write-Output "DOCKER_SOCKET_BACKUP path=$backupPath"
    }
}

$settingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    throw "Docker Desktop settings not found: $settingsPath"
}
$settingsBackup = "$settingsPath.backup-$timestamp"
Copy-Item -LiteralPath $settingsPath -Destination $settingsBackup
$settings = [System.IO.File]::ReadAllText($settingsPath) | ConvertFrom-Json
foreach ($entry in @{
    AnalyticsEnabled = $false
    EnableDockerAI = $false
    EnableIntegrationWithDefaultWslDistro = $true
}.GetEnumerator()) {
    if ($null -eq $settings.PSObject.Properties[$entry.Key]) {
        $settings | Add-Member -NotePropertyName $entry.Key -NotePropertyValue $entry.Value
    }
    else {
        $settings.($entry.Key) = $entry.Value
    }
}
[System.IO.File]::WriteAllText(
    $settingsPath,
    ($settings | ConvertTo-Json -Depth 100),
    [System.Text.UTF8Encoding]::new($false)
)

Start-Process -FilePath $DockerDesktopPath -WindowStyle Hidden
$deadline = [DateTimeOffset]::Now.AddSeconds($StartTimeoutSeconds)
do {
    Start-Sleep -Seconds 2
    if ((Test-DockerReady) -and (Test-WslDockerCli)) {
        Write-Output 'DOCKER_DESKTOP_READY recovery=completed windows=ok wsl=ok'
        exit 0
    }
} while ([DateTimeOffset]::Now -lt $deadline)

throw "Docker Desktop did not become ready within $StartTimeoutSeconds seconds."
