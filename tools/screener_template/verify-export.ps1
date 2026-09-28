# Check a workbook Screener actually returned, not the template we sent it.
#   powershell -ExecutionPolicy Bypass -File tools/screener_template/verify-export.ps1 -Path <export.xlsx>
#
# verify.ps1 recalculates the template itself, which cannot see the defect that
# made exports open blank: Screener writes its value into a Data Sheet cell as a
# bare <v> without clearing a stale <is> and without setting t=, so the cell
# claims to be numeric while holding text. Excel discards the whole sheet.
# This script reads the returned file's XML directly, so it needs no Excel.
#
# Exits 1 if the Data Sheet has any such cell, if the sheet is missing, or if
# the company name is still the template's sample company (Screener wrote
# nothing).
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [string]$SampleCompany = "VINATI ORGANICS LTD"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression.FileSystem

$full = (Resolve-Path $Path).Path
$zip = [System.IO.Compression.ZipFile]::OpenRead($full)
try {
    function Read-Part([string]$name) {
        $entry = $zip.GetEntry($name)
        if ($null -eq $entry) { return $null }
        $reader = New-Object System.IO.StreamReader($entry.Open())
        try { $reader.ReadToEnd() } finally { $reader.Dispose() }
    }

    # Find the Data Sheet part through workbook.xml -> its relationship.
    $bookXml = [xml](Read-Part "xl/workbook.xml")
    $sheet = $bookXml.workbook.sheets.sheet | Where-Object { $_.name -eq "Data Sheet" }
    if ($null -eq $sheet) {
        "FAIL: the export has no 'Data Sheet'. Sheets: $($bookXml.workbook.sheets.sheet.name -join ', ')"
        exit 1
    }
    $rid = $sheet.id
    $relsXml = [xml](Read-Part "xl/_rels/workbook.xml.rels")
    $target = ($relsXml.Relationships.Relationship | Where-Object { $_.Id -eq $rid }).Target
    $part = if ($target.StartsWith("/")) { $target.TrimStart("/") } else { "xl/$target" }

    $sheetXml = Read-Part $part
    if ($null -eq $sheetXml) { "FAIL: missing part $part"; exit 1 }

    # A cell carrying both an inline string and a value is the corruption.
    $broken = [regex]::Matches($sheetXml, '<c\b[^>]*>(?:(?!</c>).)*?</c>', "Singleline") |
        Where-Object { $_.Value -match '<is>' -and $_.Value -match '<v>' }

    if ($broken.Count -gt 0) {
        "FAIL: $($broken.Count) Data Sheet cell(s) hold both an inline string and a value."
        "Excel discards the sheet, which is why the export opens blank."
        $broken | Select-Object -First 5 | ForEach-Object { "  $($_.Value)" }
        exit 1
    }

    # Screener must actually have written this company's data in.
    $name = ""
    $b1 = [regex]::Match($sheetXml, '<c r="B1"[ >](?:(?!</c>).)*?</c>', "Singleline")
    if ($b1.Success) {
        if ($b1.Value -match '<is><t[^>]*>([^<]*)</t>') { $name = $Matches[1] }
        elseif ($b1.Value -match '<v>([^<]*)</v>') { $name = $Matches[1] }
    }
    if ($name -eq "") {
        "FAIL: could not read the company name from Data Sheet!B1."
        exit 1
    }
    if ($name -eq $SampleCompany) {
        "FAIL: B1 is still '$SampleCompany' - Screener returned the template unchanged."
        exit 1
    }

    "OK: Data Sheet is intact ($($broken.Count) malformed cells), company = '$name'."
    exit 0
}
finally {
    $zip.Dispose()
}
