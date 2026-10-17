# 常駐入力を別の PC（ThinkPad）に入れる配布 zip を作る（docs/IME.md「別の PC に入れる」）。
#
#   pwsh -NoProfile -File tools/pack-ime.ps1 [-HfHome <HF_HOME>] [-OutDir dist]
#
# 出力: dist/romaji-slm-ime-<yyyymmdd>-<短いハッシュ>.zip
#   install.cmd           ダブルクリックで入れる（app\ime\install-zip.ps1 を Windows PowerShell で呼ぶ）
#   RomajiSlmIme.exe      トレイアプリ（ime/build.ps1 でビルド済み）
#   app\                  HEAD のコード・辞書と読みの LM・zenz-v3.1-small のキャッシュ（models\hf）・CPU 版 torch の uv.lock
# - torch は CPU 版（ThinkPad に NVIDIA の GPU は無い）。pack-trial.ps1 と同じく pyproject の index を whl/cpu に差し替えて
#   uv lock を取り直す。取り直せなければ止める（GPU 版の lock を入れると ThinkPad で数 GB を取りに行く）。
# - 変換サーバは app\models\hf があればそこを HF_HOME にする（ime/server.py）ので、ThinkPad では取得が要らない。
# - コードは HEAD（未コミットの変更は入らない）。

param(
    [string]$HfHome = $(if ($env:HF_HOME) { $env:HF_HOME } else { Join-Path $HOME '.cache/huggingface' }),
    [string]$OutDir = 'dist'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$hash = (git rev-parse --short HEAD).Trim()
if (git status --porcelain -- core eval ime tools/review_history.py pyproject.toml uv.lock) {
    Write-Warning 'core/ eval/ ime/ などに未コミットの変更がある。入れるのは HEAD（未コミットの変更は入らない）'
}
$day = Get-Date -Format 'yyyyMMdd'
$name = "romaji-slm-ime-$day-$hash"
$stage = Join-Path ([IO.Path]::GetTempPath()) $name
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
$app = Join-Path $stage 'app'
New-Item -ItemType Directory -Force $app | Out-Null

Write-Host 'ビルド: ime/bin/RomajiSlmIme.exe'
& pwsh -NoProfile -File (Join-Path $root 'ime/build.ps1')
if ($LASTEXITCODE) { throw 'ビルドに失敗した' }
Copy-Item (Join-Path $root 'ime/bin/RomajiSlmIme.exe') (Join-Path $stage 'RomajiSlmIme.exe')

Write-Host "コード: HEAD ($hash)"
$zip0 = Join-Path ([IO.Path]::GetTempPath()) "$name-src.zip"
git archive --format=zip -o $zip0 HEAD core eval ime tools/review_history.py pyproject.toml uv.lock
Expand-Archive -Force $zip0 $app
Remove-Item $zip0
if (Test-Path (Join-Path $app 'eval/out')) { Remove-Item -Recurse -Force (Join-Path $app 'eval/out') }
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $app 'VERSION') "$hash $((Get-Date).ToString('yyyy-MM-dd'))"
# install-zip.ps1 は Windows PowerShell 5.1 で読まれるので UTF-8 BOM を付け直す（git archive は BOM を付けない）
$iz = Join-Path $app 'ime/install-zip.ps1'
[IO.File]::WriteAllText($iz, [IO.File]::ReadAllText($iz), (New-Object System.Text.UTF8Encoding($true)))
# cmd.exe は UTF-8 の日本語を読めないので、install.cmd は ASCII だけの起動役にする（trial/run.cmd と同じ）
Set-Content -Encoding ascii (Join-Path $stage 'install.cmd') @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\ime\install-zip.ps1" %*
pause
'@

Write-Host '辞書と読みの LM: models/mozc, models/reading'
$need = @('models/mozc/compiled/STAMP', 'models/mozc/id.def', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa')
$missing = @($need | Where-Object { -not (Test-Path $_) })
if ($missing.Count) { throw "無い: $($missing -join ', ')（docs/TRIAL.md「配布の作り方」の手順で作る）" }
robocopy models\mozc\compiled (Join-Path $app 'models\mozc\compiled') /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy に失敗した ($LASTEXITCODE)" }
foreach ($f in 'models/mozc/id.def', 'models/mozc/VERSION', 'models/mozc/README.txt', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa') {
    if (Test-Path $f) {
        New-Item -ItemType Directory -Force (Split-Path -Parent (Join-Path $app $f)) | Out-Null
        Copy-Item $f (Join-Path $app $f)
    }
}

Write-Host "zenz-v3.1-small のキャッシュ: $HfHome"
$m = 'models--Miwa-Keita--zenz-v3.1-small'
$src = Join-Path $HfHome "hub/$m"
if (-not (Test-Path "$src/snapshots")) { throw "$src が無い" }
$dst = Join-Path $app "models/hf/hub/$m"
robocopy "$src/refs" "$dst/refs" /E /NFL /NDL /NJH /NJS /NP | Out-Null
$snap = (Resolve-Path "$src/snapshots").Path
Get-ChildItem -LiteralPath $snap -Recurse -File -Force | ForEach-Object {
    $rel = [IO.Path]::GetRelativePath($snap, $_.FullName)
    $to = Join-Path "$dst/snapshots" $rel
    New-Item -ItemType Directory -Force (Split-Path -Parent $to) | Out-Null
    $real = if ($_.LinkTarget) { [IO.Path]::GetFullPath([IO.Path]::Combine($_.DirectoryName, $_.LinkTarget)) } else { $_.FullName }
    Copy-Item -LiteralPath $real $to
}

Write-Host 'pyproject.toml: torch を CPU 版の index に差し替えて uv lock'
$py = Get-Content -Raw -Encoding utf8 (Join-Path $app 'pyproject.toml')
$py2 = $py -replace 'https://download\.pytorch\.org/whl/[A-Za-z0-9_.]+', 'https://download.pytorch.org/whl/cpu'
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $app 'pyproject.toml') $py2
Push-Location $app
try {
    uv lock 2>&1 | ForEach-Object { Write-Host "  uv lock: $_" }
    if ($LASTEXITCODE -ne 0) { throw 'uv lock に失敗した（download.pytorch.org に届かない等）' }
} finally { Pop-Location }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path (Resolve-Path $OutDir).Path "$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item -Recurse -Force $stage
Write-Host "作成: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
