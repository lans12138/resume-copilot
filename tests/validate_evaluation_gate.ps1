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

# FIN-007 第四项：离线评测门禁。阈值未达标时必须以非零状态退出，否则 CI 会把一次
# 真实回归当成绿灯放过去。这里在开发镜像内跑 ``backend.app.evaluations.gate``——它不需要
# 数据库/Redis/模型配置，因此可在任何 CI 步骤里独立执行（普通 CI 用 FakeModel）。
Invoke-Docker -Arguments @(
    'build',
    '--file', (Join-Path $repoRoot 'deploy/docker/backend.Dockerfile'),
    '--target', 'development',
    '--tag', $backendDevelopmentImage,
    $repoRoot
) | Out-Null

# 完整报告（非 --quiet）故意保留：CI 红时日志必须直接指出是哪一项没达标，
# 否则排查要从头再跑一遍。
$output = Invoke-Docker -Arguments @(
    'run', '--rm',
    $backendDevelopmentImage,
    'python', '-m', 'backend.app.evaluations.gate'
)
$output | ForEach-Object { Write-Host $_ }

if (-not ($output -match 'RESULT: PASS')) {
    throw 'The offline evaluation gate did not report a passing result.'
}

Write-Host 'FIN-007 offline evaluation gate passed.'
