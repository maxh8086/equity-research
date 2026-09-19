# Recalculate a built template in Excel and list every error cell.
#   powershell -ExecutionPolicy Bypass -File tools/screener_template/verify.ps1 -Path <merged.xlsx>
# Exits 1 if Excel cannot open the file cleanly (without repair), or if any
# formula outside "Data Sheet" shows an Excel error (#REF!, #NAME?, #DIV/0!, ...).
# Prints spot values for the sample company so a rebuild can be compared with
# the previous build.
param([Parameter(Mandatory = $true)][string]$Path)

$ErrorActionPreference = "Stop"
$full = (Resolve-Path $Path).Path
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
$errors = @()
$status = 0
try {
    try {
        $wb = $excel.Workbooks.Open($full, 0, $true)
    }
    catch {
        "Excel could not open the file without repair: $_"
        $status = 1
        return
    }
    $excel.CalculateFull()
    foreach ($ws in $wb.Worksheets) {
        if ($ws.Name -eq "Data Sheet") { continue }
        foreach ($cell in $ws.UsedRange.Cells) {
            if ($cell.HasFormula -and ($cell.Text -like "#*")) {
                $errors += "{0}!{1}: {2}  {3}" -f $ws.Name, $cell.Address($false, $false), $cell.Text, $cell.Formula
            }
        }
    }
    # sheet, row label (column A), columns
    $spots = @(
        @("Summary", "Company", "C"),
        @("Summary", "P/E (TTM; latest year if TTM incomplete)", "C"),
        @("Summary", "Value created per ₹ retained (B ÷ A)", "C"),
        @("Income Statement", "Operating profit", "K,L,N"),
        @("Income Statement", "Operating profit margin", "K,L,N"),
        @("Income Statement", "Exceptional and other items", "K"),
        @("Ratios", "Return on average equity", "K"),
        @("Ratios", "Return on average capital employed", "K"),
        @("Ratios", "Self-sustainable growth rate (SSGR), 3Y", "K"),
        @("Cash Flow", "Free cash flow (CFO − capex)", "K,N"),
        @("DuPont", "Check: 3-stage = 5-stage = Ratios ROE", "C,K"),
        @("Balance Sheet", "Check: liabilities − assets", "K")
    )
    foreach ($s in $spots) {
        $ws = $wb.Worksheets.Item($s[0])
        $hit = $ws.Range("A:A").Find($s[1], [Type]::Missing, -4163, 1)  # xlValues, xlWhole
        if ($null -eq $hit) { "{0}: row '{1}' not found" -f $s[0], $s[1]; $status = 1; continue }
        $vals = ($s[2] -split ",") | ForEach-Object { "{0}={1}" -f $_, $ws.Range("$_$($hit.Row)").Text }
        "{0} | {1} | {2}" -f $s[0], $s[1], ($vals -join "  ")
    }
    $wb.Close($false)
}
finally {
    $excel.Quit()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
}
if ($errors.Count -gt 0) {
    "ERROR CELLS: $($errors.Count)"
    $errors | Select-Object -First 50
    $status = 1
}
elseif ($status -eq 0) {
    "No error cells."
}
exit $status
