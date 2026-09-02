[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverride = Join-Path $repoRoot 'compose.override.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-imp008-document-probe'
$testPassword = 'synthetic-password-123'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "resume-copilot-imp008-$([guid]::NewGuid().ToString('N'))"
$previousApiPort = $env:API_HOST_PORT
$previousMaxFileSize = $env:MAX_FILE_SIZE_MB

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    $output = & docker @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($Arguments -join ' ')`n$($output -join "`n")"
    }
    return @($output | ForEach-Object { $_.ToString() })
}

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]] $Arguments)
    return Invoke-Docker -Arguments (@(
        'compose', '--project-name', $projectName, '--env-file', $envFile,
        '--file', $composeFile, '--file', $composeOverride
    ) + $Arguments)
}

function Get-ProjectResources {
    $containers = Invoke-Docker -Arguments @(
        'ps', '--all', '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Names}}'
    ) | Where-Object { $_.Trim() }
    $volumes = Invoke-Docker -Arguments @(
        'volume', 'ls', '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Name}}'
    ) | Where-Object { $_.Trim() }
    return @($containers) + @($volumes)
}

function Invoke-Login {
    param([string] $Username)
    return Invoke-RestMethod `
        -Method Post `
        -Uri 'http://127.0.0.1:18008/api/v1/auth/token' `
        -ContentType 'application/x-www-form-urlencoded' `
        -Body @{ username = $Username; password = $testPassword }
}

function Invoke-Upload {
    param(
        [string[]] $Path,
        [string] $MediaType,
        [string] $Token
    )
    $arguments = @(
        '--silent', '--show-error', '--write-out', "`n%{http_code}",
        '--header', "Authorization: Bearer $Token"
    )
    foreach ($filePath in $Path) {
        $arguments += @('--form', "files=@$filePath;type=$MediaType")
    }
    $arguments += 'http://127.0.0.1:18008/api/v1/documents'
    $output = @(& curl.exe @arguments)
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 2) {
        throw "Upload request failed: $($output -join "`n")"
    }
    return [PSCustomObject]@{
        Status = [int] $output[-1]
        Body = ($output[0..($output.Count - 2)] -join "`n") | ConvertFrom-Json
    }
}

