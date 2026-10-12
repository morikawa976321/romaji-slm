# 推論 CLI と GGUF 変換

微調整したモデル（`models/zenz-romaji-v1`、HF 形式）で、ローマ字を日本語に変換する。コードは `infer/`、テストは `tests/test_infer.py`。

## 使い方

```
# HF 形式（transformers、貪欲法）。--model の既定は models/zenz-romaji-v1
uv run python -m infer "githubnipushshiteoite"
uv run python -m infer "nanjikaradakke?" --context "明日の会議、"

# GGUF（llama.cpp の llama-server）。--model が .gguf なら --backend llama は省ける
uv run python -m infer "githubnipushshiteoite" --model models/zenz-romaji-v1-Q5_K_M.gguf
uv run python -m infer "..." --backend llama        # 既定の GGUF は models/zenz-romaji-v1-Q5_K_M.gguf

# 対話モード: ローマ字を省くと標準入力から1行ずつ読み、1行ずつ出す。
# 1行は `ローマ字` か `左文脈<TAB>ローマ字`。空行には空行を返す
uv run python -m infer --model models/zenz-romaji-v1-Q5_K_M.gguf

# 起動済みの llama-server に繋ぐ（呼ぶたびに起動を待たずに済む）
llama-server -m models/zenz-romaji-v1-Q5_K_M.gguf -c 0 -np 1 --port 8080
uv run python -m infer "kyouhaiitenki" --server-url http://127.0.0.1:8080
```

| オプション | 意味 |
|---|---|
| `--context` | 左文脈。対話モードでは TAB で区切った左文脈がないときに使う |
| `--backend hf\|llama` | 省くと `--model` の拡張子（`.gguf` なら llama）か `--server-url` の有無で決める。どちらもなければ hf |
| `--template` | プロンプト雛形（既定 `zenz-v3-romaji`。`eval/prompts.py`） |
| `--device` | hf のみ。`cpu` / `cuda` |
| `--llama-bin` | llama-server のあるディレクトリ。省くと `$LLAMA_CPP_BIN`、`$LLAMA_CPP_DIR/build/bin`、PATH の順に探す |
| `--threads` / `--ngl` | llama-server の `-t` / `-ngl`（内蔵 GPU に載せる層数） |
| `-v` | 1件ごとの所要時間とトークン数を標準エラーに出す |

プロンプトと生成の上限（`2 × ローマ字の長さ + 16` トークン）は、評価ハーネスの `HFCausalLM` と同じにした。
`infer.llama.LlamaCppLM` は `HFCausalLM` と同じ口（`convert` / `convert_detailed`）を持つ。

## GGUF への変換

llama.cpp の**ソースのチェックアウト**（`convert_hf_to_gguf.py` と `gguf-py/`・`conversion/`）と、
**実行ファイル**（`llama-quantize`、`--check` には `llama-server`）が要る。実行ファイルは、同じ版のリリース zip
（Windows なら CPU 版、または内蔵 GPU 用の Vulkan / SYCL 版）を展開したものでも、チェックアウトをビルドしたものでもよい。

```
git clone https://github.com/ggml-org/llama.cpp <dir>
# 実行ファイル: <dir>/build/bin にビルドするか、リリース zip を展開して LLAMA_CPP_BIN で指す
uv run python -m infer.to_gguf models/zenz-romaji-v1 --llama-cpp <dir> --check
```

出力（既定はモデルと同じ `models/`。コミットしない）:

| ファイル | 作り方 |
|---|---|
| `zenz-romaji-v1-F16.gguf` | `convert_hf_to_gguf.py --outtype f16` |
| `zenz-romaji-v1-Q8_0.gguf` | `convert_hf_to_gguf.py --outtype q8_0`（元の精度から直接） |
| `zenz-romaji-v1-Q5_K_M.gguf` | `llama-quantize` で F16 から |

