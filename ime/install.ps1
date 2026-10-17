# 常駐入力をこのマシンに入れる・更新する（docs/IME.md）。管理者権限は要らない。システムの設定は変えない。
#
#   pwsh -NoProfile -File ime/install.ps1 [-NoStartup] [-NoLaunch]   # 組み立て・試験・差し替えを続けて行う（手で入れるとき）
#   pwsh -NoProfile -File ime/install.ps1 -Stage                     # 組み立てと試験だけ（動いている常駐に触れない）
#   pwsh -NoProfile -File ime/install.ps1 -Swap                      # 組み立て済みの版に差し替えて起動する
#
# 入れる先: %LOCALAPPDATA%\romaji-slm\
#   RomajiSlmIme.exe   トレイアプリ（入れた版の ime/ を ime/build.ps1 でビルドしたもの）。.prev はひとつ前
#   app\               入れた版のコード（git archive）・辞書と読みの LM の写し・専用の .venv
#   app.next\          組み立てた版（-Stage の結果）。app.prev\ はひとつ前の版（次の組み立てで使い回し、.venv の差分だけ直す）
#   source.txt         写し元のリポジトリ（トレイアプリが新しい版の有無を見る）
#   update-result.txt / update.log   最後の更新の結果（staged|ok|fail <版> <時刻> <説明>）と記録
#   ime.ini / server.log / usage.txt   設定・サーバのログ・使用状況（打った文字列は残さない）
# 開発中の作業ツリーと分けるのは、他のセッションが core/ を書き換えている最中でも常用側が止まらないようにするため。
# 組み立てた版は、別のポートで自己試験（打鍵 → 確定文）を通す。通らなければ動いている常駐には触れない
# （壊れたコミットを入れて入力が止まらないように）。
# トレイアプリの自動更新は -Stage を裏で動かし、打っていない間にトレイ自身が -Swap を起動して自分から終わる
# （外から止めると、打ちかけの文が消える）。

param([switch]$NoStartup, [switch]$NoLaunch, [switch]$Stage, [switch]$Swap)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $env:LOCALAPPDATA 'romaji-slm'
$app = Join-Path $dst 'app'
$next = Join-Path $dst 'app.next'
$prev = Join-Path $dst 'app.prev'
$exe = Join-Path $dst 'RomajiSlmIme.exe'
$exePrev = "$exe.prev"
$resultPath = Join-Path $dst 'update-result.txt'
$fromTray = $Stage -or $Swap
# 外したあとにフォルダを作り直さない（下でロックを取ってからも確かめる）
if ($fromTray -and -not (Test-Path (Join-Path $dst 'source.txt'))) { Write-Warning '外されている'; exit 3 }
New-Item -ItemType Directory -Force $dst | Out-Null
Set-Location $repo

