# ThinkPad に常駐入力を入れる手順（Git で追従）

本人の決定（2026-10-18、decisions.md D36）で、ThinkPad はこのリポジトリを clone して「追従モード」で使う。初回の準備を 1 回すれば、あとは常駐アプリが GitHub の main を 30 分ごとに見て、新しい版を自動で入れる。仕組みの詳細は [IME.md](IME.md)「別の PC（ThinkPad）に入れる（Git で追従。推奨）」。

## 前提

- GitHub へのログインが済んでいる（2026-10-18、本人が済ませた）。リポジトリは非公開で、無人の取得はこの資格情報を使う。
- Git for Windows が入っている。
- ネットワークにつながる（初回は Python・CPU 版の torch・PowerShell 7 などを取る）。

## 初回だけ

1. **リポジトリを clone する**（置き場所は任意。例: `C:\Users\<名前>\romaji-slm`）。

   ```
   git clone https://github.com/morikawa976321/romaji-slm.git
   ```

2. **配布 zip を ThinkPad に写す。** Git に入れていないモデル（Mozc の辞書・読みの LM・zenz-v3.1-small）を、ここから一度だけ取る。
   - 開発機で `pwsh -NoProfile -File tools/pack-ime.ps1` を実行すると `dist/romaji-slm-ime-<日付>-<hash>.zip`（約 170 MB）ができる。2026-10-18 時点の最新は `dist/romaji-slm-ime-20261018-37899b2.zip`。
   - 中のコードは使わず、モデルだけを使うので、古い zip でもよい。

