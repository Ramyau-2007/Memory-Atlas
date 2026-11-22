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
$outerFailureFunction = $wrapperAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Get-SafeOuterFailureMessage'
}, $true)
$uriValidatorFunction = $wrapperAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Get-NeonUriRejectionCategory'
}, $true)
if ($null -eq $parserFunction -or $null -eq $displayFunction -or
    $null -eq $outerFailureFunction -or $null -eq $uriValidatorFunction) {
    throw 'Safe diagnostic functions were not found.'
}
$wrapperSource = Get-Content -LiteralPath $wrapperPath -Raw
Invoke-Expression $parserFunction.Extent.Text
Invoke-Expression $displayFunction.Extent.Text
Invoke-Expression $outerFailureFunction.Extent.Text
Invoke-Expression $uriValidatorFunction.Extent.Text

$uriValidationCases = @(
    @{ Name = 'postgresql direct URI'; Value = 'postgresql://demo_user:demo%21pass@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require'; Expected = $null },
    @{ Name = 'postgres direct URI'; Value = 'postgres://demo_user:demo%21pass@ep-pooler-demo.us-east-2.aws.neon.tech/neondb?sslmode=require'; Expected = $null },
    @{ Name = 'pooled endpoint'; Value = 'postgresql://demo_user:demo%21pass@ep-example-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require'; Expected = 'pooled endpoint' },
    @{ Name = 'unsupported scheme'; Value = 'https://demo_user:demo%21pass@ep-example.us-east-2.aws.neon.tech/neondb'; Expected = 'unsupported scheme' },
    @{ Name = 'malformed URI'; Value = 'postgresql://[malformed'; Expected = 'invalid URI' },
    @{ Name = 'missing credential separator'; Value = 'postgresql://ep-example.us-east-2.aws.neon.tech/neondb'; Expected = 'missing credential separator' },
    @{ Name = 'non-Neon host'; Value = 'postgresql://demo_user:demo%21pass@db.example.test/neondb'; Expected = 'non-Neon host' }
)
$allowedUriRejections = @('invalid URI', 'unsupported scheme', 'non-Neon host', 'pooled endpoint', 'missing credential separator')
foreach ($case in $uriValidationCases) {
    $actual = Get-NeonUriRejectionCategory -Value $case.Value
    if ($actual -cne $case.Expected) {
        throw "Synthetic URI validation case failed: $($case.Name)."
    }
    if ($null -ne $actual -and $actual -cnotin $allowedUriRejections) {
        throw "URI validator returned a non-allowlisted rejection category for $($case.Name)."
    }
}
$validatorCallIndex = $wrapperSource.IndexOf('$uriRejectionCategory = Get-NeonUriRejectionCategory', [System.StringComparison]::Ordinal)
$pythonInvocationIndex = $wrapperSource.IndexOf('$migrationOutput = @(& $python -m scripts.migrate_sqlite_to_postgres', [System.StringComparison]::Ordinal)
if ($validatorCallIndex -lt 0 -or $pythonInvocationIndex -le $validatorCallIndex) {
    throw 'URI validation is not positioned before Python invocation.'
}
Write-Output 'Synthetic Direct URI acceptance and fixed URI rejection categories: PASS'

$outerStages = @('setup', 'SQLite preflight', 'hidden URI input', 'URI validation', 'Python invocation', 'count reporting')
foreach ($stageName in $outerStages) {
    if ($wrapperSource -notmatch [regex]::Escape("`$currentStage = '$stageName'")) {
        throw "Wrapper is missing stage tracking for $stageName."
    }
}
$mayStartIndex = $wrapperSource.IndexOf('$migrationMayHaveStarted = $true', [System.StringComparison]::Ordinal)
$invokeIndex = $wrapperSource.IndexOf('$migrationOutput = @(& $python -m scripts.migrate_sqlite_to_postgres', [System.StringComparison]::Ordinal)
$nonzeroExitIndex = $wrapperSource.IndexOf('if ($migrationExitCode -ne 0)', [System.StringComparison]::Ordinal)
$countStageIndex = $wrapperSource.IndexOf("`$currentStage = 'count reporting'", [System.StringComparison]::Ordinal)
if ($mayStartIndex -lt 0 -or $invokeIndex -le $mayStartIndex -or
    $nonzeroExitIndex -le $invokeIndex -or $countStageIndex -le $nonzeroExitIndex) {
    throw 'Python-start and successful-exit count-reporting stages are not ordered safely.'
}
foreach ($stageName in @('setup', 'SQLite preflight', 'hidden URI input', 'URI validation')) {
    $safeMessage = Get-SafeOuterFailureMessage -Stage $stageName -MigrationMayHaveStarted $false
    if ($safeMessage -cne "Migration not started. Stage: $stageName.") {
        throw "Pre-migration failure was not safely identified for stage $stageName."
    }
}
Write-Output 'Synthetic setup, SQLite preflight, hidden URI input, and URI validation failure reporting: PASS'

$pythonFailureMessage = Get-SafeOuterFailureMessage -Stage 'Python invocation' -MigrationMayHaveStarted $true
if ($pythonFailureMessage -cne 'Migration may have started; inspect Neon counts before retrying. Stage: Python invocation.') {
    throw 'Python invocation failure was not classified as potentially started.'
}
$postExitReportFailureMessage = Get-SafeOuterFailureMessage -Stage 'count reporting' -MigrationMayHaveStarted $true
if ($postExitReportFailureMessage -cne 'Migration may have started; inspect Neon counts before retrying. Stage: count reporting.') {
    throw 'Count-reporting failure after process exit was not classified as potentially started.'
}
$unknownStageMessage = Get-SafeOuterFailureMessage -Stage 'raw exception / secret' -MigrationMayHaveStarted $false
if ($unknownStageMessage -cne 'Migration not started. Stage: setup.') {
    throw 'Unrecognized stage text was not replaced with the fixed setup label.'
}
Write-Output 'Synthetic Python invocation and post-exit count-reporting failure classification: PASS'

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

$sequenceFailureLine = '2026-11-22 12:34:56,789 [ERROR] Migration failed phase=Sequence reset; subphase=ALTER SEQUENCE execution; exception_type=ProgrammingError; sqlstate=42601; diagnostic=PostgreSQL ALTER SEQUENCE execution failed; details redacted.'
$script:hostLines.Clear()
$parsedSequenceFailure = ConvertFrom-SafeMigrationOutput -CapturedText $sequenceFailureLine
if ($null -eq $parsedSequenceFailure -or $parsedSequenceFailure.Phase -cne 'Sequence reset' -or
    $parsedSequenceFailure.Subphase -cne 'ALTER SEQUENCE execution' -or
    $parsedSequenceFailure.ExceptionClass -cne 'ProgrammingError' -or $parsedSequenceFailure.SqlState -cne '42601') {
    throw 'Sequence-reset subphase diagnostic was not parsed correctly.'
}
Show-SafeMigrationDiagnostics -CapturedText $sequenceFailureLine
if (-not $script:hostLines.Contains('Migration phase: Sequence reset') -or
    -not $script:hostLines.Contains('Migration subphase: ALTER SEQUENCE execution')) {
    throw 'Sequence-reset outer phase and subphase were not displayed.'
}
Write-Output 'Sequence-reset subphase diagnostic display: PASS'

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
if (-not $script:hostLines.Contains('Migration not started. Stage: setup.')) {
    throw 'Actual setup error path did not report its fixed stage and not-started status.'
}
Write-Output 'Caller ErrorActionPreference restoration on synthetic error path: PASS'
