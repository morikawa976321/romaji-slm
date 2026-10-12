# romaji-slm

ローマ字の打鍵列を、ローカルの小型言語モデルで日本語に変換する変換コア。

- 要件・先行例・方式・進め方: [docs/DESIGN.md](docs/DESIGN.md)
- 評価ハーネスの使い方・指標・結果: [docs/EVAL.md](docs/EVAL.md)
- 学習データ生成（ローマ字化・打鍵ミス・読み付与）: [docs/DATA.md](docs/DATA.md)、`python -m data.build`
- 変換コア 工程2「読みラティス」（打鍵列 → 読みの N-best）: [docs/READING.md](docs/READING.md)、`python -m core.reading "ローマ字"`
- 推論 CLI・GGUF 変換: [docs/INFER.md](docs/INFER.md)、`python -m infer "ローマ字"`、`python -m infer.to_gguf`
