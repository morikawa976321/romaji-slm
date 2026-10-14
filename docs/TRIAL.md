# 体験版（V0）: 体感評価用のローカル比較ページ

本人が ThinkPad（Windows 11、Core Ultra 7 255H、内蔵 GPU のみ、32GB）の前に1回座るだけで、変換コアが使えるかを
体感で決めるための配布物。経緯は [decisions.md](decisions.md) の D2。

本人の手順は4手: zip を解凍 → `run.cmd` をダブルクリック → ローマ字を打ってボタンで選ぶ → 「まとめ」の結果ブロックをコピーして貼る
（配布物に入れる手順書は [trial/README-trial.md](../trial/README-trial.md)）。

## 構成

| パス | 役割 |
|---|---|
| `trial/server.py` | 標準ライブラリだけの HTTP サーバ（`127.0.0.1` だけに bind）。静的ファイルと JSON API |
| `trial/engine.py` | 変換エンジン。`CoreEngine`（`core.pipeline.CorePipeline` を包む）と `MockEngine`（偽物） |
| `trial/summary.py` | 結果ブロックの組み立て |
| `trial/machine.py` | 機種情報（Windows はレジストリの CPU 名と `GlobalMemoryStatusEx`） |
| `trial/configs.json` | 比べる設定と軸 |
| `trial/bench_sentences.jsonl` | 自動計測と「例文を入れる」の 20 文（`eval/sets/user_style.jsonl` の typo 以外から、5 カテゴリ×4） |
| `trial/static/` | ページ（`index.html`・`app.js`・`app.css`。外部 CDN・Web フォントなし） |
| `trial/run.cmd` | ThinkPad で起動する。配布物ではルートに置く。ASCII だけの起動役で、実体は `trial/run.ps1`（cmd.exe は UTF-8 のバッチの日本語を正しく読めないため。2026-10-15 開発機で確認） |
| `trial/run.ps1` | 起動の手順（uv の導入・`uv sync`・同梱モデルの指定・サーバ起動）。UTF-8 BOM 付き（Windows PowerShell 5.1 向け） |
| `tools/pack-trial.ps1` | 配布 zip を作る |
| `trial/out/` | 記録（`records.jsonl`・`bench.jsonl`・`launch.jsonl`・`launch.log`）。コミットしない |

## 開発機で動かす

```
python -m trial.server --mock --open                  # 偽の変換（torch もモデルも要らない）
python -m trial.server --mock --mock-delay 1 --open   # 偽の ms だけ実際に待つ（進捗表示の確認）
uv run python -m trial.server --open                  # 本物の変換コア（zenz と models/mozc・models/reading が要る）
```

引数: `--port`（既定 8765、使用中なら次の空きを最大 20 個まで探す）、`--mock`、`--mock-delay`、`--configs <path>`、
`--out <dir>`（記録の置き場所、既定 `trial/out`）、`--open`（既定のブラウザで開く）。

起動するとすぐページを出し、裏で全設定のパイプラインを作る（モデルの読み込み）。終わるまでページ上部に
「読み込み中」と出て、その間の変換は待たされる。読み込みに失敗してもページは開き、変換ごとにエラーを返す。

## 画面

クリックだけで操作できる（入力欄にローマ字を打つのだけが例外）。変換は Enter か「変換」ボタンを押したときだけ（ライブ変換しない）。

- **比べる**: 軸を1つ選ぶ（既定は速度/精度）。変換するとその軸の設定の結果を **ブラインド**（A/B/C、並びは入力ごとに無作為）で
  カードに並べ、各カードに所要 ms を出す。「一番いい」は1枚だけ（押し直せる）、「致命的」は複数可、「全部問題ない」は一番の選択なし。
  押すたびに `/api/record` に送り（同じ試行は同じ id、最後の行が最終の状態）、入力欄を空にする。カードは次の変換まで残るので選び直せる。
  ボタンを1つも押さなかった試行は記録しない。左文脈は送らない。「例文を入れる」は bench_sentences の先頭 10 文（各カテゴリ2文）を順に入れる。
- **打ってみる**: 設定を1つ選ぶ（既定 `default_single`、ここはラベルを見せる）。変換結果を確定欄に追記し、次の変換では確定欄の末尾
  （「文脈をリセット」以降、最大 60 字）を左文脈として送る。確定欄は編集でき、「コピー」できる。履歴に入力・出力・ms を出し、
  「使えた」「直した」を押せる（押さなくてもよい）。変換した時点で1回、押すたびにもう1回記録する。
- **まとめ**: 「自動計測を実行」で、軸に含まれる全設定を1つずつ `/api/bench` にかける（設定単位の進捗と経過秒を出す）。
  その下に結果ブロック（`/api/summary` の本文）を出し、「コピー」で写せる。

変換に `error` があると、原入力を赤で出し、理由を添える。打ってみるでは原入力をそのまま確定欄に足す（入力を失わない）。

