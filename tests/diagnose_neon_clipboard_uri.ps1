$ErrorActionPreference = 'Stop'

$clipboardText = $null
$normalizedUri = $null
$validatorAst = $null
$scriptAst = $null
$tokens = $null
$parseErrors = $null

try {
    $clipboardText = Get-Clipboard -Raw -ErrorAction Stop
    if ($null -eq $clipboardText -or [string]::IsNullOrEmpty([string]$clipboardText)) {
        Write-Output 'status=rejected; category=clipboard is empty'
        return
    }
    $clipboardText = [string]$clipboardText

    # Reject line breaks before the validator's Trim() can remove them.
    if ($clipboardText.IndexOf("`r", [System.StringComparison]::Ordinal) -ge 0 -or
        $clipboardText.IndexOf("`n", [System.StringComparison]::Ordinal) -ge 0) {
        Write-Output 'status=rejected; category=clipboard contains multiple lines'
        return
    }
    # Preserve strict validation: no raw ASCII control character is normalized away.
    if ([regex]::IsMatch($clipboardText, '[\x00-\x1F\x7F]')) {
        Write-Output 'status=rejected; category=input contains control characters'
        return
    }

    $wrapperPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\run_safe_migration.ps1'
    $scriptAst = [System.Management.Automation.Language.Parser]::ParseFile(
        $wrapperPath,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        Write-Output 'status=error; category=validator unavailable'
        return
    }
    $validatorAst = $scriptAst.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Get-NeonUriRejectionCategory'
    }, $true)
    if ($null -eq $validatorAst) {
        Write-Output 'status=error; category=validator unavailable'
        return
    }
    Invoke-Expression $validatorAst.Extent.Text

    $category = Get-NeonUriRejectionCategory -Value $clipboardText -EncodedUriOutput ([ref]$normalizedUri)
    if ($null -eq $category) {
        Write-Output 'status=accepted; category=none'
    }
    else {
        Write-Output ('status=rejected; category={0}' -f $category)
    }
}
catch {
    # Never emit exception text; it can contain sensitive clipboard contents.
    Write-Output 'status=error; category=clipboard read or validation unavailable'
}
finally {
    $clipboardText = $null
    $normalizedUri = $null
    $validatorAst = $null
    $scriptAst = $null
    $tokens = $null
    $parseErrors = $null
    [GC]::Collect()
}