`--types q8_0,q5_k_m` で作る形式を絞れる。`--check` は、作った GGUF ごとに llama-server を立て、
HF のトークナイザ（`add_special_tokens=False`。評価ハーネスと同じ）と llama-server の `/tokenize`（`/completion` と同じ条件）で、
次のトークン列が一致するかを確かめる。食い違いがあれば終了コード 1。

- zenz-v3-romaji 形式のプロンプト
- `eval/sets/user_style.jsonl` の全件のプロンプトと正解
- 空白・改行・TAB・全角空白・ASCII 記号・全角記号・絵文字・サロゲート領域の漢字（𠮷）・半角カナ・英語の短縮形（`'s` `'ll`）

`convert_hf_to_gguf.py` は書き換えず、子プロセスの中で次の2点だけを差し替えて動かす（`infer/to_gguf.py` の `_BOOTSTRAP`）:

1. pre-tokenizer の判定（`get_vocab_base_pre`）を、常に `gpt-2` を返すようにする（次節）。その前に、HF のトークナイザが
   GPT-2 と同じ ByteLevel（正規表現あり・先頭空白なし・normalizer なし）であることを確かめ、違えば変換しない。
2. GPT-2 の変換は `config.json` の `n_ctx` を必須とするが、transformers 5 の `GPT2Config` は `n_ctx` を保存しない
   （`n_positions` だけ）。transformers 5 で保存し直したモデルは `KeyError: 'n_ctx'` で止まるので、`n_positions` で補う。

## pre-tokenizer

### 結論

**`tokenizer.ggml.pre = gpt-2` にする。** 公式 llama.cpp はこれを読み込めて、HF のトークナイザと同じトークン列を返す。

### 根拠

- zenz のトークナイザは ku-nlp/gpt2-small-japanese-char のもの（`GPT2Tokenizer`、語彙 6000）。その `tokenizer.json` の
  pre-tokenizer は `ByteLevel(add_prefix_space=False)` で、正規表現による分割は既定どおり有効、normalizer はない。
  `ByteLevel` が使う正規表現は GPT-2 のもの（`'s|'t|'re|'ve|'m|'ll|'d| ?\p{L}+| ?\p{N}+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+`）。
- llama.cpp の `gpt-2` 種別は、まさにこの正規表現で分割する（`src/llama-vocab.cpp` の `LLAMA_VOCAB_PRE_TYPE_GPT2`）。
- 配布されている zenz の GGUF が名乗る `gpt2-small-japanese-char` は、azooKey のフォーク
  （`ensan-hcl/llama.cpp` の `ku-nlp/gpt2-japanese-char` ブランチ、2024-09）で追加された種別で、中身は
  **GPT-2 と同じ正規表現への割り当てだけ**（`case LLAMA_VOCAB_PRE_TYPE_GPT2: case LLAMA_VOCAB_PRE_TYPE_GPT2_SMALL_JAPANESE_CHAR:`）。
  公式に取り込まれていないので、公式版は `unknown pre-tokenizer type` で読み込みを拒む。
- 私用領域の文字（zenz の区切り `U+EE00`〜）は、HF（tokenizers）でも llama.cpp でも「文字・数字・空白以外」の側に入る
  （llama.cpp は Unicode の Co を制御文字の旗で持ち、GPT-2 の分割ではそれも記号の塊に入れる）。両者の分割は変わらない。

### 確認（2026-10-12、llama.cpp b11562・commit b2f036c を Linux x86_64 でビルド）

1. **本物の ku-nlp/gpt2-small-japanese-char のトークナイザ**（フォークの `models/tokenizers/` に同梱されているもの。
   作業用に手元へ置いただけでコミットしていない）と、ランダム初期化の小さな GPT-2 を組み合わせて変換した。
   `--check` の文に、語彙の文字を無作為に並べた2000文と語彙のトークンを無作為につないだ2000文を足した計4091文で、
   **`gpt-2` は食い違い 0**。F16・Q8_0・Q5_K_M のどれも同じ。transformers 5 で `save_pretrained` し直したトークナイザでも一致した。
   同じ GGUF の `tokenizer.ggml.pre` だけを書き換えた対照実験では、`default`・`qwen2`・`bloom` も食い違い 0 だった。
   これは、この語彙が**純粋に文字単位**だからである（6000語のうち複数文字のトークンは 0、文字の途中までのバイト列が 599）。
   BPE の結合が1文字の中で閉じるので、文字の途中で切らない分割ならどれでも結果が同じになる。
