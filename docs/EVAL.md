# 評価

どの変換器（ローマ字 → 日本語）にも同じ手順で使える評価ハーネス。コードは `eval/`、テストは `tests/test_eval_*.py`。

> **現状（2026-10-11）**: ハーネスとテストは完成。ただしこのクラウドセッションは `huggingface.co` と `download.pytorch.org` への接続が組織のネットワーク方針で遮断（403）されており、
> **トークナイザの語彙調査とベースラインの実測はまだ実行できていない**。下の「結果」は空欄で、数値は書いていない。
> 取得を許可した環境（またはローカル）で、下記のコマンドを実行して埋める。

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

「ん」の規則は IME に合わせた: 母音・y が続くなら na 行（`nya`→ニャ）、`nn` は後ろが母音・y でなければ 2 文字で「ン」、`nna`→「ンナ」、`n'` も「ン」、語末の `n` も「ン」。
したがって「ン」+ア行・ヤ行は `n'` で書く。

> **`eval/kana.py` を直した**: 従来は「ン」+母音・y を `nn`+母音（`honnya`）と書いたが、IME ではこれは「ほんにゃ」と読まれる。
> AJIMEE 200件のうち17件が該当し、romaji2ja の実測（DESIGN.md）ではこの綴りの入力を使っていた。今は `hon'ya` と書く。
> 直したあと、AJIMEE 200件すべてで「カナ → ローマ字 → カナ」が元に戻る（テストで確認）。DESIGN.md の romaji2ja の数値と本書の数値は、この17件の分だけ入力が違う。

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

### zenz の文字単位トークナイザの語彙調査

**未実施**（`huggingface.co` が遮断）。`uv run python -m eval.tokenizer_survey --model Miwa-Keita/zenz-v3.1-small` を実行し、出た表をここに貼る。
出力は a-z、0-9、ASCII 記号（空白含む）、大文字、特殊文字 `U+EE00〜EE07` のそれぞれについて、語彙に単独であるか・UNK になるか・トークン数・往復で戻るか。
微調整で語彙拡張が要るかは、この表で UNK になる文字があるかで決まる。

### ベースライン（方式1: 規則でカタカナ → 元の zenz-v3.1-small）

**未実施**（同上）。

| セット | 完全一致 | ゆるい一致 | 平均 MinCER | ms/件（参考） |
|---|---|---|---|---|
| ajimee（文脈あり） | — | — | — | — |
| ajimee-noctx | — | — | — | — |
| user_style | — | — | — | — |

測定環境（CPU 名・スレッド数）は結果の `.md` から転記する。

## テスト

`uv run pytest`（`tests/test_eval_*.py`）: ローマ字 → カタカナ（nn・促音・揺れ・往復）、指標、プロンプト雛形、データ読み込み・集計・書き出し、
`HFCausalLM`（ランダム初期化の極小 GPT-2 と文字単位トークナイザ。torch と transformers が入っているときだけ）。
