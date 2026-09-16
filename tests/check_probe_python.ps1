[CmdletBinding()]
param()

# Static guard for the probes that embed Python.
#
# A probe can embed a Python program inside a PowerShell here-string and ship it
# into a container with ``python -c``. Two failure modes are invisible to the
# PowerShell parser and only surface at runtime -- after Docker has spent minutes
# building -- which makes them expensive to discover:
#
#   1. a PowerShell variable the here-string interpolates at an unintended spot
#      in the Python source (an unexpanded ``$Token`` left in a comment, say);
#   2. a Python syntax error introduced while editing the embedded program, most
#      easily by an escaping change (the original defect here was ``\"`` inside a
#      PowerShell double-quoted string, which PowerShell does not treat as an
#      escape and which terminated the string early).
#
# This script reproduces the runtime expansion for every embedded program and
# asks CPython to parse the result, so both classes fail in the static gate
# instead.
#
# CPython comes from a digest-pinned public image rather than the locally built
# development image. Two reasons:
#
#   1. ``verify`` runs this gate long before it builds anything, so a locally
#      built tag only exists on a machine that has run a build before -- the
#      gate passed locally and failed on a clean CI runner, where Docker tried
#      to pull the tag from Docker Hub.
#   2. Only CPython itself is needed here; the probes' own code is not on the
#      path. The pin is the same 3.12.14 base the backend builds from, so the
#      grammar under test matches the interpreter that will run the programs.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonImage = 'python:3.12.14-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254'

# Every probe that embeds Python has to be listed here; adding a probe without
# adding it here would silently leave its embedded programs unchecked.
$probePaths = @(
    'tests/validate_nonseed_flow.ps1',
    'tests/validate_nginx_proxy.ps1'
)

# The values the runtime substitutes. They only need to be syntactically valid
# stand-ins: this check is about the program's shape, not its wiring.
#
# PowerShell variable names are case-insensitive, so a probe may interpolate
# ``$Token`` in one place and ``$token`` in another. Both spellings therefore need
# an entry -- the leak check matches on the literal text the AST preserved.
#
# An array of pairs rather than a hashtable: hashtable keys are compared
# case-insensitively too, so ``'$Token'`` and ``'$token'`` would collide as a
# duplicate key and the file would stop parsing -- a substitution table that
# cannot express the thing it exists for. Order is preserved by the array, which
# also keeps the longest-name-first ordering below explicit.
$substitutions = @(
    @{ Name = '$uploadFilename'; Value = 'fin010-static-check.docx' }
    @{ Name = '$mediaType'; Value = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }
    @{ Name = '$viewerToken'; Value = 'static-check-viewer-token' }
    @{ Name = '$Token'; Value = 'static-check-token' }
    @{ Name = '$token'; Value = 'static-check-token' }
    @{ Name = '$Method'; Value = 'GET' }
    @{ Name = '$Path'; Value = '/api/v1/health' }
    @{ Name = '$bodyBase64'; Value = 'e30=' }
    @{ Name = '$ContentType'; Value = 'application/json' }
    # Longer names first: '$sseRunId' contains 'RunId' as a suffix, and while the
    # leading '$' keeps '$runId' from matching inside it, relying on that is one
    # renamed variable away from substituting half an identifier.
    @{ Name = '$sseRunId'; Value = '00000000-0000-0000-0000-000000000000' }
    @{ Name = '$jobId'; Value = '00000000-0000-0000-0000-000000000000' }
    @{ Name = '$runId'; Value = '00000000-0000-0000-0000-000000000000' }
)

$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("probe-python-check-" + [Guid]::NewGuid().ToString('N'))
[void] (New-Item -ItemType Directory -Path $staging -Force)

try {
    $programCount = 0
    foreach ($relativePath in $probePaths) {
        $probePath = Join-Path $repoRoot $relativePath
        if (-not (Test-Path -LiteralPath $probePath)) {
            throw "Missing probe: $probePath"
        }

        # Read as UTF-8 explicitly: the probes hold Chinese string literals, and
        # the ANSI default would mis-decode them into a false failure.
        $probeText = [System.IO.File]::ReadAllText($probePath, [System.Text.Encoding]::UTF8)

        $parseErrors = $null
        $tokens = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseInput(
            $probeText, [ref] $tokens, [ref] $parseErrors
        )
        if ($parseErrors.Count -gt 0) {
            $messages = @($parseErrors | ForEach-Object { "$($_.Message) @line $($_.Extent.StartLineNumber)" })
            throw "$relativePath does not parse:`n$($messages -join "`n")"
        }

        # Every embedded program opens with an import statement; that marker tells
        # a here-string holding Python apart from an ordinary message.
        $embedded = @(
            $ast.FindAll(
                {
                    param($node)
                    $node -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and
                        $node.Value -match '(?m)^\s*import\s'
                },
                $true
            )
        )
        if ($embedded.Count -eq 0) {
            throw "No embedded Python program was found in $relativePath."
        }

        $index = 0
        foreach ($node in $embedded) {
            $index++
            $programCount++
            $source = $node.Value
            foreach ($entry in $substitutions) {
                $source = $source.Replace($entry.Name, $entry.Value)
            }

            # An unexpanded variable means the substitution table drifted from the
            # probe: a new ``$name`` was introduced without a matching entry.
            $leaked = [regex]::Match($source, '\$[A-Za-z_][A-Za-z0-9_]*')
            if ($leaked.Success) {
                throw (
                    "${relativePath} embedded program #${index} still contains an " +
                    "unexpanded PowerShell variable '$($leaked.Value)'. Add it to the " +
                    'substitution table.'
                )
            }

            $fileName = "$([System.IO.Path]::GetFileNameWithoutExtension($relativePath))-$index.py"
            [System.IO.File]::WriteAllText(
                (Join-Path $staging $fileName), $source, (New-Object System.Text.UTF8Encoding($false))
            )
        }
    }

    # ``ast.parse`` is enough: the programs are never executed here, only read.
    $checker = @'
import ast
import pathlib
import sys

failures = []
programs = sorted(pathlib.Path(sys.argv[1]).glob('*.py'))
for path in programs:
    try:
        ast.parse(path.read_text(encoding='utf-8'))
    except SyntaxError as error:
        failures.append(f'{path.name}: line {error.lineno}: {error.msg}')

if failures:
    print('\n'.join(failures))
    raise SystemExit(1)
print(f'PROBE_PYTHON_OK programs={len(programs)}')
'@
    [System.IO.File]::WriteAllText(
        (Join-Path $staging 'check.py'), $checker, (New-Object System.Text.UTF8Encoding($false))
    )

    $output = & docker @(
        'run', '--rm',
        '--volume', "${staging}:/probe:ro",
        $pythonImage,
        'python', '/probe/check.py', '/probe'
    ) 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Embedded probe programs are not valid Python:`n$($output -join "`n")"
    }
    Write-Output ($output | Select-Object -Last 1)
}
finally {
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
}
