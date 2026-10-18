# ThinkPad で変換の速さを測る（docs/LLAMA_PARITY.md「ThinkPad での測定手順」）。
#
#   配布 zip（tools/pack-bench.ps1 が作る）を展開して bench.cmd をダブルクリックする。
#   リポジトリの clone で動かすときは: pwsh -NoProfile -File tools/thinkpad-bench.ps1 -Gguf <GGUF> -LlamaBin <llama-server のフォルダ>
#
# torch 版（今の常駐入力と同じ設定）と llama-server 版（EOS を直して取り直しを無くした版）を、句読点を打った入力と
# 打たない入力で測り、結果を bench-out\results.md にまとめてクリップボードに入れる。所要は 10 分前後。
# Windows PowerShell 5.1 から呼ばれるので UTF-8 BOM を付けておく（BOM 無しだと日本語を ANSI として読む）。

param(
    [string]$Gguf = '',
    [string]$GgufF16 = '',
    [string]$LlamaBin = '',
    [switch]$Quick
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (-not $Gguf) { $Gguf = Join-Path $Root 'models\zenz-v3.1-small-Q8_0.gguf' }
if (-not $GgufF16) { $GgufF16 = Join-Path $Root 'models\zenz-v3.1-small-F16.gguf' }
if (-not $LlamaBin) { $LlamaBin = Join-Path $Root 'llama-cpu' }
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
$env:LLAMA_CPP_BIN = $LlamaBin

Write-Host '[1/3] Python と部品を用意しています（初回は数分）...'
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

# 設定の意味は docs/LLAMA_PARITY.md「変種」。torch は今の常駐入力と同じ（句読点 B あり、スレッドは既定）。
# llama は BOS/EOS を直し（eos=3）、取り直しを無くし（fl=tok,rf=eos）、接続を使い回す（ka=1）。ort は forward だけ ONNX Runtime
$np = 'file:eval/sets/user_style_nopunct.jsonl'
$fast = 'eos=3,fl=tok,rf=eos,ka=1,p=insert'
$ort = @('--with', 'onnxruntime', '--with', 'onnx')
$runs = @(
    @{ tag = 'torch';            args = @('--converter', 'core:p=insert') },
    @{ tag = 'torch-t4';         args = @('--converter', 'core:p=insert', '--torch-threads', '4') },
    @{ tag = 'llama-f16-np1';    args = @('--converter', "core-llama:np=1,t=4,$fast", '--model', $GgufF16) },
    @{ tag = 'llama-f16-np5';    args = @('--converter', "core-llama:np=5,t=4,$fast", '--model', $GgufF16) },
    @{ tag = 'ort-t4';           args = @('--converter', 'core-ort:t=4,p=insert'); with = $ort },
    @{ tag = 'torch-nopunct';    args = @('--converter', 'core:p=insert', '--set', $np) },
    @{ tag = 'llama-f16-np5-nopunct'; args = @('--converter', "core-llama:np=5,t=4,$fast", '--model', $GgufF16, '--set', $np) }
)
if (-not $Quick) {
    $runs += @(
        @{ tag = 'torch-t8';     args = @('--converter', 'core:p=insert', '--torch-threads', '8') },
        @{ tag = 'llama-q8-np5'; args = @('--converter', "core-llama:np=5,t=4,$fast", '--model', $Gguf) },
        @{ tag = 'llama-f16-np5-t8'; args = @('--converter', "core-llama:np=5,t=8,$fast", '--model', $GgufF16) },
        @{ tag = 'ort-t8';       args = @('--converter', 'core-ort:t=8,p=insert'); with = $ort },
        @{ tag = 'ort-nopunct';  args = @('--converter', 'core-ort:t=4,p=insert', '--set', $np); with = $ort },
        @{ tag = 'torch-again';  args = @('--converter', 'core:p=insert') }
    )
}

Write-Host "[2/3] 測ります（$($runs.Count) 通り。1 通り 1〜2 分。測っている間は PC を触らず、ほかのアプリも閉じてください）"
$i = 0
foreach ($r in $runs) {
    $i++
    Write-Host "  ($i/$($runs.Count)) $($r.tag)"
    $a = @('run', '--no-sync') + $(if ($r.with) { $r.with } else { @() }) +
         @('python', '-m', 'eval.parity_speed', '--device', 'cpu', '--repeat', '3', '--tag', "tp-$($r.tag)") + $r.args
    & uv @a 2>> $Log | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "    失敗しました（$Log）" }
}

Write-Host '[3/3] まとめています'
$md = Get-ChildItem (Join-Path $Root 'eval\out\parity') -Filter 'speed-tp-*.md' | Sort-Object LastWriteTime |
      Where-Object { $_.LastWriteTime -gt (Get-Date).AddHours(-3) } | ForEach-Object { Get-Content -Raw -Encoding UTF8 $_.FullName }
$res = Join-Path $Out 'results.md'
[IO.File]::WriteAllText($res, (($head -join "`r`n") + "`r`n" + ($md -join "`r`n")), [Text.UTF8Encoding]::new($false))
try { Get-Content -Raw -Encoding UTF8 $res | Set-Clipboard; $clip = '（クリップボードにも入れました）' } catch { $clip = '' }
Write-Host ''
Write-Host "結果: $res $clip"
Write-Host 'この内容を開発機のセッションに貼ってください。'
