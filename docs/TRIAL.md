# 体験版（V0.2）: 一本道の体感評価ページ

本人が ThinkPad（Windows 11、Core Ultra 7 255H、内蔵 GPU のみ、32GB）の前に1回座るだけで、変換コアの設定（句読点・英字の表記）と
使えそうかを決めるための配布物。経緯は [decisions.md](decisions.md) の D2・D9・D10。

V0（3 タブの自由探索）を、D10 の指摘（本人が「何をすれば終わるか」を考える時間を削る）に沿って
「少数の比較 → 選んだ組合せを体感 → 自動計測 → 結果をコピー」の一本道にした。

本人の手順は4手: zip を解凍 → `run.cmd` をダブルクリック → 画面の順にボタンを押す → 最後に「結果をコピー」を押して貼る
（配布物に入れる手順書は [trial/README-trial.md](../trial/README-trial.md)）。

## 構成

| パス | 役割 |
|---|---|
| `trial/server.py` | 標準ライブラリだけの HTTP サーバ（`127.0.0.1` だけに bind）。静的ファイルと JSON API |
| `trial/engine.py` | 変換エンジン。`CoreEngine`（`core.pipeline.CorePipeline` を包む）と `MockEngine`（偽物） |
| `trial/summary.py` | 結果ブロックの組み立て（回答 → 組合せの対応 `combo_id` もここ） |
| `trial/machine.py` | 機種情報（Windows はレジストリの CPU 名と `GlobalMemoryStatusEx`） |
| `trial/configs.json` | モデル・比較の設定・ステップ・質問カードの文言と選択肢 |
| `trial/examples.json` | 比較・体感・計測の例文（親が実コアで確かめて差し替える） |
| `trial/static/` | ページ。`index.html` は入力して変換するだけの画面（設定は本人の選択 small・句読点 B・英字は打ったまま、確定欄の末尾 60 字を左文脈に。2026-10-16 本人の依頼）。`steps.html`・`app.js`・`app.css` は比較と回答を集める一本道の画面（`/steps.html`）。外部 CDN・Web フォントなし |
| `trial/run.cmd` | ThinkPad で起動する。配布物ではルートに置く。ASCII だけの起動役で、実体は `trial/run.ps1`（cmd.exe は UTF-8 のバッチの日本語を正しく読めないため。2026-10-15 開発機で確認） |
| `trial/run.ps1` | 起動の手順（uv の導入・`uv sync`・同梱モデルの指定・サーバ起動）。UTF-8 BOM 付き（Windows PowerShell 5.1 向け） |
| `tools/pack-trial.ps1` | 配布 zip を作る |
| `trial/out/` | 記録（`records.jsonl`・`measure.jsonl`・`launch.jsonl`・`url.txt`・`launch.log`）。コミットしない |

## 開発機で動かす

```
python -m trial.server --mock --open                  # 偽の変換（torch もモデルも要らない）
python -m trial.server --mock --mock-delay 1 --open   # 偽の ms だけ実際に待つ（進捗表示の確認）
uv run python -m trial.server --open                  # 本物の変換コア（zenz と models/mozc・models/reading が要る）
```

引数: `--port`（既定 8765、使用中なら次の空きを最大 20 個まで探す。0 なら OS に選ばせる）、`--mock`、`--mock-delay`、
`--configs <path>`、`--examples <path>`、`--out <dir>`（記録の置き場所、既定 `trial/out`）、`--open`（既定のブラウザで開く）。

起動すると、実際の URL を標準出力に1行で出し（flush する）、`<out>/url.txt` にも書く。`--open` でブラウザを開けなかったとき
（`webbrowser.open` が例外か False）は「上の URL をブラウザのアドレス欄に貼って開いてください」と出して続ける。
裏で比較の設定と既定の組合せ（small・xsmall）のパイプラインを作って1回ずつ空回しし、終わると「準備できました」と出す。
読み込みに失敗してもページは開き、変換ごとにエラーを返す。

## 画面（1 ページのステップ式）

上に手順の帯（準備・1 句読点・2 英字・3 体感・4 結果。済んだものに ✓）、下に「戻る」（済んだステップなら「次へ」も）。
推論の開始は常に本人のボタン操作（ライブ変換しない）。比較のステップでは ms を出さない（表記の好みに速度を混ぜない。結果ブロックには残す）。

