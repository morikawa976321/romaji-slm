# romaji-slm

ローマ字の打鍵列を、ローカルの小型言語モデルで日本語に変換する変換コア。

**2026-10-13 に休止、2026-10-14〜15 に親セッション（AI）が本人不在で工程6（速度）・表記モード・句読点 B・体験版まで進めた。現状と次にやることは [docs/STATUS.md](docs/STATUS.md)、AI の判断と本人待ちのカードは [docs/decisions.md](docs/decisions.md)、体験版は [docs/TRIAL.md](docs/TRIAL.md)。**

- 方式の決定（学習なし・辞書ラティス＋凍結 zenz）と先行例: [docs/REDESIGN.md](docs/REDESIGN.md)。変換コアは `core/`（`python -m eval.run --converter core`）
- 常駐入力（全アプリで使う入力・MS-IME との住み分け・自己改善）: [docs/IME.md](docs/IME.md)。ThinkPad に入れる手順: [docs/THINKPAD.md](docs/THINKPAD.md)。利用履歴の振り返り: [docs/REVIEW.md](docs/REVIEW.md)
- 体験版（ThinkPad で体感評価する比較ページと配布 zip）: [docs/TRIAL.md](docs/TRIAL.md)、`python -m trial.server --mock --open`

- 要件・先行例・方式・進め方: [docs/DESIGN.md](docs/DESIGN.md)
- 評価ハーネスの使い方・指標・結果: [docs/EVAL.md](docs/EVAL.md)
- 学習データ生成（ローマ字化・打鍵ミス・読み付与）: [docs/DATA.md](docs/DATA.md)、`python -m data.build`
- 変換コア 工程2「読みラティス」（打鍵列 → 読みの N-best）: [docs/READING.md](docs/READING.md)、`python -m core.reading "ローマ字"`
- 推論 CLI・GGUF 変換: [docs/INFER.md](docs/INFER.md)、`python -m infer "ローマ字"`、`python -m infer.to_gguf`
- 常駐入力の資源（メモリ・GPU・CPU・ディスクの内訳と増え方、減らす手の候補）: [docs/RESOURCES.md](docs/RESOURCES.md)、`python -m tools.memprof out.jsonl`
- 変換の実行方式の比較（torch・ONNX Runtime・llama-server）: [docs/LLAMA_PARITY.md](docs/LLAMA_PARITY.md)。さらに速くする余地（3 経路の内訳と、出力を変えない手・変える手）: [docs/SPEEDUP.md](docs/SPEEDUP.md)
- 辞書による変換候補（読み → 表記の N-best、Mozc 辞書）: [docs/KANAKANJI.md](docs/KANAKANJI.md)、`python -m core.fetch_mozc`、`python -m core.kanakanji "キョウハイイテンキデスネ"`
