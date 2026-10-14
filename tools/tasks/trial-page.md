# 依頼: 体感評価用のローカル比較ページ（V0 体験版）を作る

最初に CLAUDE.md、docs/DESIGN.md（要件表。本人の決定なので変えない）、docs/STATUS.md、docs/decisions.md（D2）、core/pipeline.py、eval/converters.py（`Result`）、eval/sets/user_style.jsonl を読むこと。文書・コメント・コミットメッセージは日本語。

## 目的

本人が ThinkPad（Windows 11、Core Ultra 7 255H、内蔵 GPU のみ、32GB。開発機ではない）の前に **1回座るだけ**で「この変換コアが使えるか」を体感で決められる配布物を作る。手順は「zip を解凍 → `.cmd` をダブルクリック → ローマ字を打つ → 出たテキストブロックを貼る」の4手に収める。本人はキーボード操作を嫌う（起動はワンクリック、あとは選ぶボタンを押すだけ、「どちらも問題ない」の選択肢も置く）。ページで集めるのは次の3つ:

1. 設定の比較（速度優先／精度優先、句読点 A／B、英字表記の3モード）での好み。
2. 自由入力での使用感（単独の設定で打って、結果と所要時間を見る）。
3. ThinkPad 上の自動計測（常駐時の p50/p95、初回起動時間、機種情報）。本人に数値の転記をさせない。

## 作るもの（すべて新規。`trial/` 以下と `tools/pack-trial.ps1`、`docs/TRIAL.md`、`tests/test_trial_server.py`）

### 1. `trial/server.py` — 標準ライブラリだけのローカル HTTP サーバ

- `http.server.ThreadingHTTPServer`、既定 `127.0.0.1:8765`（使用中なら次の空きポート）。外部には bind しない。依存を増やさない（pyproject.toml は変えない）。
- 静的ファイル `trial/static/` を配信し、JSON API を持つ:
  - `GET /api/info` → 設定一覧（`trial/configs.json` の中身）、版（`trial/VERSION` の文字列。pack 時に git のコミットハッシュと日付を書く）、機種情報（CPU 名、論理コア数、物理メモリ GB、OS、Python、torch の版。Windows は `platform` と `ctypes` の `GlobalMemoryStatusEx` で取り、psutil 等は使わない）。
  - `POST /api/convert` `{"text": str, "left_context": str, "config_ids": [str] | null}` → `{"results": [{"config_id", "text", "ms", "finished": bool, "error": str|null}], "total_ms"}`。**設定は逐次に実行する**（同時実行は CPU を取り合い、体感速度を測れない）。1設定ごとの所要と全体の待ち時間を分けて返す。変換は要求があったときだけ（ライブ変換はしない。要件表）。
  - `POST /api/record` 任意の JSON → `trial/out/records.jsonl` に1行追記（時刻を付ける）。ページを閉じても消えないように、選択のたびに呼ぶ。
  - `GET /api/records` → 記録の全件。
  - `POST /api/bench` `{"config_ids": [str] | null}` → `trial/bench_sentences.jsonl`（下記）を各設定で 2 回空回し後に 1 回ずつ変換し、設定ごとの p50/p95/最大 ms と件数、失敗件数を返す。結果は `trial/out/bench.jsonl` にも追記。
  - `GET /api/summary` → 下記「結果ブロック」のテキスト。
- 変換エンジンは `trial/engine.py` に分ける:
  - `Engine.convert(text, left_context, config: dict) -> {"text", "ms", "finished", "candidates": [str]}`。
  - 実体 `CoreEngine` は `core.pipeline.CorePipeline` を包む。config の項目は **`model`（HF の ID かローカルパス）、`device`（"cpu"）、`k`、`w`、`f`、`punct`（"auto" | "ja" | "ascii" | "insert"）、`en_case`（"typed" | "registered" | "auto"）**。`punct` と `en_case` は親セッションが並行して `CorePipeline.__init__` に足す予定なので、**`inspect.signature` で受け付ける引数だけを渡す**（今の main では `model, device, k, w_read, w_flu` だけ。`w`→`w_read`、`f`→`w_flu` に対応づける）。同じ `model` と `device` の組はモデルを1回だけ読み込み、設定間で共有する（`CorePipeline` は `scorer` と `dic` を属性に持つので、2つ目以降はそれを流用してよい。方法は engine 内に閉じること）。`finished` は `Result.candidates` が空でないかと、`CorePipeline.candidates()` の最良の score が `-inf` でないかで判定し、失敗時は原入力をそのまま `text` に返して `error` を付ける（原入力を失わない）。
  - `MockEngine`（`--mock`）: 決定的な偽の結果（例: 入力を設定名ごとに違う規則で変形、ms は乱数固定シード）。**この環境では zenz を取得できない（huggingface.co に届かない）ので、動作確認はすべて MockEngine で行う。**
