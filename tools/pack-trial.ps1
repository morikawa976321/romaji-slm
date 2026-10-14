# 体験版（V0.2）の配布 zip を作る（docs/TRIAL.md「配布の作り方」）。
#
#   pwsh -NoProfile -File tools/pack-trial.ps1 [-HfHome <HF_HOME>] [-OutDir dist]
#
# 出力: dist/romaji-slm-trial-<yyyymmdd>-<短いハッシュ>.zip
# - torch は CPU 版にする。配布物の pyproject.toml だけ pytorch の index を whl/cpu に差し替え、uv lock を取り直す。
#   uv lock が失敗したら（download.pytorch.org に届かない等）uv.lock を入れずに警告する。元の uv.lock は cu128 用で
#   ThinkPad では使えないので、古いまま入れることはしない。
# - HF のキャッシュは snapshots の実体だけを写す（blobs への symlink を辿る。zip は symlink を持てない）。
# - models/mozc は compiled/ のほかに id.def と VERSION も要る（core.kanakanji.Dictionary.load が読む）。
#   README.txt は IPAdic 等のライセンス文なので同梱する。

param(
    [string]$HfHome = $(if ($env:HF_HOME) { $env:HF_HOME } else { Join-Path $HOME '.cache/huggingface' }),
    [string]$OutDir = 'dist'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$hash = (git rev-parse --short HEAD).Trim()
if (git status --porcelain -- core eval trial pyproject.toml uv.lock) {
    Write-Warning 'core/ eval/ trial/ pyproject.toml uv.lock に未コミットの変更がある。VERSION に +dirty を付ける'
    $hash += '+dirty'
}
$day = Get-Date -Format 'yyyyMMdd'
$name = "romaji-slm-trial-$day-$($hash -replace '\+', '-')"
$stage = Join-Path ([IO.Path]::GetTempPath()) $name
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory $stage | Out-Null
$warnings = [Collections.Generic.List[string]]::new()
function Warn([string]$msg) { Write-Warning $msg; $warnings.Add($msg) }

# ディレクトリを写す。$Exclude は相対パスの先頭（区切りは /）、__pycache__ は常に除く
function Copy-Tree([string]$Src, [string]$Dst, [string[]]$Exclude = @()) {
    $base = (Resolve-Path $Src).Path
    Get-ChildItem -LiteralPath $base -Recurse -File -Force | ForEach-Object {
        $rel = [IO.Path]::GetRelativePath($base, $_.FullName) -replace '\\', '/'
        if ($rel -match '(^|/)__pycache__/') { return }
        foreach ($e in $Exclude) { if ($rel -eq $e -or $rel.StartsWith("$e/")) { return } }
        $to = Join-Path $Dst $rel
        New-Item -ItemType Directory -Force (Split-Path -Parent $to) | Out-Null
        Copy-Item -LiteralPath $_.FullName $to
    }
}

# symlink なら実体のパス（HF キャッシュの snapshots/ は blobs/ への相対 symlink）
function Resolve-Real([IO.FileInfo]$f) {
    $t = $f.LinkTarget
    if (-not $t) { return $f.FullName }
    return [IO.Path]::GetFullPath([IO.Path]::Combine($f.DirectoryName, $t))
}

# ページに要るファイル。欠けた zip を作らないよう、無ければ止める
$required = @('trial/server.py', 'trial/engine.py', 'trial/summary.py', 'trial/machine.py', 'trial/__init__.py',
              'trial/configs.json', 'trial/examples.json', 'trial/static/index.html', 'trial/static/app.js',
              'trial/static/app.css', 'trial/run.cmd', 'trial/run.ps1', 'trial/README-trial.md')
$missing = @($required | Where-Object { -not (Test-Path $_) })
if ($missing.Count) { throw "trial/ のファイルが無い: $($missing -join ', ')" }

Write-Host "コード: core/ eval/ trial/"
Copy-Tree core (Join-Path $stage core)
Copy-Tree eval (Join-Path $stage eval) -Exclude @('out')
Copy-Tree trial (Join-Path $stage trial) -Exclude @('out', 'VERSION')
Copy-Item trial/run.cmd (Join-Path $stage run.cmd)
Copy-Item trial/README-trial.md (Join-Path $stage README-trial.md)
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $stage trial/VERSION) "$hash $((Get-Date).ToString('yyyy-MM-dd'))"

