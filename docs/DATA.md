# 学習データ（ローマ字打鍵列 → 日本語）

DESIGN.md 方式3の学習データを作るパイプライン（`data/`）の仕様と現状。

> **現状の注意（2026-10-11 作成時点）**
> このパイプラインを作ったクラウドセッションは Hugging Face（huggingface.co）に接続できなかった（プロキシが 403。`datasets` の streaming は `ProxyError: 403 Forbidden` で失敗）。
> そのため **zenz-v2.5-dataset の実データは1件も見ていない**。「input の読みに句読点・記号・英字がどう入っているか」の確認は未実施で、
> パイプラインとテストは、zenz データの形式を模した fixture（`data/fixtures/zenz_like.jsonl`、205件）で完成させた。
> 下の件数・比率は **fixture の値** であり、実データでの除外比率ではない。ローカルで `--source hf` を回して差し替えること。

## 元データとライセンス

| 素材 | 用途 | ライセンス |
|---|---|---|
| `Miwa-Keita/zenz-v2.5-dataset`（input=カタカナ読み、output=日本語、left_context） | 本番の元テキスト。streaming で読み、全量はダウンロードしない（36GB） | 大半 CC BY-SA 4.0。llm-jp-corpus v3 の CC 部分は ODC-BY（DESIGN.md の調査値） |
| SudachiPy 0.7.0 | 出力文の形態素解析（読みの付け直しと照合） | Apache-2.0（パッケージのメタデータ） |
| SudachiDict-core（`sudachidict_core`） | 同上の辞書 | Apache-2.0（パッケージのメタデータ）。辞書の元データ（UniDic 等）の帰属表示は LEGAL ファイルで未確認 |
| `data/fixtures/zenz_like.jsonl` | 実データに届かないときの代用 | 手書きの短文（このリポジトリの成果物）。input は SudachiPy の読みから生成 |

生成物のライセンスは元データに従う（CC BY-SA 4.0 前提で公開する想定。DESIGN.md「未確認の論点」参照）。

## 処理の流れ

`uv run python -m data.build --limit N --out data/out/ --seed 0 [--source hf|fixture|JSONLパス]`

1. **読み込み**: `hf` は `datasets.load_dataset(..., streaming=True)` に shuffle バッファ（既定 10000）を掛けて先頭から読む。`--limit` は出力サンプル数の上限。
2. **解析**（`data/reading.py`）: output を英数字の連続とそれ以外に切り、英数字はそのまま（NFKC→小文字）打つ綴りにする。残りを SudachiPy（mode C）で解析して読みを得る。
   英数字を先に切るのは、SudachiDict が「1日」「3回」「10分」を数字込みの1語にするため。数字の直後の助数詞は、数字を漢数字に仮置きして解析し、読みから漢数字の分を落とす（年→ネン）。
3. **zenz の読みとの照合**（`align_zenz`）: 解析器の読み（カタカナと長音だけ比べる。助詞は／へ／を は ハ・ワ、ヘ・エ、ヲ・オのどちらでも可）が zenz の input と全体で一致するか調べる。
   英数字トークンは読みが不明なので任意の長さを許す。漢字を含む語に限り最大2語まで、読みを zenz の該当部分に差し替えてよい（私＝ワタクシ／ワタシのような揺れを吸収）。
   - 一致 → `reading_from: "zenz"`
   - 不一致で、英数字を含む → 解析器の読みで残す（`"analyzer"`）。対応の取れない文を除外せず使う側
   - 不一致で、英数字を含まない → **除外**（どちらの読みが正しいか判断できない）。`--keep-mismatch` で解析器の読みで残せる
4. **区切り**: 1文ずつのサンプルにする（。！？の直後。括弧内の句点では切らない）。60字（`--max-chars`）を超える文は読点で切って詰め直し、それでも超える区間は捨てる。
   2文目以降の left_context は、元の left_context に前の文までの output を足したもの。left_context は末尾 200 字（`--max-left`）に切る。
