# 現状と再開の手引き（2026-10-13 休止時点）

2026-10-13、本人の決定でこのプロジェクトを一旦休止した。再開するときはこの文書から読む。
要件は [DESIGN.md](DESIGN.md)、方式の決定と経緯は [REDESIGN.md](REDESIGN.md)、全実測は [EVAL.md](EVAL.md)。

## 1. どこまで来たか

方式は「学習なし。前段で読みの候補を作り、Mozc 辞書のラティスで出力を制約して、凍結した zenz に選ばせる」（REDESIGN.md）。
工程1〜4 は動いている。工程5（左文脈）は評価時に渡しているだけで専用の作業はしていない。**工程6（ThinkPad での速度）は未着手**、工程7（説明つき CLI）は部分的。

```
打鍵列 ─ core/reading.py ─→ 読みの上位 k 件（打鍵ミスの編集弧・英単語の弧・n/nn・数字・記号）
       ─ core/constrained.py ─→ 各読みについて、辞書ラティス（core/kanakanji.py、数の弧 core/numbers.py）で
                                  出力を制約した zenz-v3.1-small の貪欲生成
       ─ core/pipeline.py ─→ score = zenz の変換の対数確率 + f×完成文の自然さ（core/select.py の fluency）− w×読みのコスト
                              の最大を返す（既定 k=5, w=4, f=1）
```

最新の数値（RTX 5060 Ti、詳細は EVAL.md「完成文の自然さを足す」）:

| | 方式1（規則→zenz） | 現行 core（k=5, w=4, f=1） | 工程4の目安 |
|---|---|---|---|
| user_style 73文 完全一致 | 53.4% | **82.2%**（ゆるい 89.0%） | 70% 以上 |
| うち tech（英単語） | 5.0% | **75.0%** | 60% 以上 |
| うち typo（打鍵ミス） | 9.1% | **63.6%** | 50% 以上 |
| 正常入力の誤補正 | — | 4.8% | 目安 2% 以下（工程2） |
| AJIMEE（ローマ字から） | 87.0% | 84.5% | |
| 1件の時間（GPU、中央値） | 138 ms | **1075 ms** | ThinkPad で 1 秒以内 |

使い方:

```
uv run python -m eval.run --converter core --set user_style --device cuda      # 既定 k=5,w=4,f=1
uv run python -m eval.run --converter "core:k=3,w=1.0,f=0" --set ajimee --device cuda
uv run python -m core.reading "githubnipushshiteoite"                          # 読みの候補とコストの内訳
uv run python -m core.kanakanji "キョウハイイテンキデスネ"                       # 辞書の N-best
uv run python -m eval.constrained_check --device cuda --context                # 正解の読みからの変換段だけ
```

`CorePipeline.candidates(romaji, left_context)` が候補ごとの点数の内訳（lm・fluency・read_cost・edits）を返す。1文の挙動を調べるときはこれを使う。

## 2. 再開したら最初にやること（優先順）

1. **速度（工程6）**: いまは k=5 件の制約付き生成を順に実行していて、GPU でも中央値 約1秒。対策の候補: k 件をバッチでまとめて生成、読みのコストで見込みの薄い読みを足切り、プロンプトの共通部分の KV キャッシュ共有、k を可変にする（読みの1位と2位のコスト差が大きければ k=1）。そのうえで ThinkPad（Core Ultra 7 255H）で実測する。本番の実行系（HF のまま / llama-cpp-python / llama-server の grammar）もここで決める（REDESIGN.md「残った違い」）。
2. **誤補正**: 自然さを足すと「珍しい語をよくある語に直す」（視線計測→自然計測、どれ→これ）。w を上げると減るが typo も落ちる（EVAL.md の表）。長さの偏り（字を捨てる修正で文が短くなると得をする）を打ち消す正規化や、編集の種類ごとのコストの見直しが候補。
3. **辞書に無い英字・略語**（cdma, jr, scc-0210）を打鍵ミスとして直してしまう（gen_test の clean で誤補正 約1割）。英単語リスト（core/data/tech_words.tsv）に無い英字は保持率が低い（gen_test 英語保持 約3割）。
4. **ローマ字としても読める英単語**（code→コデ、token→トケン、ci→チ）。自然さで token は救えたが、短い語（ci）は文脈が無いと決まらない。
5. **読みの段の打鍵ミス**: 打鍵ミス1〜2個入りの AJIMEE で、正解の読みが上位5以内 63.5%（目安 80%、docs/READING.md）。正解が候補に入らない分は後段で救えない。
6. 本人の理想（DESIGN.md）である **句読点・?!・鍵かっこ・改行の自動挿入**は未着手。zenz は打たれていない句読点を挿入しない（REDESIGN.md「3者で確かめた事実」）。独立の実験として可否を測る予定だった。
7. IME 化（Windows TSF、2028年以降は Debian の fcitx5）は未着手。先行例の Roman IME（打鍵の停止で自動変換）・RomKana・azooKey-Desktop は REDESIGN.md「先行例」。

## 3. 止めた路線（再開時に繰り返さない）

