# 現状と再開の手引き（2026-10-15、自律進行セッションの終了時点）

2026-10-13 に本人が休止を決め、2026-10-14〜15 に親セッション（Claude）が本人不在で進めた（規則は [parent-session-prompt.md](parent-session-prompt.md)、判断の記録 D1〜D13 と本人待ちのカードは [decisions.md](decisions.md)）。
要件は [DESIGN.md](DESIGN.md)、方式の決定と経緯は [REDESIGN.md](REDESIGN.md)、全実測は [EVAL.md](EVAL.md)、体験版は [TRIAL.md](TRIAL.md)、llama-server 実行系は [LLAMA_BACKEND.md](LLAMA_BACKEND.md)。

## 1. どこまで来たか

方式は「学習なし。前段で読みの候補を作り、Mozc 辞書のラティスで出力を制約して、凍結した zenz に選ばせる」（REDESIGN.md）。工程1〜5 は動いている。工程6（速度）はこのデスクトップの CPU で実測して改善した。**ThinkPad では未測定**で、体験版の自動計測がその値を出す。

```
打鍵列 ─ core/reading.py ─→ 読みの上位 k 件（打鍵ミスの編集弧・英単語の弧・n/nn・数字・記号）
       ─ core/constrained.py ─→ k 件をまとめて1バッチで、辞書ラティス（core/kanakanji.py、数の弧 core/numbers.py）で
                                  出力を制約した zenz-v3.1-small の貪欲生成（Decoded: 文・対数確率・完走・語の切れ目）
                                  ※ llama-server 版は core/gbnf.py（ラティス→GBNF）＋ core/llama_backend.py
       ─ core/pipeline.py ─→ score = zenz の変換の対数確率 + f×完成文の自然さ（core/select.py の fluency）− w×読みのコスト
                              の最大を返す（既定 k=5, w=4, f=1）。句読点 B（punct=insert）はここで句読点を補い、
                              英字の表記（en_case）はここで登録表記に描画する
```

### 数値（詳細は EVAL.md「工程6」）

| | small（既定） | xsmall（速度優先） | small を llama-server（Q8_0）で |
|---|---|---|---|
| user_style 73文 完全一致（GPU） | 82.2%（`e=registered` 86.3%） | 76.7% | 80.8% |
| うち tech / typo | 75.0% / 63.6% | 60.0% / 54.5% | 75.0% / 63.6% |
| 正常入力の誤補正 | 4.8% | 9.7% | 4.8% |
| gen_test 337 / AJIMEE 200 | 34.1% / 84.5% | 31.5% / 70.0% | 33.2% / 83.5% |
| 1件の中央値（**このデスクトップの CPU**、user_style） | 379 ms（休止時点の逐次実装 1208 ms） | 148 ms | 259 ms |
| 句読点 B（`p=insert`）の CPU 中央値 | 502 ms | — | — |

句読点 B: 本人の文体（句読点を打たない 68 文）で句点 適合率 0.98／再現率 0.79、読点 0.67／0.33、完全一致 0% → 51.5%。

### 2026-10-14〜15 にできたもの

- **k 件のバッチ生成**（D4）、**表記モード** `punct` = auto / ja / ascii / insert、`en_case` = typed / registered / auto / zenz（D7）、**句読点 B**（D8）、Codex（QA 役）のレビュー 5 件の修正（D11）。
- **llama-server 実行系**（PR #8、D6・D12・D13）: grammar 付きでも制約前の対数確率を返すことを確認。CPU で torch 版の約 0.7 倍の時間、精度は約 1 ポイント低い（量子化ではなく実行系の差）。本番実行系の第一候補だが、配布物はまだ torch 版。
- **体験版 V0.2**（PR #6 → #9、D2・D9・D10）: 一本道のステップ式（準備 → 句読点 3 例 → 英字 3 例 → 体感 2 例 → 自動計測 → 結果をコピー、19 クリック）。本物のエンジンで通し操作を確認。結果ブロックの冒頭に本人の明示回答（句読点・英字・使用感）が出る。
- **配布 zip**: `tools/pack-trial.ps1` → `dist/romaji-slm-trial-<日付>-<hash>.zip`（202 MB、torch は CPU 版の `uv.lock`、モデル同梱）。別フォルダへの展開、**空の環境**（uv のキャッシュと Python が無い。`UV_CACHE_DIR`・`UV_PYTHON_INSTALL_DIR` を空にして `UV_PYTHON_PREFERENCE=only-managed`）での起動を確認した。cmd.exe の UTF-8 の罠で run.cmd は ASCII の起動役にし、手順は `trial/run.ps1`（UTF-8 BOM）に置いた。
- **評価の道具**（PR #7）: `eval/diff.py`、`clean_exact`・`pair_consistent`、`eval/punct_eval.py`、gen_test の dev/val 分割。