# 同時に 2 つ走らない（トレイの自動更新と手動の実行がぶつかる）。記録（update.log）を始めるのは取れてから
try { $lock = [IO.File]::Open((Join-Path $dst 'update.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
catch { Write-Warning '別の更新が動いている'; exit 3 }
# トレイアプリから起動されたあと、ロックを取るまでの間に外された（uninstall.ps1 は source.txt を先に消す）
if ($fromTray -and -not (Test-Path (Join-Path $dst 'source.txt'))) { $lock.Dispose(); Write-Warning '外されている'; exit 3 }
if ($fromTray) { try { Start-Transcript -Path (Join-Path $dst 'update.log') -Append | Out-Null } catch { } }

$hash = ''
$stopped = $false
$swapped = $false
$exeSaved = $false
function Write-Result([string]$status, [string]$detail) {
    # 書けなくても、このあとの起こし直しは続ける
    try { Set-Content -NoNewline -Encoding utf8NoBOM -LiteralPath $resultPath -Value "$status $hash $((Get-Date).ToString('s')) $detail" } catch { }
}
function Get-Tray { Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dst*" } }

function Build-Next {
    # HEAD は 1 回だけ読み、その版を固定して使う（途中で並行セッションがコミットしても中身と VERSION が食い違わない）
    $rev = (git rev-parse HEAD).Trim()
    $script:hash = (git rev-parse --short $rev).Trim()
    if (git status --porcelain -- core eval ime pyproject.toml uv.lock) {
        Write-Warning 'core/ eval/ ime/ pyproject.toml uv.lock に未コミットの変更がある。入れるのは HEAD（未コミットの変更は入らない）'
    }

    Write-Host "組み立て: $hash → $next"
    if (-not (Test-Path $next) -and (Test-Path $prev)) { Move-Item $prev $next }
    New-Item -ItemType Directory -Force $next | Out-Null
    foreach ($d in 'core', 'eval', 'ime', 'VERSION') { if (Test-Path "$next\$d") { Remove-Item -Recurse -Force "$next\$d" } }
    $zip = Join-Path ([IO.Path]::GetTempPath()) "romaji-slm-ime-$hash.zip"
    git archive --format=zip -o $zip $rev core eval ime pyproject.toml uv.lock
    Expand-Archive -Force $zip $next
    Remove-Item $zip

    Write-Host '辞書と読みの LM: models/mozc, models/reading'
    $need = @('models/mozc/compiled/STAMP', 'models/mozc/id.def', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa')
    $missing = @($need | Where-Object { -not (Test-Path $_) })
    if ($missing.Count) { throw "無い: $($missing -join ', ')（docs/TRIAL.md「配布の作り方」の手順で作る）" }
    robocopy models\mozc\compiled "$next\models\mozc\compiled" /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy に失敗した ($LASTEXITCODE)" }
    foreach ($f in 'models/mozc/id.def', 'models/mozc/VERSION', 'models/mozc/README.txt', 'models/reading/kana_lm.pkl', 'models/reading/mozc_readings.marisa') {
        if (Test-Path $f) {
            New-Item -ItemType Directory -Force (Split-Path -Parent "$next/$f") | Out-Null
            Copy-Item $f "$next/$f"
        }
    }

    Write-Host 'Python の環境: uv sync（torch は uv のキャッシュから。GPU 版の uv.lock をそのまま使う）'
    Push-Location $next
    try {
        uv sync --frozen --no-dev 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE) { throw 'uv sync に失敗した' }
    } finally { Pop-Location }

    Write-Host "ビルド: $hash の ime/ から RomajiSlmIme.exe"
    & pwsh -NoProfile -File (Join-Path $next 'ime\build.ps1')
    if ($LASTEXITCODE) { throw 'ビルドに失敗した' }

    Write-Host '自己試験: 組み立てた版のサーバを別のポートで起動し、打鍵 → 確定文を通す'
    $port = 8767
    $ini = Join-Path $dst 'ime.ini'
    if (Test-Path $ini) {
        $m = Select-String -Path $ini -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    }
    $testPort = $port + 1
    # ポートに既にサーバがいると、トレイアプリは起動せずにそれを使う（組み立てた版を試したことにならない）
    $probe = New-Object Net.Sockets.TcpClient
    try { $probe.Connect('127.0.0.1', $testPort); $busy = $true } catch { $busy = $false } finally { $probe.Dispose() }
    if ($busy) { throw "自己試験のポート $testPort を別のプロセスが使っている" }
    $selfOut = Join-Path ([IO.Path]::GetTempPath()) "romaji-slm-selftest-$hash.txt"
    $p = Start-Process (Join-Path $next 'ime\bin\RomajiSlmIme.exe') -PassThru -Wait `
        -ArgumentList '--root', "`"$next`"", '--selftest', 'kyouhaiitenki', "`"$selfOut`"", '--port', $testPort
    $got = if (Test-Path $selfOut) { (Get-Content -Raw -Encoding utf8 $selfOut).Trim() } else { '' }
    Remove-Item "$selfOut*" -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0 -or -not $got) { throw "自己試験に失敗した（終了コード $($p.ExitCode)）: $got" }
    Write-Host "  kyouhaiitenki → $got"
    # VERSION は試験を通してから書く（-Swap は VERSION のある app.next だけを差し替える）
    Set-Content -NoNewline -Encoding utf8NoBOM -LiteralPath (Join-Path $next 'VERSION') -Value "$hash $((Get-Date).ToString('yyyy-MM-dd'))"
}

function Swap-In {
    if (-not (Test-Path (Join-Path $next 'VERSION'))) { throw "組み立て済みの版が無い（$next）" }
    $script:hash = (Get-Content -Raw (Join-Path $next 'VERSION')).Split(' ')[0].Trim()
    Write-Host "差し替え: $hash"
    if ($Swap) {
        # トレイアプリとの受け渡し: 準備ができたら swap.ready に PID を書く。トレイアプリは swap.decision に go（自分から
        # 終わる）か cancel（打ちかけている）を書く。こちらは 60 秒で timeout を書く。swap.decision は先に作った側の
        # 決定だけが残る（CreateNew）ので、期限ちょうどに go と timeout がすれ違わない。こちらからは止めない（打ちかけの文が消える）
        $ready = Join-Path $dst 'swap.ready'
        $decision = Join-Path $dst 'swap.decision'
        Set-Content -NoNewline -LiteralPath $ready -Value "$PID"   # 名前付きで渡す（位置で渡すと値のほうがファイル名になった）
        $deadline = (Get-Date).AddSeconds(60)
        $d = ''
        while (-not $d) {
            $c = ''
            # 作った側が書き終える前は、空か開けない。そのときは読み直す
            if (Test-Path $decision) { try { $c = [IO.File]::ReadAllText($decision).Trim() } catch { } }
            if ($c -in 'go', 'cancel', 'timeout') { $d = $c }
            elseif (-not (Get-Tray)) { $d = 'go' }   # 決定を書かずに終わった（落ちたときも、起こし直すほうがよい）
            # 決定が読めない・作れないまま期限をさらに 30 秒過ぎた。ロックを持ったまま待ち続けない
            # （トレイアプリは go を勝ち取ったあと、こちらが生きているのを確かめてから終わる）
            elseif ((Get-Date) -gt $deadline.AddSeconds(30)) { $d = 'stuck' }
            elseif ((Get-Date) -gt $deadline -and -not (Test-Path $decision)) {
                try { $f = [IO.File]::Open($decision, 'CreateNew', 'Write'); $f.Write([byte[]][char[]]'timeout', 0, 7); $f.Dispose(); $d = 'timeout' }
                catch { }   # トレイアプリが先に決めたか、作れない。読み直す
            }
            if (-not $d) { Start-Sleep -Milliseconds 100 }
        }
        if ($d -ne 'go') {
            # 見送る前に確かめ直す。ループで読んだ後に止まっていた間に、トレイアプリが go を書いて終わっていることがある
            # （見送ると誰も起こし直さない）
            try { $c = [IO.File]::ReadAllText($decision).Trim() } catch { $c = '' }
            if ($c -eq 'go' -or -not (Get-Tray)) { $d = 'go' }
        }
        Remove-Item $ready -ErrorAction SilentlyContinue
        # 見送るときは決定を消さない（消すと、遅れて来たトレイアプリの go が勝ち、誰も起こし直さない）。次の -Swap の前にトレイアプリが消す
        if ($d -ne 'go') { Write-Host "差し替えを見送った（$d）"; exit 5 }
        # go のあとはトレイアプリが自分で終わる。終わりきらなければ止める（もう打鍵は受けていない）
        foreach ($t in @(Get-Tray)) { if (-not $t.WaitForExit(30000)) { $t | Stop-Process -Force; $t.WaitForExit(5000) | Out-Null } }
        Remove-Item $decision -ErrorAction SilentlyContinue
        $script:stopped = $true
    }
    else {
        foreach ($t in @(Get-Tray)) {
            $t | Stop-Process -Force   # 手で入れるとき。変換サーバはジョブオブジェクトで一緒に終わる
            $t.WaitForExit(5000) | Out-Null
            $script:stopped = $true
        }
    }
    if (Test-Path $exe) { Copy-Item $exe $exePrev -Force; $script:exeSaved = $true }
    if (Test-Path $prev) { Remove-Item -Recurse -Force $prev }
    # 止めた直後は .venv の DLL がまだ掴まれていることがあるので、移せるまで少し待つ
    for ($i = 0; $i -lt 20 -and (Test-Path $app); $i++) {
        try { Move-Item $app $prev; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path $app) { throw "$app を移せなかった（使っているプロセスがある）" }
    $script:swapped = $true
    Move-Item $next $app
    Copy-Item (Join-Path $app 'ime\bin\RomajiSlmIme.exe') $exe -Force
    Set-Content -NoNewline -Encoding utf8NoBOM -LiteralPath (Join-Path $dst 'source.txt') -Value $repo
}

try {
    if (-not $Swap) { Build-Next }
    if ($Stage) {
        Write-Result 'staged' ''
        Write-Host "組み立てた: $next（$hash）"
        exit 0
    }
    Swap-In

    if (-not $fromTray) {
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
    }

    Write-Result 'ok' ''
    if (-not $NoLaunch -or $Swap) { Start-Process $exe -WorkingDirectory $dst }
    Write-Host "入れた: $dst（$hash）"
}
catch {
    $msg = $_.Exception.Message
    Write-Warning "更新しなかった: $msg"
    Write-Result 'fail' $msg
    # 常駐を止めたあとで失敗したら、前の版（app と exe の両方）で起こし直す（止めたまま放っておかない）
    if ($stopped -and -not (Get-Tray)) {
        try {
            if ($swapped -and (Test-Path $prev)) {
                if (Test-Path $app) {
                    if (Test-Path $next) { Remove-Item -Recurse -Force $next }
                    Move-Item $app $next
                    Remove-Item (Join-Path $next 'VERSION') -ErrorAction SilentlyContinue   # 組み立て済みとみなさない
                }
                Move-Item $prev $app
                if ($exeSaved) { Copy-Item $exePrev $exe -Force }   # この回に取った控えだけを戻す
            }
            elseif (-not (Test-Path $app) -and (Test-Path $prev)) { Move-Item $prev $app }
        }
        catch { Write-Warning "前の版に戻せなかった: $($_.Exception.Message)" }
        if (Test-Path $exe) { Start-Process $exe -WorkingDirectory $dst }
    }
    exit 1
}
finally {
    $lock.Dispose()
    if ($fromTray) { try { Stop-Transcript | Out-Null } catch { } }
}