- **zenz の微調整**（train/finetune.py、v1: ローマ字→出力、v2: ローマ字→読み→出力）: かな漢字変換の知識が削れる（AJIMEE 87%→28.5%／正解の読みを与えても 87%→72.5%）。EVAL.md「微調整（方式3）」。学習データ生成（data/）と学習スクリプトは残してあるが、使う予定はない。
- **romaji2ja（limoXD/romaji2ja）の流用**: AJIMEE 3〜4%。記号を入力から捨てる作り。DESIGN.md「実測」。
- 評価セットの学習データとの重複: 生成した試験分割358件のうち21件が学習データと出力一致。`eval/sets/gen_test.jsonl`（337件）は除いた版。

## 4. 手元の環境（再開時に作り直すもの）

`models/` と `data/out*`、`eval/out/` はコミットしていない。休止時点の `models/` は約3.6GB で、すべて作り直せる:

| パス | 中身 | 作り直し方 |
|---|---|---|
| `models/mozc/` | Mozc dictionary_oss（固定コミット）と索引 | `uv run python -m core.fetch_mozc`（初回の索引構築は約9秒） |
| `models/reading/kana_lm.pkl` | 読みの文字 4-gram（Mozc 辞書の読み＋京大 Wikipedia 注釈コーパス） | `core.reading` の初回実行で自動作成 |
| `models/wac/` | 京大 Wikipedia 注釈コーパス（kana_lm の素材） | 同上で自動取得 |
| `models/ajimee/` | AJIMEE-Bench の評価データ | `eval.run --set ajimee` の初回で自動取得 |
| zenz-v3.1-small / xsmall | Hugging Face のキャッシュ（`~/.cache/huggingface`） | 初回の実行で自動取得 |
| `models/zenz-v3.2-*-gguf/`、`*-bench.gguf` | 速度の模擬に使った GGUF（bench はトークナイザ種別を書き換えた計測専用のコピー） | `hf download Miwa-Keita/zenz-v3.2-small-gguf` 等。DESIGN.md「実測」 |
| `models/zenz-romaji-v1/`・`v2/`・`zenz-romaji-v2-*.gguf` | 止めた微調整のモデル | 不要。作り直すなら train/finetune.py（RTX 5060 Ti で各20〜30分） |
| `models/romaji2ja/`・`romaji2ja-*.gguf` | 不採用の既製品 | 不要 |
| `models/probe/` | 学習速度の試走の残骸 | 不要（消してよい） |
| `models/llama.cpp-src/` | llama.cpp b10549 のソース（GGUF 変換スクリプト用） | `git clone --depth 1 --branch b10549 https://github.com/ggml-org/llama.cpp` |
| `models/lrec-ime/` | LREC 2026 の誤り訂正データ（形が合わず不使用） | 不要 |
| `data/out300k/`・`data/out/` | 微調整用に生成した学習データ（30万件・2万件） | `uv run python -m data.build --source hf --limit 300000 --out data/out300k/ --seed 0`（約90秒） |

- Python 環境: `uv sync --all-groups`（torch は cu128 の index。依存グループ core・data・eval・dev）。
- llama.cpp の実行ファイル: `C:\Users\tm976\mf\llama`（b10549、CUDA と CPU のビルド）。llama.cpp を使うテストは `LLAMA_CPP_DIR=models/llama.cpp-src`、`LLAMA_CPP_BIN=C:\Users\tm976\mf\llama` で動く。b10549 の llama-server は極小のランダムモデルの壊れた UTF-8 出力を HTTP 500 で拒むので、6件はこのマシンでは落ちる（INFER.md）。
- テスト: `uv run pytest`（休止時点で 207 件通過、llama.cpp を使う 6 件はスキップ）。

## 5. 作業の進め方で分かったこと（このリポジトリ固有）

- **10分を超える評価・学習は、Claude Code のバックグラウンド実行に任せない**（10分で子プロセスごと止まる）。`eval/out/sweep*.cmd` のように cmd に書き、WMI（`Invoke-CimMethod Win32_Process Create`）で切り離して起動し、ログと完了印（`*.done`）を見張った。
- **評価は別セッションの GPU 作業と取り合う**。休止時点の ms/件はすべて GPU を共有した状態の参考値で、速度の判定には使えない。
- **クラウドセッション**（Claude Code on the web）に部品を並行で作らせた（PR #1〜#5、ブランチ `claude/*`）。立て方は `tools/cloud-task.ps1`（`claude --cloud` は TTY が要るので、アプリの Terminal パネルで実行する）。依頼文の例は `tools/tasks/`。クラウドの既定環境は huggingface.co と download.pytorch.org に届かない（403）ので、zenz を使う作業と `uv lock` は手元でやった。クレジット（$250、2026-11-04 23:59 PT 失効）が使えるのはクラウドセッションだけで、ルーティン（/schedule）は対象外。
- 部品の境目は `core/types.py`（Reading / Seg / Edit / Conversion / Word）。別々に作った部品を挿し替えるための約束なので、変えるときは両側を一緒に直す。
- 相談役（astra = Codex consult、fable = fable-consultant）と2往復して方式を決めた。要点は REDESIGN.md。