使い方:

```
uv run python -m eval.run --converter core --set user_style --device cpu                     # 既定 k=5,w=4,f=1,p=auto,e=typed
uv run python -m eval.run --converter "core:p=insert,e=registered" --set gen_val --device cuda  # 句読点 B ＋ 登録表記
uv run python -m eval.run --converter "core-llama:np=5,t=8" --model models/zenz-v3.1-small-gguf/zenz-v3.1-small-Q8_0.gguf --set user_style --device cpu
uv run python -m eval.bench_backend --gguf models/zenz-v3.1-small-gguf/zenz-v3.1-small-Q8_0.gguf   # torch 版と llama 版の速度
uv run python -m eval.diff eval/out/<before>.jsonl eval/out/<after>.jsonl                  # 改善・悪化・新たに壊れた件
uv run python -m trial.server --open                                                         # 体験版（trial/configs.json・examples.json）
pwsh -NoProfile -File tools/pack-trial.ps1                                                   # 配布 zip
```

`CorePipeline.candidates(romaji, left_context)` が候補ごとの点数の内訳（lm・fluency・read_cost・edits・bounds）を返す。

## 2. 次にやること

1. **本人待ち**（decisions.md「本人待ち」）: ThinkPad で zip を動かして結果ブロックを貼ってもらう。回答（句読点 A/B・英字・使用感）と ThinkPad の実測（各文の ms・最大・1 秒超の件数）がそれで届く。本人の言葉は decisions.md に原文で記録し、engine の 01_inbox にも 1 メモ 1 ファイルで書く。
2. **クラウドの残り**: `tools/tasks/reading-recall.md`（読み段の打鍵ミス再現率 63.5%→80%、辞書に無い英字列の保持。ブランチ `claude/reading-recall*`）が進行中だった。PR が来たら `git merge` → `uv run pytest` → `eval.run` で user_style / gen_test / AJIMEE を回し、`eval/diff.py` で改善・悪化・新たに壊れた件を見てから取り込む。
3. **ThinkPad の実測で 1 秒を超えたら**: llama-server 版を「精度優先」の実体にする（D13）。そのために llama 版の約 1 ポイント差（自然さの強制生成のトークン分割、取り直し 193 回/14 件）を潰し、Windows 向けの llama-server（CPU 版か Vulkan 版）と GGUF を zip に同梱する形に `trial/engine.py` と `tools/pack-trial.ps1` を広げる。
4. **精度**: 誤補正（視線計測→自然計測の型）、辞書に無い英字・略語（jr→上野、scc-0210→サッカー0210、cdma→狭）、ローマ字として読める英単語（todo→とど、code→コデ）。目安（user_style 70 / tech 60 / typo 50 / 誤補正 2%）と現在値からの差分で採否。
5. IME 化は本人が「後で決める」としたままで未着手。

## 3. 止めた路線（再開時に繰り返さない）

- **zenz の微調整**（train/finetune.py）: かな漢字変換の知識が削れる（AJIMEE 87%→28.5%／正解の読みを与えても 87%→72.5%）。EVAL.md「微調整（方式3）」。
- **romaji2ja（limoXD/romaji2ja）の流用**: AJIMEE 3〜4%。記号を入力から捨てる作り。DESIGN.md「実測」。
- **英字の登録表記を zenz に採点させる**（ラティスで GitHub を強制）: 誤補正 4.8%→9.7%。表記は選んだ後に描画する（D7）。
- **torch の量子化・スレッド数の調整**: 支配要因が forward の固定費なので効かない（D3・D4）。
- **句読点 B の全組合せ一括採点**: CPU で +300 ms。2 段（文末 → 読点）に変えて +120 ms（EVAL.md）。
- 評価セットの学習データとの重複: `eval/sets/gen_test.jsonl`（337件）は除いた版。

## 4. 手元の環境（再開時に作り直すもの）

`models/`・`data/out*`・`eval/out/`・`dist/` はコミットしていない。すべて作り直せる:

