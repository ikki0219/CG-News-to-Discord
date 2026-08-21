@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo === CG News Discord Bot セットアップ ===

if not exist ".venv\Scripts\python.exe" (
    echo 仮想環境を作成しています...
    where py > nul 2>&1 && (py -3 -m venv .venv) || (python -m venv .venv)
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python が見つかりませんでした。https://www.python.org/downloads/ からインストールしてください。
    pause
    exit /b 1
)

echo 依存パッケージをインストールしています...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet -r requirements.txt

if not exist ".env" (
    copy ".env.example" ".env" > nul
    echo.
    echo .env を作成しました。メモ帳が開くので DISCORD_WEBHOOK_URL に Webhook URL を貼り付けて保存してください。
    notepad ".env"
)

echo.
echo セットアップが完了しました。test-webhook.bat で疎通確認をしてください。
pause
