# 依頼: 読み段（core/reading.py）の打鍵ミス再現率を上げ、辞書に無い英字列を守る

最初に CLAUDE.md、docs/READING.md（仕組み・コスト・実測・失敗例・未解決の論点）、docs/REDESIGN.md「推奨構成」、core/reading.py、core/english.py、core/kana_lm.py、core/types.py、eval/reading_bench.py、tests/test_core_reading.py を読むこと。文書・コメント・コミットメッセージは日本語。

## 背景

変換コアは「読みの上位 k=5 件 → 辞書ラティスで制約した zenz の生成 → score = zenz の対数確率 + 自然さ − 4 × 読みのコスト」で1つを選ぶ（docs/STATUS.md）。**正解の読みが上位 5 に入らない分は後段で救えない。**

- 休止時点の読み段の実測（docs/READING.md、クラウドの Xeon）: AJIMEE 200件のきれいな入力で1位が正解 99.5%、打鍵ミス 1〜2 個入りで正解が上位5以内 **63.5%**（目安 80%、1位 27.5%、上位20 75%）、正常入力の誤補正 0.5%、1件 中央値 40 ms（20〜30字）。
- 後段で見えている読み段起因の失敗: 辞書に無い短い英字列（jr、scc、cdma のような略語・型番）を打鍵ミスとして直す経路のほうが安く、正しい「英字のまま」の経路が候補に残らない。体験版で `githubnipushshitejrnoapiwotamesu`（GitHub に push して JR の API を試す）が「GitHubにpushしているのAPIを試す」になった（jr → いる）。gen_test の clean（Wikipedia 由来）でも誤補正の約1割がこの型。
- 親セッションは後段の重み w=4 を今の読みコストの尺度で決めている。**コストの尺度全体を変えない**（個々の弧のコストの見直しはよいが、全体を定数倍するような変更は後段の重みを狂わせる。変えた弧とコストの前後の分布を報告する）。

## 目標（数値はすべて `eval/reading_bench.py` で測り、変更前・変更後を同じ表に並べる）

1. **打鍵ミス 1〜2 個入りの AJIMEE で、正解が上位 5 以内 80% 以上**（今 63.5%）。上位 1・上位 20 も併記。
2. **きれいな入力を崩さない**: AJIMEE きれいな入力で 1 位が正解 99.5% 以上（今 199/200）、正常入力の誤補正 0.5% 以下（AJIMEE）、`eval/sets/user_style.jsonl` の typo 以外（62文）で誤補正 0。`eval/sets/user_style_reading.jsonl` の上位1・上位5 を下げない。
3. **辞書に無い英字列を守る**: 新しい小さな評価セット `eval/sets/raw_tokens.jsonl`（30〜40 文。本人の用途を模した、略語・型番・固有の英字列を含む文: `jrnoekimadearuku`、`cdmanokikakuwoshiraberu`、`scc-0210womitsukete`、`rtx5060tidegakushuusuru`、`pcie4.0nossd`、`m2nomacbook` など。各行は `romaji`・`reading`（zenz_input の形。英字区間は小文字綴り、かなはカタカナ）・`meta.category`）で、正解の読みが 1 位・上位 5 に入る率を出す。目安: 上位 5 で 90% 以上。英字列の前後がかなに続く形（shite|jr|no）を必ず入れる。
4. **速度**: 20〜40 字で中央値 50 ms 以下、p95 150 ms 以下（クラウドの CPU、1プロセス）。変更前より 2 割以上遅くしない。

## 手がかり（READING.md「未解決の論点」と失敗例から。全部やる必要はない。効いたもの・効かなかったものを両方書く）

- 打鍵ミスの取りこぼしは「別の正しいかなになって 6 位以下に沈む」型と「経路が無い・途中で落ちる」型が半々。後者: 促音・記号まわりの編集弧が無い（`tsuku6tashiro` の t→6）、編集 3 つ以上、状態数の上限（MAX_STATES=12、MIN_PER_EDITS=3）で落ちる。前者: 文字 4-gram では区別できない。
- 候補の多様性: 上位 N を取り出すときに「同じ編集の仕方の変種」が枠を埋めていないか（編集位置だけ違う同種の候補、英語区間の有無だけ違う候補）。後段が比べたいのは「直さない／直す（代表1つ）／英語として読む」の異なる仮説。N-best に多様性の制約を入れる案（編集の集合が同じ候補は 1 つまで、など）。
- 辞書に無い英字列: `raw` のコスト（5.0 + 1.0/字）と編集コスト（3.0〜3.5）の関係。かなに挟まれた短い英字列（2〜4 字）で、編集で読める経路と raw 経路の両方を上位 5 に残す。raw を安くしすぎると正しいローマ字が英字のまま残る（`kai` → kai）ので、`_parses_as_romaji` で読める綴りは別扱い。
- 読みらしさの重み（lm_weight 0.3）・上限（lm_cap 9.0）・基準（lm_base 3.5）は WAC test で決めたもの。動かすなら WAC test で調整し、AJIMEE で検証する（調整と検証を分ける）。

## 作業の規則

- ブランチ `claude/reading-recall` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。**push 先はこのブランチだけ。**
- **変えてよいのは `core/reading.py`・`core/english.py`・`core/kana_lm.py`・`core/data/tech_words.tsv`・`eval/reading_bench.py`・`eval/sets/raw_tokens.jsonl`（新規）・`tests/test_core_reading.py`・`docs/READING.md`。** `core/types.py`（部品の約束）、`core/constrained.py`・`core/pipeline.py`・`eval/converters.py`（親が編集中）、`pyproject.toml`・`uv.lock` は変えない。`readings(keys, n, max_edits)` のシグネチャと戻り値の型も変えない。
- 認証情報・個人情報を書かない。外部サービスへの書き込みは PR の作成だけ。
- 素材（Mozc 辞書・京大 Wikipedia 注釈コーパス・AJIMEE）は raw.githubusercontent.com から取れる（`core/kana_lm.py`・`core/fetch_mozc.py`・`eval/datasets.py` が自動で取る）。huggingface.co には届かない（zenz は使えない。後段の評価は親がやる）。
- 最終報告（PR 本文）: 目標 1〜4 の変更前・変更後の表、変えた弧とコスト、効かなかった案、`uv run pytest`（torch 無しの venv でよい）の結果、未解決の論点。
