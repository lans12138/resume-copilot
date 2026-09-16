[CmdletBinding()]
param(
    [Parameter()]
    [string] $TemplatePath,

    [Parameter()]
    [string] $OutputPath,

    [Parameter()]
    [switch] $Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($TemplatePath)) {
    $TemplatePath = Join-Path $repoRoot '.env.example'
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $repoRoot '.env'
}

if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) {
    throw "Environment template not found: $TemplatePath"
}
if ((Test-Path -LiteralPath $OutputPath) -and -not $Force) {
    throw "Environment file already exists: $OutputPath. Use -Force to replace it."
}

function New-HexSecret {
    param(
        [Parameter(Mandatory)]
        [ValidateRange(16, 256)]
        [int] $ByteCount
    )

    $bytes = New-Object byte[] $ByteCount
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

$jwtPlaceholder = 'replace-with-at-least-48-random-characters-xxxxxxxxxxxx'
$databasePlaceholder = 'replace-with-local-database-password'
$template = [System.IO.File]::ReadAllText($TemplatePath)
if (-not $template.Contains($jwtPlaceholder) -or -not $template.Contains($databasePlaceholder)) {
    throw 'Environment template does not contain the expected local secret placeholders.'
}

$jwtSecret = New-HexSecret -ByteCount 48
$databasePassword = New-HexSecret -ByteCount 24
$content = $template.Replace($jwtPlaceholder, $jwtSecret).Replace(
    $databasePlaceholder,
    $databasePassword
)

$outputDirectory = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutputPath))
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    throw "Environment output directory not found: $outputDirectory"
}
[System.IO.File]::WriteAllText(
    $OutputPath,
    $content,
    [System.Text.UTF8Encoding]::new($false)
)

Write-Output "LOCAL_ENV_INITIALIZED path=$OutputPath mock_model=true"
