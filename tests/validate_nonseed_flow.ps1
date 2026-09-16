[CmdletBinding()]
param()

# FIN-010 item 4: after the non-seed browser path completes, the database must
# hold exactly one application, two executed approvals, one interview, and no
# orphaned file or active-run slot.
#
# Why a probe instead of Playwright assertions: none of these counts are
# observable through the public API. The application-run detail endpoint
# deliberately exposes only *current_approval* (the executed history is not
# part of the read model), and there is no endpoint that lists a run's
# approvals or reports whether a chunk carries a vector. The durable truth
# lives in PostgreSQL, so the probe reads it there -- the same instrument
# tests/validate_application_entry.ps1 and tests/validate_maintenance.ps1 use.
#
# The probe drives the path over the API (not the browser): the browser journey
# is covered by apps/web/e2e/fin010-nonseed-flow.spec.ts, and re-driving it here
# would duplicate that work while adding a Playwright dependency to a shell
# probe. What this probe adds is the non-seed candidate creation *and* the
# impossible-to-observe counts.
#
# One step the browser spec skips is driven explicitly here: pinning verbatim
# evidence. Evidence chunks are the reviewer's artifact, not the parser's, and
# the vector recall channel is built from *embedded chunks* only -- a candidate
# with none is absent from it (retrieval/vector.py). Pinning is therefore what
# makes "the MatchRun finds this brand-new profile" a real claim rather than a
# claim that happens to hold because another channel rescued the recall.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-nonseed-flow-probe'
$demoUsername = 'hr.demo'
$demoPassword = 'demo-password-123'
$demoJobTitle = '[DEMO] 高级后端工程师（Go / Python）'
$fixturePath = Join-Path $repoRoot 'apps/web/e2e/fixtures/e2e-candidate-resume.docx'

# A per-run identity so the assertions can be exact: the seeded pool is stable
# and shared, so only a fresh upload can produce a uniquely countable candidate.
$runId = [Guid]::NewGuid().ToString('N')
$uploadFilename = "fin010-$runId.docx"

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
        'compose',
        '--project-name', $projectName,
        '--env-file', $envFile,
        '--file', $composeFile
    ) + $Arguments)
}

function Get-ProjectResources {
    $containers = Invoke-Docker -Arguments @(
        'ps', '--all',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Names}}'
    ) | Where-Object { $_.Trim() }
    $volumes = Invoke-Docker -Arguments @(
        'volume', 'ls',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--format', '{{.Name}}'
    ) | Where-Object { $_.Trim() }
    return @($containers) + @($volumes)
}

function Invoke-Api {
    param(
        [Parameter(Mandatory)][string] $Method,
        [Parameter(Mandatory)][string] $Path,
        [string] $Body = '',
        [string] $ContentType = '',
        [string] $Token = ''
    )
    $bodyBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Body))
    $pythonProbe = (
        "import base64,http.client; " +
        "body=base64.b64decode('$bodyBase64'); headers={}; " +
        $(if ($ContentType) { "headers['Content-Type']='$ContentType'; " } else { '' }) +
        $(if ($Token) { "headers['Authorization']='Bearer $Token'; " } else { '' }) +
        "connection=http.client.HTTPConnection('127.0.0.1',8000,timeout=60); " +
        "connection.request('$Method','$Path',body=body,headers=headers); " +
        "response=connection.getresponse(); print(response.status); " +
        "data=response.read().decode('utf-8'); print(data if data else '__EMPTY__')"
    )
    $output = @(Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'api', 'python', '-c', $pythonProbe
    ))
    if ($output.Count -lt 2) {
        throw "API probe returned incomplete output: $($output -join ' ')"
    }
    $rawBody = $output[-1]
    return [PSCustomObject]@{
        Status = [int] $output[-2].Trim()
        Body = if ($rawBody -eq '__EMPTY__') { $null } else { $rawBody | ConvertFrom-Json }
        RawBody = $rawBody
    }
}

function Invoke-Login {
    $body = "username=$([uri]::EscapeDataString($demoUsername))&password=$([uri]::EscapeDataString($demoPassword))"
    return Invoke-Api `
        -Method 'POST' `
        -Path '/api/v1/auth/token' `
        -Body $body `
        -ContentType 'application/x-www-form-urlencoded'
}

