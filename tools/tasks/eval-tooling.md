# 依頼: 評価の道具を補強する（回帰の差分・正常入力の指標・句読点の評価・検証用分割）

最初に CLAUDE.md、docs/EVAL.md、docs/decisions.md（D2・D3）、eval/run.py、eval/report.py、eval/metrics.py、eval/datasets.py、eval/sets/*.jsonl を読むこと。文書・コメント・コミットメッセージは日本語。

## 目的

親セッションがこれから変換コアの精度（誤補正・英字の保持・句読点の自動挿入）を変えるとき、**「何が良くなり、何が壊れたか」を1件単位で見られる道具**が要る。いまの集計（完全一致率・誤補正率）は最低目安との比較しかできず、82%→70% でも合格に見える。相談役の指摘: 誤補正 4.8% は「正常入力で編集経路を通った割合」であって実害の直接測定ではない。採否の中心は「現在値との差分」と「改善した文／壊した文」に置く。

## 作るもの（すべて `eval/` と `tests/`、`docs/EVAL.md` への追記。`core/` は触らない）

### 1. `eval/diff.py` — 2つの結果 JSONL の差分

```
uv run python -m eval.diff eval/out/<before>__user_style.jsonl eval/out/<after>__user_style.jsonl [--show 20]
```

- 同じセットの結果（`eval/run.py` が書く JSONL。1行1件、`id`・`category`・`romaji`・`refs`・`output`・`exact`・`loose`・`edited`・`ms`）を `id` で突き合わせ、
  - 改善（exact: 0→1）、悪化（1→0）、変わらず（両方 1／両方 0 だが出力が変わった／出力も同じ）の件数、
  - **正常入力（category が typo・noisy 以外）で新たに壊れた件数**（＝悪化のうち正常入力）、
  - 誤補正（`edited`）の増減、英語保持（`en_kept`）の増減、ms の中央値の変化、
  - カテゴリ別の上の集計、
  - 例: 改善・悪化それぞれ `--show` 件（romaji / 正解 / before / after）
  を Markdown で標準出力に出す。`--md <path>` で保存。
- 片方にしか無い id は数えて警告する。

### 2. `eval/report.py` の集計に列を足す（最小の変更）

- `clean_exact`: 正常入力（typo・noisy 以外）だけの完全一致率。既存の `miscorrect` と並べる。
- `pair_consistent`: 下記 3 の対になる件がある場合、打鍵ミス版の出力が正常版の出力と一致した割合（`meta.pair_of` を持つ件だけ）。
- 既存の列・表の形は変えない（docs/EVAL.md の過去の表と読み比べられるように）。

### 3. 対になる試験: `eval/sets/user_style.jsonl` の typo と正常版の対応

- typo カテゴリの各文に、元になった正常入力の文があるか調べ（出力が同じ文を探す）、typo 側の `meta` に `"pair_of": "<正常版の id>"` を足す（`id` が無ければ、`eval/datasets.py` が付ける id の規則を確かめてそれを使う。無ければ `meta.id` を全件に付ける）。対が無い typo 文はそのまま。
- `eval/datasets.py` の読み込みで `meta.pair_of` が `Item` から取れるようにする。

### 4. 検証用の分割: `eval/sets/gen_test.jsonl`（337件）を dev / val に分ける

- 閾値や重みの調整には dev、最終確認には val を使う（相談役の指摘: 調整用データと未使用の検証用データを分ける）。
- `id`（無ければ romaji）のハッシュで決定的に半分に割り、`eval/sets/gen_dev.jsonl`・`eval/sets/gen_val.jsonl` を作る。カテゴリ（clean / noisy / omit_punct）の比率が両方でほぼ同じになること（比率を README か docs に書く）。元の gen_test.jsonl は残す。
- `--set gen_dev` / `--set gen_val` で読めるようにする（`load_set`）。

### 5. `eval/punct_eval.py` — 句読点の自動挿入（句読点 B）の評価

対象: 入力に句読点が無い件（gen_test の `omit_punct` 120件。うち正解に `、。？?` を含む 69 件が陽性、含まない 51 件が陰性）。将来、親が core 側に句読点を補う部品を足したとき、その結果 JSONL をこれで評価する。

```
uv run python -m eval.punct_eval eval/out/<converter>__gen_val.jsonl [--category omit_punct]
```

- **本文の保持**: 出力と正解から**句読点（、。？！?!,.）だけ**を除いて比べた一致率。既存の「ゆるい一致」は長音・記号全般を消すので使わない（専用の正規化を `eval/metrics.py` に足す: `strip_punct`）。
- **句読点の適合率・再現率・F1**: 種類別（読点、句点、疑問符）。位置は「句読点を除いた本文の何文字目の後ろか」で比べる（本文が一致する件だけ。本文が違う件は分母から外して件数を別に出す）。
- **不要な挿入率**: 陰性 51 件のうち、出力に句読点が入った割合。
- **文末だけ**の内訳: 文末の句点の再現率と、文中の読点の再現率を分ける。
- 陽性／陰性の件数と、失敗例（挿入しすぎ・足りない・位置違い）を `--show` 件。
- 結果は Markdown で標準出力。

### 6. テストと文書

- `tests/test_eval_diff.py`・`tests/test_eval_punct.py`（小さな手書きの行で、集計値を手計算と比べる）。既存テストを壊さない。`uv run pytest` 全部通ること。
- `docs/EVAL.md` の「指標」「使い方」に、diff・clean_exact・pair_consistent・dev/val・punct_eval を追記する。長くしない（各 2〜5 行）。

## 作業の規則

- ブランチ `claude/eval-tooling` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。**push 先はこのブランチだけ。**
- **`core/`・`pyproject.toml`・`uv.lock` は変えない。** `eval/converters.py` と `eval/run.py` も、親が並行して編集しているので変えない（必要なら PR 本文に提案）。
- 認証情報・個人情報を書かない。外部サービスへの書き込みは PR の作成だけ。
- この環境からは huggingface.co と download.pytorch.org に届かない。torch 無しでテストが動くこと（評価の道具は結果 JSONL だけを読む）。
- 最終報告（PR 本文）: 作ったファイル、`uv run pytest` の結果、dev/val の件数とカテゴリ比率、未解決の論点。
