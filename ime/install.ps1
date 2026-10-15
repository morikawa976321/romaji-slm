# 常駐入力をこのマシンに入れる（docs/IME.md）。管理者権限は要らない。システムの設定は変えない。
#
#   pwsh -NoProfile -File ime/install.ps1 [-NoStartup] [-NoLaunch]
#
# 入れる先: %LOCALAPPDATA%\romaji-slm\
#   RomajiSlmIme.exe   トレイアプリ（ime/build.ps1 でビルド）
#   app\               HEAD のコード（git archive）・辞書と読みの LM の写し・専用の .venv
#   ime.ini / server.log / usage.txt   設定・サーバのログ・使用状況（打った文字列は残さない）
# 開発中の作業ツリーと分けるのは、他のセッションが core/ を書き換えている最中でも常用側が止まらないようにするため。
# 同じスクリプトをもう一度動かすと、その時点の HEAD に入れ替わる（.venv はそのまま使い、uv sync で差分だけ直す）。
# ショートカット: スタートアップ（ログオン時に起動。-NoStartup で作らない）とスタートメニュー。

param([switch]$NoStartup, [switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $env:LOCALAPPDATA 'romaji-slm'
$app = Join-Path $dst 'app'
Set-Location $repo

$hash = (git rev-parse --short HEAD).Trim()
if (git status --porcelain -- core eval ime pyproject.toml uv.lock) {
    Write-Warning 'core/ eval/ ime/ pyproject.toml uv.lock に未コミットの変更がある。入れるのは HEAD（未コミットの変更は入らない）'
}

Write-Host 'ビルド: ime/bin/RomajiSlmIme.exe'
& pwsh -NoProfile -File (Join-Path $PSScriptRoot 'build.ps1')
if ($LASTEXITCODE) { throw 'ビルドに失敗した' }

Write-Host '動いている常駐を止める'
Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dst*" } | ForEach-Object {
    $_ | Stop-Process -Force   # 変換サーバはジョブオブジェクトで一緒に終わる
    $_.WaitForExit(5000) | Out-Null
}

Write-Host "コード: HEAD ($hash) → $app"
New-Item -ItemType Directory -Force $app | Out-Null
foreach ($d in 'core', 'eval', 'ime') { if (Test-Path "$app\$d") { Remove-Item -Recurse -Force "$app\$d" } }
$zip = Join-Path ([IO.Path]::GetTempPath()) "romaji-slm-ime-$hash.zip"
git archive --format=zip -o $zip HEAD core eval ime pyproject.toml uv.lock
Expand-Archive -Force $zip $app
Remove-Item $zip
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $app 'VERSION') "$hash $((Get-Date).ToString('yyyy-MM-dd'))"

Write-Host '辞書と読みの LM: models/mozc, models/reading'
$need = @('models/mozc/compiled/STAMP', 'models/mozc/id.def', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa')
$missing = @($need | Where-Object { -not (Test-Path $_) })
if ($missing.Count) { throw "無い: $($missing -join ', ')（docs/TRIAL.md「配布の作り方」の手順で作る）" }
robocopy models\mozc\compiled "$app\models\mozc\compiled" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy に失敗した ($LASTEXITCODE)" }
foreach ($f in 'models/mozc/id.def', 'models/mozc/VERSION', 'models/mozc/README.txt', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa') {
    if (Test-Path $f) {
        New-Item -ItemType Directory -Force (Split-Path -Parent "$app/$f") | Out-Null
        Copy-Item $f "$app/$f"
    }
}

Write-Host 'Python の環境: uv sync（torch は uv のキャッシュから。GPU 版の uv.lock をそのまま使う）'
Push-Location $app
try {
    uv sync --frozen --no-dev 2>&1 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE) { throw 'uv sync に失敗した' }
} finally { Pop-Location }

Copy-Item (Join-Path $PSScriptRoot 'bin\RomajiSlmIme.exe') (Join-Path $dst 'RomajiSlmIme.exe') -Force
$exe = Join-Path $dst 'RomajiSlmIme.exe'

$shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $exe
    $s.WorkingDirectory = $dst
    $s.Description = 'romaji-slm 常駐入力'
    $s.Save()
}
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'romaji-slm 常駐入力.lnk')
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'romaji-slm 常駐入力.lnk'
if ($NoStartup) { if (Test-Path $startup) { Remove-Item $startup } }
else { New-Shortcut $startup }

if (-not $NoLaunch) { Start-Process $exe -WorkingDirectory $dst }
Write-Host "入れた: $dst（$hash）"