5. **ローマ字化**（`data/romaji.py`）: 読みをワープロ式ローマ字に。表記揺れは乱数（`random.Random("{seed}:{input}\t{output}")` でレコードごとに決定的）。
   - shi/si, chi/ti, tsu/tu, fu/hu, ji/zi, ja/jya/zya, sha/sya, cha/tya/cya ほか。ファ行は fa/hwa
   - 「ん」: 母音・y・n の前、および文末・英字/記号の前は必ず nn。それ以外は n 70%・nn 30%
   - 促音: 次の綴りの子音を重ねる（3%で xtu/ltu）。子音が続かないときは xtu/ltu
   - 小書き: x 80%・l 20%。キャ→kixya のような別打ちが 4%
   - 揺れの重みは「よくある打ち方」の目安であって実測ではない
   - `eval/kana.py` の表（`_BASE`, `_DIGRAPH`）を import して使う。eval 側は変更していない（揺れなしの標準綴りは `kata_to_romaji` と一致するテストあり）
6. **記号**（`data/symbols.py`）: 、→`,` 。→`.` ー→`-` ！？：（）など全角 ASCII は NFKC で半角、「」『』【】→`[` `]`、・→`/`、〜→`~`、…→`...`、全角数字→半角。空白は打たない。出力側は元の表記のまま。
   対応のない記号（●、※ など）を含む文は除外（`excluded:unmapped_symbol`）。
7. **ノイズ**（`data/noise.py`）: サンプルの 30%（`--noise-rate`）に 1〜2 個。QWERTY の隣接キー置換・脱字・余計な1字（重複か隣接キー）・隣接2字の入れ替え。記号の位置は変えない。3字未満には入れない。
   `meta.noise` に `"sub:3:a>q"` `"drop:5:u"` `"insert:2:k"` `"swap:7:to"`（操作:位置:内容）の形で記録。
8. **分割**: 元データ1件の `sha256(input\toutput)` から決定的に train 96% / valid 2% / test 2%。同じ元データから出た文は同じ分割に入る。

出力 `{out}/{train,valid,test}.jsonl` と `stats.json`。1行1件:

```json
{"romaji": str, "output": str, "left_context": str, "meta": {"source": str, "noise": [..], "reading_from": "zenz"|"analyzer"}}
```

`meta.source` は元データの `source` 列があればそれ、なければデータセット名（fixture は `"fixture"`）。

## 件数と除外比率

### fixture（205件、`--source fixture --limit 20000 --seed 0`）

| 項目 | 件数 |
|---|---|
| 読んだ元データ | 205 |
| 採用した元データ | 203（99.0%） |
| 出力サンプル | 212（train 202 / valid 4 / test 6） |
| 除外: 読みの食い違い（英数字なし） | 2（1.0%）。わざと input を書き換えた4件のうち、残り2件は照合で救われた |
| `reading_from: zenz` / `analyzer` | 203 / 0 |
| zenz の読みに差し替えた語を含む元データ | 7（語数 10） |
| ノイズを入れたサンプル | 68（32.1%） |

`data/samples/sample_200.jsonl` は、この212件から seed 0 で選んだ200件（`python -m data.make_sample`）。
**fixture の input は解析器から作ったので、ほぼ全件が一致するのは当然で、実データの除外比率の目安にはならない。** `analyzer` が 0 件なのも、fixture の英字部分が「任意長」を許されて照合を通るため。
実データでの「読みの食い違い」「analyzer 採用」の比率は未測定。

### 実データ

未取得（上の注意）。ローカルでの確認手順:

1. `uv run python -c "from datasets import load_dataset; ds = load_dataset('Miwa-Keita/zenz-v2.5-dataset', split='train', streaming=True); [print(r) for _, r in zip(range(300), ds)]"` で数百件を見て、input に句読点・記号・英字がどう入っているかをここに書く。
2. `uv run python -m data.build --limit 20000 --out data/out/ --seed 0` を回し、`data/out/stats.json` の比率をここに書く。

## 既知の限界

