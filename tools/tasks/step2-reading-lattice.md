# 依頼: 工程2「読みラティス」（打鍵列 → 読みの N-best）を作る

最初に CLAUDE.md、docs/REDESIGN.md（特に「推奨構成」「最初の2週間」の工程2）、core/types.py を読むこと。要件表（docs/DESIGN.md）は本人の決定なので変えない。文書・コメント・コミットメッセージは日本語。

## 作るもの

`core/reading.py` に `readings(keys: str, n: int = 20, max_edits: int = 2) -> list[Reading]`（core/types.py の型。コスト昇順）。

打鍵列の各位置を節点とするラティスを作り、N-best の経路を返す:

1. **正規ローマ字の弧**（コスト0）: Microsoft IME / Google 日本語入力と同じ表。`nn` は常に「ン」、`n` は母音・y の前以外で「ン」、`n'` も「ン」。ワープロ式の揺れ（si/shi, ti/chi, tu/tsu, hu/fu, zi/ji, sya/sha, tya/cha/cya, ja/jya/zya…）、促音（子音重ね、tch）、小書き（x/l）、外来音（fa, thi, dhi, twu, wi, va…）、長音 `-`→ー。`eval/romaji_kana.py` の表を流用してよい。
   - **曖昧な `n` の扱い**: 本人は「ん」+母音・y・n を `nn` と打つ（docs/DESIGN.md）。ただし `jikkennokekka` のように `nn` の規則に合わない打ち方も実際にはある。`nn`+母音を「ン+母音」と読む弧（コスト0）に加え、「ン+な行」と読む弧も小さいコストで張る。
2. **打鍵ミスの編集弧**: QWERTY 隣接キーへの置換・脱字（1字補う）・余計な1字・隣接2字の入れ替え。隣接表は `data/noise.py` と同じもの（共通化してよい）。1文で合計 `max_edits` まで、1語（ローマ字の1音節〜英単語1つ）で1編集まで。編集ごとにコスト。**無修正の経路を必ず残す**。正しい入力を直してしまわないよう、編集コストは無修正経路より十分高くする。
3. **英語の弧**: 打鍵列の部分文字列が英単語リストに一致する区間に `kind="en"` の区間を張る。英単語リストは wordfreq（PyPI の `wordfreq`、データ CC BY-SA 4.0）の英語上位語＋ `core/data/tech_words.tsv`（新規作成。綴り<TAB>表記。github→GitHub、api→API、python→Python、llama.cpp、gguf、pull request（綴り pullrequest）、push、commit、deploy、issue、function、ok→OK など、AI への指示・プログラミングでよく使う語を数百語。ライセンス上問題のない自作リスト）。コストは −log(頻度) 由来＋英語区間の固定ペナルティ。**2文字以下の英単語は許可リスト（ok, ai, ui, os, pr, ci, id, pc など）以外は弧を張らない**。英字と `.` `-` 数字が連続する塊（llama.cpp, gpt-2, v3.1）は1つの英語区間にできるようにする。英語区間に編集弧を通すこと（`githib`→github）。
4. **数字・記号の弧**: 数字列は `kind="num"`。ASCII 記号は `kind="sym"` で、`surfaces` に出力側の候補（`,`→("、", ",")、`.`→("。", ".")、`?`→("？", "?")、`!`→("！", "!")、`[`→("「", "[")、`]`→("」", "]")、`/`→("・", "/")、`:`→("：", ":")）。ただし英語区間・数字の中の `.` `-` は英語・数字の弧が持つ（`3.14`、`llama.cpp`）。
5. 英語か日本語かは**ここで決めない**。両方の経路を N-best に残し、コストの差は控えめにする（最終判断は後段の zenz）。`Reading.breakdown` にコストの内訳を入れる。

## 合格条件（docs/REDESIGN.md 工程2。数値は目安）

- AJIMEE-Bench 200件（`eval/datasets.py` の `load_ajimee`。ローマ字は `eval/kana.py` で正解のカタカナから作ったもの）で、`Reading.zenz_input()` を記号・長音を無視して正解のカタカナと比べ、**1位が正解 100%**（きれいな入力を崩さない）。
- 同じ200件に `data/noise.py` で打鍵ミスを1〜2個入れた版で、正解が上位5以内 80% 以上。
- `eval/sets/user_style.jsonl` の40文に、正解の読みを手で付けた評価ファイル `eval/sets/user_style_reading.jsonl`（新規作成。各行に `romaji` と `reading`＝zenz_input の形。英語区間は小文字綴り、かなはカタカナ、記号は全角）を作り、上位1位・上位5位の正解率を出す。英単語を含む10文（category=tech）で、英語区間を正しく英語として切り出した経路が上位5以内に入るか。
- 正常入力の誤補正率（打鍵ミスの無い入力で、1位の経路が編集を含む割合）2% 以下。
- 1件あたりの所要時間（デスクトップ相当、Python）を記録する。目安 50ms 以下。
- 結果を `docs/READING.md` に書く（仕組み・コストの決め方と根拠・実測・失敗例）。CLI `uv run python -m core.reading "githubnipushshiteoite"` で N-best とコストの内訳を表示できること。
- pytest でテスト（`tests/test_core_reading.py`）。

## 作業の規則

- ブランチ `claude/step2-reading` で作業し、コミットはパス指定（`git add <path>` → `git commit -- <path>`）。完了したら push し、main への PR を作る。
- `core/types.py` の型は後段（別セッションが並行で作る `core/kanakanji.py`）との約束なので、変えたいときは変えずに PR の本文に提案として書く。
- この環境からは huggingface.co と download.pytorch.org に届かない。pypi.org・github.com・raw.githubusercontent.com は届く。torch は不要なはず。pyproject.toml の cu128 指定は壊さない。依存を足すなら `uv add --group core ...`。uv.lock が更新できなければその旨を報告。
- 最終報告: 合格条件ごとの実測、失敗例10件、未解決の論点。
