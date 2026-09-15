[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDevelopmentImage = 'resume-copilot-backend-development:local'

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    # Merge native stderr as data, not as a terminating error (PS 5.1 turns every
    # stderr line of a native command into an ErrorRecord, and
    # ``$ErrorActionPreference = 'Stop'`` would abort the probe mid-build).
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & docker @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')`n$($output -join "`n")"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

Invoke-Docker -Arguments @(
    'build',
    '--file', (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'),
    '--target', 'development',
    '--tag', $backendDevelopmentImage,
    $repoRoot
) | Out-Null

# FIN-008 第三项：连通诊断必须能在不打印凭据的前提下运行。探针只验证“命令可运行且输出
# 不含密钥”，不访问真实端点——CI 里没有模型凭据，而一个需要联网才能通过的门禁会让每个
# 离线构建都变红。真实连通性由运维手动执行同一条命令确认。
#
# 这里刻意注入一个明显的哨兵密钥：若诊断的任何一行（含失败描述、端点、响应体）回显了它，
# 探针必须失败。凭据泄露最可能发生的地方正是这种“让人复制进工单”的输出。
$sentinelKey = 'sk-probe-SENTINEL-must-not-appear-in-output'
# The whole configuration must be complete, not just the model fields: Settings
# validates as a unit, and a probe that dies at "configuration invalid" would
# never reach the code it is supposed to be exercising.
$diagnoseArgs = @(
    'run', '--rm',
    '--env', 'MOCK_MODEL_MODE=false',
    '--env', 'MODEL_BASE_URL=https://dashscope.invalid/compatible-mode/v1',
    '--env', "QWEN_API_KEY=$sentinelKey",
    # ``.invalid`` is reserved by RFC 2606 and never resolves, so no request can
    # escape CI; the transport classifies the failure as transient and the
    # command returns exit 2 rather than hanging.
    '--env', 'MODEL_TIMEOUT_SECONDS=1',
    '--env', 'JWT_SECRET=probe-only-secret-that-is-long-enough-to-pass-validation-000000',
    '--env', 'DATABASE_URL=postgresql+asyncpg://probe:probe@postgres/probe',
    '--env', 'REDIS_URL=redis://redis:6379/0',
    '--env', 'CELERY_BROKER_URL=redis://redis:6379/1',
    '--env', 'STORAGE_ROOT=/tmp/resume-model-probe',
    $backendDevelopmentImage,
    'python', '-m', 'backend.app.infrastructure.model_diagnose'
)

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $output = & docker @diagnoseArgs 2>&1
    $exitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousPreference
}
$lines = @($output | ForEach-Object { $_.ToString() })
$lines | ForEach-Object { Write-Host $_ }

# 0 = both reachable, 1 = permanent, 2 = transient. Anything else means the
# command never ran to completion, which is a broken diagnostic rather than a
# broken endpoint.
if ($exitCode -ne 1 -and $exitCode -ne 2) {
    throw "The connectivity diagnostic did not run cleanly (exit $exitCode)."
}

$joined = $lines -join "`n"
if ($joined -match [regex]::Escape($sentinelKey)) {
    throw 'The connectivity diagnostic echoed the API key. Credential redaction is broken.'
}
if ($joined -notmatch 'model connectivity diagnostic') {
    throw 'The connectivity diagnostic produced no recognisable report.'
}
if ($joined -notmatch 'RESULT:') {
    throw 'The connectivity diagnostic produced no RESULT line.'
}
# The endpoint is reserved-and-unresolvable by construction, so a green result
# would mean the failure classification is not actually being exercised.
if ($exitCode -eq 0) {
    throw 'The diagnostic reported success against a non-resolving endpoint.'
}
# The failure must be classified, not merely reported: this is the bit the Celery
# layer uses to decide whether burning another attempt is worth anything.
if ($joined -notmatch '\((retryable|permanent)\)') {
    throw 'The diagnostic reported a failure without classifying it.'
}

# And the offline path: with the fake gateway selected nothing is contacted, which
# is the mode ordinary CI runs in.
$mockOutput = Invoke-Docker -Arguments @(
    'run', '--rm',
    '--env', 'MOCK_MODEL_MODE=true',
    '--env', 'JWT_SECRET=probe-only-secret-that-is-long-enough-to-pass-validation-000000',
    '--env', 'DATABASE_URL=postgresql+asyncpg://probe:probe@postgres/probe',
    '--env', 'REDIS_URL=redis://redis:6379/0',
    '--env', 'CELERY_BROKER_URL=redis://redis:6379/1',
    '--env', 'STORAGE_ROOT=/tmp/resume-model-probe',
    $backendDevelopmentImage,
    'python', '-m', 'backend.app.infrastructure.model_diagnose'
)
$mockJoined = $mockOutput -join "`n"
if ($mockJoined -notmatch 'mock_model_mode is enabled') {
    throw 'The diagnostic did not report that mock mode contacts nothing.'
}

# FIN-008 第四项：合成数据集必须在容器内真实注册（不是仅定义），否则 dataset_versions
# 会一直为空，评测结果也就无法回指到具体数据集版本。
#
# NOTE: the script is built in its own variable. Windows PowerShell 5.1 rejects a
# here-string used directly as an element of an array literal ("unexpected token"),
# so it must be assigned before it is passed as an argument.
$registrationScript = @'
import asyncio

from backend.app.evaluations.datasets import register_builtin_datasets
from backend.app.evaluations.repository import InMemoryDatasetVersionRepository
from backend.app.evaluations.service import EvaluationService

service = EvaluationService(
    datasets=InMemoryDatasetVersionRepository(),
    runs=None,
)


async def main():
    registered = await register_builtin_datasets(service)
    for item in registered:
        assert len(item.content_hash) == 64, item.name
    names = sorted(item.name for item in registered)
    print("registered:", ",".join(names))
    assert len(names) == 3, names


asyncio.run(main())
'@

$registrationOutput = Invoke-Docker -Arguments @(
    'run', '--rm',
    $backendDevelopmentImage,
    'python', '-c', $registrationScript
)
$registrationOutput | ForEach-Object { Write-Host $_ }
if (($registrationOutput -join "`n") -notmatch 'registered:') {
    throw 'The builtin synthetic datasets did not register inside the container.'
}

Write-Host 'FIN-008 model adapter diagnostics and dataset registration passed.'
