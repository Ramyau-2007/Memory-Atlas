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
$currentStage = 'setup'
$migrationMayHaveStarted = $false

function Read-NeonUriHidden {
    $clipboardText = $null
    try {
        Write-Host 'Reading the Direct URI from the clipboard; its contents remain hidden.'
        $clipboardText = Get-Clipboard -Raw -ErrorAction Stop
        if ($null -eq $clipboardText -or [string]::IsNullOrEmpty([string]$clipboardText)) {
            throw 'Clipboard input is empty.'
        }
        $clipboardText = [string]$clipboardText
        if ([regex]::IsMatch($clipboardText, '[\r\n\u0085\u2028\u2029]')) {
            throw 'Clipboard input must be one line.'
        }
        if ([regex]::IsMatch($clipboardText, '[\x00-\x1F\x7F]')) {
            throw 'Clipboard input contains control characters.'
        }
        return $clipboardText
    }
    finally {
        $clipboardText = $null
    }
}

function Get-SafeOuterFailureMessage {
    param(
        [AllowNull()][string]$Stage,
        [bool]$MigrationMayHaveStarted
    )

    $allowedStages = @(
        'setup',
        'SQLite preflight',
        'hidden URI input',
        'URI validation',
        'Python invocation',
        'count reporting'
    )
    if ($Stage -cnotin $allowedStages) {
        $Stage = 'setup'
    }

    if ($MigrationMayHaveStarted) {
        return "Migration may have started; inspect Neon counts before retrying. Stage: $Stage."
    }
    return "Migration not started. Stage: $Stage."
}

