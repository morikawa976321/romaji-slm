# 変換コアの llama-server 実行系（core-llama）

変換コア（`core.pipeline.CorePipeline`）の「制約付き生成」と「完成文の自然さ」を、transformers ではなく
llama.cpp の `llama-server` で計算する実行系。点数の式・句読点の挿入・読みの列挙は CorePipeline のまま共有する。
決めた経緯は [decisions.md](decisions.md) の D3・D6、方式の比較は [REDESIGN.md](REDESIGN.md)「残った違い: 制約のかけ方の順番」。

| ファイル | 中身 |
|---|---|
| `core/gbnf.py` | 辞書ラティス → GBNF 文法（`lattice_grammar`）、文法の大きさ（`grammar_size`）、語の切れ目（`bounds_from_text`） |
| `core/llama_backend.py` | `LlamaBackend`（`decode_batch`・`fluency`）と `LlamaCorePipeline` |
| `eval/converters.py` | 変換器 `core-llama` |
| `eval/bench_backend.py` | `core` と `core-llama` の速度の比較表 |
| `tests/test_llama_backend.py` | 文法の受理性と、transformers 版との同等性 |

## 使い方

```
# GGUF は infer.to_gguf で作る（docs/INFER.md）。llama-server は $LLAMA_CPP_BIN か $LLAMA_CPP_DIR/build/bin か PATH から探す
uv run python -m eval.run --converter "core-llama:np=5,t=4" --model models/zenz-v3.1-small-gguf/zenz-v3.1-small-F16.gguf --set user_style
# 起動済みの llama-server に繋ぐ（-np はサーバー側で指定したもの。-c は 1024×スロット数、CPU だけなら -ngl 0 --device none）
llama-server -m models/zenz-v3.1-small-gguf/zenz-v3.1-small-F16.gguf -c 5120 -np 5 --no-webui -t 4 -ngl 0 --device none --port 8080
uv run python -m eval.run --converter "core-llama:url=http://127.0.0.1:8080" --set user_style
# 速度の比較（親が実モデルで走らせる。変換器は ; で区切る）
uv run python -m eval.bench_backend --gguf models/zenz-v3.1-small-Q8_0.gguf --converters "core;core-llama:np=5,t=8;core-llama:np=1,t=8"
```

`core-llama` のオプションは `core` と同じ `k,w,f,p,e,tc,te` に加えて、`np`（スロット数、既定 5）、`t`（`-t`、既定は llama.cpp の既定）、
`url`（起動済みのサーバー）、`ngl`（GPU に載せる層。既定 0 = CPU だけ、`auto` はサーバーの既定）、比べるための変種 `de`・`fl`・`rf`・`ka`・`eos`
（[LLAMA_PARITY.md](LLAMA_PARITY.md)「変種」）。Python からは `LlamaCorePipeline(gguf, n_slots=5, threads=4, k=5, punct="insert")`。

起動時に `LlamaBackend` が確かめて、足りなければ立て直すもの（自前で立てたとき。`url` の起動済みサーバーなら例外にする）:

- GGUF の EOS/BOS が `</s>`/`<s>` でなければ `--override-kv` で直す（zenz の `config.json` の誤った ID が GGUF に入りうる。`infer.to_gguf` は作るときに直す）。
- `-np` が 2 以上で 1 スロットの文脈長が学習時の長さより短ければ、`-c` を 学習時の長さ × スロット数 にする。
- 既定は CPU だけ（`-ngl 0 --device none`。CUDA ビルドは `-ngl` を渡さないと GPU に載せ、`-ngl 0` だけでも一部の演算を GPU に回す）。
- 自然さの強制生成は正規のトークン列を ID の文法で強制し（`fl=tok`）、取り直しは起動時に近似が使えるモデルか確かめて決める（`rf=auto`）。
  HTTP の接続は使い回す（`ka=1`）。どれも出力は 2026-10-18 より前の既定と同じ（LLAMA_PARITY.md の L7・Lz3）。

## 仕組み

### 文法（core/gbnf.py）

`LatticeMatcher(reading, dic, punct, en_case)` の弧（節点 → [(表層のバイト列, 次の節点)]）をそのまま右線形文法にする。

