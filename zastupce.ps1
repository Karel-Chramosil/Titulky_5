$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "Titulky_5.lnk"

if (Test-Path -LiteralPath $linkPath) {
    exit 0
}

try {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = Join-Path $root "spustit.bat"
    $link.WorkingDirectory = $root
    $link.IconLocation = "$env:SystemRoot\System32\SHELL32.dll,43"
    $link.WindowStyle = 1
    $link.Save()
    Write-Output "Zastupce na plose: Titulky_5"
} catch {
    Write-Output "Zastupce na plose se nepodarilo vytvorit."
    exit 0
}
