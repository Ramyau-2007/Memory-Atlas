$incomingErrorActionPreference = $ErrorActionPreference
$secret = $null
$databaseUrl = $null
$migrationOutput = $null
$rawOutput = $null
$uri = $null
$originalLocation = $null
$hadDatabaseUrl = $false
$databaseUrlSnapshotCaptured = $false
$previousDatabaseUrl = $null
$migrationFailed = $false

function ConvertFrom-SafeMigrationOutput {
    param([AllowNull()][string]$CapturedText)

    $phaseMessages = @{
        'SQLite source preparation' = 'SQLite source preparation failed; details redacted.'
        'PostgreSQL connection / initial count check' = 'PostgreSQL connection or initial count check failed; connection details redacted.'
        'PostgreSQL transaction acquisition' = 'PostgreSQL transaction acquisition failed; connection details redacted.'
        'Schema initialization' = 'PostgreSQL schema initialization failed; details redacted.'
        'Table locking and destination recheck' = 'PostgreSQL table locking or destination recheck failed; details redacted.'
        'Data insertion' = 'PostgreSQL data insertion failed; details redacted.'
        'Sequence reset' = 'PostgreSQL sequence reset failed; details redacted.'
        'Final row-count verification' = 'Final PostgreSQL row-count verification failed; details redacted.'
        'PostgreSQL transaction commit' = 'PostgreSQL transaction commit failed; details redacted.'
        'Post-commit reporting' = 'PostgreSQL transaction committed, but success reporting failed; inspect the destination before retrying.'
    }
    $timestampPrefix = '(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[ERROR\] )?'
    $primaryPattern = '^' + $timestampPrefix + 'Migration failed phase=(?<phase>[^;\r\n]+); exception_type=(?<exception>[A-Za-z_][A-Za-z0-9_]{0,63}); sqlstate=(?<sqlstate>[^;\r\n]+); diagnostic=(?<message>[^\r\n]+)$'
    $rollbackPattern = '^' + $timestampPrefix + 'Rollback failed; exception_type=(?<exception>[A-Za-z_][A-Za-z0-9_]{0,63}); diagnostic=(?<message>[^\r\n]+)$'
    $lines = @($CapturedText -split "`r?`n")
    $primaryCandidates = @($lines | Where-Object { $_ -match 'Migration failed phase=' })
    $rollbackCandidates = @($lines | Where-Object { $_ -match 'Rollback failed;' })

    if ($primaryCandidates.Count -ne 1 -or $rollbackCandidates.Count -gt 1) {
        return $null
    }
    if ($primaryCandidates[0] -notmatch $primaryPattern) {
        return $null
    }

    $phase = $Matches['phase']
    $exceptionClass = $Matches['exception']
    $sqlState = $Matches['sqlstate']
    $message = $Matches['message']
    if (-not $phaseMessages.ContainsKey($phase) -or $message -cne $phaseMessages[$phase]) {
        return $null
    }
    if ($sqlState -cne 'unavailable' -and $sqlState -cnotmatch '^[0-9A-Z]{5}$') {
        return $null
    }

    $rollback = $null
    if ($rollbackCandidates.Count -eq 1) {
        if ($rollbackCandidates[0] -notmatch $rollbackPattern) {
            return $null
        }
        $rollbackClass = $Matches['exception']
        $rollbackMessage = $Matches['message']
        if ($rollbackMessage -cne 'Rollback failure details redacted.') {
            return $null
        }
        $rollback = [pscustomobject]@{
            ExceptionClass = $rollbackClass
            Message = $rollbackMessage
        }
    }

    return [pscustomobject]@{
        Phase = $phase
        ExceptionClass = $exceptionClass
        SqlState = if ($sqlState -ceq 'unavailable') { $null } else { $sqlState }
        Message = $message
        Rollback = $rollback
    }
}

