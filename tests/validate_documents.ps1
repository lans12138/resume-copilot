# This script holds Chinese literals and reads Chinese documents, so it needs an
# explicit encoding on both sides when it runs outside pwsh:
#
# - the file itself carries a UTF-8 BOM, because a BOM-less UTF-8 .ps1 is decoded with
#   the ANSI code page by the older engine and dies on a parser error before line 1;
# - every Get-Content passes -Encoding UTF8, because the ANSI default misaligns
#   multi-byte sequences, which silently swallows line starts and corrupts the code
#   fence counts the checks below depend on.
#
# Both are no-ops under pwsh. Without them this gate could only run in CI.
$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)]
        [bool] $Condition,

        [Parameter(Mandatory = $true)]
        [string] $Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Read-RepositoryDocument {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    $documentPath = Join-Path $repositoryRoot $Name
    Assert-Condition (Test-Path -LiteralPath $documentPath) "缺少文档：$Name"
    return Get-Content -Encoding UTF8 -Raw -LiteralPath $documentPath
}

$markdownFiles = Get-ChildItem -LiteralPath $repositoryRoot -Filter '*.md' -File
Assert-Condition ($markdownFiles.Count -ge 8) 'Markdown 文档数量异常。'

foreach ($markdownFile in $markdownFiles) {
    $documentLines = Get-Content -Encoding UTF8 -LiteralPath $markdownFile.FullName
    $backtickFenceCount = @($documentLines | Where-Object { $_ -match '^\s*```' }).Count
    $tildeFenceCount = @($documentLines | Where-Object { $_ -match '^\s*~~~' }).Count
    Assert-Condition (($backtickFenceCount % 2) -eq 0) "反引号代码围栏未闭合：$($markdownFile.Name)"
    Assert-Condition (($tildeFenceCount % 2) -eq 0) "波浪号代码围栏未闭合：$($markdownFile.Name)"

    for ($lineIndex = 0; $lineIndex -lt $documentLines.Count; $lineIndex++) {
        $isTableStart = $documentLines[$lineIndex] -match '^\|'
        if ($lineIndex -gt 0) {
            $isTableStart = $isTableStart -and ($documentLines[$lineIndex - 1] -notmatch '^\|')
        }

        if ($isTableStart) {
            Assert-Condition ($lineIndex + 1 -lt $documentLines.Count) "表格缺少分隔行：$($markdownFile.Name):$($lineIndex + 1)"
            Assert-Condition ($documentLines[$lineIndex + 1] -match '^\|(?:\s*:?-{3,}:?\s*\|)+\s*$') "表格分隔行非法：$($markdownFile.Name):$($lineIndex + 2)"
        }
    }

    $documentText = Get-Content -Encoding UTF8 -Raw -LiteralPath $markdownFile.FullName
    $localLinks = [regex]::Matches($documentText, '\]\(\./([^\)#]+)(?:#[^\)]*)?\)')
    foreach ($localLink in $localLinks) {
        $targetPath = Join-Path $repositoryRoot $localLink.Groups[1].Value
        Assert-Condition (Test-Path -LiteralPath $targetPath) "本地链接不存在：$($markdownFile.Name) -> $($localLink.Groups[1].Value)"
    }
}

$requirements = Read-RepositoryDocument '需求分析.md'
$overview = Read-RepositoryDocument '概要设计说明书.md'
$detailedDesign = Read-RepositoryDocument '详细设计说明书.md'
$implementationPlan = Read-RepositoryDocument '编码实现计划.md'
$environmentChecklist = Read-RepositoryDocument '环境配置清单.md'
$demoScript = Read-RepositoryDocument '演示脚本.md'

$requirementDefinitions = [regex]::Matches(
    $requirements,
    '(?m)^\|\s*([A-Z]+(?:-[A-Z]+)?)-(\d{3})\s*\|'
)
Assert-Condition ($requirementDefinitions.Count -eq 162) "需求条款数量异常：$($requirementDefinitions.Count)"

