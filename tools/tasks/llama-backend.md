# 依頼: llama-server 実行系（ラティス → GBNF 文法、並列スロット、自然さの強制生成）

最初に CLAUDE.md、docs/REDESIGN.md（「残った違い: 制約のかけ方の順番」）、docs/INFER.md（「backend llama の方式」「llama.cpp の版による差」）、docs/decisions.md（D3・D6）、core/constrained.py、core/pipeline.py、core/select.py、infer/llama.py、infer/to_gguf.py、tests/test_infer.py（極小の zenz 風モデルと GGUF の作り方、llama.cpp の建て方）を読むこと。文書・コメント・コミットメッセージは日本語。

## 背景（親セッションの実測、2026-10-15、Core Ultra 5 245KF、CPU）

- 現行の変換コア（HF/torch、k=5 件の制約付き生成をバッチ化）は 20〜45 字で中央値 約400 ms。1 トークンの forward が約 13 ms で、これは torch の固定費。
- llama-server（b10549、Q8_0、`-t 8`）は 1 トークン約 2.1 ms（prompt 1.3 ms/トークン）。
- **llama-server の `/completion` は、`grammar` で制約しても `n_probs` の確率を制約前の分布で返す**（既定。`post_sampling_probs: true` にすると再正規化されて 1 になる）。文法で文を強制生成したときの選択トークンの logprob の和は、HF の teacher forcing と一致した（例文で −1.635 対 −1.653、別の文で −17.72 対 −17.48。差は Q8_0 の量子化）。つまり、変換の対数確率（`decode_scored` の logp）も完成文の自然さ（`core.select.fluency`）も llama-server だけで取れる。
- 要件は ThinkPad（Core Ultra 7 255H、内蔵 GPU のみ）で 20〜30 字 1 秒以内。llama.cpp は Windows 向けに CPU 版・Vulkan 版の実行ファイルが配布されているので、内蔵 GPU にも届きうる。

## 作るもの

### 1. `core/gbnf.py` — 辞書ラティスを GBNF 文法にする

