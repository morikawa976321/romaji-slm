# 評価

どの変換器（ローマ字 → 日本語）にも同じ手順で使える評価ハーネス。コードは `eval/`、テストは `tests/test_eval_*.py`。

## 使い方

```
uv sync --group eval
# 変換器 × セット。結果は eval/out/<変換器>__<セット>.jsonl（1件1行）と .md（集計表）
uv run python -m eval.run --converter zenz-baseline --set ajimee --set ajimee-noctx --set user_style --device cpu
uv run python -m eval.run --converter zenz-baseline --set ajimee --limit 20      # 少数で試す
uv run python -m eval.run --converter romaji2ja --set user_style                # 配布元の正規化あり
uv run python -m eval.run --converter hf --model <パス|HF ID> --template zenz-v3-romaji --set file:data/test.jsonl
uv run python -m eval.tokenizer_survey --model Miwa-Keita/zenz-v3.1-small       # 語彙調査
uv run pytest
```

### 変換器

共通インターフェースは `convert(romaji, left_context="") -> str`（`eval/converters.py`）。`convert_detailed` は加えてプロンプトと生成トークン数を返す。
実体は `HFCausalLM`（Hugging Face の因果LM。貪欲法で EOS まで）＋プロンプト雛形。

| `--converter` | モデル | 雛形 |
|---|---|---|
| `zenz-baseline`（方式1） | `Miwa-Keita/zenz-v3.1-small` | `zenz-v3`（ローマ字を規則でカタカナにして渡す） |
| `zenz-xsmall-baseline` | `Miwa-Keita/zenz-v3.1-xsmall` | `zenz-v3` |
| `zenz-romaji`（微調整後） | `--model` で指す | `zenz-v3-romaji` |
| `romaji2ja` | `models/romaji2ja` があればそれ、なければ `limoXD/romaji2ja` | `romaji2ja-normalized`（配布元の正規化を通す） |
| `romaji2ja-raw` | 同上 | `romaji2ja`（打鍵列そのまま） |
| `hf` | `--model` | `--template` |

### プロンプト雛形（`eval/prompts.py`）

| 名前 | 形 |
|---|---|
| `romaji2ja` | `{romaji}` |
| `zenz-v3` | `U+EE02 {left_context} U+EE00 {kana} U+EE01` |
| `zenz-v3-romaji` | `U+EE02 {left_context} U+EE00 {romaji} U+EE01` |