3. **clone したフォルダで準備のスクリプトを流す。**

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File ime\setup-follower.ps1 -Models <zip のパス>
   ```

   スクリプトは次を順に行う（`ime/setup-follower.ps1`）。
   1. git・PowerShell 7・uv を確かめる。PowerShell 7 が無ければ winget で入れる（管理者の確認が出たら許可する）。uv が無ければ公式のインストーラで入れる。
   2. zip からモデルを clone の `models\` に写す。
   3. `%LOCALAPPDATA%\romaji-slm\ime.ini` に `pull=main` を書く。
   4. `ime\install.ps1` で組み立て・自己試験・起動まで行う。GPU が無いので、torch は CPU 版になる。

   「入れました」と出たら終わり。

4. **使い始める。**
   - タスクバー右下（「^」の中のことがある）に romaji-slm のアイコンが出る。
   - Ctrl+Alt+Space（またはアイコンを左クリック）で使い始める。
   - 変換サーバの準備に 30 秒〜1 分かかる。
   - 次のログオンからは自動で起動する。

すでに ZIP（`install.cmd`）で入れてあっても、そのまま手順 3 を流してよい。設定（ime.ini）・ユーザー辞書・記録は残る。

## その後（操作は要らない）

- 常駐アプリが 30 分ごとに `git fetch origin main` をする。早送りで取り込めれば、新しい版を裏で組み立てて自己試験を通し、打っていない間に入れ替える。
- 取りに行った結果は `%LOCALAPPDATA%\romaji-slm\ime-debug.log` の `pull:` の行に残る（`最新`・`<前> → <後>`・失敗の理由）。
- 入れ替えの結果は、同じフォルダの `update-result.txt`（`staged|ok|fail <版> <時刻> <説明>`）と `update.log`。

## 守ること・困ったとき

- **ThinkPad の clone には手を入れない。** 作業ツリーに変更があると、「作業ツリーに変更があるので取り込まない」と記録して取り込みを止める（手で触ったものを消さないため）。戻すには、clone で `git status` を見て変更を消す。ThinkPad からはコミットしない。
- **`pull:` に fetch の失敗が続くとき**: ネットワークか GitHub のログインが切れている。clone で `git fetch` を手で 1 回打てば、ログインの画面が出て直る。
- **作業途中の版が入るのが困るとき**: 評価を通した版だけを指すブランチ（例 `stable`）を開発機で作る。ThinkPad では、clone で `git switch stable` してから `ime\setup-follower.ps1 -Models <zip> -Branch stable` を流し直す（ime.ini が `pull=stable` になる）。
- **外すとき**: clone で `pwsh -NoProfile -File ime\uninstall.ps1`（`-KeepData` で設定・辞書・記録を残す）。

## ThinkPad で分かれるもの

- **ユーザー辞書と利用履歴**は PC ごと。利用履歴の振り返り（ops-daily、REVIEW.md）はデスクトップでだけ動くので、ThinkPad の入力からは辞書が育たない。デスクトップの辞書を使いたいときは、`%LOCALAPPDATA%\romaji-slm\user_dict.tsv` を ThinkPad の同じ場所に写す（変換サーバは更新を読み直す）。
- **速さ**: ThinkPad では CPU の ONNX Runtime 版で動く（D43・D44）。デスクトップの CPU（Core Ultra 5 245KF、ほかのセッションが CPU を 3〜4 割使っていたとき）では、
  常駐入力の 1 要求（候補ウィンドウの別の書き方つき、user_style の 20〜30 字）が左文脈なしで中央値 779 ms、左文脈 60 字で 932 ms だった（SPEEDUP.md）。
  ThinkPad（Core Ultra 7 255H）での値は未測定（本人の決定で、ほかの高速化の後に最後に測る。下の「速さを測る」）。

## 速さを測る

開発機で作った zip を写して、ダブルクリックするだけ。リポジトリ・git・GPU は要らない（追従モードの clone とは別のフォルダに展開してよい）。

1. 開発機で zip を作る: `pwsh -NoProfile -File tools/pack-bench.ps1`（出力は `dist/romaji-slm-bench-<日付>-<hash>.zip`）。
   中身は HEAD のコード、辞書と読みの LM、zenz-v3.1-small（HF 形式）、CPU 版 torch の uv.lock（torch は ONNX の書き出しにだけ使う）。
2. ThinkPad に写して展開する。**AC 電源につなぎ、ほかのアプリを閉じる。**
3. 展開したフォルダの `bench.cmd` をダブルクリックする。初回は uv・Python・CPU 版 torch・onnxruntime の取得で数分、ONNX の書き出しに 20〜40 秒。
   測るのは 8 通りで 30 分前後。測っている間は触らない。
   - 早く済ませたいときは、PowerShell で `powershell -ExecutionPolicy Bypass -File tools\thinkpad-bench.ps1 -Quick`（3 通り、10 分前後）。
   - 流れだけ確かめるときは `-Smoke`（数値は使わない）。
4. 終わると `bench-out\results.md` ができ、クリップボードにも入る。開発機のセッションに貼る。

測るもの（`tools/thinkpad-bench.ps1`）:
- 1 件の変換の内訳（`eval.parity_speed`、ONNX Runtime 版、句読点 B あり）: スレッド 2・4・6・8、句読点を打たない入力。各 14 文 × 3 周。
- 常駐入力の実際の形での待ち（`eval.ime_sim`）: 候補ウィンドウの別の書き方つき・左文脈 60 字で、模擬の打鍵から全部打った打鍵列の 1 位が出るまで。
  今の要求の出し方（14 文）と、別の書き方を後回しにする（B）・古い先読みを打ち切る（C）の 4 通り（7 文）。
- 結果の先頭に CPU 名・電源・電源プラン・測る前の CPU 使用率・版が入る。各表の「20〜30 字（速度要件の範囲）」の行と、待ちの表が判定に使う値。
- bench では測らないもの（SPEEDUP.md §7）: 電池で動かしたとき、Windows の QoS（E コアに回されるか）、本人の実際の打鍵での待ち。

## 確かめたこと（2026-10-18、開発機で ThinkPad の状況を再現）

GitHub の代わりにリポジトリの bare の複製を作り、置き場所を `ROMAJI_SLM_DATA` で別にした。torch は `ROMAJI_SLM_TORCH=cpu` で CPU 版に決め打ちし、Windows PowerShell 5.1 で流した。

- 初回の準備が最後まで通った。torch は CPU 版（2.14.1+cpu）に差し替わった。zip から写したモデルで自己試験が通った（`kyouhaiitenki` → 今日はいい天気、`device=cpu`）。
- bare の複製に足したコミットを取り込んだ（`pull=f67f585 → 7d1a96b`、新しい版あり）。その版の組み立て（`install.ps1 -Stage`）も CPU 版で通った。
- clone に手を入れた状態では取り込まなかった。
- 打っていない間の差し替え（`-Swap`）は D35 の自動更新の流れで、この再現では流していない。
