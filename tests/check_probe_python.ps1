[CmdletBinding()]
param()

# Static guard for tests/validate_nonseed_flow.ps1.
#
# That probe embeds a Python program inside a PowerShell here-string and ships it
# into the api container with ``python -c``. Two failure modes are invisible to
# the PowerShell parser and only surface at runtime -- after Docker has spent
# minutes building -- which makes them expensive to discover:
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
# instead. CPython comes from the development image -- the same interpreter the
# rest of ``verify`` uses -- so this adds no new host dependency.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Split-Path -Parent $PSScriptRoot
$probePath = Join-Path $repoRoot 'tests/validate_nonseed_flow.ps1'
$developmentImage = 'resume-copilot-backend-development:local'

if (-not (Test-Path -LiteralPath $probePath)) {
    throw "Missing probe: $probePath"
}

# Read as UTF-8 explicitly: the probe holds Chinese string literals, and the
# ANSI default would mis-decode them into a false failure.
$probeText = [System.IO.File]::ReadAllText($probePath, [System.Text.Encoding]::UTF8)

$parseErrors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $probeText, [ref] $tokens, [ref] $parseErrors
)
if ($parseErrors.Count -gt 0) {
    $messages = @($parseErrors | ForEach-Object { "$($_.Message) @line $($_.Extent.StartLineNumber)" })
    throw "validate_nonseed_flow.ps1 does not parse:`n$($messages -join "`n")"
}

# Every embedded program opens with an import statement; that marker tells a
# here-string holding Python apart from an ordinary message.
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
    throw 'No embedded Python program was found in the probe.'
}

# The values the runtime substitutes. They only need to be syntactically valid
# stand-ins: this check is about the program's shape, not its wiring.
$substitutions = [ordered] @{
    '$uploadFilename' = 'fin010-static-check.docx'
    '$mediaType'      = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    '$Token'          = 'static-check-token'
    '$Method'         = 'GET'
    '$Path'           = '/api/v1/health'
    '$bodyBase64'     = 'e30='
    '$ContentType'    = 'application/json'
}

$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("fin010-probe-check-" + [Guid]::NewGuid().ToString('N'))
[void] (New-Item -ItemType Directory -Path $staging -Force)

try {
    $index = 0
    foreach ($node in $embedded) {
        $index++
        $source = $node.Value
        foreach ($name in $substitutions.Keys) {
            $source = $source.Replace($name, $substitutions[$name])
        }

        # An unexpanded variable means the substitution table drifted from the
        # probe: a new ``$name`` was introduced without a matching entry.
        $leaked = [regex]::Match($source, '\$[A-Za-z_][A-Za-z0-9_]*')
        if ($leaked.Success) {
            throw (
                "Embedded program #${index} still contains an unexpanded PowerShell " +
                "variable '$($leaked.Value)'. Add it to the substitution table."
            )
        }

        $name = "program-$index.py"
        [System.IO.File]::WriteAllText(
            (Join-Path $staging $name), $source, (New-Object System.Text.UTF8Encoding($false))
        )
    }

    # ``ast.parse`` is enough: the program is never executed here, only read.
    $checker = @'
import ast
import pathlib
import sys

failures = []
for path in sorted(pathlib.Path(sys.argv[1]).glob('*.py')):
    try:
        ast.parse(path.read_text(encoding='utf-8'))
    except SyntaxError as error:
        failures.append(f'{path.name}: line {error.lineno}: {error.msg}')

if failures:
    print('\n'.join(failures))
    raise SystemExit(1)
print(f'PROBE_PYTHON_OK programs={len(list(pathlib.Path(sys.argv[1]).glob("*.py")))}')
'@
    [System.IO.File]::WriteAllText(
        (Join-Path $staging 'check.py'), $checker, (New-Object System.Text.UTF8Encoding($false))
    )

    $output = & docker @(
        'run', '--rm',
        '--volume', "${staging}:/probe:ro",
        $developmentImage,
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