| ステップ | 本人の操作 | 最少クリック |
|---|---|---|
| 0 準備 | 「準備できました」が出たら「はじめる」 | 1 |
| 1 句読点 | 例 3 つ: 「この例を変換」→ 案を1つ選ぶ。終わったらカード 1 に答える | 7 |
| 2 英字 | 例 3 つ: 同じ。終わったらカード 2 に答える | 7 |
| 3 体感 | 例 1 を「変換」→ 続きの例 2 を「変換」→ カード 3 に答える | 3 |
| 4 結果 | （計測は自動）「結果をコピー」 | 1 |

合計 19 クリック。「どちらも使えない」を押したときだけ理由（句読点／英字／変換内容／その他）のボタンが出て、1 クリック増える。

- **比較（ステップ 1・2）**: 例は入力済みで表示し、1つ目だけを出す。選ぶと次の例が出る（前の例も見えていて選び直せる）。
  変換結果は「案1・案2（・案3）」のブラインドで並べ、並びも実行順も毎回入れ替える。各カードに「こちらがいい」、下に
  「どちらも（どれも）問題ない」「どちらも（どれも）使えない」。3 例が済むと質問カードが出る。
  - 句読点: `punct_a`（small, punct=ja, en_case=typed）と `punct_b`（small, punct=insert, en_case=typed）。例は句読点を打たない入力
    （完結した文・質問・メモ）。コアは句読点を打った入力には挿入しないため。
  - 英字: `en_typed`・`en_registered`・`en_auto`（いずれも small, punct=auto）。例は登録語・辞書に無い短い英字列・識別子。
- **質問カード**: 押すと保存して次のステップへ進む。
  - カード 1「普段は、句読点を自分で打つ形と、自動で補う形のどちらにしますか？」自分で打つ（A）／自動で補う（B）／どちらも問題ない／まだ決められない。
  - カード 2「英字の大文字・小文字は、普段どこまで自動で整えますか？」打ったまま／登録語だけ整える／短い未知語も整える（自動）／どれも問題ない／まだ決められない。
  - カード 3「今試した変換を、自分の文章でも使ってみたいですか？」使ってみたい／今は使いたくない／まだ判断できない。
  - 「問題ない」「まだ決められない」「未回答」は暫定値（句読点 A・英字 打ったまま）で先に進む。**この割当ては AI の暫定運用で、
    本人の好みではない**（結果ブロックでは「どちらも問題ない（暫定 A）」のように区別して出す）。カード 2 に答えた時点で、
    体感・計測に使う組合せ（small と xsmall）を `/api/prepare` で作って空回ししておく。
- **体感（ステップ 3）**: カード 1・2 の組合せ（small）で、例 1 を変換 → その続きの例 2 を左文脈ありで変換。例は句読点 B なら
  句読点を打たない形、A なら打った形を出す（`examples.json` の `feel[].nopunct` / `punct`）。結果は確定欄にたまる（読み取り専用）。
  例 2 の後は任意で「自分の文を試す」（Enter で変換、確定欄に追記、左文脈は確定欄の末尾 60 字）と「速度優先（xsmall）でも同じ文を試す」
  （直前の文を同じ左文脈で xsmall にかける。確定欄には足さない）。履歴には ms を出す。
- **計測と結果（ステップ 4）**: 開くと自動で計測する（進捗バー）。最終の組合せで 6 文（うち 2 文は句読点なし＝B なら挿入が働く）、
  small と xsmall を同じ 3 文で。各設定の最初に 1 回空回しする（記録しない）。終わると結果ブロックと「結果をコピー」を出す。
  途中で戻ったり再読込したりしたら、次に開いたときに残りだけを測る。

### 保存とセッション

- サーバは起動ごとにセッション id（`yyyymmdd-HHMMSS-xxxx`）を振る。記録・計測・起動の記録にはセッション id を付け、
  結果ブロックは今回のセッションだけを集計する（過去の分はセッション数と行数だけ添える）。
