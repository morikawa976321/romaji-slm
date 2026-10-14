# 現状と再開の手引き（2026-10-15 自律進行セッション中）

2026-10-13 に本人が休止を決め、2026-10-14 から親セッション（Claude）が本人不在で進めている（規則は [parent-session-prompt.md](parent-session-prompt.md)、判断の記録は [decisions.md](decisions.md)）。
要件は [DESIGN.md](DESIGN.md)、方式の決定と経緯は [REDESIGN.md](REDESIGN.md)、全実測は [EVAL.md](EVAL.md)、体験版は [TRIAL.md](TRIAL.md)。

## 1. どこまで来たか

方式は「学習なし。前段で読みの候補を作り、Mozc 辞書のラティスで出力を制約して、凍結した zenz に選ばせる」（REDESIGN.md）。工程1〜5 は動いている。**工程6（速度）はこのデスクトップの CPU で実測して改善中**、ThinkPad では未測定。

```
打鍵列 ─ core/reading.py ─→ 読みの上位 k 件（打鍵ミスの編集弧・英単語の弧・n/nn・数字・記号）
       ─ core/constrained.py ─→ k 件をまとめて1バッチで、辞書ラティス（core/kanakanji.py、数の弧 core/numbers.py）で
                                  出力を制約した zenz-v3.1-small の貪欲生成（Decoded: 文・対数確率・完走・語の切れ目）
       ─ core/pipeline.py ─→ score = zenz の変換の対数確率 + f×完成文の自然さ（core/select.py の fluency）− w×読みのコスト
                              の最大を返す（既定 k=5, w=4, f=1）。句読点 B（punct=insert）はここで句読点を補い、
                              英字の表記（en_case）はここで登録表記に描画する
```

### 2026-10-14〜15 の変更（詳細は decisions.md D1〜D8、数値は EVAL.md「工程6」）

- **k 件のバッチ生成**（D4）: CPU（Core Ultra 5 245KF）で user_style 73文の中央値 1208 → 485 ms。出力は逐次と同一。
- **速度優先の設定 = zenz-v3.1-xsmall**（D5）: CPU で約 150 ms。user_style 76.7%／AJIMEE 70.0%（small は 82.2%／84.5%）。誤補正は 2 倍。
- **表記モード**: 句読点 `punct` = auto（zenz が全角/半角を選ぶ、既定）/ ja / ascii / insert（自動挿入 = 句読点 B）、英字 `en_case` = typed（既定）/ registered / auto / zenz。英字の表記は選んだ後に描画する（zenz に大文字を採点させると読みの比較が歪む）。
- **句読点 B**: 語の切れ目に読点、末尾に句点・疑問符を補う候補を完成文の自然さで選ぶ。閾値は user_style（句読点を打たない版 `eval/sets/user_style_nopunct.jsonl`）で決め、gen_test の omit_punct で検証（EVAL.md）。
- **llama-server は grammar 付きでも制約前の対数確率を返す**（D6）: CPU で 1 トークン 2.1 ms（torch は 13 ms）。本番実行系の候補として、ラティス→GBNF・並列スロット・自然さの強制生成をクラウド（PR 予定、ブランチ `claude/llama-backend*`）で作成中。
- **体験版（V0）**: `trial/`（PR #6 取り込み済み）。`uv run python -m trial.server --open` で比較ページ。配布 zip は `tools/pack-trial.ps1`。
- **評価の道具**（PR #7 取り込み済み）: `eval/diff.py`（変更前後の差分）、`clean_exact`・`pair_consistent` 列、`eval/punct_eval.py`（句読点 B）、gen_test の dev/val 分割（`--set gen_dev` / `gen_val`）。

使い方:

```
uv run python -m eval.run --converter core --set user_style --device cpu                    # 既定 k=5,w=4,f=1,p=auto,e=typed
uv run python -m eval.run --converter "core:k=5,w=4,f=1,p=insert,e=registered" --set gen_val --device cuda
uv run python -m eval.run --converter "core:k=3" --model Miwa-Keita/zenz-v3.1-xsmall --set ajimee --device cuda
uv run python -m eval.diff eval/out/<before>.jsonl eval/out/<after>.jsonl                 # 改善・悪化・新たに壊れた件
uv run python -m trial.server --open                                                        # 体験版（trial/configs.json の設定）
uv run python -m core.reading "githubnipushshiteoite"                                       # 読みの候補とコストの内訳
```

`CorePipeline.candidates(romaji, left_context)` が候補ごとの点数の内訳（lm・fluency・read_cost・edits・bounds）を返す。

## 2. 次にやること（親セッションの残り）

1. 体験版をこのデスクトップで通しで確かめ（本物のエンジン・ブラウザ）、`tools/pack-trial.ps1` で zip を作り、別フォルダ・空の HF キャッシュで動作確認する。
2. 全セット（user_style / gen_test / AJIMEE）を CPU で回して EVAL.md に行を足す（既定・xsmall・p=insert）。
3. クラウドの llama-backend PR を取り込み、実モデルで同等性と速度を測り、本番実行系を決める。
4. 精度（STATUS 旧 §2 の 2〜5: 誤補正・辞書に無い英字・ローマ字として読める英単語・読み段の打鍵ミス）。目安（user_style 70 / tech 60 / typo 50 / 誤補正 2%）と現在値からの差分（`eval/diff.py`）で採否。
5. 本人への提示: ThinkPad で zip を解凍 → run.cmd → 打つ → 結果ブロックを貼る。意思決定カードは decisions.md「本人待ち」。

## 3. 止めた路線（再開時に繰り返さない）

