[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-application-entry-probe'
$demoUsername = 'hr.demo'
$demoPassword = 'demo-password-123'
$demoJobTitle = '[DEMO] 高级后端工程师（Go / Python）'

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
        [string] $Token = '',
        [string] $IdempotencyKey = ''
    )
    $bodyBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Body))
    $pythonProbe = (
        "import base64,http.client; " +
        "body=base64.b64decode('$bodyBase64'); headers={}; " +
        $(if ($ContentType) { "headers['Content-Type']='$ContentType'; " } else { '' }) +
        $(if ($Token) { "headers['Authorization']='Bearer $Token'; " } else { '' }) +
        $(if ($IdempotencyKey) { "headers['Idempotency-Key']='$IdempotencyKey'; " } else { '' }) +
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

function Restart-Api {
    [void] (Invoke-Compose -Arguments @('restart', 'api'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'api'
    ))
}

function Invoke-Login {
    $body = "username=$([uri]::EscapeDataString($demoUsername))&password=$([uri]::EscapeDataString($demoPassword))"
    return Invoke-Api `
        -Method 'POST' `
        -Path '/api/v1/auth/token' `
        -Body $body `
        -ContentType 'application/x-www-form-urlencoded'
}

function Wait-WorkerReady {
    # Runs execute off the request path (FIN-005), so the probe must not create a
    # run before the worker is consuming the agent queue. The worker deliberately
    # has no Docker healthcheck (see compose.yaml), so readiness is asserted from
    # its log — the same instrument tests/validate_worker.ps1 uses.
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

function Wait-MatchRunTerminal {
    param(
        [Parameter(Mandatory)][string] $RunId,
        [Parameter(Mandatory)][string] $Token
    )
    # CREATED is not terminal, so polling for a terminal status cannot return the
    # pre-execution state; a bounded wait is the only way to observe a run whose
    # execution belongs to another process.
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        $run = Invoke-Api -Method 'GET' -Path "/api/v1/match-runs/$RunId" -Token $Token
        if (
            $run.Status -eq 200 -and
            @('COMPLETED', 'FAILED', 'CANCELLED') -contains $run.Body.status
        ) {
            return $run
        }
        Start-Sleep -Seconds 3
    }
    throw "MatchRun $RunId did not reach a terminal status within the timeout."
}

function Wait-MatchRunRetried {
    param(
        [Parameter(Mandatory)][string] $RunId,
        [Parameter(Mandatory)][string] $Token
    )
    # A retry *starts* from FAILED, which is itself terminal, so waiting for
    # "terminal" would return immediately with the pre-retry status and hide any
    # failure. Wait for the second attempt to finish instead.
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        $run = Invoke-Api -Method 'GET' -Path "/api/v1/match-runs/$RunId" -Token $Token
        if (
            $run.Status -eq 200 -and
            $run.Body.status -eq 'COMPLETED' -and
            $run.Body.attempt -eq 2
        ) {
            return $run
        }
        Start-Sleep -Seconds 3
    }
    throw "MatchRun $RunId did not complete its retry within the timeout."
}

function Stop-WorkerHard {
    # Kill rather than graceful-stop so recovery never depends on an in-process
    # callback. The resume message is then published while no worker exists.
    [void] (Invoke-Compose -Arguments @('kill', 'worker'))
}

function Start-Worker {
    [void] (Invoke-Compose -Arguments @('up', '--detach', 'worker'))
}

function Wait-ApplicationRunState {
    param(
        [Parameter(Mandatory)][string] $RunId,
        [Parameter(Mandatory)][string] $Token,
        [Parameter(Mandatory)][string] $Status,
        [string] $ApprovalAction = ''
    )
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        $run = Invoke-Api -Method 'GET' -Path "/api/v1/application-runs/$RunId" -Token $Token
        $actionMatches = (
            -not $ApprovalAction -or
            ($null -ne $run.Body.current_approval -and
                $run.Body.current_approval.action_type -eq $ApprovalAction)
        )
        if ($run.Status -eq 200 -and $run.Body.status -eq $Status -and $actionMatches) {
            return $run
        }
        Start-Sleep -Seconds 2
    }
    throw "ApplicationRun $RunId did not reach $Status/$ApprovalAction within the timeout."
}

