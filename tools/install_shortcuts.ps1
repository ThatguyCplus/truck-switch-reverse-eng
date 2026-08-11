# Creates Desktop shortcuts for the panel tools.
#   powershell -ExecutionPolicy Bypass -File install_shortcuts.ps1
#
# The shortcuts point straight at pythonw.exe so no console window flashes.
# Use the .bat launchers instead if you want the pyserial dependency check.

$here    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$desktop = [Environment]::GetFolderPath('Desktop')

# resolve pythonw next to whichever python is on PATH
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Write-Error "python not found on PATH"; exit 1 }
$pyw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }

$shell = New-Object -ComObject WScript.Shell

$items = @(
    @{ Name = 'Scania Panel Monitor';     Script = 'panel_gui.py'
       Icon = 'icons\panel_monitor.ico'
       Desc = 'Live button monitor for the Scania 2545507 switch panel' },
    @{ Name = 'Scania Panel Diagnostics'; Script = 'panel_diag.py'
       Icon = 'icons\panel_diag.ico'
       Desc = 'Resistance-domain ladder fault finder' }
)

foreach ($it in $items) {
    $lnkPath = Join-Path $desktop ($it.Name + '.lnk')
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath       = $pyw
    $lnk.Arguments        = '"' + (Join-Path $here $it.Script) + '"'
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation     = (Join-Path $here $it.Icon)
    $lnk.Description      = $it.Desc
    $lnk.Save()
    Write-Host ("created  " + $lnkPath)
}

Write-Host ""
Write-Host ("python : " + $pyw)
Write-Host ("folder : " + $here)