- 選択・回答は押した時点で `/api/record` に送り、**保存の完了（サーバがファイルに書いて fsync した応答）を待ってから次へ進む**。
  失敗したら 0.5・1・2 秒あけて再試行し、それでも駄目なら赤い帯で知らせて先へ進む（「もう一度保存」で送り直せる）。
  記録は送る前に localStorage（キー `romaji-slm-trial:<セッション id>`）にも置き、保存できなかったものは結果ブロックに `*未保存` で出す。
- 同じ id の記録は版（`rev`）を上げて送り直す。サーバは追記し、最後の行が最終の状態。
- 再読込すると、サーバの記録（`/api/state`）とブラウザの記録を合わせて（`rev` の大きいほう）、回答済みの位置から続ける。
- サーバを再起動するとセッションが変わる。古いページから送ると 409 を返し、ページは「再読込してください」と出す。

## API

| | 要求 | 応答 |
|---|---|---|
| `GET /api/info` | | `version`、`session`、`started`、`machine`（cpu・logical_cores・memory_gb・os・python・torch）、`engine`（`name` と、設定ごとに CorePipeline が受け付けず無視した項目 `ignored`）、`ready`、`load_error`、`configs`（configs.json）、`examples`（examples.json） |
| `GET /api/state` | | 今回のセッションの `{"session", "records": [id ごとの最新], "measures": [...]}` |
| `POST /api/convert` | `{"text", "left_context", "config_ids": [..]}` | `{"results": [{"config_id", "text", "ms", "finished", "error"}], "total_ms"}` |
| `POST /api/prepare` | `{"config_ids": [..]}` | `{"ok": true}`。パイプラインを作って空回しする |
| `POST /api/record` | `{"session", "kind", "id", "rev", ...}` | `{"ok", "time"}`。`records.jsonl` に `time` を付けて追記し fsync。書けなければ 500、セッション違いは 409、形の誤りは 400 |
| `POST /api/measure` | `{"session", "phase": "final"\|"speed", "config_id", "index", "text", "warmup"}` | `{"result": {...convert の1件}, "warmup"}`。warmup でなければ `measure.jsonl` に追記 |
| `POST /api/summary` | `{"session", "client_records": [...]}` | 結果ブロック（text/plain）。ブラウザにだけある新しい版を重ねて `*未保存` を付ける |
| `GET /api/summary` | | 結果ブロック（サーバの記録だけ） |

- 設定 id は、configs.json の名前付き設定（`punct_a` など）か、組合せ `<model>/<punct>/<en_case>`（例 `small/insert/registered`。
  model は `models` のキー、punct・en_case は `combo` に挙げた値）。
- 変換と計測はサーバ全体で1本ずつ流す（要求をまたいでも並行させない）。並べると CPU を取り合い、体感の速度を測れない。
  1設定ごとの所要（`ms`、エンジン内の変換時間）と、要求全体の待ち時間（`total_ms`）を分けて返す。
- 変換の失敗（例外・未完了・未知の設定）は `text` に原入力を入れ、`error` に理由を書く。HTTP は 200 のまま。

### 記録の形

```json
{"time": "...", "session": "...", "rev": 2, "kind": "compare", "id": "cmp-punct-0", "step": "punct", "index": 0, "total": 3,
 "input": "...", "cards": [{"label": "案1", "config_id": "punct_b", "text": "...", "ms": 412.3, "error": null}, ...],
 "choice": "punct_b" | "all_ok" | "neither" | null, "reason": "punct" | "en" | "content" | "other" | null}
{"time": "...", "session": "...", "rev": 1, "kind": "answer", "id": "answer-punct", "question": "punct", "value": "both_ok"}
{"time": "...", "session": "...", "rev": 1, "kind": "feel", "id": "feel-3", "seq": 3, "source": "example1" | "example2" | "own" | "speed",
 "config_id": "small/ja/typed", "input": "...", "left_context": "...", "text": "...", "ms": 803.1, "error": null}
```

