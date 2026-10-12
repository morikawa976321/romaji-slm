# 依頼: 工程3「辞書による変換候補」（読み → 表記の N-best）を作る

最初に CLAUDE.md、docs/REDESIGN.md（特に「推奨構成」「最初の2週間」の工程3）、core/types.py を読むこと。要件表（docs/DESIGN.md）は本人の決定なので変えない。文書・コメント・コミットメッセージは日本語。

## 作るもの

`core/kanakanji.py`:

- `Dictionary.load(...)`: Mozc の OSS 辞書（github.com/google/mozc の `src/data/dictionary_oss/` にある `dictionary00.txt`〜`dictionary09.txt`、接続コスト `connection_single_column.txt`、`id.def`。ライセンス BSD-3）を読む。**データ本体はコミットしない**（取得スクリプト `core/fetch_mozc.py` で `models/mozc/` に落とす。版（コミットハッシュ）を固定して記録する）。ファイルの列の意味は Mozc のソースで確かめること（推測で決めない）。読みはひらがなで格納されているはずなので、Reading 側のカタカナと揃える。
- `convert(reading: Reading, dic: Dictionary, n: int = 20) -> list[Conversion]`: Reading の区間ごとに、
  - `kind="kana"` の区間: 辞書の前方一致検索で語の弧を張り、接続コストで Viterbi し、**N-best**（A* 後ろ向き探索など）を得る。辞書に無い部分はかなのまま（ひらがな・カタカナ）の弧で必ず通れるようにする（行き止まりを作らない。コストは高め）。
  - `kind="en" | "num" | "sym"` の区間: そのまま1語（`source` をそれぞれに）。表記の候補が複数あれば（`Seg.surfaces`）候補を分ける。接続コストは、英語・数字・記号を名詞相当（Mozc の id.def で適当な品詞）として扱う。
  - 文頭・文末の接続（BOS/EOS）を入れる。
  - 結果は `Conversion`（core/types.py）。各 `Word` に表層・読み・品詞 ID・コストを入れる。**表層と読みの対応が取れない語を作らない**。
- 速度: 辞書の読み込みは起動時に1回（常駐前提）。前方一致の索引は marisa-trie 等（ライセンスを確認して docs に書く）か、読みの長さで引く辞書。1件 50ms 以下（デスクトップ相当）が目安。読み込み時間とメモリも記録する。

## 合格条件（docs/REDESIGN.md 工程3。数値は目安）

- AJIMEE-Bench 200件（`eval/datasets.py` の `load_ajimee`。各項目の `meta["kana"]` が正解のカタカナ読み）で、正解の読みから作った Reading（かなだけの1区間。記号はそのまま sym 区間でよい）を入力にして、
  - 1位が正解（`refs` のどれかと一致）: 45% 以上
  - 上位20件に正解がある率（oracle@20）: 85% 以上
  - 記号・長音を無視した一致も併記する
- 1件あたりの変換時間の中央値と最大値、辞書の読み込み時間、メモリ使用量。
- 結果を `docs/KANAKANJI.md` に書く（辞書の形式、コストの扱い、実測、失敗例）。CLI `uv run python -m core.kanakanji "キョウハイイテンキデスネ"` で N-best とコストを表示できること。
- pytest でテスト（`tests/test_core_kanakanji.py`）。テストは小さな手書きの辞書 fixture で、Mozc 辞書が無くても通るようにする（実辞書を使うテストは辞書があるときだけ）。

## 作業の規則

- ブランチ `claude/step3-kanakanji` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。
- `core/types.py` の型は前段（別セッションが並行で作る `core/reading.py`）との約束なので、変えたいときは変えずに PR の本文に提案として書く。
- この環境からは huggingface.co と download.pytorch.org に届かない。github.com・raw.githubusercontent.com・pypi.org は届く。torch は不要。pyproject.toml の cu128 指定は壊さない。依存を足すなら `uv add --group core ...`。uv.lock が更新できなければその旨を報告。
- 最終報告: 合格条件ごとの実測、失敗例10件、未解決の論点（特に、辞書に無い語の扱いと、英語・数字・記号の接続コストの決め方）。
