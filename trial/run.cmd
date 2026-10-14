@echo off
chcp 65001 >nul
setlocal
rem romaji-slm 体験版の起動。配布物のルートに置いてダブルクリックする（docs/TRIAL.md）。
rem 括弧ブロックの中の echo に半角の閉じ括弧があるとブロックが閉じるので、分岐は goto で書く。

for /f %%t in ('powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()"') do set "TRIAL_LAUNCH_T0=%%t"

set "ROOT=%~dp0"
if exist "%ROOT%trial\server.py" goto root_ok
rem リポジトリの trial\ から直接起動したとき
pushd "%~dp0.."
set "ROOT=%CD%\"
popd
:root_ok
cd /d "%ROOT%"

set "OUT=%ROOT%trial\out"
set "LOG=%OUT%\launch.log"
if not exist "%OUT%" mkdir "%OUT%"
echo ==== %DATE% %TIME% 起動 ==== >> "%LOG%"

echo romaji-slm 体験版を起動します。
echo 記録とログの場所: %OUT%
echo.

set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>&1
if not errorlevel 1 goto uv_ok
echo [1/3] uv（Python の実行環境を用意する道具）が無いので、公式のインストーラで入れます。
echo       powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
where uv >nul 2>&1
if errorlevel 1 goto fail_uv
:uv_ok

set "TRIAL_LAUNCH_FIRST=0"
if not exist "%ROOT%.venv" set "TRIAL_LAUNCH_FIRST=1"

echo [2/3] Python と部品を用意しています（初回は数分かかります）...
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
uv sync --no-default-groups --group core >> "%LOG%" 2>&1
if errorlevel 1 goto fail

set "HF_HOME=%ROOT%models\hf"
if not exist "%ROOT%models\hf\hub\models--Miwa-Keita--zenz-v3.1-small" goto hf_online
if not exist "%ROOT%models\hf\hub\models--Miwa-Keita--zenz-v3.1-xsmall" goto hf_online
set "HF_HUB_OFFLINE=1"
goto hf_done
:hf_online
set "HF_HUB_OFFLINE="
echo       同梱のモデルが見つからないので、初回だけ Hugging Face から取得します（取得中は待ち時間が長くなります）。
:hf_done

echo [3/3] サーバを起動します。ブラウザが開きます。
echo       このウィンドウを閉じると終了します。
uv run --no-sync python -m trial.server --open >> "%LOG%" 2>&1
if errorlevel 1 goto fail
goto end

:fail_uv
echo.
echo uv を入れられませんでした。ネットワークにつながっているか確かめてください。
goto pause_end

:fail
echo.
echo ===== エラーで止まりました。ログの最後の部分 =====
powershell -NoProfile -Command "Get-Content -Encoding utf8 -Tail 30 '%LOG%'"
echo ==================================================
echo ログ全体: %LOG%
echo このフォルダの trial\out をまとめて送ってもらえると原因を調べられます。

:pause_end
echo.
pause
:end
endlocal
