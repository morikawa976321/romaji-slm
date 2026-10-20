# ThinkPad で変換の速さを測る（docs/THINKPAD.md「速さを測る」）。
#
#   配布 zip（tools/pack-bench.ps1 が作る）を展開して bench.cmd をダブルクリックする。
#   リポジトリの clone で動かすときは: pwsh -NoProfile -File tools/thinkpad-bench.ps1
#
# 常駐入力が GPU の無い機械で使う ONNX Runtime 版を測る。1 件の変換の内訳（eval.parity_speed、スレッド数を振る）と、
# 常駐入力の実際の形（候補ウィンドウの別の書き方つき・左文脈 60 字）で打鍵から 1 位が出るまでの待ち（eval.ime_sim）。
# 結果を bench-out\results.md にまとめてクリップボードに入れる。所要は 30 分前後（-Quick で 10 分前後）。
# Windows PowerShell 5.1 から呼ばれるので UTF-8 BOM を付けておく（BOM 無しだと日本語を ANSI として読む）。

param(
    [switch]$Quick,
    [switch]$Smoke  # 流れだけ確かめる（1〜2 文・1 周・空回しなし。数値は使わない）
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Out = Join-Path $Root 'bench-out'
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$Log = Join-Path $Out 'bench.log'

$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'uv が無いので公式のインストーラで入れます。'
    try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression } catch { Write-Host $_ }
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Write-Host 'uv を入れられませんでした'; exit 1 }
}
$env:UV_PYTHON_DOWNLOADS = 'automatic'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$Hf = Join-Path $Root 'models\hf'
if (Test-Path (Join-Path $Hf 'hub\models--Miwa-Keita--zenz-v3.1-small')) { $env:HF_HOME = $Hf; $env:HF_HUB_OFFLINE = '1' }

Write-Host '[1/3] Python と部品を用意しています（初回は数分。ONNX の書き出しに 20〜40 秒）...'
cmd /c "uv sync --no-default-groups --group core >> ""$Log"" 2>&1"
if ($LASTEXITCODE -ne 0) { Write-Host "用意に失敗しました。$Log を見てください"; exit 1 }

# 測った機械と条件（速度の数値に必ず添える。CLAUDE.md）
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1)
$bat = Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
$power = if ($bat) { if ($bat.BatteryStatus -eq 2) { 'AC 電源' } else { "バッテリー（状態 $($bat.BatteryStatus)、残り $($bat.EstimatedChargeRemaining)%）" } } else { 'バッテリー無し' }
$scheme = (powercfg /getactivescheme) -join ' '
$load = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$head = @(
    "# ThinkPad の速度（$(Get-Date -Format 'yyyy-MM-dd HH:mm')）", '',
    "- CPU: $($cpu.Name)（コア $($cpu.NumberOfCores)、論理 $($cpu.NumberOfLogicalProcessors)）",
    "- 電源: $power / $scheme",
    "- 測る前の CPU 使用率: $load%",
    "- 版: $(if (Test-Path (Join-Path $Root 'VERSION')) { Get-Content (Join-Path $Root 'VERSION') } else { (git rev-parse --short HEAD) })", ''
)

# 1 件の変換の内訳（常駐入力と同じ設定。句読点は自動で足さない、D48）。スレッド数を振る。句読点を打たない入力も測る
$np = 'file:eval/sets/user_style_nopunct.jsonl'
$runs = @(
    @{ tag = 'ort-t4';         mod = 'eval.parity_speed'; args = @('--converter', 'core-ort:t=4', '--device', 'cpu') },
    @{ tag = 'ime-t4';         mod = 'eval.ime_sim';      args = @('--threads', '4', '--profiles', 'normal', '--policies', 'base') },
    @{ tag = 'ort-t4-nopunct'; mod = 'eval.parity_speed'; args = @('--converter', 'core-ort:t=4', '--device', 'cpu', '--set', $np) }
)
if (-not $Quick) {
    $runs += @(
        @{ tag = 'ort-t2';     mod = 'eval.parity_speed'; args = @('--converter', 'core-ort:t=2', '--device', 'cpu') },
        @{ tag = 'ort-t6';     mod = 'eval.parity_speed'; args = @('--converter', 'core-ort:t=6', '--device', 'cpu') },
        @{ tag = 'ort-t8';     mod = 'eval.parity_speed'; args = @('--converter', 'core-ort:t=8', '--device', 'cpu') },
        @{ tag = 'ime-t4-all'; mod = 'eval.ime_sim';      args = @('--threads', '4', '--profiles', 'fast,normal', '--policies', 'base,B,C,BC', '--limit', '7') },
        @{ tag = 'ort-t4-again'; mod = 'eval.parity_speed'; args = @('--converter', 'core-ort:t=4', '--device', 'cpu') }
    )
}

$started = Get-Date
Write-Host "[2/3] 測ります（$($runs.Count) 通り。測っている間は PC を触らず、ほかのアプリも閉じてください）"
$i = 0
foreach ($r in $runs) {
    $i++
    Write-Host "  ($i/$($runs.Count)) $($r.tag)"
    $small = if ($r.mod -eq 'eval.ime_sim') { @('--limit', '2') } else { @('--repeat', '1', '--warmup', '0', '--min-chars', '27', '--max-chars', '27') }
    $a = @('run', '--no-sync', 'python', '-m', $r.mod, '--tag', "tp-$($r.tag)") + $r.args +
         $(if ($Smoke) { $small } elseif ($r.mod -eq 'eval.parity_speed') { @('--repeat', '3') } else { @() })
    & uv @a 2>> $Log | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "    失敗しました（$Log）" }
}

Write-Host '[3/3] まとめています'
# 今回の実行で書かれた表だけを、設定の順に集める（同じフォルダで前に測った結果を混ぜない）。表が無い設定は失敗と書く
$md = foreach ($r in $runs) {
    $dir = if ($r.mod -eq 'eval.ime_sim') { Join-Path $Root 'eval\out' } else { Join-Path $Root 'eval\out\parity' }
    $pat = if ($r.mod -eq 'eval.ime_sim') { "ime_sim-tp-$($r.tag)-2*.md" } else { "speed-tp-$($r.tag)-2*.md" }
    $f = Get-ChildItem $dir -Filter $pat -ErrorAction SilentlyContinue |
         Where-Object { $_.LastWriteTime -ge $started } | Sort-Object LastWriteTime | Select-Object -Last 1
    if ($f) { "## $($r.tag)`r`n`r`n" + (Get-Content -Raw -Encoding UTF8 $f.FullName) } else { "## $($r.tag): 失敗（$Log を見る）`r`n" }
}
$res = Join-Path $Out 'results.md'
[IO.File]::WriteAllText($res, (($head -join "`r`n") + "`r`n" + ($md -join "`r`n")), [Text.UTF8Encoding]::new($false))
try { Get-Content -Raw -Encoding UTF8 $res | Set-Clipboard; $clip = '（クリップボードにも入れました）' } catch { $clip = '' }
Write-Host ''
Write-Host "結果: $res $clip"
Write-Host 'この内容を開発機のセッションに貼ってください。'
