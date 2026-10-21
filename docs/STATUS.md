# 現状と再開の手引き（2026-10-16、このデスクトップに常駐入力を入れた後）

## 0. いまのゴール（2026-10-16、本人の依頼で autopilot）

本人が「実用可能なレベル」と判断し、このデスクトップで常用できるようにすることを求めた（原文は decisions.md「本人の依頼」）。

- **できたこと**: 常駐入力 `ime/` を作り、`%LOCALAPPDATA%\romaji-slm\` に入れた。スタートアップに登録し、起動済み（初回は「使っていない」状態）。使い方・仕組み・実測・限界は [IME.md](IME.md)、判断は D19〜D24。
  - Codex のレビューを 11 回通し、指摘をすべて直した。打ち切りの判断は D23 の末尾。
  - 途中で本人が追加した依頼で、実利用の記録（`history.jsonl`、D24）を入れた。
- **ThinkPad**（2026-10-18、D36）: Git で追従させる。手順は [THINKPAD.md](THINKPAD.md)。初回の準備は本人が ThinkPad で行う（開発機での再現は通った）。
- **本人に残ること**: Ctrl+Alt+Space で使い始め、ふだんのアプリで打つ。フックと SendInput と MS-IME の開閉は、本人の操作でしか確かめられない。使ったらトレイの「使用状況をコピー」を貼ってもらう。ThinkPad での実測（§2 の 1）も残っている。
- **誤変換を直す仕組み**（2026-10-17）: 利用履歴を AI が振り返ってユーザー辞書を育てる（REVIEW.md、D32）。1 回目は済んだ（辞書 6 行）。以後は ops-daily がマーカー以降の確定 200 件ごとに Opus（effort medium）で行う。合成セット（synth_user、EVAL.md）と本人の実入力（`%LOCALAPPDATA%\romaji-slm\review\real_local.jsonl`）で副作用を確かめる。
- **履歴の保持**（2026-10-17、D33）: 振り返り済みの行は消す、評価に残すのは誤変換の組だけ（90 日）、30 日を過ぎた行は自動で消す、記録しないアプリを指定できる。
- **候補ウィンドウ**（2026-10-18、本人の要望、D37）: 1 位の読みの別の書き方（辞書の N-best を 1 位と同じ式で採点）を足し、読みの候補と点数で混ぜる `merge` を `core/window.py` の既定にした。1 位は変わらない。短い単語で欲しい書き方が窓に入る数 1/156 → 125/156、打鍵ミスのセットでも正解が窓に入る率は下がらない（AJIMEE 62.0% → 65.0%、WAC test 61.5% → 70.0%）。常駐も本人の決定で merge にした（`ime/server.py --window`、`reading` で元に戻る）。比べる道具は `uv run python -m eval.window_eval`。
- **再開の手順**: `pwsh -NoProfile -File ime/install.ps1`（HEAD を入れ直す）。試験は次の 2 つ。
  - `pwsh -NoProfile -File ime/build.ps1 -Test`
  - `RomajiSlmIme.exe --selftest <打鍵列> <結果ファイル> [--root <repo>]`
2026-10-13 に本人が休止を決め、2026-10-14〜15 に親セッション（Claude）が本人不在で進めた（規則は [parent-session-prompt.md](parent-session-prompt.md)、判断の記録 D1〜D13 と本人待ちのカードは [decisions.md](decisions.md)）。
要件は [DESIGN.md](DESIGN.md)、方式の決定と経緯は [REDESIGN.md](REDESIGN.md)、全実測は [EVAL.md](EVAL.md)、体験版は [TRIAL.md](TRIAL.md)、速さは [SPEEDUP.md](SPEEDUP.md)。

## 1. どこまで来たか

方式は「学習なし。前段で読みの候補を作り、Mozc 辞書のラティスで出力を制約して、凍結した zenz に選ばせる」（REDESIGN.md）。工程1〜5 は動いている。工程6（速度）はこのデスクトップの CPU で実測して改善した。**ThinkPad では未測定**で、体験版の自動計測がその値を出す。

```
打鍵列 ─ core/reading.py ─→ 読みの上位 k 件（打鍵ミスの編集弧・英単語の弧・n/nn・数字・記号）
       ─ core/constrained.py ─→ k 件をまとめて1バッチで、辞書ラティス（core/kanakanji.py、数の弧 core/numbers.py）で
                                  出力を制約した zenz-v3.1-small の貪欲生成（Decoded: 文・対数確率・完走・語の切れ目）
                                  ※ GPU は torch、CPU は forward を ONNX Runtime で回す core/ort_backend.py（照合は同じ）
       ─ core/pipeline.py ─→ score = zenz の変換の対数確率 + f×完成文の自然さ（core/select.py の fluency）− w×読みのコスト
                              の最大を返す（既定 k=5, w=4, f=1）。句読点 B（punct=insert。既定では使わない、D48）はここで句読点を補い、
                              英字の表記（en_case）はここで登録表記に描画する
