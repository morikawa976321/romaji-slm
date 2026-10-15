# 常駐入力を外す（docs/IME.md）。トレイアプリを止め、ショートカットと %LOCALAPPDATA%\romaji-slm\ を消す。
#
#   pwsh -NoProfile -File ime/uninstall.ps1 [-KeepData]    # -KeepData: ime.ini・server.log・usage.txt は残す

param([switch]$KeepData)

$ErrorActionPreference = 'Stop'
$dst = Join-Path $env:LOCALAPPDATA 'romaji-slm'
Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dst*" } | ForEach-Object {
    $_ | Stop-Process -Force
    $_.WaitForExit(5000) | Out-Null
}
foreach ($dir in [Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Programs')) {
    $lnk = Join-Path $dir 'romaji-slm 常駐入力.lnk'
    if (Test-Path $lnk) { Remove-Item $lnk }
}
if (Test-Path $dst) {
    if ($KeepData) {
        Remove-Item -Recurse -Force (Join-Path $dst 'app'), (Join-Path $dst 'RomajiSlmIme.exe') -ErrorAction SilentlyContinue
    } else {
        Remove-Item -Recurse -Force $dst
    }
}
Write-Host '外した'
