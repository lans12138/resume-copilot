[CmdletBinding()]
param(
    [string[]] $Paths = @('tests/validate_nginx_proxy.ps1'),
    [string] $ReportPath = ''
)

# Static parse gate for probe scripts.
#
# Extracted from an inline command because PowerShell cannot re-parse a script it
# is already executing, and because the sandbox has no way to read the output of
# a nested `pwsh` invocation. Results therefore always go to a file when a report
# path is given, which is also what makes the gate usable from `verify` without
# depending on stdout capture.
$repoRoot = Split-Path -Parent $PSScriptRoot
$lines = @()
$failed = $false

foreach ($relative in $Paths) {
    $absolute = Join-Path $repoRoot $relative
    $tokens = $null
    $errors = $null
    $text = [System.IO.File]::ReadAllText($absolute, [System.Text.Encoding]::UTF8)
    [void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
    if ($errors.Count -eq 0) {
        $lines += "PARSE_OK $relative"
    }
    else {
        $failed = $true
        foreach ($errorRecord in $errors) {
            $lines += ("PARSE_ERR {0}:{1} {2}" -f $relative, $errorRecord.Extent.StartLineNumber, $errorRecord.Message)
        }
    }
}

$report = $lines -join "`n"
if ($ReportPath) {
    Set-Content -LiteralPath $ReportPath -Value $report -Encoding UTF8
}
Write-Output $report

if ($failed) { exit 1 }
exit 0
