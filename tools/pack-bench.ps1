# ThinkPad で速度を測るための zip を作る（docs/THINKPAD.md「速さを測る」）。
#
#   pwsh -NoProfile -File tools/pack-bench.ps1 [-HfHome <HF_HOME>] [-OutDir dist]
#
# 出力: dist/romaji-slm-bench-<yyyymmdd>-<短いハッシュ>.zip
#   bench.cmd              ダブルクリックで測る（tools\thinkpad-bench.ps1 を Windows PowerShell で呼ぶ）
#   core\ eval\ ...        HEAD のコード（未コミットの変更は入らない）
#   models\                辞書と読みの LM、zenz-v3.1-small のキャッシュ（models\hf）
# - torch は CPU 版（ONNX の書き出しにだけ使う。変換は ONNX Runtime 版）。pack-ime.ps1 と同じく pyproject の index を
#   whl/cpu に差し替えて uv lock を取り直す。

param(
    [string]$HfHome = $(if ($env:HF_HOME) { $env:HF_HOME } else { Join-Path $HOME '.cache/huggingface' }),
    [string]$OutDir = 'dist'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$hash = (git rev-parse --short HEAD).Trim()
if (git status --porcelain -- core eval tools/thinkpad-bench.ps1 pyproject.toml uv.lock) {
    Write-Warning 'core/ eval/ などに未コミットの変更がある。入れるのは HEAD（未コミットの変更は入らない）'
}
$name = "romaji-slm-bench-$(Get-Date -Format 'yyyyMMdd')-$hash"
$stage = Join-Path ([IO.Path]::GetTempPath()) $name
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force $stage | Out-Null

Write-Host "コード: HEAD ($hash)"
$zip0 = Join-Path ([IO.Path]::GetTempPath()) "$name-src.zip"
git archive --format=zip -o $zip0 HEAD core eval tools/thinkpad-bench.ps1 pyproject.toml uv.lock
Expand-Archive -Force $zip0 $stage
Remove-Item $zip0
if (Test-Path (Join-Path $stage 'eval/out')) { Remove-Item -Recurse -Force (Join-Path $stage 'eval/out') }
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $stage 'VERSION') "$hash $((Get-Date).ToString('yyyy-MM-dd'))"
$tb = Join-Path $stage 'tools/thinkpad-bench.ps1'
[IO.File]::WriteAllText($tb, [IO.File]::ReadAllText($tb), (New-Object System.Text.UTF8Encoding($true)))
# cmd.exe は UTF-8 の日本語を読めないので ASCII だけの起動役にする（trial/run.cmd と同じ）
Set-Content -Encoding ascii (Join-Path $stage 'bench.cmd') @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\thinkpad-bench.ps1" %*
pause
'@

Write-Host '辞書と読みの LM'
robocopy models\mozc\compiled (Join-Path $stage 'models\mozc\compiled') /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy に失敗した ($LASTEXITCODE)" }
foreach ($f in 'models/mozc/id.def', 'models/mozc/VERSION', 'models/mozc/README.txt', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa') {
    if (Test-Path $f) {
        New-Item -ItemType Directory -Force (Split-Path -Parent (Join-Path $stage $f)) | Out-Null
        Copy-Item $f (Join-Path $stage $f)
    }
}

Write-Host "zenz-v3.1-small のキャッシュ: $HfHome"
$m = 'models--Miwa-Keita--zenz-v3.1-small'
$src = Join-Path $HfHome "hub/$m"
if (-not (Test-Path "$src/snapshots")) { throw "$src が無い" }
$dst = Join-Path $stage "models/hf/hub/$m"
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
$py = Get-Content -Raw -Encoding utf8 (Join-Path $stage 'pyproject.toml')
$py2 = $py -replace 'https://download\.pytorch\.org/whl/[A-Za-z0-9_.]+', 'https://download.pytorch.org/whl/cpu'
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $stage 'pyproject.toml') $py2
Push-Location $stage
try {
    uv lock 2>&1 | ForEach-Object { Write-Host "  uv lock: $_" }
    if ($LASTEXITCODE -ne 0) { throw 'uv lock に失敗した' }
} finally { Pop-Location }

New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path (Resolve-Path $OutDir).Path "$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item -Recurse -Force $stage
Write-Host "作成: $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
