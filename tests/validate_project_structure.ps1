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
    'backend/app/documents/parsers.py',
    'backend/app/documents/routes.py',
    'backend/app/documents/schemas.py',
    'backend/app/documents/service.py',
    'backend/app/documents/validation.py',
    'backend/app/infrastructure/database.py',
    'backend/app/infrastructure/model_registry.py',
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
    'deploy/docker/web.Dockerfile',
    'deploy/nginx/nginx.conf',
    'deploy/nginx/security-headers.conf',
    'deploy/nginx/templates/default.conf.template',
    'scripts/start_stack.ps1',
    'tests/validate_nginx_proxy.ps1',
    'tests/validate_one_command_up.ps1',
    'migrations/env.py',
    'migrations/versions/0001_enable_pgvector.py',
    'migrations/versions/0002_create_users.py',
    'migrations/versions/0003_create_jobs.py',
    'migrations/versions/0004_create_resume_documents.py',
    'migrations/versions/0008_create_workflow_tables.py',
    'scripts/project.ps1',
    'scripts/recover_docker_desktop.ps1',
    'scripts/initialize_local_env.ps1',
    'tests/unit/test_api_smoke.py',
    'tests/validate_docker_recovery.ps1',
    'tests/validate_local_env.ps1',
    'tests/unit/test_auth.py',
    'tests/unit/test_database.py',
    'tests/unit/test_health.py',
    'tests/unit/test_jobs.py',
    'tests/unit/test_model_registry.py',
    'tests/unit/test_parsers.py',
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

# FIN-012: the web image is the public entry point, so its two properties that a
# reviewer cannot see at runtime are pinned here — a fixed Node base for the
# build stage, and a fixed Nginx base for the serve stage.
$webDockerfile = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/docker/web.Dockerfile') -Raw
if ($webDockerfile -notmatch '(?m)^FROM node:22\.\d+\.\d+-bookworm-slim AS build$') {
    throw 'The web Dockerfile must build the bundle in a pinned Node base image.'
}
if ($webDockerfile -notmatch '(?m)^FROM nginx:\d+\.\d+\.\d+-alpine AS runtime$') {
    throw 'The web Dockerfile must serve the bundle from a pinned Nginx base image.'
}
if ($webDockerfile -match '(?im)^FROM\s+\S*:latest') {
    throw 'Dockerfiles must not use a latest tag.'
}

# The SSE contract lives in two places that must agree: the proxy must disable
# buffering, and the upstream must declare the same intent. Asserting both means
# a future edit to either side alone is caught here rather than in a browser
# session that appears to hang.
$nginxTemplate = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy/nginx/templates/default.conf.template') -Raw
foreach ($directive in @('proxy_buffering off', 'proxy_cache off', 'chunked_transfer_encoding on')) {
    if ($nginxTemplate -notmatch "(?m)^\s*$([regex]::Escape($directive));") {
        throw "The Nginx SSE location must set: $directive"
    }
}
if ($nginxTemplate -notmatch '(?m)^\s*location ~ \^/api/v1/\(application-runs\|match-runs\)') {
    throw 'The Nginx config must scope the unbuffered location to the two SSE routes.'
}

$composeConfig = Get-Content -LiteralPath (Join-Path $repoRoot 'compose.yaml') -Raw
foreach ($serviceName in @('api', 'migrate', 'worker', 'scheduler', 'postgres', 'redis', 'web')) {
    if ($composeConfig -notmatch "(?m)^  $serviceName`:") {
        throw "Compose must define the FIN-012 service: $serviceName"
    }
}
# The API must not accept traffic before the schema is at head; that gate is the
# whole reason migrate is on the default profile instead of behind `tools`.
if ($composeConfig -notmatch '(?ms)^  api:.*?depends_on:.*?migrate:\s*\r?\n\s+condition: service_completed_successfully') {
    throw 'The api service must wait for migrate to complete successfully.'
}

$ciWorkflow = Get-Content -LiteralPath (Join-Path $repoRoot '.github/workflows/ci.yml') -Raw
if ($ciWorkflow -notmatch '(?m)run: \./scripts/project\.ps1 verify\r?$') {
    throw 'CI must call the same project verify gate used locally.'
}
if ($ciWorkflow -match '(?m)^\s*QWEN_API_KEY:') {
    throw 'Ordinary CI must not receive a real-model API key.'
}
# The static gates are cheap and must not be dropped in favour of only the heavy
# job: a broken document set should fail the PR in a minute, not in an hour.
foreach ($staticGate in @('./tests/validate_documents.ps1', './tests/validate_project_structure.ps1')) {
    if ($ciWorkflow -notmatch "(?m)^\s*run: $([regex]::Escape($staticGate))\r?$") {
        throw "CI must run the static gate: $staticGate"
    }
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