$duplicateRequirementIds = @(
    $requirementDefinitions |
        ForEach-Object { $_.Groups[1].Value + '-' + $_.Groups[2].Value } |
        Group-Object |
        Where-Object Count -gt 1
)
Assert-Condition ($duplicateRequirementIds.Count -eq 0) '存在重复需求 ID。'

foreach ($requirementGroup in ($requirementDefinitions | Group-Object { $_.Groups[1].Value })) {
    $actualNumbers = @(
        $requirementGroup.Group |
            ForEach-Object { [int] $_.Groups[2].Value } |
            Sort-Object
    )
    $expectedNumbers = @(1..$actualNumbers.Count)
    $differences = @(Compare-Object $actualNumbers $expectedNumbers)
    Assert-Condition ($differences.Count -eq 0) "需求 ID 不连续：$($requirementGroup.Name)"
}

$requirementApiSection = [regex]::Match(
    $requirements,
    '(?s)### 10\.1 REST 资源边界(.*?)### 10\.2'
).Groups[1].Value
$resourcePaths = @(
    [regex]::Matches(
        $requirementApiSection,
        '`(/[a-z][a-z0-9-]*(?:/[a-z0-9_{}-]+)*)`'
    ) |
        ForEach-Object { $_.Groups[1].Value } |
        Sort-Object -Unique
)
Assert-Condition ($resourcePaths.Count -eq 27) "需求资源路径数量异常：$($resourcePaths.Count)"

$missingResourcePaths = @(
    $resourcePaths | Where-Object { -not $detailedDesign.Contains($_) }
)
Assert-Condition ($missingResourcePaths.Count -eq 0) "详细设计缺少需求资源路径：$($missingResourcePaths -join ', ')"

$overviewMethodPaths = @(
    [regex]::Matches(
        $overview,
        '(?m)^\|\s*(GET|POST|PATCH|DELETE)\s+(/[^\s|]+)\s*\|'
    ) |
        ForEach-Object { $_.Groups[1].Value + ' ' + $_.Groups[2].Value } |
        Sort-Object -Unique
)
Assert-Condition ($overviewMethodPaths.Count -eq 46) "概要设计 API 方法数量异常：$($overviewMethodPaths.Count)"

$missingMethodPaths = @(
    $overviewMethodPaths | Where-Object { -not $detailedDesign.Contains($_) }
)
Assert-Condition ($missingMethodPaths.Count -eq 0) "详细设计缺少 API 方法：$($missingMethodPaths -join ', ')"

$detailedSectionMatches = [regex]::Matches($detailedDesign, '(?m)^##\s+(\d+)\.')
$detailedMainSections = $detailedSectionMatches.Count
Assert-Condition ($detailedMainSections -eq 24) "详细设计主章节数量异常：$detailedMainSections"
$detailedSectionNumbers = @($detailedSectionMatches | ForEach-Object { [int] $_.Groups[1].Value })
$detailedSectionDifferences = @(Compare-Object $detailedSectionNumbers @(1..24))
Assert-Condition ($detailedSectionDifferences.Count -eq 0) '详细设计主章节编号不连续。'

$requiredContracts = @(
    'active_application_run_id',
    '复合外键',
    'run_version 做 CAS',
    'FOR UPDATE SKIP LOCKED',
    'application/x-www-form-urlencoded',
    '每个重放批次',
    'Celery Beat',
    'Prompt Injection clean/injected',
    'MockScheduleBackend',
    'Last-Event-ID'
)

$missingContracts = @(
    $requiredContracts | Where-Object { -not $detailedDesign.Contains($_) }
)
Assert-Condition ($missingContracts.Count -eq 0) "详细设计缺少关键契约：$($missingContracts -join ', ')"

$planSectionMatches = [regex]::Matches($implementationPlan, '(?m)^##\s+(\d+)\.')
Assert-Condition ($planSectionMatches.Count -eq 21) "编码计划主章节数量异常：$($planSectionMatches.Count)"
$planSectionNumbers = @($planSectionMatches | ForEach-Object { [int] $_.Groups[1].Value })
$planSectionDifferences = @(Compare-Object $planSectionNumbers @(1..21))
Assert-Condition ($planSectionDifferences.Count -eq 0) '编码计划主章节编号不连续。'