2. **複数文字の結合を持つテスト用トークナイザ**（`tests/test_infer.py` が手元で学習させるもの）では、正規表現の違いが結果に出る。
   391文で、`gpt-2` 0件、`default` 2件、`qwen2` 2件、`deepseek-llm` 3件、`bloom` 6件が食い違った。
   `gpt-2` は、正規表現が HF と同じなので語彙によらず一致し、テストもその食い違いを検出できる。

**未確認**: zenz-v3.1-small の配布物そのもの（この環境から Hugging Face に届かない）。語彙数とクラスは ku-nlp と同じだが、
ファイルが同一かは確かめていない。微調整後のモデルを変換するときに `--check` を付ければ、その場で確かめられる。

### 配布されている zenz の GGUF を公式 llama.cpp で使うには

メタデータを書き換えれば読み込める（重みはそのまま）:

```
python <llama.cpp>/gguf-py/gguf/scripts/gguf_new_metadata.py zenz-v3.2-small-Q5_K_M.gguf out.gguf --pre-tokenizer gpt-2
```

## backend llama の方式: llama-server を子プロセスで使う

`llama-cpp-python` ではなく、llama.cpp の `llama-server` を子プロセスで起動し、標準ライブラリの HTTP（`urllib`）で呼ぶ。

- **本番機の内蔵 GPU に、配布物のまま届く。** llama.cpp は Windows 向けに Vulkan 版・SYCL 版の実行ファイルを毎版配布している。
  一方 `llama-cpp-python` は PyPI に sdist しかなく（0.3.36、2026-10-01）、入れるたびに C++ のビルドが走る。
  内蔵 GPU を使うには、Windows で MSVC と Vulkan SDK などを揃えてビルドすることになる。
- **速度を測った実行ファイルと同じものを使える。** DESIGN.md の速度の模擬は、llama.cpp の配布物（`llama-bench`）で測った。
  `llama-cpp-python` は同梱する llama.cpp の版が本体から遅れる。
- **モデルを読み込んだまま使える。** `llama-cli` を1回ずつ呼ぶと、毎回モデルの読み込みが入る。サーバーは1回読み込めば済み、
  プロンプトのキャッシュ（`cache_prompt`）も効く。
- **Python 側の依存が増えない。** pyproject.toml と uv.lock は変えていない。

代わりに、子プロセスの管理と HTTP のぶんの手間がかかる（localhost で数 ms 未満の見込み。ThinkPad では測っていない）。
CLI を1回呼ぶたびにサーバーを起動するので、繰り返し使うなら `--server-url` で起動済みのものに繋ぐ。
IME に組み込む段階で、libllama を直接呼ぶ形に替えるかを改めて決める。

llama-server の起動引数は `-c 0 -np 1 --no-webui`。`-c 0` は、文脈長をモデルから取る指定である。GPT-2 の位置埋め込みは
学習時の長さ（zenz は 1024）しかないので、それより長い文脈長で起動してはいけない。生成は `temperature 0`・`top_k 1`
（貪欲法）で、EOS か上限で止まる。

## 既知の問題・注意

- **空白・改行・CR は語彙にない**（zenz も同じ。docs/EVAL.md の語彙調査）。HF と llama.cpp のどちらでも、エンコードで黙って消える。
  両者は一致する（テストで確認）。入力に空白を打たない前提なので、実用上は困らない。
- 語彙にないバイト（`0xF2`〜`0xFF` で始まる面の文字や、一部の制御文字）が文字の塊の途中にあるとき、HF はそれを捨ててから
  前後を結合しうるが、llama.cpp は捨てたバイトの位置で結合を止める。複数文字のトークンがない zenz の語彙では、結果は変わらない。