zenz-v3 の形は [zenzai.md](https://github.com/ensan-hcl/AzooKeyKanaKanjiConverter/blob/main/Docs/zenzai.md) の用語欄
（`<context><input_katakana><output></s>`、左文脈を前置）に合わせた。モデルは `U+EE01` の直後から生成し `</s>` で止まる。
**文書から確認できていない点**（実機で確かめる）:

- 左文脈が空のとき `U+EE02` を省くか（実装は省く＝v1 形式）。
- トークナイザが BOS などを自動で付けるか。ハーネスは `add_special_tokens=False` で呼ぶ（`HFCausalLM(add_special_tokens=...)` で変更可）。`eval.tokenizer_survey` の末尾に両方のトークン列を出す。
- 左文脈の長さ上限（azooKey 側に切り詰めがあるかもしれない）。ハーネスは切り詰めない。
- `` 以降（プロフィール・トピック・スタイル・設定）は使っていない。

### ローマ字 → カタカナの規則（`eval/romaji_kana.py`、ベースライン用）

`eval/kana.py`（カナ → ローマ字）の逆。最長一致で、nn・n'・促音（同子音の重なりと `tch`）・小書き（x/l）・ワープロ式の揺れ（si/shi、ti/chi、tu/tsu、hu/fu、zi/ji、sya/sha/tya/cha…）・外来音（fa、thi、dhi、twu、wi、va…）を扱う。
ASCII 記号は全角にする（`,`→`、` `.`→`。` `-`→`ー` `?`→`？` `!`→`！` `[]`→`「」` `/`→`・` `()`→`（）` `:`→`：` `~`→`〜`）。変換できない英字・数字はそのまま残す。

「ん」の規則は Microsoft IME・Google 日本語入力に合わせた: `nn` は常に「ン」、`n` は母音・y が続けば な行（`nya`→ニャ）、それ以外（子音の前・語末）は「ン」、`n'` も「ン」。
したがって `kanna`→カンア、`kannna`→カンナ、`konnichiha`→コンイチハ（こんにちは は `konnnichiha`）。
`eval/kana.py`（カナ → ローマ字）は「ン」+母音・y・n を `nn` と書く（`honnya`、`konnnichiha`）。本人は記号を最小限にして打つので `n'` は使わない（2026-10-12）。この組み合わせで AJIMEE 200件すべてが「カナ → ローマ字 → カナ」で元に戻る（テストで確認）。

## 評価セット

| `--set` | 内容 | 件数 |
|---|---|---|
| `ajimee` | [AJIMEE-Bench](https://github.com/ensan-hcl/AJIMEE-Bench) `JWTD_v2/v1/evaluation_items.json`。カタカナ input を `kana.py` でローマ字にし、**`context_text` を左文脈に渡す** | 200 |
| `ajimee-noctx` | 同じ項目で左文脈を渡さない | 200 |
| `user_style` | 本人の用途を模した文（`eval/sets/user_style.jsonl`） | 40 |
| `file:<path>` | 共有 JSONL 形式の任意のファイル（学習データ側の test 分割など） | — |

AJIMEE のカテゴリは、元の項目が文脈を持つか（`with_context` 100 / `without_context` 100）。文脈を渡すかどうかに依らず同じ。
AJIMEE のデータは CC BY-SA 3.0（コード CC0）。初回実行時に `raw.githubusercontent.com` から `models/ajimee/evaluation_items.json` へ取得する（`models/` は .gitignore。データ本体はコミットしない）。

### 共有 JSONL 形式

```json
{"romaji": "ashitanokaigi,nanjikaradakke?", "output": "明日の会議、何時からだっけ？", "left_context": "", "meta": {"category": "chat", "alternatives": ["明日の会議、何時からだっけ?"]}}
```

`left_context` と `meta` は省略可。`meta.category` はカテゴリ別集計に、`meta.alternatives` は追加の正解に使う（`output` と合わせて複数正解）。

## 指標

| 指標 | 定義 |
|---|---|
| 完全一致 | 出力が正解のどれかと文字列として一致 |
| ゆるい一致 | NFKC・英字の小文字化のうえ、句読点・記号（Unicode の P*）、空白、制御文字、長音「ー」、チルダ類を捨てて、正解のどれかと一致 |
| MinCER | 正解ごとの文字誤り率（編集距離 ÷ 正解の文字数）の最小。表は件ごとの値の平均 |
| ms/件 | `convert_detailed` 1回の所要時間（時計時間）。平均と中央値。計測前に2回空回しする |
| 生成トークン | EOS までに生成したトークン数（EOS を含む）の平均 |

速度（ms）は**測った機械での参考値**で、要件（ThinkPad P14s Gen 6、Core Ultra 7 255H 上で 20〜30字を1秒以内）の判定には使わない。
各 `.md` に CPU 名・論理 CPU 数・torch のスレッド数を記録する。

## 結果

### zenz のトークナイザの語彙調査（2026-10-12）

`uv run python -m eval.tokenizer_survey --model Miwa-Keita/zenz-v3.1-small`

- トークナイザは `GPT2Tokenizer`（バイト単位 BPE、語彙 6000）。「の」「は」などのよく出る文字・字の並びが1トークン。
- **ラテン小文字 a-z・数字 0-9・ASCII 記号 32 種（`!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~`）はすべて語彙に単独であり、UNK にならず、往復で戻る。** 大文字 A-Z も同じ。
- 空白はエンコードで消える（トークン数 0）。入力は空白を打たない前提なので影響しない。
- zenz の特殊文字 `U+EE00〜EE07` は単独トークンではなく、UTF-8 の3バイトそれぞれが1トークン（計3トークン）。zenz 本体もこの形で学習されているので問題ない。

**結論: ローマ字を直接入力する微調整に、語彙の拡張は要らない。**

### ベースライン（方式1: 規則でカタカナ → 元の zenz、2026-10-12）

`uv run python -m eval.run --converter zenz-baseline --set ajimee --set ajimee-noctx --set user_style --device cuda`

環境: RTX 5060 Ti（CUDA）、torch 2.11.0+cu128。ms/件は参考値で、要件判定には使わない。

| モデル | セット | 完全一致 | ゆるい一致 | 平均 MinCER | ms/件 中央値 |
|---|---|---|---|---|---|
| zenz-v3.1-small（90M） | ajimee（文脈あり） | 87.0% | 88.0% | 0.037 | 120 |
| | ajimee-noctx | 84.5% | 85.0% | 0.022 | 118 |
| | user_style | 57.5% | 57.5% | 0.109 | 113 |
| zenz-v3.1-xsmall（22.5M） | ajimee（文脈あり） | 70.0% | 71.0% | 0.066 | 62 |
| | ajimee-noctx | 65.5% | 66.5% | 0.053 | 62 |
| | user_style | 55.0% | 55.0% | 0.128 | 57 |

user_style のカテゴリ別（small）: academic 87.5% / chat 87.5% / memo 87.5% / **tech 10.0%** / **typo 16.7%**。

読み取れること:

- 英字を含まない普通の文なら、規則でカタカナにするだけでも small で9割近く当たる。左文脈は効く（文脈を持つ100件で 78% → 83%）。
- **英単語を含む文（tech）と打鍵ミス（typo）はほぼ全滅する。** 英字が規則でカタカナに崩れる（`githubnipushshiteoite.` → 「ぎてゅbにプshしておいて。」、`pullrequestwotsukuttekure.` → 「プッlレクエsトォ作ってくれ。」）、1字の打ち間違いがそのまま出る（`arigatpu` → 「ありがtぷ」）。微調整で埋めるべき差はここ。
- 「ん」の打ち方を規則に合わせないと崩れる（`jikkennokekka` は nn を「ン」と読むので「実験終結日」になる）。微調整後のモデルには、どちらの打ち方でも文脈で読むことを期待する。

結果の全件は `eval/out/zenz-baseline__*.jsonl`（コミットしない）。
## テスト

`uv run pytest`（`tests/test_eval_*.py`）: ローマ字 → カタカナ（nn・促音・揺れ・往復）、指標、プロンプト雛形、データ読み込み・集計・書き出し、
`HFCausalLM`（ランダム初期化の極小 GPT-2 と文字単位トークナイザ。torch と transformers が入っているときだけ）。