- `lattice_grammar(reading, dic, punct="auto", en_case="zenz") -> str`。`core.constrained.LatticeMatcher(reading, dic, punct, en_case)` を作り、その `out`（節点 → [(表層のバイト列, 次の節点)]）と `final` から右線形文法を作る:
  - `root ::= n0`、`n<i> ::= "<表層>" n<j> | ...`。`final` へ入る弧は継続なしの裸のリテラル。`final` に届かない節点（行き止まり）は後ろ向きの到達可能性で刈る。
  - リテラルは UTF-8 の文字列として書く（`out` のバイト列は常に完全な UTF-8 文字列。`"` と `\` はエスケープ）。同じ節点の同じ表層・同じ次節点の重複は1つにする。
  - 文法の大きさ（規則数・バイト数）を返す関数も用意する（速度の記録用）。
- 性質のテスト: ラティスのどの経路の表層も文法が受理する／ラティス外の文字列は受理しない（llama.cpp の `llama-gbnf-validator` があれば使う。無ければ、文法を Python で読む小さな受理判定器をテスト内に書く）。

### 2. `core/llama_backend.py` — llama-server を使う生成と採点

- `LlamaServer`: 子プロセスで `llama-server -m <gguf> -c 0 -np <slots> --no-webui -t <threads> --port <port>` を起動して `/health` を待つ（`infer/llama.py` に先例がある。流用・共通化してよい）。`--server-url` で起動済みのものにも繋げる。終了時に必ず止める。`LLAMA_CPP_BIN` で実行ファイルの場所を指す（親の Windows では `C:\Users\tm976\mf\llama`、b10549）。
- `decode_batch(readings, dic, left_context, punct, en_case) -> list[core.constrained.Decoded]`: 読みごとに文法を作り、`/completion`（`prompt` = `core.select.zenz_prompt(reading.zenz_input(), left_context)`、`grammar`、`temperature 0`、`top_k 1`、`n_predict` = `3*len(reading.zenz_input())+8`、`n_probs 1`、`cache_prompt true`）を**スロット数ぶん並列**（スレッド）で投げる。`completion_probabilities` から選択トークンの logprob の和を取る（版により `{"token","prob","top_probs"}` と `{"id","token","logprob","top_logprobs"}` の両方の形がある。**EOS の logprob が含まれるか**を極小モデルで確かめ、含まれなければ `n_probs` の候補から EOS の確率を取るか、含まれない旨を docs に書いて HF 側（`decode_scored` は EOS を含む）との差を明記する）。`finished` は文法を完走して EOS で止まったか（`stop_type`／`stopped_eos`）。`bounds`（語の切れ目。句読点の自動挿入に使う）は、出力の文字列を `LatticeMatcher.advance` で先頭から辿り直し、`at_boundary` が真になる文字位置を集めて作る（純 Python。関数 `bounds_from_text(matcher, text)` を `core/gbnf.py` か本モジュールに置く）。
- `fluency(texts, left_context) -> list[float]`: `core.select.fluency` と同じ量を、`prompt` = `CONTEXT + left_context`、`grammar` = `root ::= "<text + INPUT>"` の強制生成で取る（EOS は含めない。`n_predict` は十分大きく）。並列。
- `LlamaCorePipeline`（`core.pipeline.CorePipeline` と同じインターフェース: `candidates`・`convert_detailed`・`convert`、同じ score の式、`punct="insert"` の `insert_punct` も同じ振る舞い）。`CorePipeline` のコードを複製せず、生成と採点の2点だけ差し替えられる形にする（例: `CorePipeline` に `backend` を注入する、または共通の基底に括り出す。**ただし `core/pipeline.py`・`core/constrained.py` は親が並行して編集しているので、変更は最小にし、変えた箇所と理由を PR 本文に書く**）。
- `eval/converters.py` に `core-llama`（`--model <gguf>`、オプションは `core` と同じ `k,w,f,p,e,tc,te` に加えて `np=<スロット数>,t=<スレッド数>`）を足す。追加は `core` の分岐のすぐ下に数行だけ。

### 3. 同等性のテスト（`tests/test_llama_backend.py`。`LLAMA_CPP_DIR` が無ければ skip）

- `tests/test_infer.py` の極小 zenz 風モデルを f32 の GGUF にし（`infer.to_gguf`）、手書きの小さな辞書（`tests/test_core_constrained.py` の fixture を流用）で、
  - `decode_batch` の出力の文が `core.constrained.ConstrainedZenz.decode_batch` と一致し、logp の差が 1e-2 以内（f32）。読みの長さが違う 5 件、左文脈あり／なし。
  - `fluency` が `core.select.fluency` と 1e-2 以内で一致。
  - `bounds_from_text` が `Decoded.bounds` と一致。
  - 文法の受理性（上記 1）。
- llama.cpp はこの環境でソースからビルドできる（tests/test_infer.py の確認環境と同じ b11562 以降でよい）。親の b10549 では壊れた UTF-8 出力に HTTP 500 を返す版差がある（docs/INFER.md）。極小モデルはでたらめなバイト列を出しうるので、**文法で制約した出力は完全な UTF-8 になる**はず。もし 500 が出る場合は原因を記録する。

### 4. 計測の道具と文書

- `eval/bench_backend.py`: 同じ入力列（`eval/sets/user_style.jsonl` の 20〜45 字）を `core` と `core-llama` で回し、1件の中央値・p95、文法の大きさの中央値、`/completion` の `timings`（prompt/predicted per token）を表にする。親が実モデルで走らせる。
- `docs/LLAMA_BACKEND.md`: 仕組み（文法の形・並列・自然さの取り方）、HF 版との差（量子化・EOS・トークン分割の違い）、使い方、極小モデルでの同等性の結果、未解決の論点（文法が大きいときの llama.cpp 側の負荷、`-np` を増やしたときの1ステップの時間、Vulkan 版での動作）。

## 作業の規則

- ブランチ `claude/llama-backend` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。**push 先はこのブランチだけ。**
- `pyproject.toml`・`uv.lock` は変えない（標準ライブラリの `urllib`・`subprocess`・`threading` で足りる）。`core/pipeline.py`・`core/constrained.py` の変更は最小（上記）。
- 認証情報・個人情報を書かない。外部サービスへの書き込みは PR の作成だけ。
- この環境からは huggingface.co と download.pytorch.org に届かない（実モデルは使えない。極小モデルで同等性を確かめる）。github.com・pypi.org は届く。
- 最終報告（PR 本文）: 作ったファイル、同等性テストの結果（llama.cpp の版を添える）、`core/pipeline.py`・`core/constrained.py` に触れた箇所、親が実モデルで確かめること、未解決の論点。
