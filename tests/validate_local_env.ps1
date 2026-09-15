$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$initializer = Join-Path $repoRoot 'scripts\initialize_local_env.ps1'
$template = Join-Path $repoRoot '.env.example'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("resume-env-test-" + [guid]::NewGuid())
$testEnv = Join-Path $testRoot '.env'

try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $output = @(& $initializer -TemplatePath $template -OutputPath $testEnv)
    $content = [System.IO.File]::ReadAllText($testEnv)

    $jwtMatch = [regex]::Match($content, '(?m)^JWT_SECRET=([0-9a-f]{96})$')
    $passwordMatch = [regex]::Match($content, '(?m)^POSTGRES_PASSWORD=([0-9a-f]{48})$')
    if (-not $jwtMatch.Success) {
        throw 'Generated JWT_SECRET does not meet the 48-byte random contract.'
    }
    if (-not $passwordMatch.Success) {
        throw 'Generated POSTGRES_PASSWORD does not meet the 24-byte random contract.'
    }
    $databasePassword = $passwordMatch.Groups[1].Value
    if ($content -notmatch "(?m)^DATABASE_URL=postgresql\+asyncpg://resume_app:$databasePassword@postgres:5432/resume_copilot$") {
        throw 'DATABASE_URL does not reuse the generated database password.'
    }
    if ($content -notmatch '(?m)^MOCK_MODEL_MODE=true$') {
        throw 'Local environment must default to FakeModel mode.'
    }
    if (($output -join "`n").Contains($jwtMatch.Groups[1].Value) -or
        ($output -join "`n").Contains($databasePassword)) {
        throw 'Initializer output leaked a generated secret.'
    }

    $refusedOverwrite = $false
    try {
        & $initializer -TemplatePath $template -OutputPath $testEnv | Out-Null
    }
    catch {
        $refusedOverwrite = $_.Exception.Message -match 'already exists'
    }
    if (-not $refusedOverwrite) {
        throw 'Initializer did not refuse to overwrite an existing environment file.'
    }

    & $initializer -TemplatePath $template -OutputPath $testEnv -Force | Out-Null
    $replacement = [System.IO.File]::ReadAllText($testEnv)
    if ($replacement -eq $content) {
        throw 'Forced initialization did not rotate generated secrets.'
    }

    Write-Output 'LOCAL_ENV_VALIDATION_OK jwt_bytes=48 database_password_bytes=24 overwrite=guarded output=redacted'
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