- 起動引数: `--port`、`--mock`、`--configs <path>`、`--open`（既定の browser を開く）。
- 左文脈: ページ側が直前の確定出力をつないで送る（下記）。サーバは状態を持たない。

### 2. `trial/configs.json` — 比較する設定（親が後で編集する。形だけ合わせる）

```json
{
  "axes": [
    {"id": "speed", "label": "速度優先 vs 精度優先", "configs": ["fast", "accurate"]},
    {"id": "punct", "label": "句読点: 打つ(A) vs 自動挿入(B)", "configs": ["punct_a", "punct_b"]},
    {"id": "en_case", "label": "英字の表記", "configs": ["en_typed", "en_registered", "en_auto"]}
  ],
  "configs": {
    "accurate": {"label": "精度優先", "model": "Miwa-Keita/zenz-v3.1-small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "auto", "en_case": "registered"},
    "fast": {"label": "速度優先", "model": "Miwa-Keita/zenz-v3.1-xsmall", "device": "cpu", "k": 3, "w": 4.0, "f": 1.0, "punct": "auto", "en_case": "registered"},
    "punct_a": {"label": "句読点A", "model": "Miwa-Keita/zenz-v3.1-small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "ja", "en_case": "registered"},
    "punct_b": {"label": "句読点B", "model": "Miwa-Keita/zenz-v3.1-small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "insert", "en_case": "registered"},
    "en_typed": {"label": "英字: 打ったまま", "model": "Miwa-Keita/zenz-v3.1-small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "auto", "en_case": "typed"},
    "en_registered": {"label": "英字: 登録語だけ", "model": "Miwa-Keita/zenz-v3.1-small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "auto", "en_case": "registered"},
    "en_auto": {"label": "英字: 自動", "model": "Miwa-Keita/zenz-v3.1-small", "device": "cpu", "k": 5, "w": 4.0, "f": 1.0, "punct": "auto", "en_case": "auto"}
  },
  "default_single": "accurate"
}
```

### 3. `trial/static/index.html`（＋同じフォルダの .css/.js。**外部 CDN・Web フォントは使わない**。ThinkPad はオフラインかもしれない）

- 画面は3つのタブ: **「比べる」「打ってみる」「まとめ」**。
- **比べる**: 軸を1つ選ぶ（既定は速度/精度）。入力欄にローマ字を打ち、Enter か「変換」ボタンで変換（打鍵ごとには変換しない）。その軸の設定の結果を**ブラインド**（ラベルは A/B/C、並び順は入力ごとに無作為）でカードに並べ、各カードに所要 ms を小さく出す。各カードに「一番いい」「致命的」ボタン、下に「全部問題ない」ボタン。「一番いい」は1枚だけ（押し直せる）、「致命的」は複数可、「全部問題ない」は一番の選択なしで記録する。記録は押した時点で `/api/record` に送る（内容: 軸、入力、左文脈、各カードの設定 id と出力と ms とブラインドラベル、選択）。送ったら入力欄を空にして次へ。既定の例文（下記 bench_sentences から 10 文）を「例文を入れる」ボタンで入力欄に入れられるようにする（本人が文を考える負荷を減らす）。
- **打ってみる**: 設定を1つ選ぶ（既定 `default_single`）。入力欄に打って Enter で変換。結果を確定欄に**追記**し、以後の変換ではその確定欄の末尾（最大 60 字）を左文脈として送る。「文脈をリセット」ボタン。確定欄は編集可能で「コピー」ボタン。各変換の所要 ms を履歴に出す。1件ごとに「使えた」「直した」のボタンを置き記録する（押さなくてもよい）。
- **まとめ**: 「自動計測を実行」ボタン（`/api/bench`、軸に含まれる全設定。進捗を表示）。その下に**結果ブロック**（下記）を `<textarea readonly>` で表示し、「コピー」ボタン。本人はこれを貼って返すだけにする。
- クリックだけで操作できること（入力欄にローマ字を打つのだけが例外）。ボタンは大きく。
- 変換の結果に `error` があれば原入力を赤く示し、変換を続けられること（ページが止まらない）。

### 4. 結果ブロック（`/api/summary` とページ側で同じ形）

