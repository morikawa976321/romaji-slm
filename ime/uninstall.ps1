# 常駐入力を外す（docs/IME.md）。トレイアプリを止め、ショートカットと %LOCALAPPDATA%\romaji-slm\ を消す。
#
#   pwsh -NoProfile -File ime/uninstall.ps1 [-KeepData]    # -KeepData: ime.ini・server.log・usage.txt は残す

param([switch]$KeepData)

$ErrorActionPreference = 'Stop'
$dst = Join-Path $env:LOCALAPPDATA 'romaji-slm'

# 更新（install.ps1 -Stage / -Swap）が動いていれば終わるのを待つ（並行して消すと、更新が消したものを作り直す）
$lock = $null
$lockPath = Join-Path $dst 'update.lock'
if (Test-Path $dst) {
    $deadline = (Get-Date).AddMinutes(10)
    while (-not $lock) {
        try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
        catch {
            if ((Get-Date) -gt $deadline) { Write-Host '更新が終わらないので外さなかった。少し待ってからもう一度動かす'; exit 1 }
            Write-Host '更新が動いているので、終わるのを待つ'
            Start-Sleep -Seconds 5
        }
    }
}

try {
    Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dst*" } | ForEach-Object {
        $_ | Stop-Process -Force
        $_.WaitForExit(5000) | Out-Null
    }
    foreach ($dir in [Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Programs')) {
        $lnk = Join-Path $dir 'romaji-slm 常駐入力.lnk'
        if (Test-Path $lnk) { Remove-Item $lnk }
    }
    # 次の更新が始まらないよう、写し元の印を先に消す
    if (Test-Path $dst) { Remove-Item (Join-Path $dst 'source.txt') -ErrorAction SilentlyContinue }
}
finally { if ($lock) { $lock.Dispose() } }

if (Test-Path $dst) {
    if ($KeepData) {
        $gone = 'app', 'app.next', 'app.prev', 'RomajiSlmIme.exe', 'RomajiSlmIme.exe.prev', 'update.lock', 'update.log',
                'update-result.txt', 'swap.ready', 'swap.decision'
        Remove-Item -Recurse -Force ($gone | ForEach-Object { Join-Path $dst $_ }) -ErrorAction SilentlyContinue
    } else {
        Remove-Item -Recurse -Force $dst
    }
}
Write-Host '外した'
