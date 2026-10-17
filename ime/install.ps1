# 常駐入力をこのマシンに入れる・更新する（docs/IME.md）。管理者権限は要らない。システムの設定は変えない。
#
#   pwsh -NoProfile -File ime/install.ps1 [-NoStartup] [-NoLaunch] [-Update]
#
# 入れる先: %LOCALAPPDATA%\romaji-slm\
#   RomajiSlmIme.exe   トレイアプリ（HEAD の ime/ を ime/build.ps1 でビルドしたもの）
#   app\               HEAD のコード（git archive）・辞書と読みの LM の写し・専用の .venv
#   app.prev\          ひとつ前の版（次の更新で組み立ての場所に使い回す。.venv の差分だけ直せば済む）
#   source.txt         写し元のリポジトリ（トレイアプリが新しい版の有無を見る）
#   update-result.txt / update.log   最後の更新の結果と記録（トレイアプリが結果を知らせる）
#   ime.ini / server.log / usage.txt   設定・サーバのログ・使用状況（打った文字列は残さない）
# 開発中の作業ツリーと分けるのは、他のセッションが core/ を書き換えている最中でも常用側が止まらないようにするため。
# 新しい版は app.next に組み立て、別のポートで自己試験（打鍵 → 確定文）を通してから差し替える。通らなければ
# 動いている常駐には触れない（壊れたコミットを入れて入力が止まらないように）。
# -Update: トレイアプリの「更新」から呼ぶとき。ショートカットに触れず、結果を update-result.txt に書く。

param([switch]$NoStartup, [switch]$NoLaunch, [switch]$Update)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$dst = Join-Path $env:LOCALAPPDATA 'romaji-slm'
$app = Join-Path $dst 'app'
$next = Join-Path $dst 'app.next'
$prev = Join-Path $dst 'app.prev'
$exe = Join-Path $dst 'RomajiSlmIme.exe'
$resultPath = Join-Path $dst 'update-result.txt'
New-Item -ItemType Directory -Force $dst | Out-Null
Set-Location $repo
if ($Update) { Start-Transcript -Path (Join-Path $dst 'update.log') -Force | Out-Null }

# 同時に 2 つ走らない（トレイの自動更新と手動の実行がぶつかる）
try { $lock = [IO.File]::Open((Join-Path $dst 'update.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
catch { Write-Warning '別の更新が動いている'; exit 3 }

$hash = (git rev-parse --short HEAD).Trim()
$stopped = $false
function Write-Result([string]$status, [string]$detail) {
    Set-Content -NoNewline -Encoding utf8NoBOM $resultPath "$status $hash $((Get-Date).ToString('s')) $detail"
}

try {
    if (git status --porcelain -- core eval ime pyproject.toml uv.lock) {
        Write-Warning 'core/ eval/ ime/ pyproject.toml uv.lock に未コミットの変更がある。入れるのは HEAD（未コミットの変更は入らない）'
    }

    Write-Host "組み立て: HEAD ($hash) → $next"
    if (-not (Test-Path $next) -and (Test-Path $prev)) { Move-Item $prev $next }
    New-Item -ItemType Directory -Force $next | Out-Null
    foreach ($d in 'core', 'eval', 'ime') { if (Test-Path "$next\$d") { Remove-Item -Recurse -Force "$next\$d" } }
    $zip = Join-Path ([IO.Path]::GetTempPath()) "romaji-slm-ime-$hash.zip"
    git archive --format=zip -o $zip HEAD core eval ime pyproject.toml uv.lock
    Expand-Archive -Force $zip $next
    Remove-Item $zip
    Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $next 'VERSION') "$hash $((Get-Date).ToString('yyyy-MM-dd'))"

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

    Write-Host 'ビルド: HEAD の ime/ から RomajiSlmIme.exe'
    & pwsh -NoProfile -File (Join-Path $next 'ime\build.ps1')
    if ($LASTEXITCODE) { throw 'ビルドに失敗した' }
    $newExe = Join-Path $next 'ime\bin\RomajiSlmIme.exe'

    Write-Host '自己試験: 新しい版のサーバを別のポートで起動し、打鍵 → 確定文を通す'
    $port = 8767
    $ini = Join-Path $dst 'ime.ini'
    if (Test-Path $ini) {
        $m = Select-String -Path $ini -Pattern '^\s*port\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    }
    $selfOut = Join-Path ([IO.Path]::GetTempPath()) "romaji-slm-selftest-$hash.txt"
    $p = Start-Process $newExe -ArgumentList '--root', "`"$next`"", '--selftest', 'kyouhaiitenki', "`"$selfOut`"", '--port', ($port + 1) -PassThru -Wait
    $got = if (Test-Path $selfOut) { (Get-Content -Raw -Encoding utf8 $selfOut).Trim() } else { '' }
    Remove-Item "$selfOut*" -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0 -or -not $got) { throw "自己試験に失敗した（終了コード $($p.ExitCode)）: $got" }
    Write-Host "  kyouhaiitenki → $got"

    Write-Host '動いている常駐を止めて差し替える'
    Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dst*" } | ForEach-Object {
        $_ | Stop-Process -Force   # 変換サーバはジョブオブジェクトで一緒に終わる
        $_.WaitForExit(5000) | Out-Null
        $stopped = $true
    }
    if (Test-Path $prev) { Remove-Item -Recurse -Force $prev }
    # 止めた直後は .venv の DLL がまだ掴まれていることがあるので、移せるまで少し待つ
    for ($i = 0; $i -lt 20 -and (Test-Path $app); $i++) {
        try { Move-Item $app $prev; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path $app) { throw "$app を移せなかった（使っているプロセスがある）" }
    Move-Item $next $app
    Copy-Item (Join-Path $app 'ime\bin\RomajiSlmIme.exe') $exe -Force
    Set-Content -NoNewline -Encoding utf8NoBOM (Join-Path $dst 'source.txt') $repo

    if (-not $Update) {
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

    if ($Update) { Write-Result 'ok' '' }
    if (-not $NoLaunch) { Start-Process $exe -WorkingDirectory $dst }
    Write-Host "入れた: $dst（$hash）"
}
catch {
    $msg = $_.Exception.Message
    Write-Warning "更新しなかった: $msg"
    if ($Update) { Write-Result 'fail' $msg }
    # 差し替えの途中で止まったら、前の版で常駐を起こし直す（止めたまま放っておかない）
    if ($stopped -and -not (Get-Process RomajiSlmIme -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dst*" })) {
        if (-not (Test-Path $app) -and (Test-Path $prev)) { Move-Item $prev $app }
        if (Test-Path $exe) { Start-Process $exe -WorkingDirectory $dst }
    }
    exit 1
}
finally {
    $lock.Dispose()
    if ($Update) { Stop-Transcript | Out-Null }
}