- **読みの誤り**: SudachiDict の読みは文脈を見ない語がある。私→ワタクシ、日本→ニホン/ニッポン、数字直後の助数詞（3日→「カ」、3分→「フン」。実際は ミッカ・サンプン）など。zenz の input と照合して食い違えば、漢字語2語までは zenz 側に寄せ、それ以上は英数字なしの文を捨てる。ただし解析器と zenz が**同じ誤りをしたら**そのまま学習データに入る。
- **英数字の読みは検証できない**: 英数字を含む文は、英数字部分の zenz 側の読みが分からないので、前後の日本語部分が合えば通す。英数字に隣接する語の読みの誤りは、1語ずれても気づきにくい。
- **英字区間**: 入力は元の綴りを小文字にしたもの。「pull request」→`pullrequest`（空白なし）。ハイフン付き（`GPT-2`→`gpt-2`）・記号付き（`llama.cpp`）は ASCII 記号がそのまま入る。カタカナ語の英語表記（「コンピュータ」を実際は computer と打つ人）は扱わない。
- **数字・助数詞**: 数字は半角にして打つ。数字の前後で解析を切るので、「第2章」「2.1%」のような文脈をまたぐ読みは単純。
- **「ん」の末尾**: 読みの末尾（文末・英数字の直前）の「ん」は nn。「100円」→`100enn`。実際の打鍵は `100en` と打つ人も多い。
- **解析の切れ目**: 英数字・数字の前後で解析を切るため、その前後の語は文脈なしで読みが決まる（品詞判定に影響しうる。実データでの影響は未測定）。
- **括弧**: 括弧内に句点がある文は切らない。ただし開き括弧だけの文などで深さがずれると、その文は切れないまま長さ制限で捨てられうる。
- **ノイズ**: 隣接キー判定は ANSI 配列の行ずらしを近似した幾何。実際の打鍵ミスの統計には基づいていない。
- **揺れの重み・ノイズ率**: 目安であって実測ではない。
- 速度の数値は扱っていない（このパイプラインは学習データ生成だけ）。

## 環境メモ

- `pyproject.toml` に dependency group `data`（`datasets`, `sudachipy`, `sudachidict-core`）と、dev に `pytest` を足した。
  **`uv.lock` は更新できていない**: クラウド環境からは `download.pytorch.org`（torch の index）に届かず、`uv lock` / `uv add` が失敗した。ローカルで `uv lock` を実行すること。
  動作確認は、PyPI だけに繋がる別の仮想環境（`uv pip install pytest datasets sudachipy sudachidict-core`）で行った（Python 3.13）。
- テスト: `uv run --group data pytest`（`tests/test_data_romaji.py` は SudachiPy なしでも動く）。

## 失敗例と判断に迷った点

1. **「1日」「3回」が1語で返る**: 最初の実装は英数字を含む形態素を混在として除外した。fixture 205件のうち「1日に3回、薬を飲む。」で即座に落ちて気づいた。英数字を先に切り出す方式に変えたが、すると「2024年」の「年」が単独解析になり「トシ」と読まれた（`2024toshida`）。漢数字の仮置きで「ネン」になるようにした。それでも「3分」は「フン」（本来プン）、「3日」は「カ」（本来ミッカ）になる。
2. **私＝ワタクシ**: 照合の最初の版は完全一致だけで、「私は使う」に zenz の input「ワタシワツカウ」が来ると落ちた。実データでは頻出のはずなので、漢字語2語までの差し替えを入れた。差し替え数に上限を置いた根拠は薄い（2は勘）。実データで食い違い語数の分布を見て決めるべき。
3. **●の読み**: SudachiDict は「●」にも読みを返す。差し替えの許可が「ひらがな・カタカナ以外を含む語」だったため、「●テスト」が `tesuto` として通ってしまった。記号は JP 文字クラスで弾くようにした。
4. **は→ワ**: 照合用の読みを発音どおり（ワ）にしたら fixture の input（ハ）が39件落ちた。実データの input がどちらかは未確認なので、両方通す。**打鍵はどちらの場合も表記どおり（ha）**。
5. **解析器採用 vs 除外**: 英数字を含む文で照合が通らなくても除外せず `analyzer` にした（依頼どおり）。英数字を含まない文は除外にした。ここは「読みを付け直す」でもよかったが、zenz と解析器が食い違ったとき解析器を信じる理由がない。実データの比率を見て、`--keep-mismatch` を既定にするか決める。
6. **sample_200 と分割**: 見本は train/valid/test をまとめて選んでいるので、そこから分割は読み取れない。
7. **ノイズ**: `sub:3:a>q` のように、実際には打ちにくい組（a と q は隣接だが、ホームポジションの運指では間違えにくい）も出る。重みづけはしていない。
8. **fixture は手書きの短文205件で、1件が1文のものがほとんど**。60字超の分割は単体テスト（`test_split_long_sentence_and_left_context_chain`）でしか通っていない。
