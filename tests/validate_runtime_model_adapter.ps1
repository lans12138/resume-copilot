[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendRuntimeImage = 'resume-copilot-api:local'

function Invoke-Docker {
    param(
        [Parameter(Mandatory)][string[]] $Arguments,
        # Most calls must succeed. The diagnostic probe below is the exception:
        # its *expected* outcome is a classified failure, which is a non-zero exit.
        [int[]] $AllowedExitCodes = @(0)
    )
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
    if ($exitCode -notin $AllowedExitCodes) {
        throw "Docker command failed: docker $($Arguments -join ' ')`n$($output -join "`n")"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

# PORT-001：真实模型适配器通过 httpx2 说话，而 httpx2 原先只声明在 dev extra 里，
# 生产锁文件不含它。运行镜像只装 requirements.lock，于是容器里 import qwen_chat /
# qwen_embedding 直接 ImportError——"把 MOCK_MODEL_MODE 切成 false 就能演示真实模型"
# 这条路径在容器里根本走不通，而 development 镜像装了 dev 依赖，用它验证等于没验证。
#
# 因此本探针只针对 runtime 目标：它是唯一真正对外服务的镜像。
Invoke-Docker -Arguments @(
    'build',
    '--file', (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'),
    '--target', 'runtime',
    '--tag', $backendRuntimeImage,
    $repoRoot
) | Out-Null

# 先证明这是运行镜像而不是开发镜像：若镜像里仍有 pytest / mypy / ruff，下面的导入
# 检查就会因为错误的依赖集而通过，探针也就失去了区分能力。
$packageProbe = 'import importlib.util as u; print("DEV_PRESENT:" + ",".join(n for n in ("pytest", "mypy", "ruff") if u.find_spec(n) is not None))'
$packageOutput = Invoke-Docker -Arguments @(
    'run', '--rm',
    $backendRuntimeImage,
    'python', '-c', $packageProbe
)
$packageLine = @(
    $packageOutput | Where-Object { $_ -match '^DEV_PRESENT:' } | Select-Object -Last 1
)
if (-not $packageLine) {
    throw 'The runtime image did not report which packages it carries.'
}
if ($packageLine -ne 'DEV_PRESENT:') {
    throw "The runtime image still carries development-only packages: $packageLine"
}

# 真正的断言：只装生产依赖的镜像必须能导入真实适配器。导入 httpx2 本身也要检查，
# 因为 qwen_chat 是惰性导入它的——只导入模块不触发，才会掩盖缺失的依赖。
$importScript = @'
import httpx2

from backend.app.infrastructure import http_transport, qwen_chat, qwen_embedding  # noqa: F401

print("IMPORTS_OK:" + httpx2.__version__)
'@
$importOutput = Invoke-Docker -Arguments @(
    'run', '--rm',
    $backendRuntimeImage,
    'python', '-c', $importScript
)
$importOutput | ForEach-Object { Write-Host $_ }
if (($importOutput -join "`n") -notmatch 'IMPORTS_OK:') {
    throw 'The runtime image could not import the real model adapters.'
}

# 选择逻辑同样要在运行镜像里成立：关掉 mock 必须解析到真实网关，而不是悄悄退回
# FakeModel（那正是 README 承诺"缺配置就明确失败"要防的退化）。
$gatewayScript = @'
from backend.app.core.settings import Settings
from backend.app.infrastructure.model_gateway import build_model_gateway

gateway = build_model_gateway(Settings())
print("GATEWAY:" + type(gateway).__module__ + "." + type(gateway).__name__)
'@
$gatewayOutput = Invoke-Docker -Arguments @(
    'run', '--rm',
    '--env', 'MOCK_MODEL_MODE=false',
    '--env', 'MODEL_BASE_URL=https://dashscope.invalid/compatible-mode/v1',
    '--env', 'QWEN_API_KEY=sk-probe-SENTINEL-must-not-appear-in-output',
    '--env', 'JWT_SECRET=probe-only-secret-that-is-long-enough-to-pass-validation-000000',
    '--env', 'DATABASE_URL=postgresql+asyncpg://probe:probe@postgres/probe',
    '--env', 'REDIS_URL=redis://redis:6379/0',
    '--env', 'CELERY_BROKER_URL=redis://redis:6379/1',
    '--env', 'STORAGE_ROOT=/tmp/resume-model-probe',
    $backendRuntimeImage,
    'python', '-c', $gatewayScript
)
$gatewayOutput | ForEach-Object { Write-Host $_ }
$gatewayJoined = $gatewayOutput -join "`n"
if ($gatewayJoined -notmatch 'GATEWAY:backend\.app\.infrastructure\.qwen_chat\.') {
    throw 'Disabling mock mode did not resolve to the real Qwen gateway in the runtime image.'
}
if ($gatewayJoined -match 'sk-probe-SENTINEL') {
    throw 'The gateway probe echoed the API key.'
}

# 最后跑一次真实诊断命令：能走到 HTTP 层并给出分类结果，才说明依赖、配置与适配器
# 三者都在运行镜像里就位。端点用 RFC 2606 保留的 .invalid，永远不解析，因此 CI 不会
# 发出任何外部请求，失败会被分类为 transient（退出码 2）。
#
# 退出码 2 就是这个探针要的结果，所以这是唯一期望非零退出的调用：0 说明根本没走到
# 网络层，1 说明把 transient 误判成 permanent（那会让运维去修一份本来正确的配置）。
$diagnoseOutput = Invoke-Docker -AllowedExitCodes @(2) -Arguments @(
    'run', '--rm',
    '--env', 'MOCK_MODEL_MODE=false',
    '--env', 'MODEL_BASE_URL=https://dashscope.invalid/compatible-mode/v1',
    '--env', 'QWEN_API_KEY=sk-probe-SENTINEL-must-not-appear-in-output',
    '--env', 'MODEL_TIMEOUT_SECONDS=1',
    '--env', 'JWT_SECRET=probe-only-secret-that-is-long-enough-to-pass-validation-000000',
    '--env', 'DATABASE_URL=postgresql+asyncpg://probe:probe@postgres/probe',
    '--env', 'REDIS_URL=redis://redis:6379/0',
    '--env', 'CELERY_BROKER_URL=redis://redis:6379/1',
    '--env', 'STORAGE_ROOT=/tmp/resume-model-probe',
    $backendRuntimeImage,
    'python', '-m', 'backend.app.infrastructure.model_diagnose'
)
$diagnoseOutput | ForEach-Object { Write-Host $_ }
$diagnoseJoined = $diagnoseOutput -join "`n"
if ($diagnoseJoined -match 'sk-probe-SENTINEL') {
    throw 'The runtime diagnostic echoed the API key.'
}
if ($diagnoseJoined -notmatch 'RESULT:') {
    throw 'The runtime diagnostic produced no RESULT line.'
}
if ($diagnoseJoined -notmatch '\((retryable)\)') {
    throw 'The runtime diagnostic classified the unresolvable endpoint as permanent.'
}

Write-Host 'PORT-001 runtime image model adapter validation passed.'