function New-Docx {
    param([string] $Path)
    $file = [IO.File]::Open($Path, [IO.FileMode]::CreateNew)
    try {
        $archive = [IO.Compression.ZipArchive]::new(
            $file, [IO.Compression.ZipArchiveMode]::Create, $false
        )
        try {
            foreach ($entryData in @(
                @('[Content_Types].xml', '<Types><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'),
                @('word/document.xml', '<w:document>synthetic resume</w:document>')
            )) {
                $entry = $archive.CreateEntry($entryData[0])
                $writer = [IO.StreamWriter]::new($entry.Open())
                try { $writer.Write($entryData[1]) } finally { $writer.Dispose() }
            }
        }
        finally { $archive.Dispose() }
    }
    finally { $file.Dispose() }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

[void] (New-Item -ItemType Directory -Path $testRoot)
$pdfPath = Join-Path $testRoot 'candidate.pdf'
$duplicatePath = Join-Path $testRoot 'renamed.pdf'
$fakePath = Join-Path $testRoot 'fake.pdf'
$largePath = Join-Path $testRoot 'large.pdf'
$docxPath = Join-Path $testRoot 'candidate.docx'
[IO.File]::WriteAllBytes($pdfPath, [Text.Encoding]::ASCII.GetBytes("%PDF-1.7`nsynthetic resume"))
[IO.File]::Copy($pdfPath, $duplicatePath)
[IO.File]::WriteAllBytes($fakePath, [Text.Encoding]::ASCII.GetBytes('not a pdf'))
$largeBytes = [byte[]]::new((1024 * 1024) + 1)
[Text.Encoding]::ASCII.GetBytes('%PDF-').CopyTo($largeBytes, 0)
[IO.File]::WriteAllBytes($largePath, $largeBytes)
New-Docx -Path $docxPath

try {
    $env:API_HOST_PORT = '18008'
    $env:MAX_FILE_SIZE_MB = '1'
    [void] (Invoke-Compose -Arguments @('build', 'api'))
    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'storage-init'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    foreach ($account in @(
        @{ Username = 'hr-upload-demo'; Role = 'HR' },
        @{ Username = 'manager-upload-demo'; Role = 'HIRING_MANAGER' }
    )) {
        [void] (Invoke-Compose -Arguments @(
            'run', '--rm', '--no-deps', '--env', "BOOTSTRAP_USERNAME=$($account.Username)",
            '--env', "BOOTSTRAP_PASSWORD=$testPassword", '--env', "BOOTSTRAP_ROLE=$($account.Role)",
            'api', 'python', '-m', 'backend.app.auth.bootstrap'
        ))
    }
    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'api'))

    $hrToken = (Invoke-Login -Username 'hr-upload-demo').access_token
    $managerToken = (Invoke-Login -Username 'manager-upload-demo').access_token
    $pdf = Invoke-Upload -Path @($pdfPath, $fakePath) -MediaType 'application/pdf' -Token $hrToken
    $duplicate = Invoke-Upload -Path $duplicatePath -MediaType 'application/pdf' -Token $hrToken
    $large = Invoke-Upload -Path $largePath -MediaType 'application/pdf' -Token $hrToken
    $docx = Invoke-Upload `
        -Path $docxPath `
        -MediaType 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' `
        -Token $hrToken
    $forbidden = Invoke-Upload -Path $pdfPath -MediaType 'application/pdf' -Token $managerToken
    $statusView = Invoke-RestMethod `
        -Uri "http://127.0.0.1:18008$($pdf.Body.items[0].status_url)" `
        -Headers @{ Authorization = "Bearer $hrToken" }
    $documentList = Invoke-RestMethod `
        -Uri 'http://127.0.0.1:18008/api/v1/documents?status=UPLOADED' `
        -Headers @{ Authorization = "Bearer $hrToken" }

    if (
        $pdf.Status -ne 202 -or
        $pdf.Body.accepted -ne 1 -or
        $pdf.Body.rejected -ne 1 -or
        $pdf.Body.items[0].outcome -ne 'accepted' -or
        $pdf.Body.items[0].document.status -ne 'UPLOADED'
    ) {
        throw "PDF was not safely accepted: $($pdf.Body | ConvertTo-Json -Depth 8 -Compress)"
    }
    if ($duplicate.Status -ne 202 -or $duplicate.Body.items[0].outcome -ne 'duplicate' -or $duplicate.Body.items[0].duplicate_of -ne $pdf.Body.items[0].document.id) {
        throw 'Duplicate content did not resolve to the original document.'
    }
    if (
        $pdf.Body.items[1].error.code -ne 'SIGNATURE_MISMATCH' -or
        $pdf.Body.items[1].error.http_status -ne 415
    ) {
        throw 'A spoofed PDF signature was not rejected.'
    }
    if (
        $large.Body.items[0].error.code -ne 'FILE_TOO_LARGE' -or
        $large.Body.items[0].error.http_status -ne 413
    ) {
        throw 'An oversized PDF was not rejected.'
    }
    if ($docx.Body.items[0].outcome -ne 'accepted') {
        throw 'A valid minimal DOCX was not accepted.'
    }
    if (
        $statusView.id -ne $pdf.Body.items[0].resource_id -or
        $statusView.status -ne 'UPLOADED' -or
        $documentList.total -ne 2
    ) {
        throw 'Document status URL or filtered list did not expose persisted facts.'
    }
    if ($forbidden.Status -ne 403 -or $forbidden.Body.code -ne 'FORBIDDEN') {
        throw 'A hiring manager unexpectedly uploaded a resume.'
    }

    $databaseProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres', 'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT count(*) || '|' || bool_and(storage_key ~ '^objects/[0-9a-f]{2}/[0-9a-f]{32}`$') || '|' || bool_and(status = 'UPLOADED') FROM resume_documents;"
        ) | Select-Object -Last 1
    ).Trim()
    $objectCount = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'api', 'python', '-c',
            "from pathlib import Path; print(sum(p.is_file() for p in Path('/data/resumes/objects').rglob('*')))"
        ) | Select-Object -Last 1
    ).Trim()
    if ($databaseProbe -ne '2|true|true' -or $objectCount -ne '2') {
        throw "Persistence or orphan cleanup invariant failed: database=$databaseProbe objects=$objectCount"
    }

    Write-Output 'DOCUMENT_UPLOAD_VALIDATION_OK pdf=stored docx=stored duplicate=detected spoof=rejected oversize=rejected orphan=none'
}
finally {
    & docker compose --project-name $projectName --env-file $envFile --file $composeFile --file $composeOverride --profile tools down --volumes --remove-orphans --timeout 15 | Out-Host
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolvedTestRoot.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove test path outside the temp root: $resolvedTestRoot"
    }
    if (Test-Path -LiteralPath $resolvedTestRoot) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
    if ($null -eq $previousApiPort) { Remove-Item Env:API_HOST_PORT -ErrorAction SilentlyContinue } else { $env:API_HOST_PORT = $previousApiPort }
    if ($null -eq $previousMaxFileSize) { Remove-Item Env:MAX_FILE_SIZE_MB -ErrorAction SilentlyContinue } else { $env:MAX_FILE_SIZE_MB = $previousMaxFileSize }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Document upload probe left Docker resources behind for project: $projectName"
}