```
romaji-slm trial <version> / <日時>
機種: <CPU> / <論理コア> / <RAM>GB / <OS>
初回起動: <秒 or 不明>（run.cmd が trial/out/launch.jsonl に書く）
常駐計測: accurate p50=<ms> p95=<ms> n=<件>; fast p50=... ; ...
[速度/精度] 入力 <n> 件: 一番 fast=<n> accurate=<n>; 致命的 fast=<n> accurate=<n>; 全部問題ない <n>
[句読点] ...
[英字表記] ...
[打ってみる] <n> 件, 使えた <n>, 直した <n>, 中央値 <ms>
--- 記録 ---
1. [速度/精度] "<入力>" | A(fast)=<出力> <ms>ms | B(accurate)=<出力> <ms>ms | 一番=B 致命的=なし
...
```

ブラインドのラベルと設定 id の対応は記録ごとにここで明かす。

### 5. `trial/run.cmd` — ダブルクリック1回で起動

- 配布物のルート（zip を解凍したフォルダ）に置く。`uv` が無ければ公式インストーラ（`powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"`）で入れる（何をしているかを画面に出す）。`uv sync` → `uv run python -m trial.server --open`。開始から「サーバが応答するまで」の秒を `trial/out/launch.jsonl` に記録する（初回起動時間）。失敗したらウィンドウを閉じずにエラーと `trial/out/launch.log` の場所を出す。
- モデルの所在: 環境変数 `HF_HUB_OFFLINE=1` と `HF_HOME=<配布物>/models/hf` を設定して起動し、同梱したモデルを使う（同梱は pack 側）。無ければ `HF_HUB_OFFLINE` を外して初回取得にフォールバックし、画面に「取得中」と出す。

### 6. `tools/pack-trial.ps1` — 配布 zip を作る（pwsh 7）

- `dist/romaji-slm-trial-<yyyymmdd>-<短いハッシュ>.zip` を作る。中身: `core/`、`eval/`（`core` が `eval.converters` と `eval.prompts` を import するので要る。`eval/out/` は除く）、`trial/`（`out/` は除く）、`pyproject.toml`、`uv.lock`、`README-trial.md`（本人向けの4手の手順だけ）、`models/mozc/compiled/`、`models/reading/kana_lm.pkl`、`models/hf/`（zenz-v3.1-small と xsmall の HF キャッシュ。`-HfHome` 引数でコピー元を指定。無ければ警告して続行）。
- **torch は CPU 版**にする: 配布物の `pyproject.toml` は `[[tool.uv.index]]` の `pytorch` を `https://download.pytorch.org/whl/cpu` に差し替えたものにする（元のリポジトリの pyproject.toml は変えない。pack スクリプトが書き換えたコピーを入れる）。**`uv lock` はこの環境からは download.pytorch.org に届かないので実行できない。**pack スクリプトは差し替え後に `uv lock` を試み、失敗したら `uv.lock` を入れずに警告する（親がローカルで作る）。
- `trial/VERSION` にコミットハッシュと日付を書き込む。

### 7. `trial/bench_sentences.jsonl` — 自動計測と例文

`eval/sets/user_style.jsonl` から typo 以外の 20 文（chat・tech・academic・memo・collision をまんべんなく、20〜40 字中心）を `{"romaji":..., "label": "<カテゴリ>"}` で抜く。

### 8. テストと文書

- `tests/test_trial_server.py`: MockEngine でサーバをスレッド起動し、`/api/info`・`/api/convert`（逐次実行、error 時に原入力を返す）・`/api/record`→`/api/records`・`/api/bench`・`/api/summary` の往復と、記録ファイルの追記を確かめる。`CoreEngine` の config→`CorePipeline` 引数の対応づけは、`CorePipeline` を偽物に差し替えて確かめる（torch 不要にする）。
- `docs/TRIAL.md`: 画面の使い方、API、config の項目、配布の作り方、ThinkPad 側の手順、既知の制限。
- `pytest` が全部通ること（既存のテストを壊さない）。

## 作業の規則

- ブランチ `claude/trial-page` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。**push 先はこのブランチだけ。**
- **`core/`・`eval/`・`pyproject.toml`・`uv.lock` は変えない**（親セッションが並行して core を編集している）。要望があれば PR の本文に書く。
- 認証情報・個人情報を書かない。外部サービスへの書き込みは PR の作成だけ。
- この環境からは huggingface.co と download.pytorch.org に届かない。pypi.org・github.com は届く。torch が無くても MockEngine とテストが動くようにする。
- 最終報告（PR 本文）: 作ったファイル、動作確認の方法と結果、親が後でやること（`uv lock`、モデルの同梱、ブラウザでの見た目の確認、`punct`/`en_case` の core 側の実装）、未解決の論点。