function Show-SafeMigrationDiagnostics {
    param([AllowNull()][string]$CapturedText)

    $stage = 'Python output capture'
    $safeDiagnostics = $null
    try {
        Write-Host 'Diagnostic stage 1 complete: Python output captured successfully.'
        $stage = 'diagnostic parsing'
        Write-Host 'Diagnostic stage 2: diagnostic parsing started.'
        $safeDiagnostics = ConvertFrom-SafeMigrationOutput -CapturedText $CapturedText
        if ($null -eq $safeDiagnostics) {
            Write-Host 'Safe diagnostic stage failed: diagnostic parsing.'
            return
        }
        Write-Host 'Diagnostic stage 3 complete: diagnostic parsing completed.'

        $stage = 'diagnostic display'
        if ($safeDiagnostics.Phase -ceq 'Post-commit reporting') {
            Write-Host 'Migration transaction committed, but success reporting failed. Inspect Neon counts before retrying.'
        }
        else {
            Write-Host 'Migration process failed before confirmed commit. Inspect Neon counts before retrying.'
        }
        Write-Host ('Migration phase: {0}' -f $safeDiagnostics.Phase)
        Write-Host ('Exception class: {0}' -f $safeDiagnostics.ExceptionClass)
        if ($safeDiagnostics.SqlState) {
            Write-Host ('SQLSTATE: {0}' -f $safeDiagnostics.SqlState)
        }
        Write-Host ('Diagnostic: {0}' -f $safeDiagnostics.Message)
        if ($null -ne $safeDiagnostics.Rollback) {
            Write-Host ('Rollback exception class: {0}' -f $safeDiagnostics.Rollback.ExceptionClass)
            Write-Host ('Rollback diagnostic: {0}' -f $safeDiagnostics.Rollback.Message)
        }
        Write-Host 'Diagnostic stage 4 complete: diagnostic display completed.'
    }
    catch {
        Write-Host ('Safe diagnostic stage failed: {0}.' -f $stage)
    }
    finally {
        $safeDiagnostics = $null
    }
}