```
root ::= n0
n0 ::= "今日" n3 | "京" n3 | "キョウ" n3 | "きょう" n3 | ...
n3 ::= "は" n4 | "ハ" n4
n9 ::= "ね" | "ネ"             # 終点へ入る弧は継続なし
```

- 節点1つが規則1つ。終点に届かない節点（行き止まり）は後ろ向きの到達可能性で刈る。
- リテラルは UTF-8 の文字列（`"` と `\` と制御文字はエスケープ）。同じ節点の同じ表層・同じ次節点は1つにする（LatticeMatcher がすでに重複を除いている）。
- 経路が1つも無ければ文法を作らず（空の言語は GBNF で書けない）、生成もしない（`finished=False`）。
- 実際の Mozc 辞書で user_style の正解 20〜45 字の14文の読み（1位）を文法にすると、中央値で約 22 規則・約 10.9 KB。
- 語の切れ目（句読点の自動挿入の候補位置）は、出力の文字列を `LatticeMatcher.advance` で1字ずつ辿り直して作る（`bounds_from_text`）。
  ConstrainedZenz はトークンの切れ目でだけ調べるので、複数文字のトークンがある語彙では内側の切れ目を拾わないが、zenz の語彙は
  1文字1トークンなので同じになる（テストで一致を確認）。

### 生成（decode_batch）

読みごとに `/completion` を1回呼ぶ: `prompt` = `zenz_prompt(読み, 左文脈)`、`grammar`、`temperature 0`・`top_k 1`（貪欲法）、
`n_predict` = `3×len(読み)+8`、`n_probs 1`、`cache_prompt`、`return_tokens`。k 件を `-np` の数までスレッドで並べて投げる。

- **logp**: `completion_probabilities` の各項目の `logprob` の和。llama-server は既定（`post_sampling_probs: false`）で
  文法をかける前の分布の確率を返す（D6 の実測）ので、ConstrainedZenz の logp と同じ量になる。古い版の `{"prob", "top_probs"}` の形なら対数を取る。
- **EOS**: llama.cpp 62df181 では、EOS で止まったときの EOS のトークンも `completion_probabilities` に載る（`tokens_predicted` も EOS を数える）。
  よって logp は ConstrainedZenz と同じく EOS を含む。載らない版でも、下の「取り直し」で EOS の分を埋めるので値は同じになる。
  `LlamaBackend.eos_logprob_included` に、その版で載ったかを記録する。
- **finished**: `stop_type == "eos"`（古い版は `stopped_eos`）。文法は終点でしか EOG を許さないので、完走して EOS で止まったことを意味する。

### 完成文の自然さ（fluency）

`core.select.fluency` と同じ量（`log P(文 + INPUT | CONTEXT + 左文脈)`、EOS は含めない）を、`prompt` = `CONTEXT + 左文脈`、
文法 `root ::= "<文 + INPUT>"` の強制生成で取る。transformers 版は正規のトークン列（トークナイザの分割）で採点するが、
強制生成は同じ文字列を別の分割（字をバイトのトークンで出すなど）で出しうる。そこで:

1. 文字列を `/tokenize` で正規のトークン列にする。それが字の途中のバイトのトークンを含まなければ（＝語彙に無い字が無ければ）、
   字の途中のバイトのトークンを `logit_bias` で禁じて強制生成する（zenz の語彙は1文字1トークンなので、これで正規の分割に揃う）。
2. 生成したトークン列が正規の分割と違えば、正規の分割の各位置の確率を1つずつ取り直す（下の「取り直し」と同じ要求）。

### llama-server の癖への対処

- **CUDA ビルドは黙って GPU に載せる。** b10549 の `-ngl` の既定は `auto` で、`LlamaServer` が `-ngl` を渡さないと、手元の CUDA ビルドでは全層が GPU に載る。
  `-ngl 0` でも既定の `--op-offload` で一部の計算を GPU に回す。`LlamaBackend` の既定は `-ngl 0 --device none`（CPU だけ。CUDA 抜きの版と点数まで同じに
  なることを user_style で確かめた）。`infer`（制約なしの生成）の `LlamaServer` の既定は今も `-ngl` を渡さない。
  2026-10-15 の「CPU で torch 版の約 0.55 倍」（D12）は GPU の数値だった（[LLAMA_PARITY.md](LLAMA_PARITY.md)）。
- **GGUF の BOS/EOS の ID が誤っている。** 上の「transformers 版との差」の表。起動時に確かめて直す（上の「使い方」）。
- **`-np 5` で 1 スロット 256 トークン**（b10549 は `-c 0` の 1280 をスロットで割る）。起動時に `-c` を 1024×スロット数にする。
- **要求ごとに接続し直すと、連続実行で Windows のポートが尽きる**（WinError 10055・10060）。1 件あたり 47 要求（取り直しを含む）のため。既定で接続を使い回し
  （`ka=1`）、サーバーが閉じた接続に送ったら 1 回だけ繋ぎ直す。
- **`[PAD]` が EOG になる。** llama.cpp 62df181 の語彙の読み込み（`src/llama-vocab.cpp`）は、`[PAD]` という名前のトークンを
  FIM_PAD とみなし、EOG の集合に入れる（Kimi-K2 向けの表）。zenz の語彙も `[PAD]`（ID 1）を持つので、文法の終点で
  `</s>` より `[PAD]` の確率が高いと `[PAD]` を選んで止まり、logp が変わる（極小モデルで実際に起きた）。
  EOS 以外の表層が空のトークン（特殊トークン）を `logit_bias` で禁じて防ぐ。ConstrainedZenz も EOS 以外の空のトークンは選ばない。
  `logit_bias` は `n_probs` の値（制約前の分布）を変えない。infer の llama backend（制約なしの生成）でも `[PAD]` で止まりうるが、
  そこでは止まり方が EOS と同じなので実害は小さい。
- **字の途中で終わるトークンの確率は返らない。** llama-server は、生成した文字列の末尾が UTF-8 の文字の途中のあいだは
  `completion_probabilities` に項目を足さない（文字が完成したトークンの項目だけが載る）。語彙に無い字をバイトのトークンで出すと、
  その字の最後のトークン以外の logprob が抜ける。起動時に語彙全体の確率を1回取って「トークン ID → バイト列」の表を作り、
  サーバーと同じ判定で抜けた位置を割り出して、そこまでのトークン列をプロンプトにした要求（文法 `root ::= ""`、`n_probs` = 語彙数）で埋める。
  zenz では語彙に無い字（まれな漢字・絵文字）を含む候補のときだけ起き、1位置につき1要求増える（`n_refill` に数える）。

## transformers 版との差

実測は [LLAMA_PARITY.md](LLAMA_PARITY.md)（2026-10-18）。

| 差 | 中身 | 影響 |
|---|---|---|
| **BOS/EOS の ID** | zenz の `config.json` は `bos_token_id=1`・`eos_token_id=2` だが、トークナイザの実際は `[PAD]`=1・`<s>`=2・`</s>`=3。`infer.to_gguf` の GGUF にはこの誤った値が入り、llama-server は `<s>` を EOS とみなす。LlamaBackend は EOS 以外の空のトークンを禁じるので、本物の `</s>` を禁じてしまう | **正常入力を壊していた 6 件の原因**。lm に log P(`<s>`)（約 −27、候補ごとに数 nats ぶれる）が乗り、また終わりを避けて余計な字を足す（「一新ン」）。起動時の `--override-kv tokenizer.ggml.bos_token_id=int:2,tokenizer.ggml.eos_token_id=int:3` で直る。2026-10-18 から、`infer.to_gguf` が作るときに直し、`LlamaBackend` も起動時に確かめて直す |
| 行き止まり | ConstrainedZenz の照合は行き止まりの節点を刈らないので、貪欲法がそこに入ると候補が尽きて未完（score −∞）で止まる。文法は行き止まりを刈るので、llama 版は終点まで行ける | torch 版に刈り込みを入れた変種（`core:dp=1`）は、7 セット 2400 文で出力が 1 件だけ変わる（両方不正解）。ほぼ効かない |
| 量子化 | Q8_0・Q5_K_M では logit が変わる | Q8_0 は同じ候補の自然さの差が中央値 0.14・p99 0.74 nats。点差の小さい文が入れ替わる。F16 は中央値 0.006 |
| 並列（`-np`）のぶれ | `-np 2` 以上ではスロットをまとめたバッチで計算し、組み合わせで計算の経路が変わる | 同じ設定・同じ入力でも実行ごとに点数が変わる。`-np 1` は完全に同じ |
| スロットの文脈長 | b10549 は `-c 0 -np 5` で全体 1280 を 5 で割り、1 スロット 256 トークン（`-np 1` は 1024） | 今の評価セットではプロンプト＋生成が 256 に届く候補は 0。長い左文脈では生成が上限で止まって未完になる |
| 探索の幅 | ConstrainedZenz は確率の上位 2000 トークンまでしか制約を満たすものを探さない（`max_tries`） | 上限を外した変種（`core:mt=0`）は 7 セット 2400 文で出力が全件同じ。効かない |
| 特殊トークンの表層 | HF 版は `[PAD]` などを文字列 `[PAD]` として照合する。llama 版は表層を空とみなし、文法が拒む | ラティスの表層に `[PAD]` のような文字列が無い限り同じ |
| 語彙に無いバイト | 文字列に語彙に無いバイト（改行・`0xF5` 以上など）があると、HF のトークナイザは黙って捨てるが、文法はその字を出せない | 自然さでは強制生成が止まり `RuntimeError`。辞書の表層には改行が無いので、実用上は起きない見込み |

## 同等性のテスト（tests/test_llama_backend.py）

```
uv run pytest tests/test_llama_backend.py   # 文法の受理性と bounds（llama.cpp 不要）
LLAMA_CPP_DIR=<llama.cpp のチェックアウト（build/bin にビルド済み）> uv run pytest tests/test_llama_backend.py
```

極小モデル: ランダム初期化の GPT-2（n_embd 64、2層）と、zenz と同じく1文字1トークンのバイト単位 BPE
（結合を1文字の中に限って学習させる。コーパスに無い字「転」「機」はバイトのトークンに割れる）。辞書は
`tests/test_core_constrained.py` の手書きの fixture、読みも同じ5件（長さ 1〜10 字、記号入り）。GGUF は f32。

| テスト | 確かめること | 結果 |
|---|---|---|
| `test_grammar_accepts_exactly_lattice_paths` | ラティスの全経路の表層を受理し、それ以外（経路＋1字、経路の途中まで、無作為な300文字列）を拒む。Python の小さな受理判定器と、`test-gbnf-validator`（llama.cpp のテスト用。あれば使う）の両方で。`"` と `\` を含む表層も | 一致 |
| `test_grammar_prunes_dead_ends` | 文法に残る節点はどれも終点へ届く | 通る |
| `test_bounds_from_text_matches_decoded` | `bounds_from_text` が `Decoded.bounds` と一致（左文脈あり／なし） | 一致 |
| `test_decode_batch_matches_hf` | 5件・左文脈あり／なしで、HF 版が完走した読みは文・finished・bounds が一致し、logp の差が 0.01 以内 | 一致（左文脈ありの1件は HF 版が行き止まりで未完、llama 版は完走） |
| `test_decode_modes_match_hf` | `punct=ja` / `ascii` で同上 | 一致 |
| `test_fluency_matches_hf` | 6文（語彙に無い字・`"`・`\` を含む）で `core.select.fluency` と 0.01 以内 | 一致（語彙に無い字を含む文は分割の取り直しを通る） |
| `test_pipeline_matches_hf` | `CorePipeline(backend=LlamaBackend)` の候補の文・点数・bounds・`convert`（`punct=insert`）が HF 版と一致 | 一致 |

確認環境（2026-10-14）: クラウドの Linux x86_64（Intel Xeon 2.10GHz、論理4コア）、Python 3.12、torch 2.14.1（PyPI）、
transformers 5.19.0、llama.cpp master 62df181（2026-10-14、b11618 の直後。CPU のみでビルド）。
既存の `tests/test_infer.py` の llama.cpp を使うテストもこの版で通った。

親の b10549 で壊れた UTF-8 の出力に HTTP 500 を返す件（docs/INFER.md）は、文法で制約した生成では EOS で止まった時点の出力が
必ず完全な UTF-8 になるので起きないはず。`n_predict` の上限で字の途中で止まった場合は起きうる（その候補は未完なので点数は −∞）。
b10549 では確かめていない。

## 計測（参考値。本番の判定は ThinkPad で行う）

上の確認環境で、zenz と同じ大きさの語彙（5932、1文字1トークン、字の途中のバイトのトークン 472）を Mozc の表層の字から作り、
ランダム初期化の極小 GPT-2（n_embd 64）に載せて、文法の llama.cpp 側の負荷だけを見た（モデルの forward はほぼ 0）。
読みは user_style の正解 20〜45 字の14文の1位、`-t 4`。

| 条件 | 1トークンの時間（中央値） |
|---|---|
| 文法なし（`ignore_eos`、`n_probs 1`） | 0.20 ms |
| 文法あり（約 10.9 KB） | 1.78 ms |
| 文法あり、5件を `-np 5` で並列 | 1要求あたり 9.94 ms（5件の合計時間は `-np 1` で逐次に投げたのとほぼ同じ、407 ms 対 406 ms） |

- ランダムな重みは貪欲法の1位がほぼ毎回文法に合わないので、llama.cpp は語彙全体に文法をかけ直す重い経路を毎回通る。
  学習済みの zenz では1位が文法に合うことが多く（その場合は1トークンだけ照合する）、これは**上限側の値**である。
- サンプリング（文法の照合を含む）はスロットごとに順番に行われるので、文法の重い経路が多いと `-np` を増やしても速くならない。
  forward（実モデルで 1 トークン約 2.1 ms、D6）はスロットをまとめて1バッチで回すので、実モデルでは `-np` が効く見込み。
- 要求1回の時間（中央値）は、文法あり 75.6 ms（生成 37 トークン）、文法なし 17.5 ms（83 トークン）。
  1トークンの時間×トークン数を引いた残り（プロンプトの処理・文法の解析・HTTP の往復）は、文法ありで約 10 ms、なしで約 1 ms。

## 親が実モデルで確かめること

1. `eval.bench_backend` で `core` と `core-llama:np=5` / `np=1` を、245KF と ThinkPad（CPU 版・Vulkan 版）で。表の「生成 ms/tok」が
   文法なしの値（D6 の 2.1 ms）からどれだけ増えるかで、文法の負荷を読む。
2. `eval.run` で `core-llama` の精度（Q8_0・Q5_K_M）を `core` と比べる（量子化の影響。D6 では logp の差 0.02〜0.24）。
3. 表の「取り直し」の数（語彙に無い字のバイト、自然さの分割の食い違い）。実モデルではほぼ 0 の見込み。多ければ速度に効く。
4. b10549 での動作（`completion_probabilities` の形、EOS の項目、`[PAD]` が EOG か、`bytes` の有無）。
   `bytes` が無い版では取り直しができず、語彙に無い字のバイトの logprob が抜ける（`LlamaBackend.tok_bytes is None`）。

## 未解決の論点

- **文法が大きいときの負荷**: 上の計測では文法あり 1.78 ms/トークン（上限側）。実モデルで1位が文法に合わない頻度しだい。
  重い場合は、llama.cpp の文法を使わず、Python 側でトークンの許可リストを作って `logit_bias` で渡す方式（1ステップ1要求になり往復が増える）か、
  文法を語の候補の少ない形に縮める（ラティスの同じ表層の弧をまとめる）ことを検討する。
- **`-np` を増やしたときの1ステップの時間**: サンプリングはスロットごとに直列。実モデルで forward のまとめ効果とどちらが勝つか。
- **`-c 0` と `-np`**: 62df181 では `-c 0 -np N` で各スロットの文脈長が学習時の長さ（`n_ctx_slot = n_ctx_train`、`kv_unified = false`）になった。
  b10549 では全体を 1280 に切り上げてスロットで割る（`-np 5` で 1 スロット 256 トークン。2026-10-18 に確かめた）。長い左文脈を渡すなら `-c` を 1024×スロット数にする。
- **Vulkan 版**: 内蔵 GPU での forward の速さと、サンプリング（CPU 側）との釣り合いは未測定。
- **行き止まりの刈り込み**: 効かないと分かった（上の表）。torch 版の `core:dp=1` と llama 版の `core-llama:de=stop`（刈らずに止める）で揃えられる。