$workPackageMatches = [regex]::Matches($implementationPlan, '(?m)^\|\s*IMP-(\d{3})\s*\|')
Assert-Condition ($workPackageMatches.Count -eq 30) "编码计划工作包数量异常：$($workPackageMatches.Count)"
$workPackageNumbers = @(
    $workPackageMatches |
        ForEach-Object { [int] $_.Groups[1].Value } |
        Sort-Object
)
$workPackageDifferences = @(Compare-Object $workPackageNumbers @(1..30))
Assert-Condition ($workPackageDifferences.Count -eq 0) '编码计划工作包编号不连续。'

$workPackageEstimateMatches = [regex]::Matches(
    $implementationPlan,
    '(?m)^\|\s*IMP-\d{3}\s*\|[^|]*\|[^|]*\|\s*([0-9.]+)\s*天\s*\|'
)
Assert-Condition ($workPackageEstimateMatches.Count -eq 30) '编码计划存在无法解析的工作包估算。'
$plannedDays = (
    $workPackageEstimateMatches |
        ForEach-Object { [decimal] $_.Groups[1].Value } |
        Measure-Object -Sum
).Sum
Assert-Condition ($plannedDays -eq 27) "编码计划理想工期异常：$plannedDays 天"

$requiredPlanContracts = @(
    '第 1 周：工程骨架、认证与岗位',
    '第 2 周：简历解析、校对与证据定位',
    '第 3 周：Embedding、混合检索与评测',
    '第 4 周：双运行图、证据报告与审批核心',
    '第 5 周：面试、SSE 与前端闭环',
    '第 6 周：加固、评测与发布',
    'Definition of Ready',
    'Definition of Done',
    '周出口门禁 G1',
    '周出口门禁 G6',
    '最后 2 天保留为显式缓冲',
    '每个工作包在同一提交中包含相应测试',
    '当前实际进度校准',
    '项目完工待办事项清单',
    '项目完工判定',
    'FIN-001 至 FIN-013'
)
$missingPlanContracts = @(
    $requiredPlanContracts | Where-Object { -not $implementationPlan.Contains($_) }
)
Assert-Condition ($missingPlanContracts.Count -eq 0) "编码计划缺少关键契约：$($missingPlanContracts -join ', ')"

$finishTaskMatches = [regex]::Matches($implementationPlan, '(?m)^\|\s*FIN-(\d{3})\s*\|')
Assert-Condition ($finishTaskMatches.Count -eq 13) "编码计划完工任务数量异常：$($finishTaskMatches.Count)"
$finishTaskNumbers = @(
    $finishTaskMatches |
        ForEach-Object { [int] $_.Groups[1].Value } |
        Sort-Object
)
$finishTaskDifferences = @(Compare-Object $finishTaskNumbers @(1..13))
Assert-Condition ($finishTaskDifferences.Count -eq 0) '编码计划完工任务编号不连续。'

$environmentSectionMatches = [regex]::Matches($environmentChecklist, '(?m)^##\s+(\d+)\.')
Assert-Condition ($environmentSectionMatches.Count -eq 19) "环境清单主章节数量异常：$($environmentSectionMatches.Count)"
$environmentSectionNumbers = @(
    $environmentSectionMatches | ForEach-Object { [int] $_.Groups[1].Value }
)
$environmentSectionDifferences = @(Compare-Object $environmentSectionNumbers @(1..19))
Assert-Condition ($environmentSectionDifferences.Count -eq 0) '环境清单主章节编号不连续。'