try {
    $ErrorActionPreference = 'Stop'

    $projectRoot = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $sqliteRelativePath = 'data/memory.sqlite3'
    $originalLocation = Get-Location
    $hadDatabaseUrl = Test-Path Env:DATABASE_URL
    if ($hadDatabaseUrl) {
        $previousDatabaseUrl = $env:DATABASE_URL
    }
    $databaseUrlSnapshotCaptured = $true

    Set-Location -LiteralPath $projectRoot

    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Project virtual-environment Python was not found.'
    }
    if (-not (Test-Path -LiteralPath $sqliteRelativePath -PathType Leaf)) {
        throw 'The local SQLite source database was not found.'
    }

    # One-line Python avoids PowerShell here-string/paste parsing problems.
    # SQLite is opened with mode=ro; this preflight only prints the violation count.
    $fkCheckCode = "import pathlib,sqlite3; p=pathlib.Path(r'data/memory.sqlite3').resolve(); c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); rows=c.execute('PRAGMA foreign_key_check').fetchall(); print(len(rows)); c.close()"
    $fkOutput = (& $python -c $fkCheckCode 2>&1 | Out-String).Trim()
    $fkExitCode = $LASTEXITCODE
    if ($fkExitCode -ne 0 -or $fkOutput -notmatch '^\d+$') {
        throw 'SQLite foreign-key preflight failed; migration was not started.'
    }
    if ([int]$fkOutput -ne 0) {
        throw "SQLite has $fkOutput foreign-key violation(s); migration was not started."
    }
    Write-Host 'SQLite foreign-key check: 0 violations'

    if (-not ('MemoryAtlasHiddenInput' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class MemoryAtlasHiddenInput
{
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetStdHandle(int nStdHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);

    public static string ReadLineHidden()
    {
        IntPtr handle = GetStdHandle(-10);
        uint originalMode;
        if (handle == IntPtr.Zero || !GetConsoleMode(handle, out originalMode))
            throw new InvalidOperationException("Console input is unavailable.");
        if (!SetConsoleMode(handle, originalMode & ~(uint)0x0004))
            throw new InvalidOperationException("Could not disable console input echo.");
        try
        {
            return Console.ReadLine() ?? "";
        }
        finally
        {
            SetConsoleMode(handle, originalMode);
        }
    }
}
'@ -ErrorAction Stop
    }

    Write-Host 'Enter the Neon DIRECT PostgreSQL URI (input is hidden):'
    $secret = [MemoryAtlasHiddenInput]::ReadLineHidden()
    try {
        $uri = [System.Uri]$secret
    }
    catch {
        throw 'The entered value is not a valid PostgreSQL URI.'
    }

    if (
        -not $uri.IsAbsoluteUri -or
        $uri.Scheme -notin @('postgres', 'postgresql') -or
        $uri.Host -notmatch '\.neon\.tech$' -or
        $uri.Host -match '-pooler' -or
        -not $uri.UserInfo.Contains(':')
    ) {
        throw 'Enter a direct Neon PostgreSQL URI. Migration was not started.'
    }

    # The URI is never passed in process arguments. The migration tool checks
    # destination emptiness before writes and rechecks under locks in its transaction.
    $databaseUrl = $secret
    $env:DATABASE_URL = $databaseUrl
    $previousErrorActionPreference = $ErrorActionPreference
    $migrationInvocationFailed = $false
    try {
        $ErrorActionPreference = 'Continue'
        $migrationOutput = @(& $python -m scripts.migrate_sqlite_to_postgres --sqlite-path $sqliteRelativePath --apply 2>&1)
        $migrationExitCode = $LASTEXITCODE
    }
    catch {
        $migrationInvocationFailed = $true
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        $previousErrorActionPreference = $null
    }

    if ($migrationInvocationFailed) {
        $migrationFailed = $true
        Write-Host 'Safe diagnostic stage failed: Python output capture.'
        Write-Host 'Migration status could not be determined. Inspect Neon counts before retrying.'
        return
    }

    try {
        $rawOutput = ($migrationOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    }
    catch {
        $migrationFailed = $true
        Write-Host 'Safe diagnostic stage failed: Python output capture.'
        Write-Host 'Migration status could not be determined. Inspect Neon counts before retrying.'
        return
    }

    if ($migrationExitCode -ne 0) {
        $migrationFailed = $true
        Show-SafeMigrationDiagnostics -CapturedText $rawOutput
        return
    }

    $counts = @{}
    $countPattern = '\[INFO\]\s+(users|invitations|sessions|conversations|messages|memories|memory_versions|temporary_context|memory_events)\s+(\d+)\s+(\d+)(?:\s|$)'
    foreach ($outputLine in ($rawOutput -split "`r?`n")) {
        if ($outputLine -match $countPattern) {
            $tableName = $Matches[1]
            $counts[$tableName] = [pscustomobject]@{
                SQLite = [int]$Matches[2]
                Neon   = [int]$Matches[3]
            }
        }
    }

    $tables = @(
        'users', 'invitations', 'sessions', 'conversations', 'messages',
        'memories', 'memory_versions', 'temporary_context', 'memory_events'
    )
    $missingCounts = @($tables | Where-Object { -not $counts.ContainsKey($_) })
    if ($missingCounts.Count -gt 0) {
        Write-Host 'Migration process exited successfully, but migration completion could not be verified. Do not rerun; check Neon counts.'
    }
    else {
        Write-Host ''
        Write-Host 'Migration completed. Final table counts:'
        foreach ($table in $tables) {
            Write-Host ('{0}: SQLite={1}, Neon={2}' -f $table, $counts[$table].SQLite, $counts[$table].Neon)
        }
    }
}
catch {
    # Never print exception details, native output, or anything that might contain a URI.
    if ($migrationFailed) {
        Write-Host 'Migration status could not be determined. Inspect Neon counts before retrying.'
    }
    else {
        Write-Host 'Safe migration stopped before completion. No raw diagnostics were displayed.'
    }
}
finally {
    try {
        if ($databaseUrlSnapshotCaptured) {
            if ($hadDatabaseUrl) {
                $env:DATABASE_URL = $previousDatabaseUrl
            }
            else {
                Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
            }
        }
        $secret = $null
        $databaseUrl = $null
        $previousDatabaseUrl = $null
        $uri = $null
        $rawOutput = $null
        $migrationOutput = $null
        $safeDiagnostics = $null
        if ($null -ne $originalLocation) {
            Set-Location -LiteralPath $originalLocation
        }
        Remove-Variable fkCheckCode, fkOutput, fkExitCode, migrationExitCode, counts, countPattern, outputLine, tableName, tables, missingCounts -ErrorAction SilentlyContinue
    }
    finally {
        $ErrorActionPreference = $incomingErrorActionPreference
        $incomingErrorActionPreference = $null
    }
}