ページ側の `client_time` も入る。`measure.jsonl` は `{"time", "session", "version", "engine", "phase", "index", "input", "config_id", "text", "ms", "finished", "error"}`。
`launch.jsonl` は `{"time", "session", "first", "serve_s", "ready_s", "error", "version"}`。`run.ps1` が開始時刻（`TRIAL_LAUNCH_T0`、UNIX ミリ秒）と
初回か（`TRIAL_LAUNCH_FIRST`、`.venv` が無かったら 1）を環境変数で渡し、サーバがモデルの読み込みを終えた時点で書く。
`serve_s` は run.ps1 の開始からサーバが応答できるまで（uv の導入・`uv sync` を含む）、`ready_s` はモデルの読み込みが終わるまで。

## configs.json と examples.json

configs.json:

| 項目 | 意味 |
|---|---|
| `models` | `small`・`xsmall` → `label`・`model`（HF の ID かローカルパス）・`device`・`k`・`w`（読みのコストの重み）・`f`（自然さの重み）。どちらも k=5（docs の xsmall の精度は k=5 の値） |
| `combo` | 組合せの id に使える `punct`・`en_case` の値 |
| `configs` | 比較の設定。`base`（models のキー）・`punct`・`en_case` |
| `steps` | 比較のステップ（`punct`・`en_case`）。`title`・`configs`（並べる設定）・`names`（結果ブロックでの呼び名）・`question` |
| `questions` | 質問カード（`punct`・`en_case`・`use`）。`text`・`note`・`provisional`（暫定値の呼び名）・`options`（`value`・`label`・`short`・選んだときの `punct`/`en_case`・`provisional`） |
| `reasons` | 「使えない」の理由のボタン |
| `feel_base` / `speed_base` | 体感と計測のモデル（small）と速度優先のモデル（xsmall） |

回答 → 組合せは `summary.combo_id`（ページの `comboId` も同じ規則）: 選んだ選択肢の `punct`・`en_case`、未回答は最後の選択肢（まだ決められない）の値。

config → CorePipeline の引数: `model`→`model`、`device`→`device`、`k`→`k`、`w`→`w_read`、`f`→`w_flu`、`punct`→`punct`、`en_case`→`en_case`
（core は `punct`・`en_case` を受け付ける。`"ja"` は打った句読点を全角に、`"insert"` は句読点を打たない入力に読点・句点・疑問符を補う（句読点 B）、
`"auto"` は文脈で決める。英字は `typed`／`registered`／`auto` を選んだ後に描画する。D7・D8）。
`CoreEngine` は `inspect.signature(CorePipeline.__init__)` を見て、受け付ける引数だけを渡す（受け付けない項目は `/api/info` の
`engine.ignored` に出し、ページ上部に注意を出す）。同じ `model` と `device` の組はモデルと辞書を1回だけ読み、
2つ目以降の `CorePipeline` は `__init__` の中の `ZenzScorer(...)` と `Dictionary.load()` を読み込み済みのものに差し替えて作る。
`finished` は `Result.candidates` が空でなく、`candidates()` の最良の score が `-inf` でないこと。

examples.json: `punct`・`en_case`（比較の例。`romaji` と `kind`）、`feel`（体感の例 2 つ。`punct`＝句読点を打った形、`nopunct`＝打たない形。
2 つ目は 1 つ目の続き）、`measure.final`（6 文。2 文は句読点なし）、`measure.speed`（3 文）。
英字の例は、**親が実コアで 3 方式の出力に差が出ることを確かめてから採用する**（差し替え候補: `pythonnoba-jonwo3.12niageta.`、
`scc-0210womitsukete.`。句読点のメモの候補: `kaimonogyuunyuutamagopan`）。

## 結果ブロック

