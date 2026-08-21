# CG News Discord Bot

CG 系ニュースサイトの新着記事を検出して、Discord のチャンネルに URL 付きで自動投稿する Bot です。
Discord Webhook を使うので、Bot アプリの申請やトークン発行は不要です。

## 監視対象（初期設定）

| サイト | フィード |
| --- | --- |
| CGWORLD.jp | `https://cgworld.jp/atom.xml` |
| 3DCG NEWS (3dnchu) | `https://3dnchu.com/feed/` |
| CGchannel | `https://www.cgchannel.com/feed/` |
| 80.lv | `https://80.lv/feed/` |

すべて `config.json` の `feeds` で追加・削除・無効化できます。

## セットアップ

### 1. Webhook URL を取得する

1. Discord で投稿先チャンネルの **歯車アイコン（チャンネルの編集）** を開く
2. **連携サービス → ウェブフック → 新しいウェブフック**
3. 名前とアイコンを設定して **「ウェブフックURLをコピー」**

### 2. `.env` に貼り付ける

`.env.example` を `.env` という名前でコピーし、URL を書き込みます。

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx/yyyyy
```

> `.env` は `.gitignore` に入れてあります（`state.json` は GitHub Actions が既読状態を引き継ぐためコミットします）。Webhook URL は知っている人なら誰でもそのチャンネルに投稿できるので、公開しないでください。

### 3. 疎通確認

`test-webhook.bat` をダブルクリック。チャンネルにテストメッセージが届けば成功です。

（Python の仮想環境と依存パッケージはセットアップ済みです。別の PC に持っていく場合は `setup.bat` を実行してください。）

## 実行

| ファイル / コマンド | 動作 |
| --- | --- |
| `run.bat` | 常駐して `interval_seconds`（既定 600 秒）ごとにチェック。Ctrl+C で終了 |
| `check-once.bat` | 1 回だけチェックして終了。タスクスケジューラ向け |
| `python bot.py --dry-run` | Discord に送らず、送信内容を画面に表示（動作確認用） |
| `python bot.py --test` | テスト投稿 |
| `python bot.py --sample 1` | 既読かどうかに関わらず各フィードの最新 1 件を投稿（`state.json` は変更しない） |
| `python bot.py --reset` | 現時点の記事をすべて既読にして、通知が出ない状態に戻す |

**初回起動時は既存の記事を「既読」として記録するだけで、通知は出ません。**
2 回目以降のチェックで見つかった新着記事だけが投稿されます。
最初から手元のフィードを流し込みたい場合は `config.json` の `post_on_first_run` を `true` にしてください。

## PC を落としても動かす（GitHub Actions）

[.github/workflows/cg-news.yml](.github/workflows/cg-news.yml) で 30 分おきに自動実行できます。
GitHub のサーバー上で動くので、手元の PC の電源は関係ありません。

### 1. GitHub にリポジトリを作って push する

[github.com/new](https://github.com/new) で空のリポジトリを作成します（README や .gitignore は追加しない）。作成後に表示される URL を使って:

```bash
git remote add origin https://github.com/<ユーザー名>/<リポジトリ名>.git
git push -u origin main
```

`.env` は `.gitignore` で除外済みなので、Webhook URL は push されません。

### 2. Webhook URL を Secrets に登録する

リポジトリの **Settings → Secrets and variables → Actions → New repository secret**

- Name: `DISCORD_WEBHOOK_URL`
- Secret: Webhook URL

### 3. 動作確認

**Actions タブ → CG News to Discord → Run workflow** で手動実行できます。
以降は 30 分おきに自動で走ります。

### 注意点

- **リポジトリは Public を推奨**。Private だと Actions の無料枠（月 2,000 分）を使い、30 分間隔では月 1,400 分ほど消費して上限に近づきます。Private のままにするなら cron を `"0 * * * *"`（1 時間おき）にしてください
- 実行時刻は GitHub の混雑状況により **数分〜十数分ずれます**。稀にスキップされることもあります
- リポジトリに 60 日間 activity が無いと、GitHub が定期実行を自動停止します（メールが届くので Actions タブから再有効化）
- **ローカル実行と併用しない**。既読管理 `state.json` が二重管理になり、同じ記事が 2 回投稿されます。GitHub Actions に移したら `run.bat` での常駐は止めてください

## （別案）PC 起動中だけ動かす（タスクスケジューラ）

GitHub を使わない場合は、`check-once.bat` を定期実行するのが手軽です。

```bash
schtasks /create /tn "CG News Discord Bot" /tr "K:\discord\check-once.bat" /sc minute /mo 30 /f
```

30 分ごとに 1 回チェックします。解除は `schtasks /delete /tn "CG News Discord Bot" /f`。

## 設定（`config.json`）

### 全体設定

| キー | 説明 |
| --- | --- |
| `username` | Discord 上での表示名 |
| `interval_seconds` | 常駐モードのチェック間隔（秒） |
| `post_on_first_run` | 初回に既存記事も投稿するか（既定 `false`） |
| `max_posts_per_feed` | 1 回のチェックでフィードごとに投稿する上限。溢れた分は既読化される |
| `post_delay_seconds` | 連投時の待ち時間。Discord のレート制限対策 |
| `message_style` | `embed`（カード表示）か `plain`（タイトル + URL のみ） |
| `include_url_in_content` | 埋め込みに加えて本文にも生 URL を残す（コピーしやすい） |
| `show_thumbnail` / `large_image` | サムネイルを出すか / 大きい画像で出すか |
| `fetch_og_image` | フィードが画像を持たないとき、記事ページから og:image や本文画像を探す |
| `summary_length` | 本文抜粋の最大文字数 |

### フィードごとの設定

```json
{
  "name": "サイト名",
  "url": "https://example.com/feed/",
  "site_url": "https://example.com/",
  "color": 15964160,
  "enabled": true,
  "include_keywords": ["Blender", "Houdini"],
  "exclude_keywords": ["PR", "セール"],
  "webhook_url": "https://discord.com/api/webhooks/..."
}
```

- `include_keywords`: いずれかがタイトル・本文に含まれる記事だけ投稿（空なら全件）
- `exclude_keywords`: 含まれる記事は投稿しない
- `webhook_url`: このフィードだけ別チャンネルに流したいときに指定（省略時は `.env` の値）
- `color`: 埋め込み左端の色。10 進数で指定（`#F39800` → `15964160`）

## 仕組み

- RSS/Atom を条件付き GET（ETag / Last-Modified）で取得するので、更新が無ければ 304 で終わりサーバに負荷をかけません
- 投稿済み記事は `state.json` に ID で記録され、重複投稿しません（フィードごとに直近 400 件を保持）
- 送信に失敗した記事は既読にせず、次回のチェックで再試行します
- 429（レート制限）は `Retry-After` に従って自動で待ってから再送します
- ネットワーク断や 1 サイトの障害があっても、他のフィードの処理と常駐は継続します

## トラブルシューティング

**何も投稿されない**
初回は既読化のみです。`state.json` を削除して `config.json` の `post_on_first_run` を `true` にすると強制的に投稿できます。

**特定のサイトだけ「取得に失敗しました」と出る**
フィード URL が変わった可能性があります。ブラウザでその URL を開いて XML が返るか確認してください。

**投稿が多すぎる**
`max_posts_per_feed` を下げるか、`include_keywords` で絞り込んでください。
