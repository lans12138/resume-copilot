[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet(
        'bootstrap',
        'docker-recover',
        'docker-recover-test',
        'env-init',
        'env-test',
        'api',
        'api-smoke',
        'auth-test',
        'compose-test',
        'job-test',
        'document-upload-test',
        'application-entry-test',
        'parser-test',
        'migration-test',
        'seed',
        'reset-demo',
        'web',
        'backend-lint',
        'backend-typecheck',
        'backend-test',
        'web-typecheck',
        'web-test',
        'web-build',
        'web-e2e',
        'verify'
    )]
    [string] $Action = 'verify'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDevelopmentImage = 'resume-copilot-backend-development:local'
$backendRuntimeImage = 'resume-copilot-api:local'
$backendDockerfile = Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'
$webDirectory = Join-Path $repoRoot 'apps/web'
$localEnvFile = Join-Path $repoRoot '.env'

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [string] $Command,

        [Parameter()]
        [string[]] $Arguments = @()
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

function Build-BackendDevelopmentImage {
    Invoke-Checked 'docker' @(
        'build',
        '--file', $backendDockerfile,
        '--target', 'development',
        '--tag', $backendDevelopmentImage,
        $repoRoot
    )
}

function Invoke-BackendTool {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    Invoke-Checked 'docker' (@('run', '--rm', $backendDevelopmentImage) + $Arguments)
}

function Install-WebDependencies {
    Push-Location $webDirectory
    try {
        Invoke-Checked 'npm' @('ci')
    }
    finally {
        Pop-Location
    }
}

function Invoke-WebTool {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    Push-Location $webDirectory
    try {
        Invoke-Checked 'npm' $Arguments
    }
    finally {
        Pop-Location
    }
}

Push-Location $repoRoot
try {
    switch ($Action) {
        'docker-recover' {
            & (Join-Path $repoRoot 'scripts/recover_docker_desktop.ps1')
        }
        'docker-recover-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_docker_recovery.ps1')
        }
        'env-init' {
            & (Join-Path $repoRoot 'scripts/initialize_local_env.ps1')
        }
        'env-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_local_env.ps1')
        }
        'bootstrap' {
            Build-BackendDevelopmentImage
            Install-WebDependencies
        }
        'api' {
            if (-not (Test-Path -LiteralPath $localEnvFile -PathType Leaf)) {
                throw 'Missing .env. Copy .env.example to .env and replace every secret placeholder.'
            }
            Invoke-Checked 'docker' @(
                'build',
                '--file', $backendDockerfile,
                '--target', 'runtime',
                '--tag', $backendRuntimeImage,
                $repoRoot
            )
            Invoke-Checked 'docker' @(
                'run', '--rm',
                '--env-file', $localEnvFile,
                '--publish', '8000:8000',
                $backendRuntimeImage
            )
        }
        'api-smoke' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_api_runtime.ps1')
        }
        'auth-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_auth.ps1')
        }
        'compose-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_compose_stack.ps1')
        }
        'job-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_auth.ps1')
        }
        'document-upload-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_document_upload.ps1')
        }
        'application-entry-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_application_entry.ps1')
        }
        'parser-test' {
            Build-BackendDevelopmentImage
            Invoke-BackendTool @('pytest', '-q', 'tests/unit/test_parsers.py')
        }
        'migration-test' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_migrations.ps1')
        }
        'seed' {
            Invoke-Checked 'docker' @('compose', '--profile', 'tools', 'run', '--rm', '--build', 'seed')
        }
        'reset-demo' {
            Invoke-Checked 'docker' @('compose', '--profile', 'tools', 'run', '--rm', '--build', 'seed', '--', '--reset')
        }
        'web' {
            Invoke-WebTool @('run', 'dev', '--', '--host', '0.0.0.0')
        }
        'backend-lint' {
            Build-BackendDevelopmentImage
            Invoke-BackendTool @('ruff', 'check', 'backend', 'apps', 'tests/unit')
        }
        'backend-typecheck' {
            Build-BackendDevelopmentImage
            Invoke-BackendTool @('mypy', 'backend', 'apps', 'tests/unit')
        }
        'backend-test' {
            Build-BackendDevelopmentImage
            Invoke-BackendTool @('pytest', '-q')
        }
        'web-typecheck' {
            Install-WebDependencies
            Invoke-WebTool @('run', 'typecheck')
        }
        'web-test' {
            Install-WebDependencies
            Invoke-WebTool @('run', 'test')
        }
        'web-build' {
            Install-WebDependencies
            Invoke-WebTool @('run', 'build')
        }
        'web-e2e' {
            Install-WebDependencies
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_web.ps1')
        }
        'verify' {
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_project_structure.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_docker_recovery.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_local_env.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_compose_stack.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_migrations.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_idempotency.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_document_pipeline.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_worker.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_auth.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_document_upload.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_application_entry.ps1')
            Build-BackendDevelopmentImage
            Invoke-BackendTool @('ruff', 'check', 'backend', 'apps', 'tests/unit', 'tests/integration')
            Invoke-BackendTool @('mypy', 'backend', 'apps', 'tests/unit')
            Invoke-BackendTool @('pytest', '-q')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_api_runtime.ps1')
            Install-WebDependencies
            Invoke-WebTool @('run', 'typecheck')
            Invoke-WebTool @('run', 'test')
            Invoke-WebTool @('run', 'build')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_web.ps1')
            Invoke-Checked 'pwsh' @('-NoProfile', '-File', 'tests/validate_documents.ps1')
        }
    }
}
finally {
    Pop-Location
}
