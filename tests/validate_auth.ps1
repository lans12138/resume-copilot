[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-imp005-auth-probe'
$testPassword = 'synthetic-password-123'

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
        "connection=http.client.HTTPConnection('127.0.0.1',8000,timeout=10); " +
        "connection.request('$Method','$Path',body=body,headers=headers); " +
        "response=connection.getresponse(); print(response.status); " +
        "print(response.getheader('WWW-Authenticate') or ''); " +
        "data=response.read().decode('utf-8'); print(data if data else '__EMPTY__')"
    )
    $output = @(Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'api', 'python', '-c', $pythonProbe
    ))
    if ($output.Count -lt 3) {
        throw "API probe returned incomplete output: $($output -join ' ')"
    }
    $rawBody = $output[-1]
    return [PSCustomObject]@{
        Status = [int] $output[-3].Trim()
        Authenticate = $output[-2].Trim()
        Body = if ($rawBody -eq '__EMPTY__') { $null } else { $rawBody | ConvertFrom-Json }
        RawBody = $rawBody
    }
}

function Invoke-Login {
    param([string] $Username, [string] $Password)
    $body = "username=$([uri]::EscapeDataString($Username))&password=$([uri]::EscapeDataString($Password))"
    return Invoke-Api `
        -Method 'POST' `
        -Path '/api/v1/auth/token' `
        -Body $body `
        -ContentType 'application/x-www-form-urlencoded'
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

    foreach ($account in @(
        @{ Username = 'HR-Demo'; Role = 'HR' },
        @{ Username = 'manager-demo'; Role = 'HIRING_MANAGER' },
        @{ Username = 'disabled-manager'; Role = 'HIRING_MANAGER' },
        @{ Username = 'admin-demo'; Role = 'ADMIN' }
    )) {
        [void] (Invoke-Compose -Arguments @(
            'run', '--rm', '--no-deps',
            '--env', "BOOTSTRAP_USERNAME=$($account.Username)",
            '--env', "BOOTSTRAP_PASSWORD=$testPassword",
            '--env', "BOOTSTRAP_ROLE=$($account.Role)",
            'api', 'python', '-m', 'backend.app.auth.bootstrap'
        ))
    }
    [void] (Invoke-Compose -Arguments @(
        'exec', '--no-TTY', 'postgres',
        'psql', '-U', 'resume_app', '-d', 'resume_copilot',
        '-v', 'ON_ERROR_STOP=1', '-c',
        "UPDATE users SET is_active = false WHERE username = 'disabled-manager';"
    ))

    [void] (Invoke-Compose -Arguments @('up', '--detach', '--wait', '--wait-timeout', '120', 'api'))

    $success = Invoke-Login -Username '  HR-DEMO  ' -Password $testPassword
    if (
        $success.Status -ne 200 -or
        $success.Body.token_type -ne 'bearer' -or
        $success.Body.expires_in -ne 1800 -or
        -not $success.Body.access_token
    ) {
        throw "Valid form login failed: $($success.RawBody)"
    }

    $claimsProbe = (
        "import json,jwt; " +
        "print(json.dumps(jwt.decode('$($success.Body.access_token)',options={'verify_signature':False})))"
    )
    $claims = (
        Invoke-Compose -Arguments @('exec', '--no-TTY', 'api', 'python', '-c', $claimsProbe)
        | Select-Object -Last 1
    ) | ConvertFrom-Json
    $claimNames = @($claims.PSObject.Properties.Name | Sort-Object)
    if (($claimNames -join ',') -ne 'exp,iat,jti,role,sub' -or $claims.role -ne 'HR') {
        throw "JWT contains unexpected claims: $($claimNames -join ',')"
    }

    $me = Invoke-Api `
        -Method 'GET' `
        -Path '/api/v1/auth/me' `
        -Token $success.Body.access_token
    if ($me.Status -ne 200 -or $me.Body.username -ne 'hr-demo' -or $me.Body.role -ne 'HR') {
        throw "Bearer current-user lookup failed: $($me.RawBody)"
    }

    $wrongPassword = Invoke-Login -Username 'hr-demo' -Password 'wrong-password'
    $missingUser = Invoke-Login -Username 'missing-user' -Password 'wrong-password'
    $disabledUser = Invoke-Login -Username 'disabled-manager' -Password $testPassword
    foreach ($failure in @($wrongPassword, $missingUser, $disabledUser)) {
        if (
            $failure.Status -ne 401 -or
            $failure.Body.code -ne 'INVALID_CREDENTIALS' -or
            $failure.Authenticate -ne 'Bearer'
        ) {
            throw "Authentication failure contract was not uniform: $($failure.RawBody)"
        }
    }
    if (
        $wrongPassword.Body.message -ne $missingUser.Body.message -or
        $wrongPassword.Body.message -ne $disabledUser.Body.message
    ) {
        throw 'Credential failures exposed which account condition failed.'
    }

    $jsonLogin = Invoke-Api `
        -Method 'POST' `
        -Path '/api/v1/auth/token' `
        -Body '{"username":"hr-demo","password":"synthetic-password-123"}' `
        -ContentType 'application/json'
    if ($jsonLogin.Status -notin @(415, 422)) {
        throw "JSON login was unexpectedly accepted: $($jsonLogin.RawBody)"
    }

    $databaseProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT username || '|' || role || '|' || (password_hash LIKE '`$argon2%') || '|' || (last_login_at IS NOT NULL) FROM users WHERE username = 'hr-demo';"
        ) | Select-Object -Last 1
    ).Trim()
    if ($databaseProbe -ne 'hr-demo|HR|true|true') {
        throw "User persistence probe failed: $databaseProbe"
    }

    $managerLogin = Invoke-Login -Username 'manager-demo' -Password $testPassword
    $adminLogin = Invoke-Login -Username 'admin-demo' -Password $testPassword
    $managerMe = Invoke-Api -Method 'GET' -Path '/api/v1/auth/me' -Token $managerLogin.Body.access_token
    $adminMe = Invoke-Api -Method 'GET' -Path '/api/v1/auth/me' -Token $adminLogin.Body.access_token

    $jobPayload = @{
        title = 'Senior Backend Engineer'
        description = 'Build reliable recruiting services.'
        requirements = @{
            required_skills = @(' Python ', 'PostgreSQL')
            preferred_skills = @('Docker')
            minimum_years_experience = 3
            education_level = 'BACHELOR'
        }
    } | ConvertTo-Json -Depth 5 -Compress
    $createdJob = Invoke-Api `
        -Method 'POST' `
        -Path '/api/v1/jobs' `
        -Body $jobPayload `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if (
        $createdJob.Status -ne 201 -or
        $createdJob.Body.status -ne 'DRAFT' -or
        $createdJob.Body.version -ne 1 -or
        $createdJob.Body.current_version.version_no -ne 1 -or
        $createdJob.Body.current_version.requirements.required_skills[0] -ne 'python'
    ) {
        throw "Job creation contract failed: $($createdJob.RawBody)"
    }
    $jobId = $createdJob.Body.id

    $managerBeforeGrant = Invoke-Api `
        -Method 'GET' -Path "/api/v1/jobs/$jobId" -Token $managerLogin.Body.access_token
    if ($managerBeforeGrant.Status -ne 404) {
        throw 'Manager accessed a job before receiving an assignment.'
    }
    $adminList = Invoke-Api `
        -Method 'GET' -Path '/api/v1/jobs' -Token $adminLogin.Body.access_token
    if ($adminList.Status -ne 403) {
        throw 'ADMIN unexpectedly received recruiting business access.'
    }
    $managerCreate = Invoke-Api `
        -Method 'POST' `
        -Path '/api/v1/jobs' `
        -Body $jobPayload `
        -ContentType 'application/json' `
        -Token $managerLogin.Body.access_token
    if ($managerCreate.Status -ne 403) {
        throw 'HIRING_MANAGER unexpectedly created a job.'
    }

    $assignmentPayload = @{ user_id = $managerMe.Body.id } | ConvertTo-Json -Compress
    $assignment = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/assignments" `
        -Body $assignmentPayload `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($assignment.Status -ne 201 -or $assignment.Body.user_id -ne $managerMe.Body.id) {
        throw "Assignment grant failed: $($assignment.RawBody)"
    }
    $duplicateAssignment = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/assignments" `
        -Body $assignmentPayload `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($duplicateAssignment.Status -ne 409 -or $duplicateAssignment.Body.code -ne 'ASSIGNMENT_EXISTS') {
        throw "Duplicate active assignment was not rejected: $($duplicateAssignment.RawBody)"
    }
    $invalidAssignmentPayload = @{ user_id = $adminMe.Body.id } | ConvertTo-Json -Compress
    $invalidAssignment = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/assignments" `
        -Body $invalidAssignmentPayload `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($invalidAssignment.Status -ne 422 -or $invalidAssignment.Body.code -ne 'INVALID_ASSIGNEE') {
        throw "Non-manager assignment was not rejected: $($invalidAssignment.RawBody)"
    }
    $managerAfterGrant = Invoke-Api `
        -Method 'GET' -Path "/api/v1/jobs/$jobId" -Token $managerLogin.Body.access_token
    if ($managerAfterGrant.Status -ne 200) {
        throw 'Manager could not access the assigned job.'
    }

    $updatePayload = @{
        expected_version = 1
        description = 'Build reliable and secure recruiting services.'
        requirements = @{
            required_skills = @('Python', 'PostgreSQL', 'FastAPI')
            preferred_skills = @('Docker')
            minimum_years_experience = 4
            education_level = 'BACHELOR'
        }
    } | ConvertTo-Json -Depth 5 -Compress
    $updatedJob = Invoke-Api `
        -Method 'PATCH' `
        -Path "/api/v1/jobs/$jobId" `
        -Body $updatePayload `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($updatedJob.Status -ne 200 -or $updatedJob.Body.version -ne 2 -or $updatedJob.Body.current_version.version_no -ne 2) {
        throw "Immutable JobVersion update failed: $($updatedJob.RawBody)"
    }
    $staleUpdate = Invoke-Api `
        -Method 'PATCH' `
        -Path "/api/v1/jobs/$jobId" `
        -Body $updatePayload `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if (
        $staleUpdate.Status -ne 409 -or
        $staleUpdate.Body.code -ne 'VERSION_CONFLICT' -or
        $staleUpdate.Body.details.current_version -ne 2
    ) {
        throw "Stale Job update did not return version conflict: $($staleUpdate.RawBody)"
    }

    $activate = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/activate" `
        -Body '{"expected_version":2}' `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($activate.Status -ne 200 -or $activate.Body.status -ne 'ACTIVE' -or $activate.Body.version -ne 3) {
        throw "Job activation failed: $($activate.RawBody)"
    }
    $close = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/close" `
        -Body '{"expected_version":3}' `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($close.Status -ne 200 -or $close.Body.status -ne 'CLOSED' -or $close.Body.version -ne 4) {
        throw "Job close failed: $($close.RawBody)"
    }
    $invalidReactivate = Invoke-Api `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/activate" `
        -Body '{"expected_version":4}' `
        -ContentType 'application/json' `
        -Token $success.Body.access_token
    if ($invalidReactivate.Status -ne 409 -or $invalidReactivate.Body.code -ne 'INVALID_STATE') {
        throw "Closed job was unexpectedly reactivated: $($invalidReactivate.RawBody)"
    }

    $revoke = Invoke-Api `
        -Method 'DELETE' `
        -Path "/api/v1/jobs/$jobId/assignments/$($managerMe.Body.id)" `
        -Token $success.Body.access_token
    if ($revoke.Status -ne 204) {
        throw "Assignment revoke failed: $($revoke.RawBody)"
    }
    $managerAfterRevoke = Invoke-Api `
        -Method 'GET' -Path "/api/v1/jobs/$jobId" -Token $managerLogin.Body.access_token
    if ($managerAfterRevoke.Status -ne 404) {
        throw 'Manager retained resource access after assignment revocation.'
    }
    $assignmentHistory = Invoke-Api `
        -Method 'GET' `
        -Path "/api/v1/jobs/$jobId/assignments" `
        -Token $success.Body.access_token
    if (
        $assignmentHistory.Status -ne 200 -or
        $assignmentHistory.Body.total -ne 1 -or
        $null -eq $assignmentHistory.Body.items[0].revoked_at
    ) {
        throw "Revoked assignment history was not preserved: $($assignmentHistory.RawBody)"
    }

    $jobDatabaseProbe = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', 'resume_app', '-d', 'resume_copilot',
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT (SELECT count(*) FROM job_versions WHERE job_id = '$jobId') || '|' || (SELECT version FROM jobs WHERE id = '$jobId') || '|' || (SELECT (revoked_at IS NOT NULL AND revoked_by IS NOT NULL) FROM job_assignments WHERE job_id = '$jobId');"
        ) | Select-Object -Last 1
    ).Trim()
    if ($jobDatabaseProbe -ne '2|4|true') {
        throw "Job database invariants failed: $jobDatabaseProbe"
    }

    Write-Output 'AUTH_VALIDATION_OK form=only argon2=ok jwt=5-claims bearer=ok failures=uniform disabled=rejected'
    Write-Output 'JOB_VALIDATION_OK versions=immutable lifecycle=closed optimistic_lock=409 assignment=revoked access=removed'
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
    throw "Auth probe left Docker resources behind for project: $projectName"
}
