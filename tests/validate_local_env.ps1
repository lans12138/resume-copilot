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

    # The documented fresh-clone path is "copy the example, replace the secrets,
    # run scripts/start_stack.ps1". That script refuses to start while a
    # *blocking* variable still holds a template placeholder, so the file this
    # initializer produces has to satisfy it.
    #
    # The two agreed only by accident before: the guard rejected every
    # placeholder, while the initializer deliberately leaves the inert
    # model/Langfuse ones in place -- so the documented path could not be
    # followed at all, and no gate noticed because the one-command probe writes
    # its own synthetic environment instead of using a generated one.
    $startStack = Get-Content -Encoding UTF8 -Raw -LiteralPath (
        Join-Path $repoRoot 'scripts\start_stack.ps1'
    )
    $blockingDeclaration = [regex]::Match(
        $startStack,
        '(?ms)\$blockingPlaceholders\s*=\s*@\((.*?)\)'
    )
    if (-not $blockingDeclaration.Success) {
        throw 'start_stack.ps1 no longer declares $blockingPlaceholders; update this contract.'
    }
    $blockingVariables = @(
        [regex]::Matches($blockingDeclaration.Groups[1].Value, "'([A-Z_]+)'") |
            ForEach-Object { $_.Groups[1].Value }
    )
    if ($blockingVariables.Count -eq 0) {
        throw 'start_stack.ps1 declares an empty placeholder blocklist.'
    }
    $leftoverPlaceholders = @(
        [regex]::Matches($content, '(?m)^([A-Z_]+)=replace-with-[a-z-]+') |
            ForEach-Object { $_.Groups[1].Value }
    )
    $blockedLeftovers = @($leftoverPlaceholders | Where-Object { $blockingVariables -contains $_ })
    if ($blockedLeftovers.Count -gt 0) {
        throw (
            'The generated environment still holds placeholders that ' +
            "scripts/start_stack.ps1 refuses to start with: $($blockedLeftovers -join ', ')"
        )
    }

    Write-Output (
        'LOCAL_ENV_VALIDATION_OK jwt_bytes=48 database_password_bytes=24 ' +
        'overwrite=guarded output=redacted ' +
        "start_stack_blocking=$($blockingVariables.Count) inert_leftovers=$($leftoverPlaceholders.Count)"
    )
}
finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
