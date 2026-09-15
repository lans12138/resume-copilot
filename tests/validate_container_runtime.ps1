[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$probeLabelName = 'io.openai.resume-env-probe'
$probeLabelValue = 'true'
$redisContainer = 'resume-env-redis-probe'
$postgresContainer = 'resume-env-pgvector-probe'

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $DockerArguments
    )

    $output = & docker @DockerArguments 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Docker command failed ($exitCode): docker $($DockerArguments -join ' ')`n$($output -join "`n")"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

function Test-ContainerExists {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    $containerNames = Invoke-Docker -DockerArguments @('ps', '--all', '--format', '{{.Names}}')
    return $Name -in $containerNames
}

function Remove-ProbeContainer {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    if (-not (Test-ContainerExists -Name $Name)) {
        return
    }

    $label = Invoke-Docker -DockerArguments @(
        'inspect',
        '--format',
        '{{ index .Config.Labels "io.openai.resume-env-probe" }}',
        $Name
    )
    if (($label -join '').Trim() -ne $probeLabelValue) {
        throw "Refusing to remove container without the probe label: $Name"
    }

    [void] (Invoke-Docker -DockerArguments @('rm', '--force', $Name))
}

function Wait-ForProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Description,

        [Parameter(Mandatory = $true)]
        [scriptblock] $Probe,

        [Parameter()]
        [int] $TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $result = & $Probe
            if ($result) {
                return $result
            }
        } catch {
            # The service can reject connections while its entrypoint is starting.
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "Timed out waiting for $Description after $TimeoutSeconds seconds."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI is not available on PATH.'
}

$dockerOperatingSystem = @(
    Invoke-Docker -DockerArguments @('info', '--format', '{{.OSType}}')
    | Where-Object { $_.Trim() }
    | Select-Object -Last 1
)[0].Trim()
if ($dockerOperatingSystem -ne 'linux') {
    throw "Docker daemon is not using Linux containers: $dockerOperatingSystem"
}

$helloWorld = Invoke-Docker -DockerArguments @('run', '--rm', 'hello-world:linux')
if (($helloWorld -join "`n") -notmatch 'Hello from Docker!') {
    throw 'hello-world did not return the expected success message.'
}

$pythonOutput = Invoke-Docker -DockerArguments @(
    'run', '--rm', 'python:3.12-slim', 'python', '--version'
)
$pythonVersionLine = @($pythonOutput | Where-Object { $_ -match '^Python 3\.12\.\d+$' } | Select-Object -Last 1)
if ($pythonVersionLine.Count -ne 1) {
    throw "Python container did not emit one 3.12 version line:`n$($pythonOutput -join "`n")"
}
$pythonVersion = $pythonVersionLine[0].Trim()
if ($pythonVersion -notmatch '^Python 3\.12\.\d+$') {
    throw "Python 3.12 container probe returned an unexpected version: $pythonVersion"
}

foreach ($containerName in @($redisContainer, $postgresContainer)) {
    if (Test-ContainerExists -Name $containerName) {
        throw "A container already uses the reserved probe name: $containerName"
    }
}

$redisVersion = $null
$pgvectorVersion = $null
try {
    [void] (Invoke-Docker -DockerArguments @(
        'run',
        '--detach',
        '--name', $redisContainer,
        '--label', "$probeLabelName=$probeLabelValue",
        'redis:7.4-alpine'
    ))
    $redisPing = Wait-ForProbe -Description 'Redis PING' -Probe {
        $output = & docker exec $redisContainer redis-cli ping 2>&1
        if ($LASTEXITCODE -eq 0 -and ($output -join '').Trim() -eq 'PONG') {
            return 'PONG'
        }
        return $null
    }
    $redisVersion = (
        Invoke-Docker -DockerArguments @('exec', $redisContainer, 'redis-server', '--version')
        | Select-Object -First 1
    ).Trim()

    [void] (Invoke-Docker -DockerArguments @(
        'run',
        '--detach',
        '--name', $postgresContainer,
        '--label', "$probeLabelName=$probeLabelValue",
        '--env', 'POSTGRES_PASSWORD=probe-only-password',
        '--env', 'POSTGRES_DB=probe_db',
        'pgvector/pgvector:pg17'
    ))
    [void] (Wait-ForProbe -Description 'PostgreSQL readiness' -Probe {
        $output = & docker exec $postgresContainer pg_isready -U postgres -d probe_db 2>&1
        if ($LASTEXITCODE -eq 0) {
            return ($output -join "`n")
        }
        return $null
    })
    $pgvectorVersion = (
        Invoke-Docker -DockerArguments @(
            'exec',
            $postgresContainer,
            'psql',
            '-U', 'postgres',
            '-d', 'probe_db',
            '-v', 'ON_ERROR_STOP=1',
            '-tAc',
            'CREATE EXTENSION IF NOT EXISTS vector; SELECT extversion FROM pg_extension WHERE extname = ''vector'';'
        )
        | Select-Object -Last 1
    ).Trim()
    if ($pgvectorVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "pgvector extension probe returned an unexpected version: $pgvectorVersion"
    }
} finally {
    Remove-ProbeContainer -Name $redisContainer
    Remove-ProbeContainer -Name $postgresContainer
}

Write-Output (
    'CONTAINER_RUNTIME_VALIDATION_OK ' +
    "ostype=$dockerOperatingSystem " +
    "python=$($pythonVersion.Replace(' ', '_')) " +
    "redis_ping=$redisPing " +
    "redis_version=$($redisVersion.Split(' ')[2]) " +
    "pgvector=$pgvectorVersion"
)