- **zenz の微調整**（train/finetune.py、v1: ローマ字→出力、v2: ローマ字→読み→出力）: かな漢字変換の知識が削れる（AJIMEE 87%→28.5%／正解の読みを与えても 87%→72.5%）。EVAL.md「微調整（方式3）」。学習データ生成（data/）と学習スクリプトは残してあるが、使う予定はない。
- **romaji2ja（limoXD/romaji2ja）の流用**: AJIMEE 3〜4%。記号を入力から捨てる作り。DESIGN.md「実測」。
- **英字の登録表記を zenz に採点させる**（ラティスで GitHub を強制）: 誤補正 4.8%→9.7%。表記は選んだ後に描画する。
- **torch の量子化・スレッド数の調整**: 支配要因が forward の固定費なので効かない（D3・D4）。
- 評価セットの学習データとの重複: 生成した試験分割358件のうち21件が学習データと出力一致。`eval/sets/gen_test.jsonl`（337件）は除いた版。

## 4. 手元の環境（再開時に作り直すもの）

`models/` と `data/out*`、`eval/out/` はコミットしていない。すべて作り直せる:

| パス | 中身 | 作り直し方 |
|---|---|---|
| `models/mozc/` | Mozc dictionary_oss（固定コミット）と索引 | `uv run python -m core.fetch_mozc`（初回の索引構築は約9秒） |
| `models/reading/kana_lm.pkl` | 読みの文字 4-gram（Mozc 辞書の読み＋京大 Wikipedia 注釈コーパス） | `core.reading` の初回実行で自動作成 |
| `models/wac/` | 京大 Wikipedia 注釈コーパス（kana_lm の素材） | 同上で自動取得 |
| `models/ajimee/` | AJIMEE-Bench の評価データ | `eval.run --set ajimee` の初回で自動取得 |
| zenz-v3.1-small / xsmall | Hugging Face のキャッシュ（`~/.cache/huggingface`） | 初回の実行で自動取得 |
| `models/zenz-v3.1-small-gguf/` | zenz-v3.1-small の GGUF（F16・Q8_0。llama-server の確認用） | `uv run python -m infer.to_gguf <HF の snapshot> --llama-cpp models/llama.cpp-src --llama-bin C:\Users\tm976\mf\llama --outdir models/zenz-v3.1-small-gguf --types f16,q8_0` |
| `models/zenz-v3.2-*-gguf/`、`*-bench.gguf` | 速度の模擬に使った GGUF | `hf download Miwa-Keita/zenz-v3.2-small-gguf` 等。DESIGN.md「実測」 |
| `models/zenz-romaji-v1/`・`v2/`・`romaji2ja*`・`probe/`・`lrec-ime/` | 止めた路線の残骸 | 不要（消してよい） |
| `models/llama.cpp-src/` | llama.cpp b10549 のソース（GGUF 変換スクリプト用） | `git clone --depth 1 --branch b10549 https://github.com/ggml-org/llama.cpp` |
| `data/out300k/`・`data/out/` | 微調整用に生成した学習データ | `uv run python -m data.build --source hf --limit 300000 --out data/out300k/ --seed 0`（約90秒） |

- Python 環境: `uv sync --all-groups`（torch は cu128 の index。依存グループ core・data・eval・dev）。
- llama.cpp の実行ファイル: `C:\Users\tm976\mf\llama`（b10549、CUDA と CPU のビルド）。llama.cpp を使うテストは `LLAMA_CPP_DIR=models/llama.cpp-src`、`LLAMA_CPP_BIN=C:\Users\tm976\mf\llama` で動く。b10549 の llama-server は極小のランダムモデルの壊れた UTF-8 出力を HTTP 500 で拒むので、6件はこのマシンでは落ちる（INFER.md）。
- テスト: `uv run pytest`（2026-10-15 時点で 239 件通過、llama.cpp を使う 6 件はスキップ）。

## 5. 作業の進め方で分かったこと（このリポジトリ固有）

- **10分を超える評価・学習は、Claude Code のバックグラウンド実行に任せない**（10分で子プロセスごと止まる）。cmd に書いて WMI（`Invoke-CimMethod Win32_Process Create`）で切り離して起動し、ログと完了印（`*.done`）を見張る。GPU なら user_style＋gen_test＋AJIMEE の全部で約5分なのでバックグラウンドで足りる。
- **評価は別セッションの GPU 作業と取り合う**。ms/件は GPU を共有した状態の参考値で、速度の判定には使えない。速度はこのデスクトップの `--device cpu` で測る（ThinkPad への換算は書かない）。
- **クラウドセッション**（Claude Code on the web）に部品を並行で作らせる（PR #1〜#7、ブランチ `claude/*`）。立て方は `tools/cloud-task.ps1`（`claude --cloud` は TTY が要るので、アプリの Terminal パネルで実行する）。依頼文は `tools/tasks/`。クラウド側は依頼文のブランチ名に接尾辞を付ける（`claude/trial-page-98uhw1` など）。クラウドの既定環境は huggingface.co と download.pytorch.org に届かない（403）ので、zenz を使う作業と `uv lock` は手元でやる。
- 部品の境目は `core/types.py`（Reading / Seg / Edit / Conversion / Word）。別々に作った部品を挿し替えるための約束なので、変えるときは両側を一緒に直す。
- 相談役（astra = Codex consult、fable = fable-consultant）。2026-10-14 の計画レビューは decisions.md D2・D3。
- Bash ツールのヒアドキュメントは長い内容（おおよそ 8KB 超）で壊れる。長いファイルは Write ツールで書く。
