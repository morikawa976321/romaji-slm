# romaji-slm 体験版の起動（run.cmd から呼ばれる。docs/TRIAL.md）。
#
# 外部の罠: cmd.exe は UTF-8 のバッチファイルの日本語を正しく読めない（chcp 65001 を先頭に置いても、日本語を含む
# rem や echo の行が途中で切れて「コマンドとして認識されていません」が並ぶ。開発機で確認）。
# そこで run.cmd は ASCII だけの起動役にし、日本語の案内と手順はこのファイル（UTF-8 BOM 付き。Windows PowerShell 5.1 が
# BOM 無しの UTF-8 を ANSI として読むため）に置く。uv の出力はバイト列のまま残したいので cmd /c の >> でログに落とす。

$ErrorActionPreference = 'Continue'
$t0 = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

# 配布物のルート（run.ps1 は <ルート>\trial\ にある）
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root 'trial\server.py'))) {
    Write-Host "trial\server.py が見つかりません: $Root"
    exit 1
}
Set-Location $Root
$Out = Join-Path $Root 'trial\out'
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Log = Join-Path $Out 'launch.log'
[IO.File]::AppendAllText($Log, "==== $(Get-Date -Format s) 起動 ====`r`n", [Text.UTF8Encoding]::new($false))

function Show-Failure([string]$Message) {
    Write-Host ''
    Write-Host "===== $Message ====="
    if (Test-Path $Log) { Get-Content -Encoding UTF8 -Tail 30 $Log }
    Write-Host '=================================================='
    Write-Host "ログ全体: $Log"
    Write-Host 'このフォルダの trial\out をまとめて送ってもらえると原因を調べられます。'
}

Write-Host 'romaji-slm 体験版を起動します。'
Write-Host "記録とログの場所: $Out"
Write-Host ''

$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host '[1/3] uv（Python の実行環境を用意する道具）が無いので、公式のインストーラで入れます。'
    Write-Host '      powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression } catch { Write-Host $_ }
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Show-Failure 'uv を入れられませんでした。ネットワークにつながっているか確かめてください'
        exit 1
    }
}

$env:TRIAL_LAUNCH_T0 = "$t0"
$env:TRIAL_LAUNCH_FIRST = if (Test-Path (Join-Path $Root '.venv')) { '0' } else { '1' }
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

Write-Host '[2/3] Python と部品を用意しています（初回は数分かかります）...'
cmd /c "uv sync --no-default-groups --group core >> ""$Log"" 2>&1"
if ($LASTEXITCODE -ne 0) {
    Show-Failure 'エラーで止まりました（Python と部品の用意）。ログの最後の部分'
    exit 1
}

$Hf = Join-Path $Root 'models\hf'
$env:HF_HOME = $Hf
if ((Test-Path (Join-Path $Hf 'hub\models--Miwa-Keita--zenz-v3.1-small')) -and
    (Test-Path (Join-Path $Hf 'hub\models--Miwa-Keita--zenz-v3.1-xsmall'))) {
    $env:HF_HUB_OFFLINE = '1'
} else {
    Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
    Write-Host '      同梱のモデルが見つからないので、初回だけ Hugging Face から取得します（取得中は待ち時間が長くなります）。'
}

Write-Host '[3/3] サーバを起動します。ブラウザが開きます。'
Write-Host '      このウィンドウを閉じると終了します。'
cmd /c "uv run --no-sync python -m trial.server --open 2>> ""$Log"""  # 標準出力（URL など）は画面に出す
if ($LASTEXITCODE -ne 0) {
    Show-Failure 'エラーで止まりました（サーバ）。ログの最後の部分'
    exit 1
}
exit 0