function Invoke-Sql {
    param([Parameter(Mandatory)][string] $Sql)
    # ``-tAc`` returns a single unaligned row; the caller compares it literally.
    return (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc', $Sql
        ) | Select-Object -Last 1
    ).Trim()
}

function Wait-WorkerReady {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        $log = (Invoke-Compose -Arguments @('logs', 'worker') | Out-String)
        if ($log -match 'celery@.*ready' -or $log -match 'Connected to redis') {
            return
        }
        Start-Sleep -Seconds 3
    }
    throw 'Worker did not become ready within the timeout.'
}

function Wait-Until {
    param(
        [Parameter(Mandatory)][string] $Description,
        [Parameter(Mandatory)][scriptblock] $Probe,
        [int] $TimeoutSeconds = 180
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (& $Probe) { return $true }
        Start-Sleep -Seconds 3
    }
    throw "Timed out waiting for: $Description"
}

function Upload-Fixture {
    param([Parameter(Mandatory)][string] $Token)
    # The upload is multipart, which the base64-JSON helper cannot express, so
    # the file is copied into the api container and posted with Python there.
    $mediaType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    $containerId = (Invoke-Docker -Arguments @(
        'ps',
        '--filter', "label=com.docker.compose.project=$projectName",
        '--filter', 'label=com.docker.compose.service=api',
        '--format', '{{.ID}}'
    ) | Select-Object -First 1).Trim()
    if (-not $containerId) {
        throw 'Could not resolve the running api container for the upload.'
    }
    [void] (Invoke-Docker -Arguments @('cp', $fixturePath, "${containerId}:/tmp/$uploadFilename"))

    # PowerShell has no backslash escape: a here-string keeps the Python literal
    # for the multipart header readable, and ``\\r\\n`` reaches Python intact
    # because single-quoted PS here-strings do not expand anything.
    $script = @"
import http.client
import uuid

boundary = '----fin010' + uuid.uuid4().hex
data = open('/tmp/$uploadFilename', 'rb').read()
quote = chr(34)
parts = [
    ('--' + boundary + '\r\n').encode(),
    ('Content-Disposition: form-data; name=' + quote + 'files' + quote +
     '; filename=' + quote + '$uploadFilename' + quote + '\r\n').encode(),
    ('Content-Type: $mediaType\r\n\r\n').encode(),
    data,
    b'\r\n',
    ('--' + boundary + '--\r\n').encode(),
]
headers = {
    'Content-Type': 'multipart/form-data; boundary=' + boundary,
    'Authorization': 'Bearer $Token',
}
connection = http.client.HTTPConnection('127.0.0.1', 8000, timeout=120)
connection.request('POST', '/api/v1/documents', body=b''.join(parts), headers=headers)
response = connection.getresponse()
print(response.status)
print(response.read().decode('utf-8'))
"@
    $output = @(Invoke-Compose -Arguments @('exec', '--no-TTY', 'api', 'python', '-c', $script))
    if ($output.Count -lt 2) {
        throw "Upload probe returned incomplete output: $($output -join ' ')"
    }
    $status = [int] $output[-2].Trim()
    if ($status -ne 202) {
        throw "Upload failed with status ${status}: $($output[-1])"
    }
    return ($output[-1] | ConvertFrom-Json)
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    [void] (Invoke-Compose -Arguments @('build', 'api'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'postgres', 'redis'
    ))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'storage-init'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'seed'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'api', 'worker'
    ))
    Wait-WorkerReady

    $login = Invoke-Login
    if ($login.Status -ne 200 -or -not $login.Body.access_token) {
        throw "Demo login failed: $($login.RawBody)"
    }
    $token = $login.Body.access_token

    $jobs = Invoke-Api -Method 'GET' -Path '/api/v1/jobs?status=ACTIVE' -Token $token
    if ($jobs.Status -ne 200) { throw "Job listing failed: $($jobs.RawBody)" }
    $job = @($jobs.Body.items | Where-Object { $_.title -eq $demoJobTitle }) | Select-Object -First 1
    if ($null -eq $job) { throw "Seed job was not available: $($jobs.RawBody)" }

    # ---- 1. Non-seed upload -------------------------------------------------
    # The batch endpoint returns counts plus one result row per file; the
    # document id is ``items[0].resource_id`` (``accepted`` is only a number).
    $upload = Upload-Fixture -Token $token
    if ($upload.accepted -ne 1) {
        throw "Expected the upload to be accepted once: $($upload | ConvertTo-Json -Compress)"
    }
    $documentId = @($upload.items)[0].resource_id
    if (-not $documentId) {
        throw "Upload did not return a document id: $($upload | ConvertTo-Json -Compress)"
    }

    # The worker parses, then the extraction task writes the draft, then the
    # confirm enqueues the embedding task. Each is a separate hop.
    [void] (Wait-Until -Description 'the document to reach REVIEW_REQUIRED' -Probe {
        (Invoke-Sql "SELECT status FROM resume_documents WHERE id = '$documentId';") -eq 'REVIEW_REQUIRED'
    })

    # ---- 2. Pin verbatim evidence, then confirm the profile ----------------
    # Read the draft through the API (by-document is job-free, which is exactly
    # how the review screen resolves a document to its draft).
    [void] (Wait-Until -Description 'the profile draft' -Probe {
        $draft = Invoke-Api -Method 'GET' -Path "/api/v1/candidate-profiles/by-document/$documentId" -Token $token
        return ($draft.Status -eq 200 -and $null -ne $draft.Body.profile_json)
    })
    $draftResponse = Invoke-Api -Method 'GET' -Path "/api/v1/candidate-profiles/by-document/$documentId" -Token $token
    $profileId = $draftResponse.Body.id
    $version = $draftResponse.Body.version

    # 2a. Pin evidence -- the step the browser spec leaves out.
    # Nothing in the pipeline invents a chunk: the parser produces blocks and the
    # extractor produces a draft, but an EvidenceChunk is a verbatim excerpt a
    # human pins on the review screen. That makes this step load-bearing twice
    # over, and skipping it is what used to hang this probe on a vector that was
    # never queued:
    #   * ``confirm`` publishes ``embeddings.generate_chunks`` for the profile's
    #     chunks, so with none pinned the task has an empty work set;
    #   * the vector channel recalls embedded chunks only, so without a pin the
    #     MatchRun below could not surface this brand-new profile on that channel.
    # The locator is copied from the parser's own block payload so the pin is the
    # same shape the review screen submits (see buildQuoteLocator in the web app).
    $content = Invoke-Api -Method 'GET' -Path "/api/v1/documents/$documentId/content" -Token $token
    if ($content.Status -ne 200) {
        throw "Document content read failed with status $($content.Status): $($content.RawBody)"
    }
    $paragraphs = @($content.Body.blocks | Where-Object {
        $_.locator.kind -eq 'docx_paragraph' -and $_.text.Trim()
    })
    if ($paragraphs.Count -lt 1) {
        throw "The parsed fixture exposed no docx paragraph to pin: $($content.RawBody)"
    }
    # Two chunks, not one: "every chunk carries a vector" is only a real
    # assertion once there is more than a single chunk to cover.
    $pinnedCount = [Math]::Min(2, $paragraphs.Count)
    $pinItems = @()
    for ($index = 0; $index -lt $pinnedCount; $index++) {
        $block = $paragraphs[$index]
        $pinItems += @{
            document_id = $documentId
            chunk_index = $index
            section_type = '工作经历'
            locator = @{
                kind = 'docx_paragraph'
                paragraph_index = [int] $block.locator.paragraph_index
                char_start = 0
                char_end = $block.text.Length
            }
            text = $block.text
        }
    }
    # Built by hand rather than with ``ConvertTo-Json -AsArray``: the probe has to
    # stay readable under Windows PowerShell 5.1 too, and a one-element array
    # there serialises as a bare object, which the list-typed route rejects 422.
    $pinBody = '[' + (($pinItems | ForEach-Object { $_ | ConvertTo-Json -Depth 6 -Compress }) -join ',') + ']'
    $pinned = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$($job.id)/profiles/$profileId/evidence" `
        -Body $pinBody `
        -ContentType 'application/json' `
        -Token $token
    if ($pinned.Status -ne 201) {
        throw "Evidence pinning failed with status $($pinned.Status): $($pinned.RawBody)"
    }
    if (@($pinned.Body).Count -ne $pinnedCount) {
        throw "Expected $pinnedCount pinned chunks, got $(@($pinned.Body).Count): $($pinned.RawBody)"
    }

    # 2b. Confirm.
    # Echo the draft's own profile_json back with only the intended edits applied
    # -- the same thing the review screen submits. Reconstructing the whole
    # object here would drift from the extractor's schema.
    $profileJson = $draftResponse.Body.profile_json
    $editBody = (@{
        profile_json = $profileJson
        normalized_skills = @($draftResponse.Body.normalized_skills)
        years_experience = 6
        education_level = 'BACHELOR'
    } | ConvertTo-Json -Depth 12 -Compress)
    $confirm = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$($job.id)/profiles/$profileId/confirm?expected_version=$version" `
        -Body $editBody `
        -ContentType 'application/json' `
        -Token $token
    if ($confirm.Status -ne 200) {
        throw "Profile confirm failed with status $($confirm.Status): $($confirm.RawBody)"
    }

    # Assert the pinned rows are visible before waiting on the vector. A pin that
    # silently failed would otherwise surface as a timeout on the embedding task
    # below -- an error three steps away from its cause.
    $chunkCount = Invoke-Sql (
        "SELECT count(*) FROM evidence_chunks WHERE candidate_profile_id = '$profileId';"
    )
    if ($chunkCount -ne "$pinnedCount") {
        throw "Expected $pinnedCount pinned evidence chunks for the profile, found $chunkCount."
    }

    # The embedding task is enqueued by the confirm; wait for every pinned chunk
    # of this profile to carry a vector with the configured model.
    [void] (Wait-Until -Description 'the embedding task to fill every pinned chunk' -Probe {
        $counts = Invoke-Sql (
            "SELECT count(*) || '|' || count(*) FILTER (WHERE embedding IS NOT NULL) " +
            "FROM evidence_chunks WHERE candidate_profile_id = '$profileId';"
        )
        $parts = $counts.Split('|')
        return ($parts[0] -eq "$pinnedCount" -and $parts[0] -eq $parts[1])
    })

    # ---- 3. MatchRun over the demo job --------------------------------------
    $created = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$($job.id)/match-runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    if ($created.Status -ne 202) {
        throw "MatchRun creation failed with status $($created.Status): $($created.RawBody)"
    }
    $matchRunId = $created.Body.run_id

    [void] (Wait-Until -Description 'the MatchRun to complete' -TimeoutSeconds 240 -Probe {
        $run = Invoke-Api -Method 'GET' -Path "/api/v1/match-runs/$matchRunId" -Token $token
        return ($run.Status -eq 200 -and $run.Body.status -eq 'COMPLETED')
    })

    $applicationId = Invoke-Sql (
        "SELECT ja.id FROM job_applications ja " +
        "JOIN candidate_profiles cp ON cp.candidate_id = ja.candidate_id " +
        "WHERE ja.job_id = '$($job.id)' AND cp.id = '$profileId';"
    )
    if (-not $applicationId) {
        throw 'The MatchRun did not create an application for the uploaded profile.'
    }

    # ---- 4. ApplicationRun: dual approval -----------------------------------
    $started = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/applications/$applicationId/runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    if ($started.Status -ne 202) {
        throw "ApplicationRun start failed with status $($started.Status): $($started.RawBody)"
    }
    $applicationRunId = $started.Body.run_id

    function Get-CurrentApproval {
        param([string] $ExpectedAction = '')
        $run = Invoke-Api -Method 'GET' -Path "/api/v1/application-runs/$applicationRunId" -Token $token
        if ($run.Status -ne 200) { return $null }
        if ($run.Body.status -ne 'WAITING_APPROVAL') { return $null }
        if (-not $run.Body.current_approval) { return $null }
        if ($ExpectedAction -and $run.Body.current_approval.action_type -ne $ExpectedAction) { return $null }
        return $run.Body.current_approval
    }

    function Invoke-ApprovalDecision {
        param(
            [Parameter(Mandatory)] $Approval,
            [Parameter(Mandatory)][string] $Token
        )
        $body = (@{ decision = 'APPROVE'; expected_version = $Approval.version } | ConvertTo-Json -Compress)
        $result = Invoke-Api `
            -Method 'POST' `
            -Path "/api/v1/approvals/$($Approval.id)/decision" `
            -Body $body `
            -ContentType 'application/json' `
            -Token $Token
        if ($result.Status -ne 200) {
            throw "Approval decision failed with status $($result.Status): $($result.RawBody)"
        }
    }

    [void] (Wait-Until -Description 'the status approval' -Probe {
        return [bool](Get-CurrentApproval -ExpectedAction 'UPDATE_APPLICATION_STATUS')
    })
    $firstApproval = Get-CurrentApproval -ExpectedAction 'UPDATE_APPLICATION_STATUS'
    if (-not $firstApproval) { throw 'The first approval never became pending.' }
    Invoke-ApprovalDecision -Approval $firstApproval -Token $token

    [void] (Wait-Until -Description 'the schedule approval' -Probe {
        return [bool](Get-CurrentApproval -ExpectedAction 'CREATE_INTERVIEW_SCHEDULE')
    })
    $secondApproval = Get-CurrentApproval -ExpectedAction 'CREATE_INTERVIEW_SCHEDULE'
    if (-not $secondApproval) { throw 'The second approval never became pending.' }
    Invoke-ApprovalDecision -Approval $secondApproval -Token $token

    [void] (Wait-Until -Description 'the ApplicationRun to complete' -Probe {
        $run = Invoke-Api -Method 'GET' -Path "/api/v1/application-runs/$applicationRunId" -Token $token
        return ($run.Status -eq 200 -and $run.Body.status -eq 'COMPLETED')
    })

    # ---- 5. Durable invariants (the reason this probe exists) ---------------
    # One application for this run's candidate+job. The MatchRun creates one per
    # ranked profile; a duplicated start would surface here as 2.
    $applicationCount = Invoke-Sql (
        "SELECT count(*) FROM job_applications WHERE id = '$applicationId';"
    )
    if ($applicationCount -ne '1') {
        throw "Expected exactly 1 target application, found $applicationCount."
    }

    # Exactly two approvals for this run, both executed.
    $approvalProbe = Invoke-Sql (
        "SELECT count(*) || '|' || count(*) FILTER (WHERE status = 'EXECUTED') " +
        "FROM approvals WHERE application_run_id = '$applicationRunId';"
    )
    if ($approvalProbe -ne '2|2') {
        throw "Expected exactly 2 executed approvals, found: $approvalProbe"
    }

    # Exactly one interview, bound to this run and the schedule approval.
    $interviewProbe = Invoke-Sql (
        "SELECT count(*) || '|' || " +
        "count(*) FILTER (WHERE run_id = '$applicationRunId') || '|' || " +
        "count(*) FILTER (WHERE approval_id = '$($secondApproval.id)') " +
        "FROM interviews WHERE run_id = '$applicationRunId';"
    )
    if ($interviewProbe -ne '1|1|1') {
        throw "Expected exactly 1 interview for this run, found: $interviewProbe"
    }

    # No orphaned active-run slot: a completed run must have released it.
    $activeSlot = Invoke-Sql (
        "SELECT coalesce(active_application_run_id::text, 'NULL') " +
        "FROM job_applications WHERE id = '$applicationId';"
    )
    if ($activeSlot -ne 'NULL') {
        throw "The application still holds an active run slot: $activeSlot"
    }

    # No orphaned file: the uploaded document row still exists and is referenced
    # by exactly one profile. Two independent scalar subqueries rather than one
    # correlated form: the outer projection here is an aggregate, and PostgreSQL
    # rejects an outer column reference inside a subquery of an aggregate query
    # ("subquery uses ungrouped column ... from outer query").
    $orphanProbe = Invoke-Sql (
        "SELECT (SELECT count(*) FROM resume_documents WHERE id = '$documentId') || '|' || " +
        "(SELECT count(*) FROM candidate_profiles WHERE document_id = '$documentId');"
    )
    if ($orphanProbe -ne '1|1') {
        throw "Expected the upload to be referenced by exactly 1 profile, found: $orphanProbe"
    }

    # The run itself is the only one for this application: no duplicate attempt.
    $runCount = Invoke-Sql (
        "SELECT count(*) FROM application_runs WHERE application_id = '$applicationId';"
    )
    if ($runCount -ne '1') {
        throw "Expected exactly 1 application run for the application, found $runCount."
    }

    Write-Output (
        "NON_SEED_FLOW_OK run=$runId document=$documentId profile=$profileId " +
        "application=$applicationId applicationRun=$applicationRunId " +
        "chunks=$pinnedCount embedded=$pinnedCount approvals=2 executed=2 " +
        "interviews=1 activeSlot=NULL"
    )
}
finally {
    # Always tear the project down, volumes included: the probe must not leave a
    # database behind that a later run could mistake for a seeded baseline.
    [void] (Invoke-Compose -Arguments @(
        '--profile', 'tools', 'down', '--volumes', '--remove-orphans', '--timeout', '15'
    ))
}
