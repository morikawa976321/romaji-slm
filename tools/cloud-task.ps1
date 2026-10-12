# このリポジトリの現在のブランチで、クラウドセッション（Claude Code on the web）を1本立てる。
#
#   pwsh -NoProfile -File tools/cloud-task.ps1 -TaskFile <依頼文.md>
#
# 外部の罠:
# - `claude --cloud` は TTY が要る（パイプや -p から呼ぶと「--cloud requires an interactive terminal」で止まる）。
#   エージェントからは Bash ツールではなく、アプリの Terminal パネル（run_in_terminal）で走らせる。
# - クラウド側は GitHub の現在のブランチを clone する。未 push のコミットは見えないので、先に push しておく。
# - クラウドセッションクレジットの対象は、この方法やアプリの Cloud 環境で立てたセッションだけ。
#   ルーティン（/schedule・RemoteTrigger）は対象外で、プランの使用量から引かれる（support.claude.com 17152539）。
# - 既定の環境は huggingface.co と download.pytorch.org に届かない（403）。要るときは claude.ai/code の
#   環境設定 → Network access → Custom で許可する。
# - Windows PowerShell 5.1 は引用符を含む複数行の引数を壊すので pwsh（7.3 以降）で実行する。

param([Parameter(Mandatory)][string]$TaskFile)

$ErrorActionPreference = 'Stop'
$ahead = git rev-list --count '@{u}..HEAD' 2>$null
if ($LASTEXITCODE -ne 0) { throw "上流ブランチが無い。先に git push -u する" }
if ([int]$ahead -gt 0) { throw "未 push のコミットが $ahead 件ある。クラウド側からは見えないので先に push する" }

$task = Get-Content -Raw -Encoding utf8 $TaskFile
claude --cloud $task
