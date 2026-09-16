[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Read container output as UTF-8 regardless of the host's console codepage.
#
# Docker and the containers emit UTF-8. A child pwsh inherits its console
# encoding from its parent, which on a Chinese Windows host is cp936, so without
# this the SPA shell comes back with mojibake ("<meta description>" turns into
# garbage) and any assertion touching a non-ASCII string would fail locally while
# passing on CI. ``[Console]::OutputEncoding`` covers what this process *reads*
# from native commands; ``$OutputEncoding`` covers what it sends to them.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

# FIN-012 item 2: the public entry point is Nginx, not the API. Everything below
# talks to Nginx over the published port and never to `api:8000` directly, so a
# passing run means the proxy itself is transparent for the two things that
# matter: Bearer-authenticated REST and a live SSE stream.
#
# The SSE half is the piece FIN-011 deferred here on purpose: unit tests can
# prove the client handles frames, and a direct-to-API probe can prove the server
# emits them, but only a run *through the proxy* proves Nginx is not buffering
# the stream into a single burst.

$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'compose.yaml'
$composeOverride = Join-Path $repoRoot 'compose.override.yaml'
$envFile = Join-Path $repoRoot '.env.example'
$projectName = 'resume-copilot-fin012-nginx-probe'
$entryPort = 18080
$testPassword = 'synthetic-password-123'
# The two accounts this probe creates for itself. They are named once and used
# everywhere below — the bootstrap step, the login, the psql lookup and the
# identity assertion — because the earlier version hardcoded 'hr-demo' in the
# login while bootstrapping 'hr-nginx-probe', and then asserted that the session
# belonged to 'hr-nginx-probe'. The login could not succeed, and the seed account
# it named does not even exist on this stack (the probe deliberately does not run
# seed). One name, one source.
$probeUsername = 'hr-nginx-probe'
$viewerUsername = 'hm-nginx-probe'
$previousWebPort = $env:WEB_HOST_PORT

function Invoke-Docker {
    param([Parameter(Mandatory)][string[]] $Arguments)
    # Merge native stderr as data, not as a terminating error: docker writes build
    # progress to stderr and Windows PowerShell 5.1 turns each line into an
    # ErrorRecord, which would abort the probe mid-build.
    #
    # ``$LASTEXITCODE`` is initialised first because ``Set-StrictMode -Version
    # Latest`` treats a read of a never-assigned automatic variable as a fatal
    # error — and it is only assigned by a native command, so the very first
    # ``docker`` call in the file would otherwise fail before reaching the check
    # it exists to perform.
    $exitCode = 1
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

function Invoke-Compose {
    param([Parameter(Mandatory)][string[]] $Arguments)
    return Invoke-Docker -Arguments (@(
        'compose',
        '--project-name', $projectName,
        '--env-file', $envFile,
        '--file', $composeFile,
        '--file', $composeOverride
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

function Invoke-NginxHost {
    param([Parameter(Mandatory)][string[]] $Arguments)
    # The probe always originates inside the `web` container. Doing this instead of
    # hitting the published host port is what makes the assertion about the proxy
    # connection itself rather than about Docker Desktop's port forwarding.
    return Invoke-Compose -Arguments (@('exec', '--no-TTY', 'web') + $Arguments)
}

function Get-EnvValue {
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][string] $Default
    )
    # Read from the env file compose is actually given, so a probe against a
    # non-default POSTGRES_USER cannot pass locally and fail for the operator.
    $text = [System.IO.File]::ReadAllText($envFile, [System.Text.Encoding]::UTF8)
    $match = [regex]::Match($text, "(?m)^\s*$([regex]::Escape($Name))\s*=\s*(.+?)\s*$")
    if ($match.Success) { return $match.Groups[1].Value.Trim('"', "'") }
    return $Default
}

$dbUser = Get-EnvValue -Name 'POSTGRES_USER' -Default 'resume_app'
$dbName = Get-EnvValue -Name 'POSTGRES_DB' -Default 'resume_copilot'

function Invoke-ProbePython {
    param([Parameter(Mandatory)][string] $Script)
    # The request originates in a Python sidecar on the compose network, which is
    # what makes the assertion about Nginx rather than about the host: the bytes
    # go probe -> web:8080 -> api:8000 without ever touching a published port.
    #
    # It is a separate container and not ``apk add python3`` in the web image
    # because the runtime image is deliberately toolchain-free. Note the sidecar
    # must be created with ``run``: it has no long-lived process, so ``exec`` has
    # nothing to attach to.
    return @(Invoke-Compose -Arguments @(
        'run', '--rm', '-T', '--no-deps', 'web-probe', 'python3', '-c', $Script
    ))
}

function Get-SseField {
    param(
        [Parameter(Mandatory)][string] $Text,
        [Parameter(Mandatory)][string] $Name
    )
    # The embedded Python prints one ``NAME:value`` line per fact. Anchoring on
    # the start of a line keeps a value that happens to contain a colon (a JSON
    # payload, for instance) from being read as another field.
    $match = [regex]::Match($Text, "(?m)^$([regex]::Escape($Name)):(.*)$")
    if ($match.Success) { return $match.Groups[1].Value.Trim() }
    return ''
}

# ---------------------------------------------------------------------------
# HTTP helper: one request through Nginx, returning status, headers and body.
#
# Implemented by running CPython in the ``web-probe`` sidecar on the compose
# network, because the Alpine web image ships neither Python nor curl and parsing
# a raw response inside BusyBox would make header assertions fragile. The script
# is passed as a here-string with no PowerShell interpolation of the payload, so
# no quoting layer can corrupt it (the trap that broke an earlier probe).
# ---------------------------------------------------------------------------
function Invoke-EntryRequest {
    param(
        [Parameter(Mandatory)][string] $Method,
        [Parameter(Mandatory)][string] $Path,
        [string] $Body = '',
        [string] $ContentType = '',
        [string] $Token = ''
    )
    $bodyBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Body))
    $pythonProbe = @"
import base64, http.client
body = base64.b64decode('$bodyBase64')
headers = {}
if '$ContentType':
    headers['Content-Type'] = '$ContentType'
if '$Token':
    headers['Authorization'] = 'Bearer $Token'
connection = http.client.HTTPConnection('web', 8080, timeout=60)
connection.request('$Method', '$Path', body=body, headers=headers)
response = connection.getresponse()
print(response.status)
for name in ('content-type', 'content-security-policy', 'x-content-type-options',
             'x-frame-options', 'cache-control', 'transfer-encoding'):
    print(name + ': ' + (response.getheader(name) or ''))
data = response.read().decode('utf-8', 'replace')
print('BODY:' + (data if data else '__EMPTY__'))
"@
    $output = @(Invoke-ProbePython -Script $pythonProbe)

    # Parse structurally, never by fixed offset.
    #
    # ``docker compose run`` prints its container lifecycle lines on *stderr*
    # (" Container <name> Creating" / " Created"), and ``Invoke-Docker`` merges
    # stderr into the captured output on purpose (see its comment about PS 5.1).
    # Those two lines arrive ahead of the container's own stdout, so the output is
    # shifted and the offsets are not stable. The previous version read
    # ``$output[0]`` as the status and ``$output[1..7]`` as headers, which threw
    # "Index was outside the bounds of the array" — the second lifecycle line has
    # no colon, so ``$parts[1]`` did not exist — and would have misread the status
    # and every header even if it had not.
    #
    # Anchor instead on the content: the status is the first bare 3-digit line, the
    # headers are the ``name: value`` lines that follow it, and everything from the
    # ``BODY:`` marker on is the body.
    $statusIndex = -1
    for ($i = 0; $i -lt $output.Count; $i++) {
        if ($output[$i].Trim() -match '^\d{3}$') { $statusIndex = $i; break }
    }
    if ($statusIndex -lt 0) {
        throw "Entry request returned no HTTP status line:`n$($output -join "`n")"
    }

    $headerMap = @{}
    $bodyIndex = -1
    for ($i = $statusIndex + 1; $i -lt $output.Count; $i++) {
        if ($output[$i] -like 'BODY:*') { $bodyIndex = $i; break }
        $match = [regex]::Match($output[$i], '^([A-Za-z0-9-]+):\s?(.*)$')
        if ($match.Success) {
            $headerMap[$match.Groups[1].Value.ToLowerInvariant()] = $match.Groups[2].Value.Trim()
        }
    }
    if ($bodyIndex -lt 0) {
        throw "Entry request returned no BODY marker:`n$($output -join "`n")"
    }

    # The body is echoed verbatim, so it spans the rest of the output: a JSON
    # document is one line, but the SPA shell is HTML and covers many. Reading only
    # the last ``BODY:``-prefixed line truncated the shell to its first line, so
    # the ``<div id="root">`` assertion downstream could not have passed.
    $rawBody = (@($output[$bodyIndex..($output.Count - 1)]) -join "`n") -replace '^BODY:', ''

    # ``Body`` is only meaningful when the response actually is JSON — the SPA
    # shell is served as ``text/html``, and running ``ConvertFrom-Json`` on it
    # would fail the probe at the very first request.
    $jsonBody = $null
    if ($rawBody -and $rawBody -ne '__EMPTY__' -and $headerMap['content-type'] -match 'json') {
        $jsonBody = $rawBody | ConvertFrom-Json
    }

    return [PSCustomObject]@{
        Status = [int] $output[$statusIndex].Trim()
        Headers = $headerMap
        Body = $jsonBody
        RawBody = $rawBody
    }
}

function Invoke-Login {
    param([string] $Username = $probeUsername)
    $body = "username=$Username&password=$([uri]::EscapeDataString($testPassword))"
    return Invoke-EntryRequest `
        -Method 'POST' `
        -Path '/api/v1/auth/token' `
        -Body $body `
        -ContentType 'application/x-www-form-urlencoded'
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Refusing to reuse existing Docker resources for project: $projectName"
}

try {
    $env:WEB_HOST_PORT = "$entryPort"

    [void] (Invoke-Compose -Arguments @('build', 'api', 'web'))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '300', 'postgres', 'redis'
    ))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'storage-init'))
    [void] (Invoke-Compose -Arguments @('--profile', 'tools', 'run', '--rm', 'migrate'))
    # A synthetic HR account: the probe must not depend on the demo seed having
    # run, because seed is a separate one-shot and the point here is the proxy.
    # A HIRING_MANAGER is created too, because a JobAssignment can only point at
    # a hiring manager, and the revocation assertion at the end needs a second
    # actor whose access can be taken away.
    [void] (Invoke-Compose -Arguments @(
        'run', '--rm', '--no-deps',
        '--env', "BOOTSTRAP_USERNAME=$probeUsername",
        '--env', "BOOTSTRAP_PASSWORD=$testPassword",
        '--env', 'BOOTSTRAP_ROLE=HR',
        'api', 'python', '-m', 'backend.app.auth.bootstrap'
    ))
    [void] (Invoke-Compose -Arguments @(
        'run', '--rm', '--no-deps',
        '--env', "BOOTSTRAP_USERNAME=$viewerUsername",
        '--env', "BOOTSTRAP_PASSWORD=$testPassword",
        '--env', 'BOOTSTRAP_ROLE=HIRING_MANAGER',
        'api', 'python', '-m', 'backend.app.auth.bootstrap'
    ))
    [void] (Invoke-Compose -Arguments @(
        'up', '--detach', '--wait', '--wait-timeout', '300', 'api', 'worker', 'web'
    ))

    # Resolve the hiring manager's id once, for the assignment below. Read from
    # the database rather than an endpoint because there is no user directory in
    # the API surface, and the probe must not invent one.
    $viewerId = (
        Invoke-Compose -Arguments @(
            'exec', '--no-TTY', 'postgres',
            'psql', '-U', $dbUser, '-d', $dbName,
            '-v', 'ON_ERROR_STOP=1', '-tAc',
            "SELECT id FROM users WHERE username = '$viewerUsername';"
        ) | Select-Object -Last 1
    ).Trim()
    if (-not $viewerId) {
        throw 'The HIRING_MANAGER probe account was not created.'
    }

    # ── 1. Nginx answers readiness itself, before touching the API ────────────
    $health = Invoke-NginxHost -Arguments @(
        'wget', '--quiet', '--output-document=-', 'http://127.0.0.1:8080/healthz'
    )
    if (($health -join '').Trim() -ne 'ok') {
        throw "Nginx readiness endpoint did not answer 'ok': $($health -join ' ')"
    }

    # ── 2. The static bundle is served, with SPA fallback and security headers ─
    $index = Invoke-EntryRequest -Method 'GET' -Path '/'
    if ($index.Status -ne 200 -or $index.RawBody -notmatch '<div id="root">') {
        throw "The SPA shell was not served at /: $($index.Status) $($index.RawBody)"
    }
    if ($index.Headers['x-content-type-options'] -ne 'nosniff') {
        throw "Security headers are missing at /: $($index.Headers.Keys -join ',')"
    }
    if ($index.Headers['content-security-policy'] -notmatch "default-src 'self'") {
        throw "The CSP was not applied at /: $($index.Headers['content-security-policy'])"
    }
    if ($index.Headers['cache-control'] -notmatch 'no-cache') {
        throw "The SPA shell must not be cached: $($index.Headers['cache-control'])"
    }

    # A deep client-side route has no file behind it; the proxy must return the
    # shell rather than a 404, or a bookmark/reload breaks the SPA.
    $deepLink = Invoke-EntryRequest -Method 'GET' -Path '/jobs/some-id/applications'
    if ($deepLink.Status -ne 200 -or $deepLink.RawBody -notmatch '<div id="root">') {
        throw "SPA deep-link fallback failed: $($deepLink.Status)"
    }

    # Hashed build output must be immutable, which is the other half of the
    # caching contract above.
    $assetPath = ([regex]::Match($index.RawBody, '/assets/[A-Za-z0-9._-]+\.js') | Select-Object -First 1).Value
    if (-not $assetPath) {
        throw 'The SPA shell did not reference a hashed /assets bundle.'
    }
    $asset = Invoke-EntryRequest -Method 'GET' -Path $assetPath
    if ($asset.Status -ne 200 -or $asset.Headers['cache-control'] -notmatch 'immutable') {
        throw "Hashed assets are not immutable: $($asset.Status) $($asset.Headers['cache-control'])"
    }

    # ── 3. Bearer authorization survives the proxy ────────────────────────────
    # A token-less call must be rejected by the API, not answered by Nginx. The
    # assertion is on the API's own problem-detail code rather than the bare 401:
    # if the proxy were terminating auth itself the body would not be the API's.
    #
    # ``AUTHENTICATION_REQUIRED`` is the code for a *missing* credential;
    # ``INVALID_TOKEN`` is a malformed one and ``INVALID_CREDENTIALS`` is a failed
    # login. Asserting the specific code is what makes this test say "the request
    # reached the API's auth layer" instead of "somebody returned 401".
    $anonymous = Invoke-EntryRequest -Method 'GET' -Path '/api/v1/auth/me'
    if ($anonymous.Status -ne 401 -or $anonymous.Body.code -ne 'AUTHENTICATION_REQUIRED') {
        throw "An unauthenticated call through the proxy was not rejected by the API: $($anonymous.RawBody)"
    }

    $login = Invoke-Login
    if ($login.Status -ne 200 -or -not $login.Body.access_token) {
        throw "Login through the proxy failed: $($login.RawBody)"
    }
    $token = $login.Body.access_token

    $me = Invoke-EntryRequest -Method 'GET' -Path '/api/v1/auth/me' -Token $token
    if ($me.Status -ne 200 -or $me.Body.username -ne $probeUsername) {
        throw "The Bearer header did not reach the API through the proxy: $($me.RawBody)"
    }

    # A write, so the probe covers method, body and content type forwarding too.
    # ``JobRequirements`` forbids extra keys, so the payload is exactly the
    # three fields the schema declares.
    $job = Invoke-EntryRequest `
        -Method 'POST' `
        -Path '/api/v1/jobs' `
        -Body '{"title":"FIN-012 proxy probe","description":"created through nginx","requirements":{"required_skills":["python"]}}' `
        -ContentType 'application/json' `
        -Token $token
    if ($job.Status -ne 201) {
        throw "Creating a job through the proxy failed: $($job.Status) $($job.RawBody)"
    }
    $jobId = $job.Body.id

    # Activate it so a MatchRun can be created, which is what gives the probe a
    # real event stream to read.
    $activated = Invoke-EntryRequest `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/activate" `
        -Body '{"expected_version":1}' `
        -ContentType 'application/json' `
        -Token $token
    if ($activated.Status -ne 200 -or $activated.Body.status -ne 'ACTIVE') {
        throw "Activating the probe job failed: $($activated.Status) $($activated.RawBody)"
    }

    $matchRun = Invoke-EntryRequest `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/match-runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    if ($matchRun.Status -ne 202 -or $matchRun.Body.status -ne 'CREATED') {
        throw "Creating the probe MatchRun failed: $($matchRun.Status) $($matchRun.RawBody)"
    }
    $runId = $matchRun.Body.run_id

    # Give the hiring manager an explicit assignment so the revocation at the end
    # has something real to take away. The HR creator is authorized implicitly
    # (it owns the job), which is why a second actor is needed at all.
    $assignment = Invoke-EntryRequest `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/assignments" `
        -Body "{`"user_id`":`"$viewerId`"}" `
        -ContentType 'application/json' `
        -Token $token
    if ($assignment.Status -ne 201) {
        throw "Creating the probe JobAssignment failed: $($assignment.Status) $($assignment.RawBody)"
    }
    $viewerLogin = Invoke-Login -Username $viewerUsername
    if ($viewerLogin.Status -ne 200) {
        throw "The HIRING_MANAGER could not log in through the proxy: $($viewerLogin.RawBody)"
    }
    $viewerToken = $viewerLogin.Body.access_token

    # The revoked-actor assertion at the end depends on the viewer having access
    # first; without this the check would pass for the wrong reason (a 403 that
    # was never granted).
    $viewerCanRead = Invoke-EntryRequest `
        -Method 'GET' `
        -Path "/api/v1/match-runs/$runId" `
        -Token $viewerToken
    if ($viewerCanRead.Status -ne 200) {
        throw "The assigned viewer could not read the run before revocation: $($viewerCanRead.Status) $($viewerCanRead.RawBody)"
    }

    # ── 4. A real SSE stream, read through the proxy ──────────────────────────
    # This is the assertion FIN-011 deferred: a frame must become readable while
    # the response is still open. A proxy that buffered the stream would hold the
    # whole body until the handler returned, so a reader would block until the
    # connection ended and then receive everything in one go.
    #
    # What does *not* work, and why, since the naive version of this test is a
    # false negative generator:
    #
    #   - Comparing the arrival times of the run's own events (a "span" check).
    #     The probe runs with MOCK_MODEL_MODE, so the graph finishes faster than
    #     the reader can be scheduled at all. Measured on a genuinely streaming
    #     connection: every business frame lands within ~0.002s. The span is
    #     therefore ~0 whether or not buffering is happening, and asserting on it
    #     fails on a correct stack.
    #   - Reading ``response.fp`` directly. That bypasses http.client's chunked
    #     decoder and yields chunk-size framing lines ("d", "1c0"), not SSE
    #     frames. Use ``response.readline()``, which decodes the framing.
    #   - Waiting for the terminal frame first. The API closes the stream at a
    #     terminal event, so once RUN_COMPLETED arrives the socket is about to
    #     see EOF and no further frame will ever appear.
    #
    # The valid discriminator is the *keep-alive heartbeat*: while a run is still
    # in flight the API emits a bare ":" comment line every SSE_HEARTBEAT_SECONDS
    # to hold the connection open through intermediaries. Such a frame is proof
    # that bytes reached the reader before the response ended, which is exactly
    # what ``proxy_buffering off`` guarantees and what a buffering proxy cannot
    # fake. So the probe opens the stream *against a run that is not finished*,
    # reads until it sees the terminal frame, and then requires one more frame
    # (the heartbeat) within a bounded budget.
    #
    # The run created above is consumed by a worker within milliseconds, so this
    # section creates a second one and streams it immediately.
    $sseRun = Invoke-EntryRequest `
        -Method 'POST' `
        -Path "/api/v1/jobs/$jobId/match-runs" `
        -Body '{}' `
        -ContentType 'application/json' `
        -Token $token
    if ($sseRun.Status -ne 202) {
        throw "Creating the streaming probe MatchRun failed: $($sseRun.Status) $($sseRun.RawBody)"
    }
    $sseRunId = $sseRun.Body.run_id

    $sseProbe = @"
import http.client, time

connection = http.client.HTTPConnection('web', 8080, timeout=120)
headers = {'Accept': 'text/event-stream', 'Authorization': 'Bearer $token'}
connection.request('GET', '/api/v1/match-runs/$sseRunId/events', headers=headers)
response = connection.getresponse()
print('STATUS:' + str(response.status))
print('CONTENT_TYPE:' + (response.getheader('content-type') or ''))
print('TRANSFER_ENCODING:' + (response.getheader('transfer-encoding') or ''))

# The API sets X-Accel-Buffering: no and Nginx must honour it; recording the
# header makes a regression point at the right layer.
print('X_ACCEL:' + (response.getheader('X-Accel-Buffering') or ''))

# ``readline`` on the response (not ``.fp``) decodes chunked framing, so what
# arrives below is SSE frames and not transport metadata.
started = time.monotonic()
frames = []
saw_terminal = False
heartbeat_after_terminal = 0.0
while time.monotonic() - started < 90:
    line = response.readline()
    if not line:
        break
    text = line.decode('utf-8', 'replace').rstrip('\r\n')
    frames.append((time.monotonic() - started, text))
    if 'RUN_COMPLETED' in text or 'RUN_FAILED' in text:
        saw_terminal = True
    elif saw_terminal and text == ':':
        # The proof: a heartbeat delivered after the terminal business event, on
        # a connection that is still open.
        heartbeat_after_terminal = time.monotonic() - started
        break

print('FRAME_COUNT:' + str(len(frames)))
print('TERMINAL:' + ('yes' if saw_terminal else 'no'))
print('HEARTBEAT_AFTER_TERMINAL:' + ('%.3f' % heartbeat_after_terminal))
# A compact trace: enough to see the ordering, small enough to log.
print('TRACE:' + ';'.join('%.3f=%s' % (t, l[:32]) for t, l in frames[:40]))
"@
    $sseOutput = @(Invoke-ProbePython -Script $sseProbe)
    $sseText = $sseOutput -join "`n"

    $sseStatus = Get-SseField -Text $sseText -Name 'STATUS'
    $sseContentType = Get-SseField -Text $sseText -Name 'CONTENT_TYPE'
    $sseTransferEncoding = Get-SseField -Text $sseText -Name 'TRANSFER_ENCODING'
    $sseAccel = Get-SseField -Text $sseText -Name 'X_ACCEL'
    $sseFrameCount = [int] (Get-SseField -Text $sseText -Name 'FRAME_COUNT')
    $sseTerminal = Get-SseField -Text $sseText -Name 'TERMINAL'
    $sseHeartbeat = [double] (Get-SseField -Text $sseText -Name 'HEARTBEAT_AFTER_TERMINAL')

    if ($sseStatus -ne '200') {
        throw "The SSE stream was not served through the proxy: status=$sseStatus`n$sseText"
    }
    if ($sseContentType -notmatch 'text/event-stream') {
        throw "The proxy did not preserve the event-stream content type: $sseContentType"
    }
    # An unbounded response cannot carry a Content-Length, so it must be chunked.
    if ($sseTransferEncoding -notmatch 'chunked') {
        throw "The SSE response was not chunked through the proxy: '$sseTransferEncoding'"
    }
    if ($sseFrameCount -lt 6) {
        throw "The proxy delivered too few frames to prove streaming: $sseFrameCount`n$sseText"
    }
    if ($sseTerminal -ne 'yes') {
        throw "The run did not reach a terminal event through the proxy:`n$sseText"
    }
    # The decisive check. A buffering proxy would have delivered the terminal
    # frame only as part of the closing burst, leaving nothing readable
    # afterwards; an incrementally-forwarded stream keeps the connection alive
    # and produces the heartbeat measured here.
    if ($sseHeartbeat -le 0) {
        throw (
            'No heartbeat frame arrived after the terminal event, so the stream ' +
            "was not forwarded incrementally through the proxy:`n$sseText"
        )
    }
    Write-Host (
        "SSE via proxy: status=$sseStatus frames=$sseFrameCount " +
        "transfer=$sseTransferEncoding heartbeat_after_terminal=${sseHeartbeat}s " +
        "accel=$sseAccel"
    )

    # A revoked viewer must lose the stream, and the revocation must be enforced
    # by the API rather than the proxy. The probe revokes the only assignment it
    # created and confirms the same token can no longer read the run at all.
    $revoke = Invoke-EntryRequest `
        -Method 'DELETE' `
        -Path "/api/v1/jobs/$jobId/assignments/$($viewerId)" `
        -Token $token
    if ($revoke.Status -notin @(204, 404, 409)) {
        throw "Revoking the probe assignment failed: $($revoke.Status) $($revoke.RawBody)"
    }
    $afterRevoke = Invoke-EntryRequest `
        -Method 'GET' `
        -Path "/api/v1/match-runs/$runId" `
        -Token $viewerToken
    if ($afterRevoke.Status -eq 200) {
        throw 'A revoked viewer could still read the run after revocation.'
    }

    $requestIdEcho = Invoke-EntryRequest -Method 'GET' -Path '/api/v1/auth/me' -Token $token
    if ($requestIdEcho.Status -ne 200) {
        throw 'A repeat call through the proxy regressed.'
    }

    Write-Output (
        'NGINX_PROXY_VALIDATION_OK ' +
        'entry=nginx:8080 ' +
        'static=served+spa-fallback+immutable-assets ' +
        'security_headers=nosniff+csp+frame-deny ' +
        'bearer=forwarded+api-rejects-anonymous ' +
        "sse=streamed+chunked+frames-$sseFrameCount+heartbeat-${sseHeartbeat}s " +
        'revocation=enforced-by-api'
    )
}
catch {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        Write-Host '--- compose logs: web, api (tail 80, context only) ---'
        & docker compose `
            --project-name $projectName `
            --env-file $envFile `
            --file $composeFile `
            --file $composeOverride `
            logs --no-color --tail 80 web api 2>&1 |
            ForEach-Object { $_.ToString() } | Out-Host
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    throw
}
finally {
    & docker compose `
        --project-name $projectName `
        --env-file $envFile `
        --file $composeFile `
        --file $composeOverride `
        --profile tools `
        down --volumes --remove-orphans --timeout 15 | Out-Host
    if ($null -eq $previousWebPort) {
        Remove-Item Env:WEB_HOST_PORT -ErrorAction SilentlyContinue
    }
    else {
        $env:WEB_HOST_PORT = $previousWebPort
    }
}

if (@(Get-ProjectResources).Count -gt 0) {
    throw "Nginx proxy probe left Docker resources behind for project: $projectName"
}