function Get-NeonUriRejectionCategory {
    param(
        [AllowNull()][string]$Value,
        [ref]$EncodedUriOutput
    )

    if ($null -eq $Value) {
        return 'hidden input empty'
    }

    # ReadLineHidden returns a plain .NET string with console echo disabled.
    # Normalize only copy/paste whitespace and one leading Unicode BOM.
    $normalizedValue = $Value.Trim()
    if ($normalizedValue.StartsWith([string][char]0xFEFF, [System.StringComparison]::Ordinal)) {
        $normalizedValue = $normalizedValue.Substring(1).TrimStart()
    }
    if ([string]::IsNullOrWhiteSpace($normalizedValue)) {
        return 'hidden input empty'
    }
    if ($normalizedValue -match '[\x00-\x1F\x7F]') {
        return 'input contains control characters'
    }
    if ($normalizedValue -match '[\u200B\u200E\u200F\u202A-\u202E\u2060-\u206F\uFEFF]') {
        return 'input contains invisible formatting characters'
    }

    # Locate the host boundary from the right so raw URI delimiters in copied
    # credentials (such as @, /, ? and #) stay inside the credential section.
    # Encode the username and password separately, preserving existing %HH.
    $credentialUriPattern = '^(?<prefix>[A-Za-z][A-Za-z0-9+.-]*://)(?<userInfo>.*)@(?<host>[^:/?#]+)(?<port>:\d+)?(?<suffix>[/\?#].*)?$'
    $credentialUriMatch = [regex]::Match($normalizedValue, $credentialUriPattern)
    $uriToParse = $normalizedValue
    if ($credentialUriMatch.Success) {
        $rawUserInfo = $credentialUriMatch.Groups['userInfo'].Value
        if ($rawUserInfo -match '%(?![0-9A-Fa-f]{2})') {
            return 'malformed credential percent escape'
        }
        $credentialColon = $rawUserInfo.IndexOf(':')
        if ($credentialColon -lt 0) {
            return 'missing credential separator'
        }

        $encodeCredentialComponent = {
            param([string]$Component)
            $builder = New-Object System.Text.StringBuilder
            $position = 0
            foreach ($escape in [regex]::Matches($Component, '%[0-9A-Fa-f]{2}')) {
                if ($escape.Index -gt $position) {
                    [void]$builder.Append([System.Uri]::EscapeDataString($Component.Substring($position, $escape.Index - $position)))
                }
                [void]$builder.Append($escape.Value)
                $position = $escape.Index + $escape.Length
            }
            if ($position -lt $Component.Length) {
                [void]$builder.Append([System.Uri]::EscapeDataString($Component.Substring($position)))
            }
            return $builder.ToString()
        }

        $rawUsername = $rawUserInfo.Substring(0, $credentialColon)
        $rawPassword = $rawUserInfo.Substring($credentialColon + 1)
        $safeUserInfo = (& $encodeCredentialComponent $rawUsername) + ':' + (& $encodeCredentialComponent $rawPassword)
        $uriToParse = $credentialUriMatch.Groups['prefix'].Value + $safeUserInfo + '@' +
            $credentialUriMatch.Groups['host'].Value + $credentialUriMatch.Groups['port'].Value +
            $credentialUriMatch.Groups['suffix'].Value
    }

    $parsedUri = $null
    if (-not [System.Uri]::TryCreate($uriToParse, [System.UriKind]::Absolute, [ref]$parsedUri)) {
        return 'System.Uri rejected syntax; reserved credential characters may require percent-encoding'
    }
    if ($null -eq $parsedUri -or -not $parsedUri.IsAbsoluteUri) {
        return 'URI is not absolute'
    }
    if ($parsedUri.Scheme -notin @('postgres', 'postgresql')) {
        return 'unsupported scheme'
    }
    if ($parsedUri.Host -notmatch '\.neon\.tech$') {
        return 'non-Neon host'
    }
    if ($parsedUri.Host -match '-pooler\.') {
        return 'pooled endpoint'
    }

    $userInfo = $parsedUri.UserInfo
    if (-not $userInfo.Contains(':')) {
        return 'missing credential separator'
    }
    if ($userInfo -match '%(?![0-9A-Fa-f]{2})') {
        return 'malformed credential percent escape'
    }
    if ($null -ne $EncodedUriOutput) {
        $EncodedUriOutput.Value = $uriToParse
    }
    return $null
}

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
    $sequenceSubphaseMessages = @{
        'Sequence lookup using pg_get_serial_sequence' = 'PostgreSQL serial sequence lookup failed; details redacted.'
        'Maximum primary-key ID query' = 'PostgreSQL maximum primary-key ID query failed; details redacted.'
        'ALTER SEQUENCE statement generation' = 'PostgreSQL ALTER SEQUENCE statement generation failed; details redacted.'
        'ALTER SEQUENCE execution' = 'PostgreSQL ALTER SEQUENCE execution failed; details redacted.'
    }
    $timestampPrefix = '(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \[ERROR\] )?'
    $primaryPattern = '^' + $timestampPrefix + 'Migration failed phase=(?<phase>[^;\r\n]+); (?:subphase=(?<subphase>[^;\r\n]+); )?exception_type=(?<exception>[A-Za-z_][A-Za-z0-9_]{0,63}); sqlstate=(?<sqlstate>[^;\r\n]+); diagnostic=(?<message>[^\r\n]+)$'
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
    $subphase = $Matches['subphase']
    $exceptionClass = $Matches['exception']
    $sqlState = $Matches['sqlstate']
    $message = $Matches['message']
    if (-not $phaseMessages.ContainsKey($phase)) {
        return $null
    }
    if ($subphase) {
        if ($phase -cne 'Sequence reset' -or -not $sequenceSubphaseMessages.ContainsKey($subphase) -or
            $message -cne $sequenceSubphaseMessages[$subphase]) {
            return $null
        }
    }
    elseif ($message -cne $phaseMessages[$phase]) {
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
        Subphase = $subphase
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
        if ($safeDiagnostics.Subphase) {
            Write-Host ('Migration subphase: {0}' -f $safeDiagnostics.Subphase)
        }
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

    $currentStage = 'setup'
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

    $currentStage = 'SQLite preflight'
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

    $currentStage = 'hidden URI input'
    $secret = Read-NeonUriHidden

    $currentStage = 'URI validation'
    $normalizedDatabaseUrl = $null
    $uriRejectionCategory = Get-NeonUriRejectionCategory -Value $secret -EncodedUriOutput ([ref]$normalizedDatabaseUrl)
    if ($null -ne $uriRejectionCategory) {
        Write-Host ("Migration not started. URI validation rejected: {0}." -f $uriRejectionCategory)
        return
    }
    $secret = $normalizedDatabaseUrl

    # The URI is never passed in process arguments. The migration tool checks
    # destination emptiness before writes and rechecks under locks in its transaction.
    $currentStage = 'Python invocation'
    $databaseUrl = $secret
    $env:DATABASE_URL = $databaseUrl
    $previousErrorActionPreference = $ErrorActionPreference
    $migrationInvocationFailed = $false
    try {
        $ErrorActionPreference = 'Continue'
        $migrationMayHaveStarted = $true
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
        Write-Host 'Safe diagnostic stage failed: Python output capture.'
        Write-Host (Get-SafeOuterFailureMessage -Stage $currentStage -MigrationMayHaveStarted $migrationMayHaveStarted)
        return
    }

    try {
        $rawOutput = ($migrationOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    }
    catch {
        Write-Host 'Safe diagnostic stage failed: Python output capture.'
        Write-Host (Get-SafeOuterFailureMessage -Stage $currentStage -MigrationMayHaveStarted $migrationMayHaveStarted)
        return
    }

    if ($migrationExitCode -ne 0) {
        Show-SafeMigrationDiagnostics -CapturedText $rawOutput
        return
    }

    $currentStage = 'count reporting'
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
    Write-Host (Get-SafeOuterFailureMessage -Stage $currentStage -MigrationMayHaveStarted $migrationMayHaveStarted)
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
