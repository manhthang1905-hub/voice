# make_shortcut.ps1 - Tao shortcut chay tool voi duong dan DUNG cua may hien tai.
# setup.bat goi file nay. Tao: START_GUI.lnk (trong thu muc) + shortcut ngoai Desktop.
$ErrorActionPreference = 'SilentlyContinue'

$app = Split-Path -Parent $MyInvocation.MyCommand.Path
$run = Join-Path $app 'run.py'

# Tim pythonw.exe (chay khong hien CMD)
$pyw = (Get-Command pythonw.exe).Source
if (-not $pyw) {
    $py = (Get-Command python.exe).Source
    if ($py) { $pyw = Join-Path (Split-Path $py) 'pythonw.exe' }
}
if (-not $pyw) { $pyw = 'pythonw.exe' }

$ws = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path $app 'START_GUI.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop')) '11Lab Voice Tool.lnk')
)
foreach ($t in $targets) {
    $l = $ws.CreateShortcut($t)
    $l.TargetPath = $pyw
    $l.Arguments = '"' + $run + '"'
    $l.WorkingDirectory = $app
    $l.IconLocation = $pyw
    $l.Save()
}
Write-Host "[OK] Da tao shortcut: START_GUI.lnk (trong thu muc) + '11Lab Voice Tool' ngoai Desktop"
Write-Host "     Python: $pyw"
