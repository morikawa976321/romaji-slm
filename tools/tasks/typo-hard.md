# 依頼: 読み段で、難しい打鍵ミス2種を副作用なしに拾う

最初に CLAUDE.md、docs/READING.md（特に PR #10 で足した「実測」「効いた案」「効かなかった案」「未解決の論点」）、docs/decisions.md の「本人の方針（2026-10-15）」と D15・D16、core/reading.py、core/kana_lm.py、core/kanakanji.py、core/types.py、eval/reading_bench.py、eval/sets/user_real.jsonl、tests/test_core_reading.py を読むこと。文書・コメント・コミットメッセージは日本語。

## 本人の方針（原文。最優先の基準）

> 1つ目と2つ目は、直せるなら直したいが、副作用が出るなら慎重に行う必要がある

対象の2種（本人が体験版で実際に打った文 `kanariyoitoomowarerugahokanidonnnaikairyoutennngakangkaraereudaroukasokudonokousokukadarouka`、意図は「かなり良いと思われるが、他にどんな改良点が考えられるだろうか。速度の高速化だろうか」）:

1. **1語に打鍵ミスが3つ以上**: `kangaerareru` → `kangkaraereu`。今の読み段は 1 弧 1 編集・1 文 `max_edits=2` までなので経路が無い。
2. **余計な1字が別の正しいローマ字になる**: `donnna|i|kairyou` → 読み「ドンナイカイリョウ」→「異界量」。i を捨てる編集の経路はあるが、正しいローマ字のまま読めるので読みらしさの差が編集コストに届かない。

## 作るもの

### A. 評価セット（新規、先に作る）

- `eval/sets/typo_hard.jsonl`（60〜100 文）: 本人の用途（チャット・AI への指示・技術文・メモ。user_style.jsonl の文体）の文に、種類 1（1 語に 2〜3 個の打鍵ミス）と種類 2（余計な 1 字で別の正しいローマ字になる: 母音の重ね・子音の後の母音の差し込みなど）を入れたもの。各行 `romaji`・`reading`（意図した読み、zenz_input の形）・`meta.category`（`multi` / `extra_valid`）・`meta.clean_romaji`（打鍵ミスの無い版）。打鍵ミスは QWERTY の隣接・重ね打ち・転置・脱字など、実際に起きる形に限る（data/noise.py の隣接表を使ってよい）。`user_real.jsonl` の 1 文目を含める。
- 同じ文の打鍵ミスの無い版で「誤補正しないか」も測れるようにする（`clean_romaji`）。

### B. 読み段の改良（効いたものだけ残す）

候補（全部やる必要はない。効いた案・効かなかった案を両方 docs/READING.md に書く）:

- **PR #10 の未解決の論点 1**: 読み段の候補 20〜40 件を、辞書 Viterbi の変換コスト（`core/kanakanji.py` の `convert` の 1 位のコスト）で並べ直して上位 5 件を選ぶ（机上で AJIMEE の打鍵ミス上位 5 が 74% → 82%）。辞書の語で説明できない読み（異界量より改良点）が下がるはずで、種類 2 に効く見込み。時間が 1 件あたり何 ms 増えるかを必ず書く。関数として `core/reading.py` か新しい `core/preselect.py` に置き、`readings()` の既定の振る舞いは変えない（後段の `core/pipeline.py` への組み込みは親がやる）。
- **1 語に 2 編集**: 長い語（かな 4 字以上、または辞書の語に一致する区間）に限って 1 語 2 編集を許す。文全体の `max_edits` を 3 に上げる案も、上げた場合の誤補正とともに測る。
- **辞書の語を手がかりにした編集**: Mozc の読みの索引（`models/reading/mozc_readings.marisa`、PR #10 の `WordCost`）で、編集後に辞書の長い語ができる経路を安くする。

### C. 入れる条件（すべて満たすこと。満たさない案は残さず、数値だけ docs に書く）

`eval/reading_bench.py` で、変更前（main の今のコード）と変更後を同じ機械で測り、同じ表に並べる:

| 条件 | 基準 |
|---|---|
| AJIMEE きれいな入力 1 位 | 199/200 以上（今と同じ） |
| AJIMEE 正常入力の誤補正 | 1/200 以下（今と同じ） |
| user_style の typo 以外 62 文の誤補正 | 0 |
| typo_hard の `clean_romaji` での誤補正 | 今より増えない |
| raw_tokens（辞書に無い英字列）上位 5 | 36/39 以上（今と同じ） |
| user_style_reading 1 位 / 上位 5 | 35/40 / 40/40 以上 |
| 20〜39 字の所要 中央 / p95 | 変更前の 1.3 倍以下 |
| 目的: typo_hard の上位 5 | 上がること（種類別に出す） |
| 参考: AJIMEE 打鍵ミス 1〜2 個の上位 5 | 下がらないこと |

## 作業の規則

- ブランチ `claude/typo-hard` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。**push 先はこのブランチだけ。**
- **変えてよいのは `core/reading.py`・`core/english.py`・`core/kana_lm.py`・新規 `core/preselect.py`・`eval/reading_bench.py`・新規 `eval/sets/typo_hard.jsonl`・`tests/test_core_reading.py`（と新規のテスト）・`docs/READING.md`。** `core/types.py`・`core/pipeline.py`・`core/constrained.py`・`core/kanakanji.py`・`eval/converters.py`・`trial/`・`pyproject.toml`・`uv.lock` は変えない。`readings(keys, n, max_edits)` のシグネチャと既定の振る舞いを変えるなら、変える前後の数値を PR 本文に並べる。
- 認証情報・個人情報を書かない。外部サービスへの書き込みは PR の作成だけ。
- huggingface.co には届かない（zenz は使えない。後段込みの評価は親がやる）。素材は raw.githubusercontent.com から自動で取れる。
- 最終報告（PR 本文）: C の表（変更前・変更後）、残した案と捨てた案、追加した関数と後段への組み込み方の提案、`pytest` の結果、未解決の論点。
