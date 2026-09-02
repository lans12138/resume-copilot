$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$setupScriptPath = Join-Path $repositoryRoot 'scripts\configure_windows_environment.ps1'
$runtimeProbePath = Join-Path $repositoryRoot 'tests\validate_container_runtime.ps1'

if (-not (Test-Path -LiteralPath $setupScriptPath)) {
    throw '缺少 Windows 环境配置脚本。'
}
if (-not (Test-Path -LiteralPath $runtimeProbePath)) {
    throw '缺少容器运行时验证脚本。'
}

$setupScript = Get-Content -Raw -LiteralPath $setupScriptPath
$requiredFragments = @(
    "ValidateSet('Validate', 'ConfigureNodePath', 'EnableWsl', 'EnableWslFeatures', 'InstallWslPackage', 'InstallUbuntu', 'InstallDocker')",
    'D:\develop',
    'node-v22.23.2-win-x64',
    '--install --no-distribution --web-download',
    '--install --inbox --no-distribution',
    'VirtualMachinePlatform',
    'Microsoft-Windows-Subsystem-Linux',
    'wsl.2.7.12.0.x64.msi',
    'Microsoft Corporation',
    'System32\msiexec.exe',
    '/norestart',
    'D:\develop\wsl\Ubuntu-24.04',
    '--install Ubuntu-24.04 --location $ubuntuRoot --no-launch --web-download',
    'ubuntu-24.04.4-wsl-amd64.wsl',
    '9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5',
    '--install --from-file $ubuntuInstaller --location $ubuntuRoot --name Ubuntu-24.04 --no-launch',
    'Get-FileHash -LiteralPath $ubuntuInstaller -Algorithm SHA256',
    'PendingRestart = -not $distributionFilesReady',
    '/Enable-Feature',
    '/NoRestart',
    '--installation-dir=',
    '--backend=wsl-2',
    '--wsl-default-data-root=',
    '--no-windows-containers',
    'Get-AuthenticodeSignature',
    'Assert-Administrator',
    'Assert-PathUnderDevelop'
)

$missingFragments = @(
    $requiredFragments | Where-Object { -not $setupScript.Contains($_) }
)
if ($missingFragments.Count -gt 0) {
    throw "环境配置脚本缺少安全契约：$($missingFragments -join ', ')"
}

$forbiddenFragments = @(
    'Remove-Item -Recurse',
    'rm -rf',
    '--accept-license',
    'QWEN_API_KEY=',
    'JWT_SECRET='
)

$foundForbiddenFragments = @(
    $forbiddenFragments | Where-Object { $setupScript.Contains($_) }
)
if ($foundForbiddenFragments.Count -gt 0) {
    throw "环境配置脚本包含禁止内容：$($foundForbiddenFragments -join ', ')"
}

$syntaxErrors = $null
$tokens = $null
[void] [System.Management.Automation.Language.Parser]::ParseFile(
    $setupScriptPath,
    [ref] $tokens,
    [ref] $syntaxErrors
)
if ($syntaxErrors.Count -gt 0) {
    throw "环境配置脚本存在语法错误：$($syntaxErrors.Message -join '; ')"
}

$runtimeProbe = Get-Content -Raw -LiteralPath $runtimeProbePath
$runtimeProbeContracts = @(
    'io.openai.resume-env-probe',
    'hello-world:linux',
    'python:3.12-slim',
    'redis:7.4-alpine',
    'pgvector/pgvector:pg17',
    'CREATE EXTENSION IF NOT EXISTS vector',
    'Refusing to remove container without the probe label',
    'CONTAINER_RUNTIME_VALIDATION_OK'
)
$missingRuntimeContracts = @(
    $runtimeProbeContracts | Where-Object { -not $runtimeProbe.Contains($_) }
)
if ($missingRuntimeContracts.Count -gt 0) {
    throw "容器运行时探针缺少安全或验证契约：$($missingRuntimeContracts -join ', ')"
}

$runtimeSyntaxErrors = $null
$runtimeTokens = $null
[void] [System.Management.Automation.Language.Parser]::ParseFile(
    $runtimeProbePath,
    [ref] $runtimeTokens,
    [ref] $runtimeSyntaxErrors
)
if ($runtimeSyntaxErrors.Count -gt 0) {
    throw "容器运行时探针存在语法错误：$($runtimeSyntaxErrors.Message -join '; ')"
}

$validationResult = & $setupScriptPath -Action Validate
if ($validationResult.DevelopRoot -ne 'D:\develop') {
    throw "环境根目录异常：$($validationResult.DevelopRoot)"
}
if ($validationResult.NodeTargetVersion -ne 'v22.23.2') {
    throw "Node 目标版本未就绪：$($validationResult.NodeTargetVersion)"
}

Write-Output (
    'ENVIRONMENT_SETUP_VALIDATION_OK ' +
    "required=$($requiredFragments.Count) " +
    "runtime_contracts=$($runtimeProbeContracts.Count) " +
    "forbidden=$($forbiddenFragments.Count) " +
    "node_target=$($validationResult.NodeTargetVersion)"
)