```
romaji-slm trial <version> / <日時> / session <id>
機種: <CPU> / <論理コア> / <RAM>GB / <OS> / Python <版> / torch <版> / エンジン <core|mock>
初回起動: serve <s> / ready <s>（今回が初回でなければ「／ 今回の起動: serve <s> / ready <s>」）
【回答】句読点: A ／ B ／ どちらも問題ない（暫定 A）／ 未決（暫定 A）／ 未回答（暫定 A）
【回答】英字: 打ったまま ／ 登録語だけ ／ 自動 ／ どれも問題ない（暫定 打ったまま）／ 未決（暫定 打ったまま）／ 未回答（暫定 打ったまま）
【回答】使用感: 使ってみたい ／ 今は使いたくない ／ まだ判断できない ／ 未回答
体感した設定: small + punct=<..> + en_case=<..>（xsmall も試した: はい/いいえ）
計測: small + punct=<..> + en_case=<..> 6文: 各 <ms>, … ms, 最大 <ms>ms, 1秒超 <n>件 ／ small vs xsmall 3文: small <ms>, … ms / xsmall <ms>, … ms
過去の記録: <n> セッション・<n> 行（今回の集計には含めない）
未保存の記録: <n> 件（…）
--- 比較の記録 ---
句読点 1/3 "<入力>" A(punct_a)=<出力> <ms>ms | B(punct_b)=<出力> <ms>ms | 同一出力/差あり | B: 挿入なし/句点/読点/疑問符 | 回答: A / B / どちらも問題ない / どちらも使えない(<理由>) / 未回答
英字 1/3 "<入力>" 打ったまま(en_typed)=… | 登録語だけ(en_registered)=… | 自動(en_auto)=… | 同一出力/差あり | 回答: …
--- 体感の記録 ---
1. [small + punct=ja + en_case=typed] "<入力>" → <出力> <ms>ms 文脈=<n>字（例1）
```

- 冒頭の【回答】が本人の明示回答。比較の記録は補助（「一番いい」は相対評価、「問題ない」は無差別で、設定の採用への同意ではない）。
- 「B: …」は B の出力が A より多く含む記号（句点 。・読点 、・疑問符 ？）。失敗した出力は `(失敗:<原入力>)`。
- 計測は少数なので p95 は出さない。失敗した文は ms の後に `(失敗)`。最終の組合せは回答から決める（戻って答えを変えたら、
  ステップ 4 を開き直したときに新しい組合せで測る）。
- 「過去の記録」「未保存の記録」の行は該当があるときだけ出る。

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

`trial/` のうちページに要るファイル（`server.py`・`engine.py`・`summary.py`・`machine.py`・`configs.json`・`examples.json`・`static/*`・`run.ps1`）が
無ければ止める。それ以外で無いものは警告して続行し、最後に警告の一覧を出す。

## ThinkPad 側の手順

1. zip を解凍する（パスに `&` を含まない場所）。
2. `run.cmd` をダブルクリック。
   - `uv` が無ければ公式インストーラ（`powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"`）で入れる。
   - `uv sync --no-default-groups --group core`（torch CPU 版・transformers・marisa-trie・wordfreq。Python が無ければ uv が取る）。
   - `HF_HOME=<配布物>\models\hf`。同梱のモデルが2つともあれば `HF_HUB_OFFLINE=1`、無ければ外して初回取得（画面に「取得します」と出す）。
   - `uv run --no-sync python -m trial.server --open`。標準出力（URL・「準備できました」）は黒いウィンドウに、標準エラーは `trial\out\launch.log` に行く。
   - 失敗したらウィンドウを閉じずに、ログの末尾 30 行と `launch.log` の場所を出す。
3. ページの順にボタンを押し、最後に「結果をコピー」を押して貼って返してもらう。

## 既知の制限

- **本物の変換（CoreEngine + zenz）はクラウドでは動かしていない**（huggingface.co に届かない）。V0.2 の画面は MockEngine で
  通し操作して確かめた。開発機で `uv run python -m trial.server --open` を一度通す必要がある（例文が実コアで狙いどおりの差を出すかも）。
- **ThinkPad 用の `uv.lock` が無い**（この環境から download.pytorch.org に届かない）。開発機で pack すれば作られる。
  lock が無くても初回の `uv sync` が解決するが、ネットワークが要り、版が固定されない。
- `machine.py` の Windows 分岐（レジストリ・`GlobalMemoryStatusEx`・`SO_EXCLUSIVEADDRUSE`）と `run.cmd`→`run.ps1` は、開発機（Windows 11）で zip を展開して確認した（2026-10-15。初回 serve 5.1 s / ready 19.8 s、uv のキャッシュが温かい状態）。ThinkPad では未確認。
- 比較の待ち時間は 2〜3 設定の逐次実行の合計なので、比較のステップでは速さを見せない。速さは体感（単独の設定）と計測で見る。
- 同じ id の記録をサーバに保存できないまま再読込すると、ブラウザの localStorage が使えない環境では、その選択は失われる。