$requiredEnvironmentVariables = @(
    'APP_ENV',
    'LOG_LEVEL',
    'PUBLIC_BASE_URL',
    'JWT_SECRET',
    'JWT_ALGORITHM',
    'ACCESS_TOKEN_TTL_MINUTES',
    'DATABASE_URL',
    'DB_POOL_SIZE',
    'DB_POOL_TIMEOUT',
    'REDIS_URL',
    'CELERY_BROKER_URL',
    'STORAGE_ROOT',
    'MAX_FILE_SIZE_MB',
    'MAX_BATCH_FILES',
    'MAX_PDF_PAGES',
    'MAX_EXTRACTED_CHARS',
    'PARSER_TIMEOUT_SECONDS',
    'MODEL_BASE_URL',
    'QWEN_API_KEY',
    'CHAT_MODEL',
    'EMBEDDING_MODEL',
    'EMBEDDING_DIMENSION',
    'EMBEDDING_BATCH_SIZE',
    'TOP_K',
    'RRF_K',
    'STRUCTURED_WEIGHT',
    'KEYWORD_WEIGHT',
    'VECTOR_WEIGHT',
    'NODE_TIMEOUT_SECONDS',
    'MAX_TRANSIENT_RETRIES',
    'APPROVAL_TTL_MINUTES',
    'SSE_HEARTBEAT_SECONDS',
    'SSE_BATCH_SIZE',
    'SSE_RETRY_MILLISECONDS',
    'LANGFUSE_ENABLED',
    'LANGFUSE_HOST',
    'LANGFUSE_PUBLIC_KEY',
    'LANGFUSE_SECRET',
    'MOCK_MODEL_MODE',
    'HNSW_ENABLED'
)
$missingEnvironmentVariables = @(
    $requiredEnvironmentVariables |
        Where-Object { -not $environmentChecklist.Contains($_) }
)
Assert-Condition ($missingEnvironmentVariables.Count -eq 0) "环境清单缺少配置变量：$($missingEnvironmentVariables -join ', ')"

$requiredEnvironmentContracts = @(
    'WSL 2',
    'Docker Desktop',
    'Node 22 LTS',
    'Python 3.12',
    'Linux containers',
    '.env.example',
    '不保存任何真实密码、Token、API Key',
    'Gate 0 通过标准',
    '普通 CI 不配置 QWEN_API_KEY',
    '安装系统组件会改变主机状态',
    '当前阻塞结论'
)
$missingEnvironmentContracts = @(
    $requiredEnvironmentContracts |
        Where-Object { -not $environmentChecklist.Contains($_) }
)
Assert-Condition ($missingEnvironmentContracts.Count -eq 0) "环境清单缺少关键契约：$($missingEnvironmentContracts -join ', ')"

$requiredDemoContracts = @(
    'docker compose --profile tools down --volumes --remove-orphans',
    'apps/web/e2e/fixtures/e2e-candidate-resume.pdf',
    '"status":"ready"',
    '启动批量分析',
    '启动单人流程',
    '写操作岗位',
    '业务等效的 exactly-once',
    '只重建 DEMO 标记的数据',
    '现场状态速查'
)
$missingDemoContracts = @(
    $requiredDemoContracts | Where-Object { -not $demoScript.Contains($_) }
)
Assert-Condition ($missingDemoContracts.Count -eq 0) "演示脚本缺少现场契约：$($missingDemoContracts -join ', ')"
Assert-Condition (-not $demoScript.Contains('残留数据也不怕')) '演示脚本错误宣称一键启动允许残留 Compose 资源。'
Assert-Condition (-not $demoScript.Contains('期望 {"status":"ok"}')) '演示脚本仍把 readiness 状态写成 ok。'

Write-Output (
    'DOCUMENT_VALIDATION_OK ' +
    "markdown=$($markdownFiles.Count) " +
    "requirements=$($requirementDefinitions.Count) " +
    "resources=$($resourcePaths.Count) " +
    "api_methods=$($overviewMethodPaths.Count) " +
    "detailed_sections=$detailedMainSections " +
    "contracts=$($requiredContracts.Count) " +
    "plan_sections=$($planSectionMatches.Count) " +
    "work_packages=$($workPackageMatches.Count) " +
    "planned_days=$plannedDays " +
    "plan_contracts=$($requiredPlanContracts.Count) " +
    "environment_sections=$($environmentSectionMatches.Count) " +
    "environment_variables=$($requiredEnvironmentVariables.Count) " +
    "environment_contracts=$($requiredEnvironmentContracts.Count) " +
    "demo_contracts=$($requiredDemoContracts.Count)"
)
