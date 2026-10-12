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
### 微調整（方式3、2026-10-12）

学習: `train/finetune.py`、zenz-v3.1-small から、`data/out300k`（train 287,645件）で1周、lr 1e-4、1バッチ約 4096 トークン、RTX 5060 Ti で v1 約17分・v2 約26分。
評価環境は RTX 5060 Ti（ms/件は学習と GPU を共有した時間帯を含む参考値）。

| 変換器 | user_style | うち tech | うち typo | 試験分割 358件 | AJIMEE 完全 | AJIMEE ゆるい |
|---|---|---|---|---|---|---|
| 方式1（規則でカタカナ → 元の zenz） | 57.5% | 10.0% | 16.7% | 29.3% | **87.0%** | **88.0%** |
| v1: ローマ字 → 出力（`zenz-romaji`） | 50.0% | 20.0% | 33.3% | 26.8% | 28.5% | 34.5% |
| v2: ローマ字 → 読み → 出力（`zenz-romaji-reading`） | 60.0% | 20.0% | 33.3% | 37.2% | 58.0% | 74.0% |
| **v2 の読み → 元の zenz で変換（`reader+zenz`）** | **65.0%** | 20.0% | **50.0%** | **37.4%** | 67.0% | 86.0% |

試験分割は `data/out/test.jsonl`（打鍵ミス 30%・句読点省略 50% を含む生成データ）。

読み取れること:

- **v1 は元の zenz のかな漢字変換の知識を失った**（AJIMEE 87.0% → 28.5%。「浄土真宗事典」→「上土新周時点」）。入力の形を変えたことで、事前学習で得た「カタカナ読み → 表記」の対応が使えなくなった。
- **v2（読みを先に書かせる）でも変換の段は削られる**。ステップ1000時点で、AJIMEE の読みの段は 86.5% 正しいが、正解の読みを元の形式で与えても 72.5%（元の zenz は 87%）だった（`eval/reading_check.py`）。
- **読みの段だけを v2 に任せ、変換は元の重みの zenz に任せる組み合わせが最もよい**。打鍵ミスを含む入力では方式1を上回り（typo 16.7% → 50.0%、試験分割 29.3% → 37.4%）、AJIMEE でも記号を無視すれば方式1に近い（86.0% 対 88.0%）。完全一致の差（67.0% 対 87.0%）は主に記号の全角・半角（`(` と `（`、数字）。
- **英単語を含む文（tech）は 20% のまま**。読みの段で英単語をカタカナに崩す（`githubnipushshiteoite` → 読み「ギテュビンイプシ…」）。学習データの英字を含む文は約 8% で、Wikipedia の固有名詞が中心。英単語混じりの口語・技術文を学習データに足す必要がある。
- 生成トークンは方式1の約2倍（読み + 出力。AJIMEE で 23 → 56 トークン）。DESIGN.md の速度の模擬（95M・P コア4基で入力96＋生成32トークンが 118ms）から見て、ThinkPad でも1秒の要件に収まる見込みだが、ThinkPad では未測定。
### 再設計の基準値（工程1、2026-10-12）

評価データを作り直した後の基準。user_style は 73 文（tech 20・typo 11・collision 7 など）、gen_test は生成した試験分割から学習データと出力が重なる 21 件を除いた 337 件（`eval/sets/gen_test.jsonl`。clean 121・noisy 96・omit_punct 120）。英語保持は、正解に含まれる英単語のうち出力に残った割合（大小・空白は無視）。RTX 5060 Ti。

| 変換器 | user_style 完全 / ゆるい | tech | typo | collision | 英語保持（user_style） | gen_test 完全 / ゆるい | gen_test 英語保持 |
|---|---|---|---|---|---|---|---|
| 方式1（規則でカタカナ → 元の zenz） | 53.4% / 53.4% | 5.0% | 9.1% | 71.4% | 0.0% | 29.1% / 47.8% | 30.3% |
| v2 の読み → 元の zenz（`reader+zenz`） | 60.3% / 67.1% | 15.0% | 36.4% | 71.4% | 32.5% | 35.6% / 53.4% | 67.4% |

