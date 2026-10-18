# 別の PC（ThinkPad）を「追従モード」にする初回の準備（docs/IME.md「別の PC に入れる（Git で追従）」）。1 回だけ動かす。
#
#   （リポジトリを clone したフォルダで）
#   powershell -NoProfile -ExecutionPolicy Bypass -File ime\setup-follower.ps1 -Models <配布 zip か、それを展開したフォルダ>
#
# すること:
#   1. git・PowerShell 7（pwsh）・uv があるか確かめる。pwsh と uv は無ければ入れる（pwsh は winget。管理者の確認が出ることがある）
#   2. 配布 zip（tools/pack-ime.ps1）から、Git に入れていないモデル（models\mozc・models\reading・models\hf）を写す
#   3. ime.ini に pull=<ブランチ> を書く（常駐アプリが 30 分ごとに GitHub から取ってきて、新しい版を自動で入れる）
#   4. ime\install.ps1 で組み立て・自己試験・起動まで行う（GPU が無いので torch は CPU 版になる）
#
# Windows PowerShell 5.1 で動くように書き、UTF-8 BOM 付きで保存する（5.1 は BOM 無しの UTF-8 を ANSI として読む）。
# GitHub へのログインは本人が済ませておく（無人の取得はその資格情報を使う）。

param([Parameter(Mandatory = $true)][string]$Models, [string]$Branch = 'main',
      [switch]$NoShortcuts, [switch]$NoLaunch)   # 試験用（別の場所に試しに入れるとき）。install.ps1 にそのまま渡す

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$data = if ($env:ROMAJI_SLM_DATA) { $env:ROMAJI_SLM_DATA } else { Join-Path $env:LOCALAPPDATA 'romaji-slm' }
Set-Location $repo
Write-Host "romaji-slm を追従モードで入れます（リポジトリ: $repo、ブランチ: $Branch）"

Write-Host '[1/4] 道具を確かめます'
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Write-Host 'git がありません。Git for Windows を入れてから動かしてください'; exit 1 }
if ((git rev-parse --abbrev-ref HEAD).Trim() -ne $Branch) { Write-Host "リポジトリが $Branch ブランチではありません（git switch $Branch）"; exit 1 }
$env:PATH = "$env:USERPROFILE\.local\bin;$env:ProgramFiles\PowerShell\7;$env:PATH"
if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) {
    Write-Host '      PowerShell 7 が無いので winget で入れます（管理者の確認が出たら許可してください）'
    winget install --id Microsoft.PowerShell --exact --source winget --accept-package-agreements --accept-source-agreements
    $env:PATH = "$env:ProgramFiles\PowerShell\7;$env:PATH"
    if (-not (Get-Command pwsh -ErrorAction SilentlyContinue)) { Write-Host 'PowerShell 7 を入れられませんでした'; exit 1 }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '      uv が無いので、公式のインストーラで入れます（https://astral.sh/uv/install.ps1）'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host 'uv を入れられませんでした'; exit 1 }
}
$env:UV_PYTHON_DOWNLOADS = 'automatic'

Write-Host '[2/4] モデルを写します（Git には入れていない大きいもの）'
$src = $Models
$tmp = $null
if ($Models -like '*.zip') {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('romaji-slm-models-' + [guid]::NewGuid().ToString('N'))
    Expand-Archive -Force $Models $tmp
    $src = $tmp
}
try {
    $from = Join-Path $src 'app\models'
    if (-not (Test-Path (Join-Path $from 'mozc\compiled\STAMP'))) { Write-Host "配布 zip の中にモデルが見つかりません: $from"; exit 1 }
    foreach ($d in 'mozc', 'reading', 'hf') {
        robocopy (Join-Path $from $d) (Join-Path $repo "models\$d") /E /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { Write-Host "写せませんでした（$d、robocopy $LASTEXITCODE）"; exit 1 }
    }
} finally { if ($tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue } }

Write-Host "[3/4] 追従モードにします（ime.ini に pull=$Branch）"
New-Item -ItemType Directory -Force $data | Out-Null
$ini = Join-Path $data 'ime.ini'
$lines = @()
if (Test-Path $ini) { $lines = @(Get-Content -Encoding UTF8 $ini | Where-Object { $_ -notmatch '^\s*pull\s*=' }) }
$lines += "pull=$Branch"
[IO.File]::WriteAllText($ini, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))

Write-Host '[4/4] 組み立て・自己試験・起動をします（初回は Python と部品の用意に数分かかります）'
$extra = @()
if ($NoShortcuts) { $extra += '-NoShortcuts' }
if ($NoLaunch) { $extra += '-NoLaunch' }
& pwsh -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo 'ime\install.ps1') @extra
if ($LASTEXITCODE -ne 0) { Write-Host '入れられませんでした（上の表示を見てください）'; exit 1 }
Write-Host ''
Write-Host '入れました。タスクバー右下（^ の中のことがある）に romaji-slm のアイコンが出ます。'
Write-Host 'Ctrl+Alt+Space（またはアイコンを左クリック）で使い始めます。'
Write-Host "以後は 30 分ごとに GitHub の $Branch を見て、新しい版があれば打っていない間に自動で入れ替えます。"
