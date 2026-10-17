# 配布 zip（tools/pack-ime.ps1 が作る）から常駐入力を入れる。zip のルートの install.cmd から呼ばれる（docs/IME.md「別の PC に入れる」）。
#
#   install.cmd                       # ダブルクリック（%LOCALAPPDATA%\romaji-slm に入れ、ログオン時に起動、すぐ起動）
#   powershell -ExecutionPolicy Bypass -File app\ime\install-zip.ps1 [-Target <フォルダ>] [-NoStartup] [-NoLaunch]
#
# Windows PowerShell 5.1 で動くように書く（ThinkPad に pwsh があるとは限らない）。UTF-8 BOM 付きで保存する
# （5.1 は BOM 無しの UTF-8 を ANSI として読む。trial/run.ps1 と同じ罠）。
# 設定（ime.ini）・記録（history.jsonl）・ユーザー辞書（user_dict.tsv）は入れ替えない（その PC のものを残す）。

param([string]$Target = (Join-Path $env:LOCALAPPDATA 'romaji-slm'), [switch]$NoStartup, [switch]$NoLaunch,
      [switch]$NoShortcuts)   # NoShortcuts: ショートカットを一切作らない（別のフォルダに試しに入れるとき。今のショートカットを上書きしない）

$ErrorActionPreference = 'Stop'
$Src = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # zip のルート（このファイルは <ルート>\app\ime\ にある）
if (-not (Test-Path (Join-Path $Src 'app\ime\server.py')) -or -not (Test-Path (Join-Path $Src 'RomajiSlmIme.exe'))) {
    Write-Host "配布物の中身が足りません: $Src"
    exit 1
}
$App = Join-Path $Target 'app'
Write-Host "romaji-slm 常駐入力を入れます: $Target"

Write-Host '[1/5] 動いている常駐を止めます'
Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$Target*" } | ForEach-Object {
    $_ | Stop-Process -Force
    $_.WaitForExit(5000) | Out-Null
}

Write-Host '[2/5] uv（Python の実行環境を用意する道具）を確かめます'
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '      uv が無いので、公式のインストーラで入れます（https://astral.sh/uv/install.ps1）'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host 'uv を入れられませんでした'; exit 1 }
}

Write-Host '[3/5] プログラムとモデルを写します'
New-Item -ItemType Directory -Force $App | Out-Null
foreach ($d in 'core', 'eval', 'ime', 'tools') { if (Test-Path "$App\$d") { Remove-Item -Recurse -Force "$App\$d" } }
robocopy (Join-Path $Src 'app') $App /E /XD .venv /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { Write-Host "写せませんでした（robocopy $LASTEXITCODE）"; exit 1 }
Copy-Item (Join-Path $Src 'RomajiSlmIme.exe') (Join-Path $Target 'RomajiSlmIme.exe') -Force

Write-Host '[4/5] Python と部品を用意します（初回は数分かかります）'
$env:UV_PYTHON_DOWNLOADS = 'automatic'   # uv の設定で Python の自動取得が manual でも取る（trial/run.ps1 と同じ）
Push-Location $App
try {
    & uv sync --frozen --no-default-groups --group core
    if ($LASTEXITCODE -ne 0) { Write-Host 'Python と部品を用意できませんでした'; exit 1 }
} finally { Pop-Location }

Write-Host '[5/5] ショートカットを作ります'
if ($NoShortcuts) { Write-Host '      作りません（-NoShortcuts）'; exit 0 }
$exe = Join-Path $Target 'RomajiSlmIme.exe'
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $exe
    $s.WorkingDirectory = $Target
    $s.Description = 'romaji-slm 常駐入力'
    $s.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'romaji-slm 常駐入力.lnk')
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'romaji-slm 常駐入力.lnk'
if ($NoStartup) { if (Test-Path $startup) { Remove-Item $startup } } else { New-Shortcut $startup }

if (-not $NoLaunch) { Start-Process $exe -WorkingDirectory $Target }
Write-Host ''
Write-Host '入れました。タスクバー右下（^ の中のことがある）に romaji-slm のアイコンが出ます。'
Write-Host 'Ctrl+Alt+Space（またはアイコンを左クリック）で使い始めます。変換サーバの準備に 10〜60 秒かかります。'
