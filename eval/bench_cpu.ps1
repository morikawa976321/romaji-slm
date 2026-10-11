# CPU だけで変換1回ぶんの所要時間を見積もる（llama-bench）。
#
# 1回の変換 = 入力の処理（prompt, n_prompt トークン）＋ 出力の生成（n_gen トークン）。
# 20〜30字の日本語文は、文字単位モデル（zenz）でローマ字入力だと約80〜90トークン、出力は約25〜30トークン。
# 左文脈を付けると入力はさらに約100トークン増える。そこで p=96（文脈なし）/224（文脈あり）、n=32 を測る。
#
# 他プロセスの負荷で値が揺れるので、各条件を繰り返して「最良の回」と「中央値」の両方を出す。
#
# 使い方:
#   pwsh eval/bench_cpu.ps1 -Llama C:\path\to\llama-bench.exe -Configs @(@('all','',0))
#   Configs の各要素は @(ラベル, コアマスク(16進, 空なら指定なし), スレッド数(0ならllama既定))

param(
    [string]$Llama = 'C:\Users\tm976\mf\llama\llama-bench.exe',
    [string[]]$Models = @(
        'models/zenz-v3.2-xsmall-bench.gguf',
        'models/zenz-v3.2-small-bench.gguf',
        'models/romaji2ja-Q5_K_M.gguf',
        'models/romaji2ja-f16.gguf'
    ),
    [object[]]$Configs = @(
        @('P4', '0x3C00', 4), @('P2', '0x3000', 2), @('P1', '0x2000', 1),
        @('E4', '0x3C0', 4), @('E2', '0x30', 2)
    ),
    [int]$Reps = 15,
    [string]$Out = 'eval/out/bench_cpu.json'
)

$rows = @()
foreach ($m in $Models) {
    foreach ($c in $Configs) {
        $args = @('-m', $m, '-ngl', '0', '-dev', 'none', '-p', '96,224', '-n', '32', '-r', $Reps, '-o', 'json')
        if ($c[1]) { $args += @('-C', $c[1], '--cpu-strict', '1') }
        if ($c[2] -gt 0) { $args += @('-t', $c[2]) }
        $res = & $Llama @args 2>$null | Out-String | ConvertFrom-Json
        $best = @{}; $med = @{}
        foreach ($r in $res) {
            $key = if ($r.n_gen -gt 0) { 'tg' } else { "pp$($r.n_prompt)" }
            $s = @($r.samples_ts | Sort-Object)
            $best[$key] = $s[-1]
            $med[$key] = $s[[int][math]::Floor($s.Count / 2)]
        }
        $lat = { param($t, $p) 1000 * ($p / $t["pp$p"] + 32 / $t['tg']) }
        $rows += [pscustomobject]@{
            model = $m; cfg = $c[0]
            pp96_best = [math]::Round($best['pp96']); tg_best = [math]::Round($best['tg'])
            ms_noctx_best = [math]::Round((& $lat $best 96)); ms_ctx_best = [math]::Round((& $lat $best 224))
            ms_noctx_med = [math]::Round((& $lat $med 96)); ms_ctx_med = [math]::Round((& $lat $med 224))
        }
    }
}
New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
$rows | ConvertTo-Json | Set-Content $Out
$rows | Format-Table -AutoSize | Out-String -Width 220
