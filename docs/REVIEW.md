# 利用履歴の振り返り（誤変換を探してユーザー辞書を育てる）

本人の決定（2026-10-17、decisions.md D32）: 本人の利用履歴を AI が定期的に読み、誤変換を探して、それを直すユーザー辞書を作る。本人の手間は増やさない。どこまで読んだかはマーカーで分かるようにする。ops-daily に入れ、最新の Opus・effort medium で動かす。

この文書は、振り返りを行う AI（ops-daily から起動される `claude -p`、または対話セッション）への手順と、毎回の記録を兼ねる。

## 置き場所（本人が打った文を含むものは、リポジトリに入れない）

| 物 | 場所 |
|---|---|
| 常駐入力の記録 | `%LOCALAPPDATA%\romaji-slm\history.jsonl`（項目は IME.md「実利用の記録」） |
| マーカー | `%LOCALAPPDATA%\romaji-slm\review-state.json` の `reviewed_until`（最後に振り返った行の `time`） |
| 作業フォルダ | `%LOCALAPPDATA%\romaji-slm\review\<日付>\` |
| 本人の実入力の評価セット | `%LOCALAPPDATA%\romaji-slm\review\real_local.jsonl`（回を重ねて足す） |
| ユーザー辞書 | `%LOCALAPPDATA%\romaji-slm\user_dict.tsv`（書式は `core/userdict.py` の冒頭） |
| 毎回の記録 | この文書の「記録」（件数・足した語・評価の結果だけ。本人の文は書かない） |

常駐の変換サーバは、`user_dict.tsv` が更新されると次の変換で読み直す。入れ直し・再起動は要らない。

## 手順（1 回分）

1. **読み出す**: `uv run python -m tools.review_history extract`。マーカー以降の行が `review\<日付>\new.jsonl` に出る。表示された `until` を控える。行が 0 なら、記録に「新しい行なし」と書いて終える。
2. **誤変換を探す**: `new.jsonl` を読む。手がかりの `flags` が付いた行から読む。
   - `chose_other`: 2 位以下を選んだ。
   - `cancel`: Esc で捨てた。
   - `restore`: 戻した。
   - `retype`: 確定の直後に Backspace で消して打ち直した。`prev_text` が誤りで、次の確定が直した形。
   - `retyped_next`: すぐ後に似た打鍵列を打ち直した。
   - `same_romaji_next`: 同じ打鍵列が続いた。

   手がかりの無い行も、出力が本人の意図（前後の行から読む）に合っているかを確かめる。誤変換ごとに、打鍵列・出力・本人が意図したとみられる文・誤りの型を、`review\<日付>\findings.jsonl` に書く。誤りの型は EVAL.md の仕分け（同音異義語・英単語・打鍵ミス・記号・区切り…）に揃える。

   - 意図が読めない行は推測で埋めない（「不明」とする）。
   - 誤りでない表記の揺れ（方/ほう など）は数えない。
3. **辞書の候補を作る**: 直せる誤りだけを `review\<日付>\proposed.tsv` に書く（書式は `core/userdict.py` の冒頭）。
   - 本人が英字のまま使う語で、カナ・漢字に化けたもの → `en`。日本語と紛れる語（main/毎、node/ので）は加点を小さく（0.3〜1.0）から。
   - 辞書に無い語、または本人の分野で決まって使う漢字 → `word`（読み・表記・コスト）。
   - 同じ綴りの打ち間違いが 2 回以上あったもの → `fix`（打鍵・意図）。1 回だけのものは足さない（直すべきでない入力まで直す恐れ）。
   - 同音異義語で、文脈によって正解が変わるもの（移行/以降）は辞書では直らない。findings に残すだけにする。
4. **評価の組を足す**: 意図がはっきりした誤変換と、正しく変換できた行の一部（同じ数くらい）を、`review\real_local.jsonl` に足す。書式は評価セットと同じ `{"romaji", "output", "left_context": "", "meta": {"category": "real", "source": "<日付>"}}`。
5. **確かめる**: 今の辞書と、提案を足した辞書の 2 通りで評価し、`eval.diff` で比べる（GPU があれば `--device cuda`）。
   - 提案を足した辞書は `review\<日付>\candidate_dict.tsv`。今の `user_dict.tsv` に `proposed.tsv` を足したもの。
   - 評価するセット: `user_style`、`file:eval/sets/gen_test.jsonl`、`ajimee`、`file:eval/sets/synth_user.jsonl`、`file:%LOCALAPPDATA%\romaji-slm\review\real_local.jsonl`。
   - 変換器の指定: `core:p=insert,e=typed,ud=<辞書のパス>`（`ud` を付けない回が「今の辞書なし」の基準。今の辞書がある回は `ud=<user_dict.tsv>` も）。
   - **採る条件**: user_style・gen_test・AJIMEE・synth_user で「正常入力で新たに壊れた」が 0 件。かつ、real_local で改善がある。新たに壊れた文が出たら、その原因の行を proposed.tsv から外して確かめ直す。
6. **入れる**: 条件を満たした行だけを `uv run python -m tools.review_history merge review\<日付>\proposed.tsv` で `user_dict.tsv` に足す（控えが `user_dict.<時刻>.bak` に残る）。
7. **マーカーを進める**: `uv run python -m tools.review_history mark <1 で控えた until> --note "<一言>"`。
8. **記録する**: 下の「記録」に 1 回 1 節を足す。入れるのは、読んだ範囲（since → until）、行数と確定の数、誤変換の件数（型ごと）、足した辞書の行（語・綴りだけ。本人の文は書かない）、評価の結果（各セットの完全一致の前後と新たに壊れた件数）、見送った候補とその理由。
9. **コミットする**: この文書だけを `git add docs/REVIEW.md` → `git commit -- docs/REVIEW.md`。push はしない。

## 守ること

- 本人の文（history.jsonl・findings・real_local の中身）をリポジトリ・この文書・コミットメッセージに書かない。
- 変換器のコード（core/・ime/）は変えない。辞書で直らない型が続くなら、記録の「次に変換器で直すべきこと」に書く。
- 評価で新たに壊れた文が出る候補は入れない。迷ったら入れない。
- `user_dict.tsv` を手で書き換えない（merge を使う。控えが残る）。

## 記録

（1 回ごとに、新しい節を下に足す）