工程4（凍結 zenz で選ぶ）の目安（docs/REDESIGN.md）は、user_style 70% 以上・tech 60% 以上・typo 50% 以上。
### 変換コア（工程2〜4、2026-10-12）

`--converter core:k=3,w=1.0`（core/pipeline.py）: 読みの上位 k 件（core/reading.py）それぞれについて、Mozc 辞書ラティスで出力を制約した zenz-v3.1-small の生成（core/constrained.py、数の読み→数字の弧つき）を行い、zenz の対数確率 − w × 読みのコスト で1つを選ぶ。読みの候補がすべて打鍵の修正を含むときは、修正しない最良の読みも加える。学習は一切していない（zenz は凍結）。RTX 5060 Ti、ms/件は参考値。

| 設定 | user_style 完全 / ゆるい | tech | typo | 英語保持 | 誤補正 | gen_test 完全 / ゆるい | gen_test clean 誤補正 | AJIMEE（ローマ字） | ms/件 中央値 |
|---|---|---|---|---|---|---|---|---|---|
| 方式1（基準） | 53.4% / 53.4% | 5.0% | 9.1% | 0.0% | — | 29.1% / 47.8% | — | 87.0% | 138 |
| core k=1, w=1.0 | 71.2% / 75.3% | 60.0% | 27.3% | 75.4% | 0.0% | 29.4% / 48.7% | 9.1% | 85.0% | 117 |
| **core k=3, w=1.0** | **76.7% / 83.6%** | **70.0%** | 45.5% | 88.1% | 1.6% | 31.8% / 52.5% | 9.9% | 85.0% | 325 |
| core k=5, w=0.5 | 78.1% / 84.9% | 75.0% | 54.5% | 90.5% | 3.2% | 32.6% / 52.8% | 19.0% | 83.5% | 920 |
| core k=5, w=2.0 | 75.3% / 82.2% | 70.0% | 36.4% | 88.1% | 0.0% | 32.0% / 51.9% | 11.6% | 85.0% | 518 |

正解の読みを与えた変換の段だけ（`eval/constrained_check.py`、AJIMEE、左文脈あり）: 辞書だけの1位 53.0% → zenz の N-best 再順位 82.5%（N=100）→ 制約付き生成 83.5% → 数字の弧を足して 85.5%（記号を無視 86.5%）。無制約の zenz は 87.0%。

読み取れること:

- 工程4の目安（user_style 70% 以上・tech 60% 以上・typo 50% 以上）は、k=3 で全体と tech を満たし、typo は 45.5%（k=5, w=0.5 なら 54.5% だが誤補正が増え、時間が約2倍）。
- 制約付き生成なので、出力は辞書・かな・英字・数字の弧でできており、読みと無関係な語（遅く→お酒、ユウイサ→優位性）は構造上出ない。
- **残る問題: 読みの選択に zenz がほとんど効いていない。** zenz は「この読みをどう表記するか」を採点するので、読みをそのまま写すような出力はどの読みでも尤度がほぼ 0 になる（`scc-0210` の読みは lm −0.00、修正した `サッカー0210` も −0.10）。読みの選択は事実上、読みの段のコストで決まる。生成した試験データ（Wikipedia 由来）の誤補正 9.9% は、辞書に無い略語・型番（cdma、jr、scc）を打鍵ミスとして直したもの。出力した日本語の自然さ（左文脈の位置での zenz の尤度など）を読みの比較に足すのが次の課題。
- 英単語リストにある語は保持できる（user_style 88.1%）。リストに無い英字（Wikipedia の固有名詞）は保持率が低い（gen_test 28%）。
## テスト

`uv run pytest`（`tests/test_eval_*.py`）: ローマ字 → カタカナ（nn・促音・揺れ・往復）、指標、プロンプト雛形、データ読み込み・集計・書き出し、
`HFCausalLM`（ランダム初期化の極小 GPT-2 と文字単位トークナイザ。torch と transformers が入っているときだけ）。
