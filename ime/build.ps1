# 常駐入力のトレイアプリをビルドする（docs/IME.md）。.NET SDK は要らない（Windows 同梱の .NET Framework 4.x の csc、C# 5）。
#
#   pwsh -NoProfile -File ime/build.ps1          # ime/bin/RomajiSlmIme.exe
#   pwsh -NoProfile -File ime/build.ps1 -Test    # Composer の試験をビルドして実行する

param([switch]$Test)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "csc.exe が無い: $csc" }
$bin = Join-Path $here 'bin'
New-Item -ItemType Directory -Force $bin | Out-Null
$wpf = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\WPF'

if ($Test) {
    $exe = Join-Path $bin 'ComposerTests.exe'
    & $csc /nologo /target:exe /optimize /utf8output "/out:$exe" (Join-Path $here 'Composer.cs') (Join-Path $here 'ComposerTests.cs')
    if ($LASTEXITCODE) { throw 'ビルドに失敗した' }
    & $exe
    exit $LASTEXITCODE
}

$exe = Join-Path $bin 'RomajiSlmIme.exe'
& $csc /nologo /target:winexe /optimize /utf8output "/out:$exe" `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll `
    "/r:$wpf\UIAutomationClient.dll" "/r:$wpf\UIAutomationTypes.dll" "/r:$wpf\WindowsBase.dll" `
    (Join-Path $here 'Composer.cs') (Join-Path $here 'RomajiSlmIme.cs')
if ($LASTEXITCODE) { throw 'ビルドに失敗した' }
Write-Host "作成: $exe"