```

### 数値（詳細は EVAL.md「工程6」）

| | small（既定） | xsmall（速度優先） | small を llama-server（Q8_0）で（2026-10-15。その後削除、D44） |
|---|---|---|---|
| user_style 73文 完全一致（GPU） | 82.2%（`e=registered` 86.3%） | 76.7% | 80.8% |
| うち tech / typo | 75.0% / 63.6% | 60.0% / 54.5% | 75.0% / 63.6% |
| 正常入力の誤補正 | 4.8% | 9.7% | 4.8% |
| gen_test 337 / AJIMEE 200 | 34.1% / 84.5% | 31.5% / 70.0% | 33.2% / 83.5% |
| 1件の中央値（**このデスクトップの CPU**、user_style） | 379 ms（休止時点の逐次実装 1208 ms） | 148 ms | 259 ms |
| 句読点 B（`p=insert`）の CPU 中央値 | 502 ms | — | — |

句読点は自動で足さない（本人の決定、D48。句点は D31、読点も 2026-10-20 に止めた）。句読点 B は `p=insert` で残しており、読点の適合率 0.91・
再現率 0.48（synth_user の句読点を打たない 680 文。EVAL.md「読点の採点の形と短い文の下限」）。

### 2026-10-14〜15 にできたもの

- **k 件のバッチ生成**（D4）、**表記モード** `punct` = auto / ja / ascii / insert、`en_case` = typed / registered / auto / zenz（D7）、**句読点 B**（D8）、Codex（QA 役）のレビュー 5 件の修正（D11）。
- **llama-server 実行系**（PR #8、D6・D12・D13。2026-10-20 に削除、D44）: grammar 付きでも制約前の対数確率を返すことを確認。CPU で torch 版の約 0.7 倍の時間、精度は約 1 ポイント低い（量子化ではなく実行系の差）。本番実行系の第一候補だが、配布物はまだ torch 版。
- **体験版 V0.2**（PR #6 → #9、D2・D9・D10）: 一本道のステップ式（準備 → 句読点 3 例 → 英字 3 例 → 体感 2 例 → 自動計測 → 結果をコピー、19 クリック）。本物のエンジンで通し操作を確認。結果ブロックの冒頭に本人の明示回答（句読点・英字・使用感）が出る。
- **配布 zip**: `tools/pack-trial.ps1` → `dist/romaji-slm-trial-<日付>-<hash>.zip`（202 MB、torch は CPU 版の `uv.lock`、モデル同梱）。別フォルダへの展開、**空の環境**（uv のキャッシュと Python が無い。`UV_CACHE_DIR`・`UV_PYTHON_INSTALL_DIR` を空にして `UV_PYTHON_PREFERENCE=only-managed`）での起動を確認した。cmd.exe の UTF-8 の罠で run.cmd は ASCII の起動役にし、手順は `trial/run.ps1`（UTF-8 BOM）に置いた。
- **評価の道具**（PR #7）: `eval/diff.py`、`clean_exact`・`pair_consistent`、`eval/punct_eval.py`、gen_test の dev/val 分割。

使い方:

```
uv run python -m eval.run --converter core --set user_style --device cuda                    # 既定 k=5,w=4,f=1,p=auto,e=typed（torch は GPU だけ）
uv run python -m eval.run --converter "core-ort:t=4" --set user_style --device cpu             # CPU は ONNX Runtime 版（出力は torch 版と同じ）
uv run python -m eval.run --converter "core:p=insert,e=registered" --set gen_val --device cuda  # 句読点 B ＋ 登録表記
uv run python -m eval.parity_speed --converter "core-ort:t=4,p=insert" --device cpu             # 1 件の変換の時間の内訳
uv run python -m eval.ime_sim                                                                # 打鍵から 1 位が出るまでの待ち（常駐入力の要求の出し方を模擬）
uv run python -m eval.diff eval/out/<before>.jsonl eval/out/<after>.jsonl                  # 改善・悪化・新たに壊れた件
uv run python -m trial.server --open                                                         # 体験版（trial/configs.json・examples.json）
pwsh -NoProfile -File tools/pack-trial.ps1                                                   # 配布 zip
```

`CorePipeline.candidates(romaji, left_context)` が候補ごとの点数の内訳（lm・fluency・read_cost・edits・bounds）を返す。

## 2. 次にやること

本人は 2026-10-15 にデスクトップで体験版を試し、句読点 B・英字 打ったまま・「使ってみたい」と答えた（decisions.md「本人の回答」、D14）。未解決 4 点への本人の方針（原文は decisions.md「本人の方針」）に沿って、D16〜D18 で進めた: 読み段の PR #10・#11 と候補の絞り込みを取り込み、user_style 82.2% → 86.3%、打鍵ミスの文 63.6% → 90.9%、正常入力で新たに壊れた文 0。CPU の中央値は 379 → 507 ms。

1. **ThinkPad での実測はまだ**。速度要件（20〜30 字 1 秒以内）の判定には ThinkPad で測る必要がある。本人の決定（2026-10-19）で、ほかの高速化を入れた後に最後に測る。torch・ONNX・llama の 3 方式を測る zip は `tools/pack-bench.ps1`（手順は LLAMA_PARITY.md「ThinkPad での測定手順」）。ただし今の bench は左文脈なしの `convert` だけを測る。常駐入力の実際の形（convert_nbest・左文脈 60 字）はこのデスクトップで約 2.4 倍重いので、最後の測定ではそれも測る（[SPEEDUP.md](SPEEDUP.md) §7）。
2. **1 語に打鍵ミスが 3 つ以上**（本人の実入力 kangkaraereu）は読み段の候補に入らないまま（PR #11 の未解決の論点 1）。`max_edits=3` や 3 編集の語の弧は副作用が大きかった（docs/READING.md）。本人の方針（副作用が出るなら慎重に）に従い、入れていない。
3. **速度**（2026-10-18 の調査、[LLAMA_PARITY.md](LLAMA_PARITY.md)・D41）: llama-server 版が正常入力を 6 件壊していた原因は GGUF の BOS/EOS の ID の取り違え（直せる。`core-llama:eos=3`）。これまでの llama 版の「CPU で 0.55 倍」は GPU の値で、本当の CPU では torch 版とほぼ同じ時間。forward だけ ONNX Runtime に替えた版（`core-ort`）は出力が torch 版と全件同じで、CPU で 0.72 倍。torch の既定スレッドはほかの負荷に弱い（スレッド 4 の 6 倍の時間の例）。その後、常駐入力のスレッド数を 4 に固定し（D42）、GPU の無い機械は ONNX 版で動かすことにした（D43。デスクトップは torch＋GPU のまま）。ThinkPad の測定は `dist/romaji-slm-bench-*.zip` の `bench.cmd`。
4. 文中の句点（2 文の間）は本人の方針で着手しない（1 文ずつ変換する）。
5. IME 化は 2026-10-16 の本人の依頼で常駐入力として着手した（§0）。
6. `uv run pytest` が 1 回だけ 1 件落ち、続く 3 回は全件通った（2026-10-16）。どのテストかは特定していない。
7. **候補ウィンドウ（merge、D37）の所要**: 別の書き方を足す分は、このデスクトップの GPU で中央値 20〜75 ms、CPU（別の作業で負荷 100% のとき）で短い単語 153 ms・文 445 ms（D37）。ThinkPad では測っていない。1 位も同じ読みの別の書き方と同じ式で選び直すと AJIMEE きれい 153 → 155・user_style 63 → 64（誤補正 3 のまま）になる（保存した候補での机上の値。1 位が変わるので今回は入れていない）。
8. **速くした（2026-10-20、[SPEEDUP.md](SPEEDUP.md) §0、D44・D45）**: ONNX 版に A（共通の前置きの使い回し）・D（KV の写し）・E（融合）を入れた。
   常駐入力の 1 要求は左文脈 60 字で 2224 → 932 ms（このデスクトップ、負荷あり）、出力は変わらない。llama-server の経路と torch の CPU の経路は削除した。
   B（別の書き方を後に回す）・C（古い先読みの打ち切り）は、模擬の打鍵で打ち終えてから 1 位が出るまでの待ちが B＋C で約 0.55 倍（1342 → 738 ms）と測ったあと、
   本人の決定で常駐入力に入れた（D47、IME.md「変換の要求」）。候補の列は前と同じ（986 要求で確認）。ThinkPad の測定は bench の zip を作り直してから（`tools/pack-bench.ps1`）。
   全セットの出力が 2026-10-18 の記録と 79 文で違ったのは、その記録の E セットが句読点 B なし（`p=auto`）だったため。同じ設定なら 2400 文とも同じ（SPEEDUP.md §0）。
   以下は入れる前の検討（2026-10-20）: ONNX 版の常駐入力の 1 要求（左文脈 60 字）は、このデスクトップ（負荷あり）で 1.8 秒、94% が forward。行どうしで共通の前置きの KV を使い回す試作で、出力を変えずに 0.55 倍。次は 1 手ごとの KV の写し（1 手の 2〜4 割）。別の書き方を最初の応答から外す・古い先読みを打ち切るは、振る舞いが変わるので本人の判断。int8 の動的量子化は精度が落ちて使えない。torch＋GPU は 0.3 秒、llama 版には投資しない。
9. **資源を減らす手（2026-10-21、[RESOURCES.md](RESOURCES.md)「減らす手の実測」、D49）**: 検討と実測と報告まで。コードは変えていない。
   出力を変えない手は、ONNX 版で torch を読まない・スピン待ちを止める・読み段のキャッシュに上限を付ける・アリーナを縮める・GPU で empty_cache の 5 つ。
   重ねると ONNX 版の専有 −50%・CPU 時間 −34%、待ちは +8〜13%（このデスクトップ、負荷あり）。
   入れるかどうかと、fp16・暇なときの用意・トレイのタイマーは本人の判断。ThinkPad では測っていない。

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
| `models/onnx/zenz-v3.1-small-v2.onnx` | ONNX Runtime 版の重み（380 MB） | `OrtBackend` の初回で自動で書き出す（torch が要る。20〜40 秒） |
| `dist/romaji-slm-trial-*.zip` | 体験版の配布物 | `pwsh -NoProfile -File tools/pack-trial.ps1`（開発機で。CPU 版の `uv lock` とモデル同梱を含む） |
| `models/zenz-romaji-v1/`・`v2/`・`romaji2ja*`・`probe/`・`lrec-ime/`・`zenz-v3.2-*`・`zenz-v3.1-small-gguf/`・`llama.cpp-src/` | 止めた路線・削除した llama 版・速度の模擬の残骸 | 不要（消してよい） |
| `data/out300k/`・`data/out/` | 微調整用に生成した学習データ | `uv run python -m data.build --source hf --limit 300000 --out data/out300k/ --seed 0`（約90秒） |

- Python 環境: `uv sync --all-groups`（torch は cu128 の index。依存グループ core・data・eval・dev）。
- テスト: `uv run pytest`。torch 版の試験は GPU（cuda）で動き、GPU の無い機械では飛ばす（tests/conftest.py の `torch_device`）。
- 体験版の開発用: `.claude/launch.json` の `trial`（本物のエンジン、8765）と `trial-mock`（MockEngine、8766）。

## 5. 作業の進め方で分かったこと（このリポジトリ固有）

- **10分を超える評価・学習は、Claude Code のバックグラウンド実行に任せない**（10分で子プロセスごと止まる）。GPU なら user_style＋gen_test＋AJIMEE の全部で約5分、CPU でも約8分なので、1 本ずつバックグラウンドで足りる。
- **速度はこのデスクトップの CPU の ONNX Runtime 版（`core-ort`）で測る**（ThinkPad への換算は書かない）。GPU の ms は別セッションと取り合うので参考値。
- **クラウドセッション**（Claude Code on the web）に CPU だけで閉じる部品を並行で作らせる（PR #1〜#9）。立て方は `tools/cloud-task.ps1`（`claude --cloud` は TTY が要るので、アプリの Terminal パネルで実行する。先に push する）。依頼文は `tools/tasks/`。クラウド側は依頼文のブランチ名に接尾辞を付ける。huggingface.co と download.pytorch.org に届かないので、zenz を使う作業と `uv lock` は手元でやる。クラウドは `core/pipeline.py` のような親が編集中のファイルも触るので、取り込み時に衝突を解く前提で依頼する。
- **相談役**: astra（Codex consult）の計画レビューと体験版の設計レビューは D2・D3・D10。Codex review（QA 役）はコアの差分に対して 5 件の正当な欠陥を最小再現つきで返した（D11）。どちらも返答は第二意見として親が確かめてから採った。
- 部品の境目は `core/types.py`（Reading / Seg / Edit / Conversion / Word）。変えるときは両側を一緒に直す。
- Bash ツールのヒアドキュメントは長い内容（おおよそ 8KB 超）で壊れ、`\\` が `\` に化ける。長いファイルやバックスラッシュを含む内容は Write ツールで書く。cmd.exe は UTF-8 のバッチの日本語を壊すので、Windows の起動役は ASCII の .cmd から PowerShell（UTF-8 BOM）を呼ぶ。
