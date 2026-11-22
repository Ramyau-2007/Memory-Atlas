$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$wrapperPath = Join-Path $projectRoot 'scripts\run_safe_migration.ps1'
$tokens = $null
$parseErrors = $null
$wrapperAst = [System.Management.Automation.Language.Parser]::ParseFile(
    $wrapperPath,
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -gt 0) {
    throw 'Migration wrapper has PowerShell parse errors.'
}

$parserFunction = $wrapperAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'ConvertFrom-SafeMigrationOutput'
}, $true)
$displayFunction = $wrapperAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Show-SafeMigrationDiagnostics'
}, $true)
if ($null -eq $parserFunction -or $null -eq $displayFunction) {
    throw 'Safe diagnostic functions were not found.'
}
Invoke-Expression $parserFunction.Extent.Text
Invoke-Expression $displayFunction.Extent.Text

$script:hostLines = New-Object 'System.Collections.Generic.List[string]'
$script:failDisplayOnce = $false
$script:failOnText = $null
function Write-Host {
    param([Parameter(Position = 0)][object]$Object)
    $line = [string]$Object
    if ($script:failOnText -and $line -like $script:failOnText) {
        $script:failOnText = $null
        throw 'synthetic stage marker failure'
    }
    if ($script:failDisplayOnce -and $line -like 'Migration phase:*') {
        $script:failDisplayOnce = $false
        throw 'synthetic display failure'
    }
    $script:hostLines.Add($line)
}

$validLines = @(
    '2026-11-22 12:34:56,789 [ERROR] Rollback failed; exception_type=InterfaceError; diagnostic=Rollback failure details redacted.',
    '2026-11-22 12:34:56,789 [ERROR] Migration failed phase=Data insertion; exception_type=IntegrityError; sqlstate=23505; diagnostic=PostgreSQL data insertion failed; details redacted.'
) -join "`n"
$parsed = ConvertFrom-SafeMigrationOutput -CapturedText $validLines
if ($null -eq $parsed) { throw 'Valid sanitized diagnostic lines were rejected.' }
if ($parsed.Phase -cne 'Data insertion' -or $parsed.ExceptionClass -cne 'IntegrityError' -or $parsed.SqlState -cne '23505') {
    throw 'Allowlisted primary diagnostic fields were parsed incorrectly.'
}
if ($parsed.Message -cne 'PostgreSQL data insertion failed; details redacted.') {
    throw 'Fixed primary diagnostic message was not preserved.'
}
if ($null -eq $parsed.Rollback -or $parsed.Rollback.ExceptionClass -cne 'InterfaceError' -or
    $parsed.Rollback.Message -cne 'Rollback failure details redacted.') {
    throw 'Allowlisted rollback diagnostic fields were parsed incorrectly.'
}
Show-SafeMigrationDiagnostics -CapturedText $validLines
foreach ($marker in @(
    'Diagnostic stage 1 complete: Python output captured successfully.',
    'Diagnostic stage 2: diagnostic parsing started.',
    'Diagnostic stage 3 complete: diagnostic parsing completed.',
    'Diagnostic stage 4 complete: diagnostic display completed.'
)) {
    if (-not $script:hostLines.Contains($marker)) { throw "Missing stage marker: $marker" }
}
if (-not $script:hostLines.Contains('Migration phase: Data insertion') -or
    -not $script:hostLines.Contains('Rollback diagnostic: Rollback failure details redacted.')) {
    throw 'Sanitized allowlisted diagnostic fields were not displayed.'
}
Write-Output 'Synthetic sanitized diagnostic display and all four stage markers: PASS'

$postCommitLine = '2026-11-22 12:34:56,789 [ERROR] Migration failed phase=Post-commit reporting; exception_type=RuntimeError; sqlstate=unavailable; diagnostic=PostgreSQL transaction committed, but success reporting failed; inspect the destination before retrying.'
$script:hostLines.Clear()
Show-SafeMigrationDiagnostics -CapturedText $postCommitLine
if (-not $script:hostLines.Contains('Migration transaction committed, but success reporting failed. Inspect Neon counts before retrying.')) {
    throw 'Post-commit reporting failure was not distinguished in output.'
}
if ($script:hostLines -contains 'Migration process failed before confirmed commit. Inspect Neon counts before retrying.') {
    throw 'Post-commit failure was mislabeled as a pre-commit failure.'
}
Write-Output 'Post-commit diagnostic display distinction: PASS'

$script:hostLines.Clear()
$invalidSqlState = 'Migration failed phase=Data insertion; exception_type=IntegrityError; sqlstate=password; diagnostic=PostgreSQL data insertion failed; details redacted.'
if ($null -ne (ConvertFrom-SafeMigrationOutput -CapturedText $invalidSqlState)) {
    throw 'Invalid SQLSTATE was accepted.'
}
Write-Output 'Invalid SQLSTATE rejection: PASS'

$script:hostLines.Clear()
$invalidOutput = 'Migration failed phase=not-allowlisted; exception_type=IntegrityError; sqlstate=23505; diagnostic=raw exception text.'
Show-SafeMigrationDiagnostics -CapturedText $invalidOutput
if (-not $script:hostLines.Contains('Safe diagnostic stage failed: diagnostic parsing.')) {
    throw 'Parsing failure did not produce its fixed stage message.'
}
if ($script:hostLines -contains 'Diagnostic stage 3 complete: diagnostic parsing completed.') {
    throw 'Parsing failure incorrectly reported parsing completion.'
}
Write-Output 'Parser-stage failure marker: PASS'

$script:hostLines.Clear()
$script:failOnText = 'Diagnostic stage 1 complete:*'
Show-SafeMigrationDiagnostics -CapturedText $validLines
if (-not $script:hostLines.Contains('Safe diagnostic stage failed: Python output capture.')) {
    throw 'Capture-stage failure did not produce its fixed stage message.'
}
Write-Output 'Capture-stage failure marker: PASS'

$script:hostLines.Clear()
$script:failDisplayOnce = $true
Show-SafeMigrationDiagnostics -CapturedText $validLines
if (-not $script:hostLines.Contains('Safe diagnostic stage failed: diagnostic display.')) {
    throw 'Display failure did not produce its fixed stage message.'
}
Write-Output 'Display-stage failure marker: PASS'

$invalidMessage = 'Migration failed phase=Data insertion; exception_type=IntegrityError; sqlstate=23505; diagnostic=raw exception text.'
if ($null -ne (ConvertFrom-SafeMigrationOutput -CapturedText $invalidMessage)) {
    throw 'Non-allowlisted diagnostic message was accepted.'
}
Write-Output 'Non-allowlisted message rejection: PASS'

function Invoke-WrapperPreferenceErrorPathTest {
    param([string]$WrapperPath)

    $ErrorActionPreference = 'Continue'
    function Join-Path {
        param([string]$Path, [string]$ChildPath)
        if ($ChildPath -eq '.venv\Scripts\python.exe') {
            return Microsoft.PowerShell.Management\Join-Path -Path $env:TEMP -ChildPath 'memory-atlas-missing-python-for-test.exe'
        }
        return Microsoft.PowerShell.Management\Join-Path -Path $Path -ChildPath $ChildPath
    }

    . $WrapperPath
    return $ErrorActionPreference
}

$restoredPreference = Invoke-WrapperPreferenceErrorPathTest -WrapperPath $wrapperPath
if ($restoredPreference -cne 'Continue') {
    throw 'Wrapper did not restore the caller preference after its error path.'
}
Write-Output 'Caller ErrorActionPreference restoration on synthetic error path: PASS'
