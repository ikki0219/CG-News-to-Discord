#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CG 系ニュースサイトの更新を Discord に通知する Bot（Webhook 版）.

使い方:
    python bot.py --test     Webhook の疎通確認
    python bot.py --once     1 回だけチェックして終了（タスクスケジューラ向け）
    python bot.py            常駐して interval_seconds ごとにチェック
    python bot.py --reset    現時点の記事をすべて既読にする（通知は出さない）
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

try:
    import feedparser
    import requests
except ImportError as exc:  # 依存が入っていないときは親切に落とす
    sys.exit(f"依存パッケージが不足しています ({exc.name})。setup.bat を実行してください。")

# Windows の日本語コンソール（cp932）では扱えない文字が記事タイトルに入ることがある。
# そこで落とさず「?」に置き換えて出力を続ける。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
ENV_PATH = BASE_DIR / ".env"

USER_AGENT = "Mozilla/5.0 (compatible; cg-news-discord-bot/1.0)"
SEEN_LIMIT = 400  # フィードごとに覚えておく既読 ID の上限
TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
META_KEY_RE = re.compile(r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
META_CONTENT_RE = re.compile(r"content\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
OG_IMAGE_KEYS = ("og:image", "og:image:url", "og:image:secure_url",
                 "twitter:image", "twitter:image:src")
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
SRC_RE = re.compile(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
DIM_RE = re.compile(r"\b(?:width|height)\s*=\s*[\"']?(\d+)", re.IGNORECASE)
# ロゴ・広告・アイコンなど、記事のサムネイルではない画像を除外するための手がかり
BAD_IMG_HINTS = ("logo", "icon", "avatar", "sprite", "spacer", "blank", "pixel",
                 "adserver", "/ads/", "banner", "badge", "placeholder", "1x1",
                 "gravatar", "emoji")
OG_CACHE: dict = {}


# --------------------------------------------------------------------------- #
# 基本ユーティリティ
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env() -> None:
    """.env を読んで os.environ に流し込む（既存の環境変数は上書きしない）。"""
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log(f"WARN: {path.name} を読めませんでした ({exc})。初期値を使います。")
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# フィード解析
# --------------------------------------------------------------------------- #
def entry_link(entry) -> str:
    link = entry.get("link") or ""
    if not link:
        for cand in entry.get("links", []):
            if cand.get("rel") in (None, "alternate") and cand.get("href"):
                link = cand["href"]
                break
    return link


def entry_uid(entry) -> str:
    """記事を一意に識別するキー。id > link > タイトルのハッシュ の順で採用。"""
    for key in ("id", "guid"):
        value = entry.get(key)
        if value:
            return str(value)
    link = entry_link(entry)
    if link:
        return link
    return hashlib.sha1(entry.get("title", "").encode("utf-8")).hexdigest()


def entry_timestamp(entry) -> float:
    """記事の公開時刻（epoch 秒）。取れなければ 0。"""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return time.mktime(parsed)
            except (TypeError, ValueError, OverflowError):
                continue
    return 0.0


def entry_iso8601(entry):
    ts = entry_timestamp(entry)
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def clean_text(raw: str, limit: int) -> str:
    """HTML タグを落としてプレーンテキストにし、指定長で切り詰める。"""
    text = html.unescape(TAG_RE.sub(" ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def entry_summary(entry, limit: int) -> str:
    for key in ("summary", "description"):
        if entry.get(key):
            return clean_text(entry[key], limit)
    contents = entry.get("content") or []
    if contents and contents[0].get("value"):
        return clean_text(contents[0]["value"], limit)
    return ""


def entry_thumbnail(entry):
    """サムネイル画像 URL を各種フィード形式から拾う。"""
    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]
    for media in entry.get("media_content", []) or []:
        if media.get("url") and str(media.get("medium", "image")) == "image":
            return media["url"]
    for enc in entry.get("enclosures", []) or []:
        if enc.get("href") and str(enc.get("type", "")).startswith("image/"):
            return enc["href"]
    haystack = entry.get("summary", "") or ""
    contents = entry.get("content") or []
    if contents:
        haystack += contents[0].get("value", "")
    match = IMG_RE.search(haystack)
    return match.group(1) if match else None


def pick_og_image(markup: str, base_url: str):
    """<head> の og:image / twitter:image を取り出す。"""
    for tag in META_RE.findall(markup):
        key = META_KEY_RE.search(tag)
        content = META_CONTENT_RE.search(tag)
        if key and content and key.group(1).lower() in OG_IMAGE_KEYS:
            return urljoin(base_url, html.unescape(content.group(1)))
    return None


def pick_content_image(markup: str, base_url: str):
    """本文中の <img> から、記事サムネイルらしいものを 1 つ選ぶ。

    og:image を出していないサイト（CGchannel など）向けのフォールバック。
    ロゴや広告を掴まないよう、URL の手がかりと表示サイズで足切りする。
    """
    fallback = None
    for tag in IMG_TAG_RE.findall(markup):
        src = SRC_RE.search(tag)
        if not src:
            continue
        url = urljoin(base_url, html.unescape(src.group(1)))
        low = url.lower()
        if not low.startswith("http") or low.endswith(".svg"):
            continue
        if any(hint in low for hint in BAD_IMG_HINTS):
            continue
        dims = [int(d) for d in DIM_RE.findall(tag)]
        if dims and max(dims) < 300:  # 小さすぎる画像はサムネイルではない
            continue
        if "/uploads/" in low or dims:  # 本文画像らしさが高いものを優先
            return url
        fallback = fallback or url
    return fallback


def fetch_page_image(url: str, timeout: int):
    """記事ページを読んでサムネイル画像の URL を探す。

    フィード自体が画像を配信していないサイト向けの補完。
    取得できなければ None を返し、投稿は画像なしで続行する。
    """
    if url in OG_CACHE:
        return OG_CACHE[url]

    image = None
    try:
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True)
        res.raise_for_status()
        body = res.raw.read(400_000, decode_content=True)
        res.close()
        markup = body.decode(res.encoding or "utf-8", errors="replace")
        image = pick_og_image(markup, url) or pick_content_image(markup, url)
    except Exception as exc:
        log(f"  （画像の取得をスキップしました: {exc}）")

    OG_CACHE[url] = image
    return image


def passes_filters(entry, feed_cfg: dict) -> bool:
    """include_keywords / exclude_keywords による絞り込み。"""
    include = [k.lower() for k in feed_cfg.get("include_keywords", []) if k]
    exclude = [k.lower() for k in feed_cfg.get("exclude_keywords", []) if k]
    if not include and not exclude:
        return True
    haystack = f"{entry.get('title', '')} {entry_summary(entry, 500)}".lower()
    if include and not any(k in haystack for k in include):
        return False
    if exclude and any(k in haystack for k in exclude):
        return False
    return True


def fetch_feed(url: str, feed_state: dict, timeout: int):
    """条件付き GET でフィードを取得する。更新が無ければ None を返す。"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, */*",
    }
    if feed_state.get("etag"):
        headers["If-None-Match"] = feed_state["etag"]
    if feed_state.get("modified"):
        headers["If-Modified-Since"] = feed_state["modified"]

    res = requests.get(url, headers=headers, timeout=timeout)
    if res.status_code == 304:
        return None
    res.raise_for_status()

    if res.headers.get("ETag"):
        feed_state["etag"] = res.headers["ETag"]
    if res.headers.get("Last-Modified"):
        feed_state["modified"] = res.headers["Last-Modified"]

    parsed = feedparser.parse(res.content)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"フィードを解析できませんでした: {parsed.bozo_exception}")
    return parsed


# --------------------------------------------------------------------------- #
# Discord への投稿
# --------------------------------------------------------------------------- #
def build_payload(entry, feed_cfg: dict, config: dict) -> dict:
    title = clean_text(entry.get("title", "(無題)"), 250)
    link = entry_link(entry)
    source = feed_cfg.get("name", "feed")
    payload: dict = {}

    # 表示名・アイコンはフィード単位の指定を優先する（無ければ config の共通値）
    username = feed_cfg.get("username") or config.get("username")
    avatar_url = feed_cfg.get("avatar_url") or config.get("avatar_url")
    if username:
        payload["username"] = username
    if avatar_url:
        payload["avatar_url"] = avatar_url

    # Google ニュースなどのアグリゲータでは、記事を書いた媒体名が source に入る
    publisher = (entry.get("source") or {}).get("title", "")

    if config.get("message_style", "embed") == "plain":
        # URL をそのまま貼る形式。Discord 側が自動でプレビューを展開する。
        label = f"{source}｜{publisher}" if publisher else source
        payload["content"] = f"**{label}**｜{title}\n{link}"
        return payload

    embed = {
        "title": title,
        "url": link,
        "color": feed_cfg.get("color", 5814783),
        "author": {"name": source, "url": feed_cfg.get("site_url", link)},
        # Google ニュースのように配信元がまとまっているフィードでは元媒体名も出す
        "footer": {"text": f"{source}｜{publisher}" if publisher else source},
    }
    summary = entry_summary(entry, config.get("summary_length", 300))
    if summary:
        embed["description"] = summary
    published = entry_iso8601(entry)
    if published:
        embed["timestamp"] = published
    if config.get("show_thumbnail", True):
        thumb = entry_thumbnail(entry)
        fetch_image = feed_cfg.get("fetch_og_image", config.get("fetch_og_image", True))
        if not thumb and link and fetch_image:
            # フィードが画像を持たない場合は記事ページから探す
            thumb = fetch_page_image(link, config.get("http_timeout", 20))
        if thumb:
            embed["image" if config.get("large_image", False) else "thumbnail"] = {"url": thumb}

    payload["embeds"] = [embed]
    if config.get("include_url_in_content", False):
        # 本文にも生 URL を置く。埋め込みタイトルからも記事へ飛べるので既定では付けない。
        # （ここで flags=4 を付けると自前の埋め込みごと消えるので絶対に付けないこと）
        payload["content"] = link
    return payload


DRY_RUN = False   # True なら送信せず内容を表示するだけ
NO_SAVE = False   # True なら state.json を更新しない（動作確認用）


def post_to_discord(webhook_url: str, payload: dict, timeout: int = 20) -> bool:
    """Webhook に POST する。429 は Retry-After に従って最大 3 回まで再試行。"""
    if DRY_RUN:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True
    for _ in range(3):
        try:
            res = requests.post(webhook_url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            log(f"  ERROR: 送信に失敗しました ({exc})")
            return False

        if res.status_code in (200, 204):
            return True
        if res.status_code == 429:
            try:
                wait = float(res.json().get("retry_after", 5))
            except (ValueError, AttributeError, TypeError):
                wait = 5.0
            log(f"  レート制限。{wait:.1f} 秒待って再送します。")
            time.sleep(wait + 0.5)
            continue
        log(f"  ERROR: Discord が {res.status_code} を返しました: {res.text[:200]}")
        return False
    log("  ERROR: レート制限が続いたため送信を諦めました。")
    return False


# --------------------------------------------------------------------------- #
# メイン処理
# --------------------------------------------------------------------------- #
def process_feed(feed_cfg: dict, config: dict, state: dict, webhook_url: str,
                 mark_only: bool = False) -> int:
    """1 フィードを処理して、投稿した件数を返す。"""
    name = feed_cfg.get("name", feed_cfg["url"])
    feed_state = state["feeds"].setdefault(feed_cfg["url"], {"seen": []})
    seen = feed_state.setdefault("seen", [])
    seen_set = set(seen)
    first_run = not feed_state.get("initialized")

    try:
        parsed = fetch_feed(feed_cfg["url"], feed_state, config.get("http_timeout", 20))
    except Exception as exc:  # ネットワーク断で常駐を止めない
        log(f"  {name}: 取得に失敗しました ({exc})")
        return 0

    feed_state["last_checked"] = datetime.now(timezone.utc).isoformat()
    if parsed is None:
        log(f"  {name}: 更新なし (304)")
        return 0

    def mark_seen(entry) -> None:
        uid = entry_uid(entry)
        if uid not in seen_set:
            seen.append(uid)
            seen_set.add(uid)

    def trim_seen() -> None:
        if len(seen) > SEEN_LIMIT:
            feed_state["seen"] = seen[-SEEN_LIMIT:]

    # 初回起動時と --reset 時は通知せず、既読化だけ行う（過去記事の一斉投稿を防ぐ）
    if mark_only or (first_run and not config.get("post_on_first_run", False)):
        for entry in parsed.entries:
            mark_seen(entry)
        feed_state["initialized"] = True
        trim_seen()
        log(f"  {name}: {len(parsed.entries)} 件を既読として記録しました（通知なし）")
        return 0

    fresh = [e for e in parsed.entries if entry_uid(e) not in seen_set]
    fresh.sort(key=entry_timestamp)  # 古い記事から順に投稿する

    targets = [e for e in fresh if passes_filters(e, feed_cfg)]
    skipped = len(fresh) - len(targets)
    limit = config.get("max_posts_per_feed", 5)
    overflow = 0
    if limit and len(targets) > limit:
        overflow = len(targets) - limit
        targets = targets[-limit:]  # 新しいものを優先

    posted = 0
    for entry in targets:
        if not post_to_discord(webhook_url, build_payload(entry, feed_cfg, config)):
            continue  # 送れなかった記事は既読にせず、次回もう一度試す
        posted += 1
        mark_seen(entry)
        log(f"  投稿: [{name}] {clean_text(entry.get('title', ''), 60)}")
        time.sleep(config.get("post_delay_seconds", 1.5))

    # フィルタで落とした記事・上限超過分も既読にする（毎回評価し直さないため）
    for entry in fresh:
        mark_seen(entry)

    feed_state["initialized"] = True
    trim_seen()

    if not fresh:
        log(f"  {name}: 新着なし")
    else:
        extra = []
        if skipped:
            extra.append(f"フィルタ除外 {skipped} 件")
        if overflow:
            extra.append(f"上限超過 {overflow} 件は既読化")
        suffix = f"（{' / '.join(extra)}）" if extra else ""
        log(f"  {name}: 新着 {len(fresh)} 件 / 投稿 {posted} 件{suffix}")
    return posted


def resolve_webhook(feed_cfg: dict, default_url: str):
    """このフィードの投稿先 Webhook を決める。

    webhook_env（環境変数名）> webhook_url（直書き）> 既定の DISCORD_WEBHOOK_URL の順。
    別チャンネルに分けたい場合は、URL を config.json に書かずに
    webhook_env で環境変数名だけを指定する（GitHub Actions では Secrets から渡す）。
    """
    env_name = feed_cfg.get("webhook_env")
    if env_name:
        url = os.environ.get(env_name, "").strip()
        if url:
            return url
        log(f"  {feed_cfg.get('name', '?')}: 環境変数 {env_name} が空のため、このフィードは飛ばします。")
        return None
    return feed_cfg.get("webhook_url") or default_url


def run_once(config: dict, state: dict, webhook_url: str, mark_only: bool = False) -> int:
    feeds = [f for f in config.get("feeds", []) if f.get("enabled", True)]
    if not feeds:
        log("有効なフィードが config.json にありません。")
        return 0
    log(f"チェック開始（{len(feeds)} フィード）")
    total = 0
    for feed_cfg in feeds:
        target = resolve_webhook(feed_cfg, webhook_url)
        if not target and not DRY_RUN:
            continue
        total += process_feed(
            feed_cfg, config, state, target or "dry-run",
            mark_only=mark_only,
        )
    if not NO_SAVE:
        save_json(STATE_PATH, state)
    log(f"チェック完了: 合計 {total} 件投稿")
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="CG ニュースを Discord に流す Bot")
    parser.add_argument("--once", action="store_true", help="1 回だけ実行して終了する")
    parser.add_argument("--test", action="store_true", help="Webhook にテスト投稿する")
    parser.add_argument("--reset", action="store_true",
                        help="現在の記事をすべて既読にする（通知は出さない）")
    parser.add_argument("--dry-run", action="store_true",
                        help="Discord に送らず、送信内容を標準出力に表示する")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="既読かどうかに関わらず、各フィードの最新 N 件を投稿する"
                             "（state.json は変更しない。動作確認用）")
    parser.add_argument("--interval", type=int, default=None,
                        help="チェック間隔（秒）。config.json より優先される")
    args = parser.parse_args()

    global DRY_RUN, NO_SAVE
    DRY_RUN = args.dry_run
    NO_SAVE = args.dry_run or bool(args.sample)

    load_env()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url and not (DRY_RUN or args.reset):
        log("ERROR: DISCORD_WEBHOOK_URL が設定されていません。")
        log("       .env.example を .env にコピーして Webhook URL を貼り付けてください。")
        return 1

    config = load_json(CONFIG_PATH, None)
    if config is None:
        log(f"ERROR: {CONFIG_PATH.name} が見つかりません。")
        return 1
    state = load_json(STATE_PATH, {"feeds": {}})
    state.setdefault("feeds", {})

    if DRY_RUN or args.sample:
        # 既読状態を無視して、いま取得できる記事から最新のものを対象にする
        config["post_on_first_run"] = True
        if args.sample:
            config["max_posts_per_feed"] = args.sample
            log(f"サンプル投稿モード: 各フィードの最新 {args.sample} 件を投稿します"
                f"（state.json は変更しません）")
        else:
            config["post_delay_seconds"] = 0
        run_once(config, {"feeds": {}}, webhook_url or "dry-run")
        return 0

    if args.test:
        # 使っている投稿先ごとに 1 通ずつ送る（別チャンネルに分けている場合の確認用）
        targets = {}
        for feed_cfg in config.get("feeds", []):
            if not feed_cfg.get("enabled", True):
                continue
            url = resolve_webhook(feed_cfg, webhook_url)
            if url:
                targets.setdefault(url, feed_cfg)
        targets.setdefault(webhook_url, {})

        ok = True
        for url, feed_cfg in targets.items():
            sent = post_to_discord(url, {
                "username": feed_cfg.get("username") or config.get("username", "CG News"),
                "content": "✅ テスト投稿です。Webhook は正常に動作しています。",
            })
            log(f"  {'成功' if sent else '失敗'}: ...{url[-12:]}")
            ok = ok and sent
        log("テスト投稿に成功しました。" if ok else "テスト投稿に失敗しました。")
        return 0 if ok else 1

    if args.reset:
        run_once(config, state, webhook_url, mark_only=True)
        return 0

    if args.once:
        run_once(config, state, webhook_url)
        return 0

    interval = args.interval or config.get("interval_seconds", 600)
    log(f"常駐モードで起動しました（{interval} 秒ごとにチェック / Ctrl+C で終了）")
    while True:
        try:
            run_once(config, state, webhook_url)
        except Exception as exc:  # 予期しない例外でも常駐を継続する
            log(f"ERROR: 予期しないエラー ({exc})")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("終了します。")
        sys.exit(0)
