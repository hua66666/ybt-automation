$ErrorActionPreference = "Stop"
$ToolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $ToolDir "launch_ybt.ps1"
if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "launch_ybt.ps1 was not found next to this installer."
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "一本通单题工具.lnk"
$PowerShell = Join-Path $PSHOME "powershell.exe"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PowerShell
$Shortcut.Arguments = "-NoLogo -NoExit -ExecutionPolicy Bypass -File `"$Launcher`""
$Shortcut.WorkingDirectory = $ToolDir
$Shortcut.Description = "YBT single-problem generator, validator and submit tool"
$Shortcut.IconLocation = "$PowerShell,0"
$Shortcut.Save()

Write-Host ""
Write-Host "Desktop shortcut created:" -ForegroundColor Green
Write-Host "  $ShortcutPath"