function Get-MatchRunDerivedRowCounts {
    param([Parameter(Mandatory)][string] $RunId)
    # Everything a pass produces is derived data, so a retry must not change these
    # counts: a duplicated candidate would break uq_match_run_candidates_profile,
    # and a duplicated report would break uq_match_reports_run_application.
    $sql = (
        "SELECT (SELECT count(*) FROM match_run_candidates WHERE run_id = '$RunId')" +
        " || '|' || (SELECT count(*) FROM match_reports WHERE run_id = '$RunId')" +
        " || '|' || (SELECT count(*) FROM report_claims WHERE report_id IN" +
        " (SELECT id FROM match_reports WHERE run_id = '$RunId'))" +
        " || '|' || (SELECT count(*) FROM claim_evidences WHERE claim_id IN" +
        " (SELECT id FROM report_claims WHERE report_id IN" +
        " (SELECT id FROM match_reports WHERE run_id = '$RunId')));"
    )
    return (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc', $sql
        ) | Select-Object -Last 1
    ).Trim()
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
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'seed'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '120', 'api', 'worker'
    ))
    # FIN-005: the API no longer executes runs itself, so the worker must be
    # consuming the agent queue before the first MatchRun is created.
    Wait-WorkerReady

    $login = Invoke-Login
    if ($login.Status -ne 200 -or -not $login.Body.access_token) {
        throw "Demo login failed: $($login.RawBody)"
    }
    $token = $login.Body.access_token

    $jobs = Invoke-Api -Method 'GET' -Path '/api/v1/jobs?status=ACTIVE' -Token $token
    if ($jobs.Status -ne 200) {
        throw "Seeded job listing failed: $($jobs.RawBody)"
    }
    $job = @($jobs.Body.items | Where-Object { $_.title -eq $demoJobTitle }) | Select-Object -First 1
    if ($null -eq $job) {
        throw "Seeded ACTIVE demo job was not available: $($jobs.RawBody)"
    }

    $seedProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT count(*) || '|' || count(*) FILTER (WHERE status = 'CREATED') FROM job_applications WHERE job_id = '$($job.id)';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($seedProbe -ne '5|5') {
        throw "Demo JobApplications were not seeded idempotently: $seedProbe"
    }
    [void] (Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'postgres',
        'psql', '-U', 'resume_app', '-d', 'resume_copilot',
        '-v', 'ON_ERROR_STOP=1', '-c',
        "DELETE FROM job_applications WHERE id = (SELECT id FROM job_applications WHERE job_id = '$($job.id)' ORDER BY candidate_id LIMIT 1); UPDATE job_applications SET status = 'ON_HOLD' WHERE id = (SELECT id FROM job_applications WHERE job_id = '$($job.id)' ORDER BY candidate_id LIMIT 1);"
    ))

    $createdMatchRun = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$($job.id)/match-runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    # FIN-005 §14.4: the request commits the run and returns CREATED; a worker
    # executes it. COMPLETED here would mean the request path still did the work.
    if ($createdMatchRun.Status -ne 202 -or $createdMatchRun.Body.status -ne 'CREATED') {
        throw "MatchRun creation failed: $($createdMatchRun.RawBody)"
    }
    $matchRunId = $createdMatchRun.Body.run_id

    $matchRun = Wait-MatchRunTerminal -RunId $matchRunId -Token $token
    $applicationIds = @($matchRun.Body.candidates | ForEach-Object { $_.application_id })
    if (
        $matchRun.Status -ne 200 -or
        $matchRun.Body.status -ne 'COMPLETED' -or
        $applicationIds.Count -ne 5 -or
        @($applicationIds | Sort-Object -Unique).Count -ne 5
    ) {
        throw "MatchRun did not expose five durable application ids: $($matchRun.RawBody)"
    }

    $linkProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT count(*) || '|' || count(*) FILTER (WHERE ja.status = 'ON_HOLD') FROM match_run_candidates AS mrc JOIN job_applications AS ja ON ja.id = mrc.application_id WHERE mrc.run_id = '$matchRunId' AND ja.job_id = '$($job.id)';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($linkProbe -ne '5|1') {
        throw "MatchRun candidates are not linked to JobApplications: $linkProbe"
    }

    # §5.6 retry. Put the run into the only state that admits a retry and let the
    # worker re-drive it as attempt 2. A retry re-enters the *same* run, so every
    # row the pass derives has to be replaced: appending would collide with
    # uq_match_run_candidates_profile / uq_match_reports_run_application, the
    # worker's transaction would roll back, and the run would stay FAILED — which
    # is what the bounded wait below turns into a failure instead of a hang.
    $rowsBeforeRetry = Get-MatchRunDerivedRowCounts -RunId $matchRunId
    [void] (Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'postgres',
        'psql', '-U', 'resume_app', '-d', 'resume_copilot',
        '-v', 'ON_ERROR_STOP=1', '-c',
        "UPDATE agent_runs SET status = 'FAILED', retryable = true, error_code = 'ALL_CANDIDATES_FAILED', finished_at = now() WHERE id = '$matchRunId';"
    ))

    $retriedMatchRun = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/match-runs/$matchRunId/retry" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token `
        -IdempotencyKey 'application-entry-match-run-retry'
    # The retry is only *enqueued*: nothing about the stored state changes until a
    # worker claims it with the retry intent, so the run still reads FAILED here.
    if ($retriedMatchRun.Status -ne 202 -or $retriedMatchRun.Body.status -ne 'FAILED') {
        throw "MatchRun retry was not accepted: $($retriedMatchRun.RawBody)"
    }

    $reRun = Wait-MatchRunRetried -RunId $matchRunId -Token $token
    $retriedApplicationIds = @($reRun.Body.candidates | ForEach-Object { $_.application_id })
    if (
        @($retriedApplicationIds | Sort-Object -Unique).Count -ne 5 -or
        (Compare-Object $applicationIds $retriedApplicationIds)
    ) {
        throw "MatchRun retry did not re-snapshot the same candidates: $($reRun.RawBody)"
    }
    $rowsAfterRetry = Get-MatchRunDerivedRowCounts -RunId $matchRunId
    if ($rowsAfterRetry -ne $rowsBeforeRetry) {
        throw "MatchRun retry duplicated derived rows: before=$rowsBeforeRetry after=$rowsAfterRetry"
    }

    $applicationId = $applicationIds[0]
    $createdApplicationRun = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/applications/$applicationId/runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    if (
        $createdApplicationRun.Status -ne 202 -or
        $createdApplicationRun.Body.application_id -ne $applicationId -or
        $createdApplicationRun.Body.status -ne 'CREATED'
    ) {
        throw "ApplicationRun entry failed: $($createdApplicationRun.RawBody)"
    }
    $applicationRunId = $createdApplicationRun.Body.run_id

    $applicationRun = Wait-ApplicationRunState `
        -RunId $applicationRunId `
        -Token $token `
        -Status 'WAITING_APPROVAL' `
        -ApprovalAction 'UPDATE_APPLICATION_STATUS'
    if (
        $applicationRun.Status -ne 200 -or
        $applicationRun.Body.status -ne 'WAITING_APPROVAL' -or
        $applicationRun.Body.current_approval.status -ne 'PENDING' -or
        $applicationRun.Body.current_approval.expected_application_version -ne 2
    ) {
        throw "ApplicationRun was not committed with its pending approval: $($applicationRun.RawBody)"
    }

    $duplicate = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/applications/$applicationId/runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    if (
        $duplicate.Status -ne 409 -or
        $duplicate.Body.code -ne 'APPLICATION_RUN_ALREADY_ACTIVE' -or
        $duplicate.Body.details.current_run_id -ne $applicationRunId
    ) {
        throw "Duplicate ApplicationRun did not resolve the active run: $($duplicate.RawBody)"
    }

    $runProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT (active_application_run_id = '$applicationRunId') || '|' || (SELECT count(*) FROM application_runs WHERE application_id = '$applicationId') || '|' || (SELECT count(*) FROM approvals WHERE application_run_id = '$applicationRunId' AND status = 'PENDING') FROM job_applications WHERE id = '$applicationId';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($runProbe -ne 'true|1|1') {
        throw "ApplicationRun persistence invariants failed: $runProbe"
    }

    $firstApproval = $applicationRun.Body.current_approval
    Restart-Api
    Stop-WorkerHard
    $firstDecisionBody = @{
        decision = 'APPROVE'
        expected_version = $firstApproval.version
    } | ConvertTo-Json -Compress
    $firstDecision = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/approvals/$($firstApproval.id)/decision" `
        -Body $firstDecisionBody `
        -ContentType 'application/json' `
        -Token $token `
        -IdempotencyKey 'application-entry-first-approval'
    if (
        $firstDecision.Status -ne 200 -or
        $firstDecision.Body.status -ne 'EXECUTED' -or
        $firstDecision.Body.action_type -ne 'UPDATE_APPLICATION_STATUS'
    ) {
        throw "First approval did not execute after API restart: $($firstDecision.RawBody)"
    }
    Start-Worker

    $midRun = Wait-ApplicationRunState `
        -RunId $applicationRunId `
        -Token $token `
        -Status 'WAITING_APPROVAL' `
        -ApprovalAction 'CREATE_INTERVIEW_SCHEDULE'
    if (
        $midRun.Status -ne 200 -or
        $midRun.Body.status -ne 'WAITING_APPROVAL' -or
        $midRun.Body.completion_reason -ne $null -or
        $midRun.Body.question_set.schema_version -ne 'v1' -or
        @($midRun.Body.question_set.questions).Count -eq 0 -or
        $midRun.Body.current_approval.status -ne 'PENDING' -or
        $midRun.Body.current_approval.action_type -ne 'CREATE_INTERVIEW_SCHEDULE' -or
        $midRun.Body.current_approval.expected_application_version -ne 3
    ) {
        throw "First approval did not persist the second gate: $($midRun.RawBody)"
    }

    $midProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT ja.status || '|' || (ja.active_application_run_id = '$applicationRunId') || '|' || ar.question_schema_version || '|' || (SELECT count(*) FROM application_status_history WHERE application_id = '$applicationId') || '|' || (SELECT count(*) FROM agent_checkpoints WHERE thread_id = (SELECT thread_id FROM agent_runs WHERE id = '$applicationRunId')) || '|' || (SELECT status FROM approvals WHERE id = '$($firstApproval.id)') FROM job_applications AS ja JOIN application_runs AS ar ON ar.application_id = ja.id WHERE ja.id = '$applicationId' AND ar.run_id = '$applicationRunId';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($midProbe -ne 'SHORTLISTED|true|v1|1|2|EXECUTED') {
        throw "First approval persistence invariants failed: $midProbe"
    }

    $secondApproval = $midRun.Body.current_approval
    Restart-Api
    Stop-WorkerHard
    $secondDecisionBody = @{
        decision = 'APPROVE'
        expected_version = $secondApproval.version
    } | ConvertTo-Json -Compress
    $secondDecision = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/approvals/$($secondApproval.id)/decision" `
        -Body $secondDecisionBody `
        -ContentType 'application/json' `
        -Token $token `
        -IdempotencyKey 'application-entry-second-approval'
    if (
        $secondDecision.Status -ne 200 -or
        $secondDecision.Body.status -ne 'EXECUTED' -or
        $secondDecision.Body.action_type -ne 'CREATE_INTERVIEW_SCHEDULE'
    ) {
        throw "Second approval did not execute after API restart: $($secondDecision.RawBody)"
    }
    Start-Worker

    $completedRun = Wait-ApplicationRunState `
        -RunId $applicationRunId `
        -Token $token `
        -Status 'COMPLETED'
    if (
        $completedRun.Status -ne 200 -or
        $completedRun.Body.status -ne 'COMPLETED' -or
        $completedRun.Body.completion_reason -ne 'SUCCESS' -or
        $null -ne $completedRun.Body.current_approval -or
        $completedRun.Body.question_set.schema_version -ne 'v1' -or
        -not $completedRun.Body.interview_id -or
        $completedRun.Body.interview_status -ne 'SCHEDULED' -or
        -not $completedRun.Body.interview_external_id
    ) {
        throw "ApplicationRun tail was not fully persisted: $($completedRun.RawBody)"
    }

    $interview = Invoke-Api `
        -Method 'GET' `
        -Path "/api/v1/interviews/$($completedRun.Body.interview_id)" `
        -Token $token
    $interviews = Invoke-Api `
        -Method 'GET' `
        -Path "/api/v1/interviews?job_id=$($job.id)" `
        -Token $token
    if (
        $interview.Status -ne 200 -or
        $interview.Body.run_id -ne $applicationRunId -or
        $interview.Body.approval_id -ne $secondApproval.id -or
        $interview.Body.status -ne 'SCHEDULED' -or
        @($interviews.Body.interviews).Count -ne 1 -or
        $interviews.Body.interviews[0].id -ne $interview.Body.id
    ) {
        throw "Persisted interview read models are inconsistent: $($interview.RawBody)"
    }

    $duplicateDecision = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/approvals/$($secondApproval.id)/decision" `
        -Body $secondDecisionBody `
        -ContentType 'application/json' `
        -Token $token `
        -IdempotencyKey 'application-entry-second-approval'
    if (
        $duplicateDecision.Status -ne 409 -or
        $duplicateDecision.Body.code -ne 'APPROVAL_ALREADY_DECIDED'
    ) {
        throw "A duplicate decision was unexpectedly re-executed: $($duplicateDecision.RawBody)"
    }

    $tailProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT ja.status || '|' || (ja.active_application_run_id IS NULL) || '|' || ar.completion_reason || '|' || (SELECT count(*) FROM application_status_history WHERE application_id = '$applicationId') || '|' || (SELECT count(*) FROM approvals WHERE application_run_id = '$applicationRunId' AND status = 'EXECUTED') || '|' || (SELECT count(*) FROM interviews WHERE application_id = '$applicationId') || '|' || (SELECT count(*) FROM agent_checkpoints WHERE thread_id = (SELECT thread_id FROM agent_runs WHERE id = '$applicationRunId')) FROM job_applications AS ja JOIN application_runs AS ar ON ar.application_id = ja.id WHERE ja.id = '$applicationId' AND ar.run_id = '$applicationRunId';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($tailProbe -ne 'INTERVIEW_SCHEDULED|true|SUCCESS|2|2|1|2') {
        throw "Workflow-tail database invariants failed: $tailProbe"
    }

    Write-Output 'APPLICATION_ENTRY_VALIDATION_OK ranking=linked async=worker retry=attempt2 replaced=derived-rows api_restarts=2 worker_hard_restarts=2 checkpoints=persisted approvals=2 history=2 interview=1 duplicate=guarded'
}
catch {
    # Runs now execute in the worker (FIN-005), so its log is the only place a
    # failed claim or a rolled-back pass is visible.
    & docker compose `
        --project-name $projectName `
        --env-file $envFile `
        --file $composeFile `
        logs --no-color --tail 120 api worker | Out-Host
    throw
}
finally {
    & docker compose `
        --project-name $projectName `
        --env-file $envFile `
        --file $composeFile `
        --profile tools `
        down --volumes --remove-orphans --timeout 15 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Failed to fully remove probe project: $projectName"
    }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Application entry probe left Docker resources behind for project: $projectName"
}
