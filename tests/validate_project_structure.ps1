[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot

$requiredPaths = @(
    '.dockerignore',
    '.env.example',
    '.gitattributes',
    '.gitignore',
    '.nvmrc',
    '.github/workflows/ci.yml',
    'alembic.ini',
    'pyproject.toml',
    'requirements.lock',
    'requirements-dev.lock',
    'apps/api/main.py',
    'apps/worker/__init__.py',
    'apps/scheduler/__init__.py',
    'apps/web/.npmrc',
    'apps/web/package.json',
    'apps/web/package-lock.json',
    'apps/web/playwright.config.ts',
    'apps/web/src/App.test.tsx',
    'apps/web/e2e/auth-jobs.spec.ts',
    'backend/app/main.py',
    'backend/app/auth/bootstrap.py',
    'backend/app/auth/dependencies.py',
    'backend/app/auth/models.py',
    'backend/app/auth/passwords.py',
    'backend/app/auth/repository.py',
    'backend/app/auth/routes.py',
    'backend/app/auth/schemas.py',
    'backend/app/auth/service.py',
    'backend/app/auth/tokens.py',
    'backend/app/core/context.py',
    'backend/app/core/errors.py',
    'backend/app/core/health.py',
    'backend/app/core/logging.py',
    'backend/app/core/middleware.py',
    'backend/app/core/settings.py',
    'backend/app/documents/models.py',
    'backend/app/documents/routes.py',
    'backend/app/documents/schemas.py',
    'backend/app/documents/service.py',
    'backend/app/documents/validation.py',
    'backend/app/infrastructure/database.py',
    'backend/app/infrastructure/redis.py',
    'backend/app/infrastructure/runtime.py',
    'backend/app/infrastructure/storage.py',
    'backend/app/jobs/models.py',
    'backend/app/jobs/routes.py',
    'backend/app/jobs/schemas.py',
    'backend/app/jobs/service.py',
    'compose.yaml',
    'compose.override.yaml',
    'deploy/docker/backend.Dockerfile',
    'migrations/env.py',
    'migrations/versions/0001_enable_pgvector.py',
    'migrations/versions/0002_create_users.py',
    'migrations/versions/0003_create_jobs.py',
    'migrations/versions/0004_create_resume_documents.py',
    'scripts/project.ps1',
    'tests/unit/test_api_smoke.py',
    'tests/unit/test_auth.py',
    'tests/unit/test_database.py',
    'tests/unit/test_health.py',
    'tests/unit/test_jobs.py',
    'tests/unit/test_documents.py',
    'tests/validate_api_runtime.ps1',
    'tests/validate_auth.ps1',
    'tests/validate_compose_stack.ps1'
    'tests/validate_document_upload.ps1'
    'tests/validate_migrations.ps1'
    'tests/validate_web.ps1'
)

foreach ($relativePath in $requiredPaths) {
    $absolutePath = Join-Path $repoRoot $relativePath
    if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
        throw "Missing required IMP-001 file: $relativePath"
    }
}

$requiredDirectories = @(
    'apps/api',
    'apps/worker',
    'apps/scheduler',
    'apps/web',
    'backend/app/core',
    'backend/app/infrastructure',
    'deploy/compose',
    'deploy/nginx',
    'tests/unit'
)

foreach ($relativePath in $requiredDirectories) {
    $absolutePath = Join-Path $repoRoot $relativePath
    if (-not (Test-Path -LiteralPath $absolutePath -PathType Container)) {
        throw "Missing required IMP-001 directory: $relativePath"
    }
}

$nodeTarget = (Get-Content -LiteralPath (Join-Path $repoRoot '.nvmrc') -Raw).Trim()
if ($nodeTarget -ne '22.23.2') {
    throw "Unexpected Node target in .nvmrc: $nodeTarget"
}

$pyproject = Get-Content -LiteralPath (Join-Path $repoRoot 'pyproject.toml') -Raw
if ($pyproject -notmatch 'requires-python\s*=\s*">=3\.12,<3\.13"') {
    throw 'pyproject.toml must constrain the project runtime to Python 3.12.'
}

$dockerfile = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile') -Raw
if ($dockerfile -notmatch '(?m)^FROM python:3\.12\.14-slim-bookworm AS base$') {
    throw 'The backend Dockerfile must use the pinned Python 3.12.14 base tag.'
}
if ($dockerfile -match '(?im)^FROM\s+\S*:latest') {
    throw 'Dockerfiles must not use a latest tag.'
}
foreach ($migrationAsset in @('alembic.ini', 'migrations')) {
    if ($dockerfile -notmatch "(?m)^COPY $([regex]::Escape($migrationAsset)) ") {
        throw "The backend image must include the migration asset: $migrationAsset"
    }
}

$composeConfig = Get-Content -LiteralPath (Join-Path $repoRoot 'compose.yaml') -Raw
foreach ($serviceName in @('api', 'migrate')) {
    if ($composeConfig -notmatch "(?m)^  $serviceName`:") {
        throw "Compose must define the IMP-004 service: $serviceName"
    }
}

$ciWorkflow = Get-Content -LiteralPath (Join-Path $repoRoot '.github/workflows/ci.yml') -Raw
if ($ciWorkflow -notmatch '(?m)run: \./scripts/project\.ps1 verify$') {
    throw 'CI must call the same project verify gate used locally.'
}
if ($ciWorkflow -match '(?m)^\s*QWEN_API_KEY:') {
    throw 'Ordinary CI must not receive a real-model API key.'
}

$packageJson = Get-Content -LiteralPath (Join-Path $repoRoot 'apps/web/package.json') -Raw | ConvertFrom-Json
foreach ($scriptName in @('dev', 'typecheck', 'test', 'build', 'e2e')) {
    if ($null -eq $packageJson.scripts.$scriptName) {
        throw "Missing frontend script: $scriptName"
    }
}

$npmConfig = Get-Content -LiteralPath (Join-Path $repoRoot 'apps/web/.npmrc')
if ($npmConfig -notcontains 'registry=https://registry.npmjs.org/') {
    throw 'The frontend project must use the official npm registry.'
}

$gitignore = Get-Content -LiteralPath (Join-Path $repoRoot '.gitignore')
foreach ($ignorePattern in @('.env', '.env.*', '!.env.example', 'node_modules/', '.venv/')) {
    if ($gitignore -notcontains $ignorePattern) {
        throw "Missing .gitignore rule: $ignorePattern"
    }
}

$envVariableNames = Get-Content -LiteralPath (Join-Path $repoRoot '.env.example') |
    Where-Object { $_ -match '^[A-Z][A-Z0-9_]*=' } |
    ForEach-Object { ($_ -split '=', 2)[0] }

foreach ($requiredVariable in @(
    'APP_ENV',
    'JWT_SECRET',
    'DATABASE_URL',
    'REDIS_URL',
    'CELERY_BROKER_URL',
    'STORAGE_ROOT',
    'QWEN_API_KEY',
    'MOCK_MODEL_MODE'
)) {
    if ($envVariableNames -notcontains $requiredVariable) {
        throw "Missing .env.example variable: $requiredVariable"
    }
}

$envTemplate = Get-Content -LiteralPath (Join-Path $repoRoot '.env.example') -Raw
if ($envTemplate -match '(?i)(sk-[a-z0-9]{16,}|-----BEGIN [A-Z ]+PRIVATE KEY-----)') {
    throw '.env.example appears to contain a usable secret.'
}

Write-Output "PROJECT_STRUCTURE_VALIDATION_OK files=$($requiredPaths.Count) directories=$($requiredDirectories.Count) node=$nodeTarget"