## API

| | 要求 | 応答 |
|---|---|---|
| `GET /api/info` | | `configs`（configs.json）、`version`、`machine`（cpu・logical_cores・memory_gb・os・python・torch）、`engine`（`name` と、設定ごとに CorePipeline が受け付けず無視した項目 `ignored`）、`ready`、`load_error`、`bench_sentences` |
| `POST /api/convert` | `{"text", "left_context", "config_ids": [..] \| null}`（null は全設定） | `{"results": [{"config_id", "text", "ms", "finished", "error"}], "total_ms"}` |
| `POST /api/record` | 任意の JSON オブジェクト | `{"ok", "time"}`。`trial/out/records.jsonl` に `time` を付けて1行追記 |
| `GET /api/records` | | `{"records": [...]}` |
| `POST /api/bench` | `{"config_ids": [..] \| null}` | `{"results": [{"config_id", "n", "failed", "p50", "p95", "max", "ms"}], "total_s"}`。`trial/out/bench.jsonl` にも追記 |
| `GET /api/summary` | | 結果ブロック（text/plain） |

- 変換と計測はサーバ全体で1本ずつ流す（要求をまたいでも並行させない）。並べると CPU を取り合い、体感の速度を測れない。
  1設定ごとの所要（`ms`、エンジン内の変換時間）と、要求全体の待ち時間（`total_ms`）を分けて返す。
- 失敗（例外・未完了・未知の設定）は `text` に原入力を入れ、`error` に理由を書く。HTTP は 200 のまま。
- 計測は設定ごとに先頭 2 文で空回ししてから、20 文を1回ずつ左文脈なしで変換する。p50/p95 は最近接順位法。失敗した文の ms も含める。
- サーバは状態を持たない。左文脈はページが組み立てて送る。

### 記録の形（records.jsonl）

```json
{"time": "...", "kind": "compare", "id": "<試行>", "axis": "speed", "input": "...", "left_context": "",
 "cards": [{"label": "A", "config_id": "fast", "text": "...", "ms": 412.3, "error": null}, ...],
 "best": "B", "fatal": ["A"], "all_ok": false}
{"time": "...", "kind": "single", "id": "<試行>", "config_id": "accurate", "input": "...", "left_context": "...",
 "text": "...", "ms": 803.1, "error": null, "verdict": "used" | "fixed" | null}
```

`launch.jsonl` は `{"time", "first", "serve_s", "ready_s", "error", "version"}`。`run.ps1` が開始時刻（`TRIAL_LAUNCH_T0`、UNIX ミリ秒）と
初回か（`TRIAL_LAUNCH_FIRST`、`.venv` が無かったら 1）を環境変数で渡し、サーバがモデルの読み込みを終えた時点で書く。
`serve_s` は run.ps1 の開始からサーバが応答できるまで（uv の導入・`uv sync` を含む）、`ready_s` はモデルの読み込みが終わるまで。

## config の項目

| 項目 | 意味 | CorePipeline の引数 |
|---|---|---|
| `label` | 画面の表示名（打ってみる・まとめで使う。比べるでは隠す） | — |
| `model` | HF の ID かローカルパス | `model` |
| `device` | `"cpu"` | `device` |
| `k` | 読みの候補数 | `k` |
| `w` | 読みのコストの重み | `w_read` |
| `f` | 完成文の自然さの重み | `w_flu` |
| `punct` | `"auto"` \| `"ja"` \| `"ascii"` \| `"insert"` | `punct`（core 側で追加予定） |
| `en_case` | `"typed"` \| `"registered"` \| `"auto"` | `en_case`（core 側で追加予定） |

`CoreEngine` は `inspect.signature(CorePipeline.__init__)` を見て、受け付ける引数だけを渡す（受け付けない項目は `/api/info` の
`engine.ignored` に出し、比べるタブにも注意書きを出す）。同じ `model` と `device` の組はモデルと辞書を1回だけ読み、
2つ目以降の `CorePipeline` は `__init__` の中の `ZenzScorer(...)` と `Dictionary.load()` を読み込み済みのものに差し替えて作る。
`finished` は `Result.candidates` が空でなく、`candidates()` の最良の score が `-inf` でないこと（`convert_detailed` の中の
`candidates()` の戻り値を横取りして見るので、2回計算しない）。

軸（`axes`）は `id`・`label`（ボタンの表示）・`short`（結果ブロックの見出し。無ければ label）・`configs`。`default_single` は打ってみるの既定。

## 結果ブロック