- `convert_hf_to_gguf.py` の内部（`get_vocab_base_pre`・`TextModel.__init__`）を差し替えている。llama.cpp の版によっては
  名前が変わって動かなくなるおそれがある。確認したのは b11562 だけ。DESIGN.md の速度の模擬に使った b10549 では試していない。
  そこで止まったら、エラーを見て `_BOOTSTRAP` を直す。
- **量子化による精度の低下は、まだ測っていない**（実モデルが要る）。f32 の GGUF なら、貪欲法の生成トークン列が HF と一致することは
  テストで確かめた（ランダム初期化の極小モデル）。f16 以下は数値が変わるので、変換後に評価セットで比べる必要がある。
  評価ハーネス（`eval/run.py`）から llama backend を選べるようにするのは、まだやっていない。
- GGUF の `tokenizer.ggml.add_bos_token` が真だと、llama-server は BOS を前置して HF と食い違う。zenz の `tokenizer_config.json`
  は `add_bos_token: false` で、変換結果もそうなる（テストで確認）。
- **速度は測っていない。** 本番の判定は ThinkPad（Core Ultra 7 255H）上の値で行う。

## テスト

```
uv run pytest tests/test_infer.py
# llama.cpp を使うテスト（変換・トークン列の一致・貪欲法の一致・CLI）も動かすとき
LLAMA_CPP_DIR=<llama.cpp のチェックアウト（build/bin にビルド済み）> uv run pytest tests/test_infer.py
```

ネットワークは要らない。トークナイザも重みも手元で作る。トークナイザは zenz と同じ種類にした
（`GPT2Tokenizer` を名乗る、バイト単位 BPE、特殊トークンの並び `[UNK] [PAD] <s> </s>`、空白・改行・CR のバイトを語彙に持たない）。
ただし zenz と違って複数文字の結合も学習させ、pre-tokenizer の違いが結果に出るようにしてある（上の「確認」2）。
`LLAMA_CPP_DIR` がなければ llama.cpp を使う6件は飛ばす。

| テスト | 確かめること |
|---|---|
| `test_tokenizer_is_zenz_like` / `test_check_tokenizer_rejects_other_pre_tokenizers` | テスト用トークナイザの性質と、変換前の判定 |
| `test_cli_hf_*` / `test_parse_args_backend` | CLI（1件・標準入力）と backend の決め方 |
| `test_gguf_metadata` | GGUF の `tokenizer.ggml.pre = gpt-2`、EOS、`add_bos_token` |
| `test_tokenization_matches_hf` | F16・Q5_K_M で、HF と llama-server のトークン列が一致（`--check` の文＋無作為な300文） |
| `test_greedy_generation_matches_hf` | f32 の GGUF で、貪欲法の生成トークン列が HF と一致 |
| `test_cli_llama` / `test_llama_converter_interface` | llama backend の CLI、`--server-url` での接続 |

確認環境（2026-10-12）: Linux x86_64（4コア）、Python 3.12、torch 2.14.1（PyPI）、transformers 5.19.0、
llama.cpp b11562（commit b2f036c、CPU のみでビルド）。この環境は download.pytorch.org に届かないので、
pyproject.toml の cu128 版ではなく、別の仮想環境に PyPI の torch を入れて動かした。

## llama.cpp の版による差（2026-10-12、ローカルの Windows で確認）

- llama.cpp b10549 の `llama-server` は、生成結果が UTF-8 として壊れていると `/completion` が HTTP 500（`The model produced output that does not match the expected Content-only format`）を返す。`tests/test_infer.py` の極小ランダムモデルはでたらめなバイト列を出すので、b10549 では llama.cpp を使うテストが落ちる（クラウドで確かめた b11562 では通る）。学習済みのモデル（`models/zenz-romaji-v2-Q5_K_M.gguf`）では起きなかった。
- 二段形式（`zenz-v3-romaji-reading`）は、`infer` の llama 経由でも読みを切り落として変換結果だけを返す。