Write-Host "辞書と読みの LM: models/mozc, models/reading/kana_lm.pkl"
$mozc = 'models/mozc'
if (Test-Path "$mozc/compiled/STAMP") {
    Copy-Tree "$mozc/compiled" (Join-Path $stage "$mozc/compiled")
    foreach ($f in 'id.def', 'VERSION', 'README.txt') {
        if (Test-Path "$mozc/$f") { Copy-Item "$mozc/$f" (Join-Path $stage "$mozc/$f") }
        else { Warn "$mozc/$f が無い（uv run python -m core.fetch_mozc で取る）" }
    }
} else {
    Warn "$mozc/compiled が無い。uv run python -m core.fetch_mozc のあと core.kanakanji を1回動かして索引を作る"
}
$lm = 'models/reading/kana_lm.pkl'
if (Test-Path $lm) {
    New-Item -ItemType Directory -Force (Join-Path $stage 'models/reading') | Out-Null
    Copy-Item $lm (Join-Path $stage $lm)
} else {
    Warn "$lm が無い（uv run python -m core.reading で一度動かすと作られる）"
}

Write-Host "zenz のキャッシュ: $HfHome"
foreach ($m in 'models--Miwa-Keita--zenz-v3.1-small', 'models--Miwa-Keita--zenz-v3.1-xsmall') {
    $src = Join-Path $HfHome "hub/$m"
    if (-not (Test-Path "$src/snapshots")) { Warn "$src が無い。ThinkPad 側で初回に取得する（HF_HUB_OFFLINE を外して起動）"; continue }
    $dst = Join-Path $stage "models/hf/hub/$m"
    Copy-Tree "$src/refs" "$dst/refs"
    $snap = (Resolve-Path "$src/snapshots").Path
    Get-ChildItem -LiteralPath $snap -Recurse -File -Force | ForEach-Object {
        $rel = [IO.Path]::GetRelativePath($snap, $_.FullName)
        $to = Join-Path "$dst/snapshots" $rel
        New-Item -ItemType Directory -Force (Split-Path -Parent $to) | Out-Null
        Copy-Item -LiteralPath (Resolve-Real $_) $to
    }
}

Write-Host "pyproject.toml: torch を CPU 版の index に差し替え"
$py = Get-Content -Raw -Encoding utf8 pyproject.toml
$py2 = $py -replace 'https://download\.pytorch\.org/whl/[A-Za-z0-9_.]+', 'https://download.pytorch.org/whl/cpu'
if ($py2 -eq $py -and $py -notmatch 'whl/cpu') { Warn 'pyproject.toml に pytorch の index が見つからない。差し替えていない' }
Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $stage pyproject.toml) $py2
Copy-Item uv.lock (Join-Path $stage uv.lock)

Push-Location $stage
try {
    $lockOk = $false
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv lock 2>&1 | ForEach-Object { Write-Host "  uv lock: $_" }
        $lockOk = ($LASTEXITCODE -eq 0)
    }
    if (-not $lockOk) {
        Remove-Item -Force uv.lock
        Warn 'uv lock に失敗した（download.pytorch.org に届かない等）。uv.lock を入れていない。ThinkPad の初回 uv sync が解決する'
    }
} finally {
    Pop-Location
}

New-Item -ItemType Directory -Force $OutDir | Out-Null
$zip = Join-Path (Resolve-Path $OutDir).Path "$name.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $false)
Remove-Item -Recurse -Force $stage

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "作成: $zip ($mb MB)"
if ($warnings.Count) {
    Write-Host "警告 $($warnings.Count) 件:"
    $warnings | ForEach-Object { Write-Host "  - $_" }
}