| パス | 中身 | 作り直し方 |
|---|---|---|
| `models/mozc/` | Mozc dictionary_oss（固定コミット）と索引 | `uv run python -m core.fetch_mozc`（初回の索引構築は約9秒） |
| `models/reading/kana_lm.pkl` | 読みの文字 4-gram | `core.reading` の初回実行で自動作成 |
| `models/wac/` | 京大 Wikipedia 注釈コーパス（kana_lm の素材） | 同上で自動取得 |
| `models/ajimee/` | AJIMEE-Bench の評価データ | `eval.run --set ajimee` の初回で自動取得 |
| zenz-v3.1-small / xsmall | Hugging Face のキャッシュ（`~/.cache/huggingface`） | 初回の実行で自動取得 |
| `models/zenz-v3.1-small-gguf/` | zenz-v3.1-small の GGUF（F16・Q8_0。llama-server 用） | `uv run python -m infer.to_gguf <HF の snapshot> --llama-cpp models/llama.cpp-src --llama-bin C:\Users\tm976\mf\llama --outdir models/zenz-v3.1-small-gguf --types f16,q8_0` |
| `dist/romaji-slm-trial-*.zip` | 体験版の配布物 | `pwsh -NoProfile -File tools/pack-trial.ps1`（開発機で。CPU 版の `uv lock` とモデル同梱を含む） |
| `models/llama.cpp-src/` | llama.cpp b10549 のソース（GGUF 変換スクリプト用） | `git clone --depth 1 --branch b10549 https://github.com/ggml-org/llama.cpp` |
| `models/zenz-romaji-v1/`・`v2/`・`romaji2ja*`・`probe/`・`lrec-ime/`・`zenz-v3.2-*` | 止めた路線・速度の模擬の残骸 | 不要（消してよい） |
| `data/out300k/`・`data/out/` | 微調整用に生成した学習データ | `uv run python -m data.build --source hf --limit 300000 --out data/out300k/ --seed 0`（約90秒） |

- Python 環境: `uv sync --all-groups`（torch は cu128 の index。依存グループ core・data・eval・dev）。
- llama.cpp の実行ファイル: `C:\Users\tm976\mf\llama`（b10549、CUDA と CPU のビルド）。llama.cpp を使うテストは `LLAMA_CPP_DIR=models/llama.cpp-src`、`LLAMA_CPP_BIN=C:\Users\tm976\mf\llama` で動く。b10549 の llama-server は極小のランダムモデルの壊れた UTF-8 出力を HTTP 500 で拒むので、`tests/test_infer.py` の 3 件はこのマシンでは落ちる（INFER.md）。`tests/test_llama_backend.py` は文法で制約するので通る。
- テスト: `uv run pytest`（2026-10-15 時点で 265 件通過、llama.cpp 無しでは 13 件スキップ）。
- 体験版の開発用: `.claude/launch.json` の `trial`（本物のエンジン、8765）と `trial-mock`（MockEngine、8766）。

## 5. 作業の進め方で分かったこと（このリポジトリ固有）

- **10分を超える評価・学習は、Claude Code のバックグラウンド実行に任せない**（10分で子プロセスごと止まる）。GPU なら user_style＋gen_test＋AJIMEE の全部で約5分、CPU でも約8分なので、1 本ずつバックグラウンドで足りる。
- **速度はこのデスクトップの `--device cpu` で測る**（ThinkPad への換算は書かない）。GPU の ms は別セッションと取り合うので参考値。
- **クラウドセッション**（Claude Code on the web）に CPU だけで閉じる部品を並行で作らせる（PR #1〜#9）。立て方は `tools/cloud-task.ps1`（`claude --cloud` は TTY が要るので、アプリの Terminal パネルで実行する。先に push する）。依頼文は `tools/tasks/`。クラウド側は依頼文のブランチ名に接尾辞を付ける。huggingface.co と download.pytorch.org に届かないので、zenz を使う作業と `uv lock` は手元でやる。クラウドは `core/pipeline.py` のような親が編集中のファイルも触るので、取り込み時に衝突を解く前提で依頼する。
- **相談役**: astra（Codex consult）の計画レビューと体験版の設計レビューは D2・D3・D10。Codex review（QA 役）はコアの差分に対して 5 件の正当な欠陥を最小再現つきで返した（D11）。どちらも返答は第二意見として親が確かめてから採った。
- 部品の境目は `core/types.py`（Reading / Seg / Edit / Conversion / Word）。変えるときは両側を一緒に直す。
- Bash ツールのヒアドキュメントは長い内容（おおよそ 8KB 超）で壊れ、`\\` が `\` に化ける。長いファイルやバックスラッシュを含む内容は Write ツールで書く。cmd.exe は UTF-8 のバッチの日本語を壊すので、Windows の起動役は ASCII の .cmd から PowerShell（UTF-8 BOM）を呼ぶ。
