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
$validatorAst = $wrapperAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Get-NeonUriRejectionCategory'
}, $true)
if ($null -eq $validatorAst) {
    throw 'URI validator was not found.'
}
Invoke-Expression $validatorAst.Extent.Text
$hiddenReaderAst = $wrapperAst.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Read-NeonUriHidden'
}, $true)
if ($null -eq $hiddenReaderAst) {
    throw 'Secure hidden URI reader was not found.'
}
Invoke-Expression $hiddenReaderAst.Extent.Text

$script:syntheticClipboard = $null
function Get-Clipboard {
    param([switch]$Raw)
    if (-not $Raw) {
        throw 'Clipboard reader did not request raw text.'
    }
    return $script:syntheticClipboard
}

$script:syntheticClipboard = 'postgresql://demo:p%16ss@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
$clipboardReaderResult = Read-NeonUriHidden
if ($clipboardReaderResult -cne 'postgresql://demo:p%16ss@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require') {
    throw 'Clipboard reader altered valid percent-encoded URI text.'
}
$clipboardReaderResult = $null
Write-Output 'Clipboard reader preserves valid percent-encoded credentials: PASS'

$clipboardRejectionCases = @(
    @{ Name = 'empty clipboard'; Value = '' },
    @{ Name = 'raw U+0016'; Value = ([string][char]0x0016) + 'synthetic' },
    @{ Name = 'tab'; Value = "`t" + 'synthetic' },
    @{ Name = 'carriage return'; Value = "`r" + 'synthetic' },
    @{ Name = 'line feed'; Value = "`n" + 'synthetic' }
)
foreach ($case in $clipboardRejectionCases) {
    $script:syntheticClipboard = $case.Value
    $rejected = $false
    try {
        $null = Read-NeonUriHidden
    }
    catch {
        $rejected = $true
    }
    if (-not $rejected) {
        throw 'Clipboard reader accepted an invalid synthetic input.'
    }
    Write-Output ('Clipboard reader rejects {0}: PASS' -f $case.Name)
}
Remove-Item Function:\Get-Clipboard
$script:syntheticClipboard = $null

$wrapperSource = Get-Content -LiteralPath $wrapperPath -Raw
if ($wrapperSource -notmatch '(?m)^\s*\$secret = Read-NeonUriHidden\s*$' -or
    $wrapperSource -notmatch 'Get-Clipboard\s+-Raw' -or
    $wrapperSource -match 'MemoryAtlasHiddenInput|Console\.ReadLine\(\)|SetConsoleMode\(|Read-Host\s+-Prompt.*-AsSecureString') {
    throw 'Production input flow is not using the strict clipboard reader.'
}
Write-Output 'Production input flow uses the strict clipboard reader: PASS'

function Read-SyntheticHiddenInput {
    param([string]$SyntheticLine)
    # Synthetic cases pass their input directly to the production validator.
    return $SyntheticLine
}

function Invoke-SyntheticProductionInputFlow {
    param(
        [string]$Name,
        [string]$SyntheticLine,
        [AllowNull()][string]$ExpectedHandoff,
        [AllowNull()][string]$ExpectedRejection
    )

    $stage = 'hidden URI input'
    try {
        $secret = Read-SyntheticHiddenInput -SyntheticLine $SyntheticLine
        $stage = 'URI validation and normalization'
        $normalizedDatabaseUrl = $null
        $rejectionCategory = Get-NeonUriRejectionCategory -Value $secret -EncodedUriOutput ([ref]$normalizedDatabaseUrl)
        $expectsRejection = -not [string]::IsNullOrEmpty($ExpectedRejection)

        if (($expectsRejection -and $rejectionCategory -cne $ExpectedRejection) -or
            (-not $expectsRejection -and $null -ne $rejectionCategory)) {
            throw [System.FormatException]::new()
        }
        if ($expectsRejection) {
            if ($null -ne $normalizedDatabaseUrl) {
                throw [System.InvalidDataException]::new()
            }
            Write-Output ("PASS case={0} stage=URI validation" -f $Name)
            return
        }

        $secret = $normalizedDatabaseUrl
        $stage = 'DATABASE_URL handoff'
        $databaseUrl = $secret
        if ($databaseUrl -cne $ExpectedHandoff) {
            throw [System.UriFormatException]::new()
        }
        Write-Output ("PASS case={0} stage=DATABASE_URL handoff" -f $Name)
    }
    catch {
        $exceptionType = $_.Exception.GetType().Name
        Write-Output ("FAIL case={0} stage={1} exception_type={2}" -f $Name, $stage, $exceptionType)
        throw 'Synthetic URI input-flow test failed.'
    }
}

$cases = @(
    @{
        Name = 'reserved characters across username and password'
        Input = 'postgresql://demo/user:p@ss:/?#@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Handoff = 'postgresql://demo%2Fuser:p%40ss%3A%2F%3F%23@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Rejection = $null
    },
    @{
        Name = 'valid percent escapes preserved'
        Input = 'postgresql://demo%40user:p%2fss%3A@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Handoff = 'postgresql://demo%40user:p%2fss%3A@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Rejection = $null
    },
    @{
        Name = 'leading U+0016 control character rejected without trimming'
        Input = ([string][char]0x0016) + 'postgresql://demo:synthetic-pass@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Handoff = $null
        Rejection = 'input contains control characters'
    },
    @{
        Name = 'percent-encoded U+0016 credential byte preserved'
        Input = 'postgresql://demo:p%16ss@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Handoff = 'postgresql://demo:p%16ss@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Rejection = $null
    },
    @{
        Name = 'malformed percent escape rejected'
        Input = 'postgresql://demo%ZZuser:synthetic-pass@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Handoff = $null
        Rejection = 'malformed credential percent escape'
    },
    @{
        Name = 'BOM and surrounding whitespace normalized'
        Input = " `t$([char]0xFEFF)postgresql://demo:synthetic-pass@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require `t"
        Handoff = 'postgresql://demo:synthetic-pass@ep-example.us-east-2.aws.neon.tech/neondb?sslmode=require'
        Rejection = $null
    }
)

foreach ($case in $cases) {
    Invoke-SyntheticProductionInputFlow -Name $case['Name'] -SyntheticLine $case['Input'] `
        -ExpectedHandoff $case['Handoff'] -ExpectedRejection $case['Rejection']
}

Write-Output 'Synthetic production input-flow audit: PASS'