```
romaji-slm trial <version> / <日時>
機種: <CPU> / <論理コア> / <RAM>GB / <OS> / Python <版> / torch <版>
初回起動: 応答 <秒>・モデル読込完了 <秒>（2回目以降の起動があれば「/ 最新の起動: ...」）
常駐計測: accurate p50=<ms> p95=<ms> max=<ms> n=<件>; fast ...
[速度/精度] 入力 <n> 件: 一番 fast=<n> accurate=<n>; 致命的 fast=<n> accurate=<n>; 全部問題ない <n>
[句読点] ...
[英字表記] ...
[打ってみる] <n> 件, 使えた <n>, 直した <n>, 中央値 <ms>ms
--- 記録 ---
1. [速度/精度] "<入力>" | A(fast)=<出力> <ms>ms | B(accurate)=<出力> <ms>ms | 一番=B 致命的=なし
2. [打ってみる accurate] "<入力>" → <出力> <ms>ms 文脈=<n>字 | 使えた
```

ブラインドのラベルと設定 id の対応は、記録ごとにここで明かす。失敗した出力は `(失敗:<原入力>)`。常駐計測は設定ごとに最新の1回。

## 配布の作り方（開発機・pwsh 7）

```
pwsh -NoProfile -File tools/pack-trial.ps1 -HfHome $env:USERPROFILE\.cache\huggingface
```

`dist/romaji-slm-trial-<yyyymmdd>-<短いハッシュ>.zip` ができる。中身:

- `run.cmd`・`README-trial.md`（ルート）、`core/`、`eval/`（`core` が `eval.converters` と `eval.prompts` を import する。`eval/out/` は除く）、
  `trial/`（`out/` は除く。`trial/VERSION` にコミットハッシュと日付を書く。未コミットの変更があれば `+dirty`）
- `pyproject.toml`: pytorch の index を `https://download.pytorch.org/whl/cpu` に差し替えたもの（元のファイルは変えない）。
  差し替え後に `uv lock` を取り直し、失敗したら `uv.lock` を入れずに警告する（cu128 用の古い lock は入れない）
- `models/mozc/compiled/` と `id.def`・`VERSION`・`README.txt`（`Dictionary.load` は compiled/ のほかに id.def と VERSION を読む。README.txt は IPAdic 等のライセンス文）
- `models/reading/kana_lm.pkl`
- `models/hf/hub/models--Miwa-Keita--zenz-v3.1-{small,xsmall}/`（`refs/` と `snapshots/` の実体。blobs への symlink は辿って実体を入れる）

無いものは警告して続行する。最後に警告の一覧を出す。

## ThinkPad 側の手順

1. zip を解凍する（パスに `&` を含まない場所）。
2. `run.cmd` をダブルクリック。
   - `uv` が無ければ公式インストーラ（`powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"`）で入れる。
   - `uv sync --no-default-groups --group core`（torch CPU 版・transformers・marisa-trie・wordfreq。Python が無ければ uv が取る）。
   - `HF_HOME=<配布物>\models\hf`。同梱のモデルが2つともあれば `HF_HUB_OFFLINE=1`、無ければ外して初回取得（画面に「取得します」と出す）。
   - `uv run --no-sync python -m trial.server --open`。サーバの出力は `trial\out\launch.log` に行く。
   - 失敗したらウィンドウを閉じずに、ログの末尾 30 行と `launch.log` の場所を出す。
3. ページで比べる・打ってみる・まとめの順に操作し、結果ブロックを返してもらう。

## 既知の制限

- **本物の変換（CoreEngine + zenz）はクラウドでは動かしていない**（huggingface.co に届かない）。CoreEngine は CorePipeline を
  偽物に差し替えたテストだけで確かめた。開発機で `uv run python -m trial.server --open` を一度通す必要がある。
- **今の CorePipeline は `punct`・`en_case` を受け付けない**ので、句読点と英字表記の軸は、同じ設定を比べることになる
  （ページに注意書きが出る）。core 側に引数が入れば自動で渡る。
- **ThinkPad 用の `uv.lock` が無い**（この環境から download.pytorch.org に届かない）。開発機で pack すれば作られる。
  lock が無くても初回の `uv sync` が解決するが、ネットワークが要り、版が固定されない。
- `machine.py` の Windows 分岐（レジストリ・`GlobalMemoryStatusEx`・`SO_EXCLUSIVEADDRUSE`）と `run.cmd`→`run.ps1` は、開発機（Windows 11）で zip を展開して確認した（2026-10-15。初回 serve 5.1 s / ready 19.8 s、uv のキャッシュが温かい状態）。ThinkPad では未確認。
- 初回起動時間は、依頼では run.cmd が書く形だったが、モデルの読み込み完了を知っているのはサーバなので、run.cmd は開始時刻を渡し、
  サーバが `launch.jsonl` に書く形にした。
- 比べるでの変換は軸の設定順（configs.json の順）に逐次実行する。カードの並びは無作為だが、実行順は固定。
- ページは比べるの左文脈を送らない。打ってみるの左文脈は確定欄の末尾だけ（確定欄を手で直した内容はそのまま文脈になる）。
