"""収集バッチ: RSS を取得し、画像URLを補って D1 の articles に保存する。

使い方:
  python batch/collect.py                 # config/sources.yaml を使う
  python batch/collect.py --config path   # 別の設定ファイルを使う
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import Database, connect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "news-curator/1.0 (personal RSS reader)"
MAX_ENTRIES_PER_FEED = 30        # 1配信元あたり、新しい順にこの件数まで
MAX_AGE_DAYS = 3                 # これより古い記事は取り込まない
OGP_MAX_BYTES = 200_000          # OGP取得で読むのは先頭のこのバイト数まで
OGP_WORKERS = 8
RETENTION_DAYS = 90              # 評価・お気に入りのない記事はこの日数で削除
DESCRIPTION_MAX = 300

GOOGLE_NEWS_HOST = "news.google.com"
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|ref$|ref_src$)")


# ---------------------------------------------------------------- HTTP

def http_get(url: str, max_bytes: int | None = None, timeout: int = 15) -> bytes:
    """URL を取得する（テストでは差し替える）。max_bytes を超えた分は読まない。"""
    with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, stream=True) as res:
        res.raise_for_status()
        chunks, size = [], 0
        for chunk in res.iter_content(16_384):
            chunks.append(chunk)
            size += len(chunk)
            if max_bytes and size >= max_bytes:
                break
        return b"".join(chunks)


# ---------------------------------------------------------------- 設定

@dataclass
class Source:
    name: str
    url: str
    category: str
    enabled: bool = True
    id: int | None = None


def google_news_url(keyword: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(keyword)}&hl=ja&gl=JP&ceid=JP:ja"


def load_sources(path: Path) -> list[Source]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sources = [Source(f["name"], f["url"], f["category"], f.get("enabled", True))
               for f in cfg.get("feeds") or []]
    for category, keywords in (cfg.get("google_news") or {}).items():
        for kw in keywords or []:
            sources.append(Source(f"Googleニュース「{kw}」", google_news_url(kw), category))
    return sources


def sync_sources(db: Database, sources: list[Source]) -> list[Source]:
    """設定ファイルの配信元を sources テーブルに反映し、id を付けて返す。"""
    db.insert_many("sources", ["name", "url", "category", "enabled"],
                   [(s.name, s.url, s.category, int(s.enabled)) for s in sources])
    for s in sources:
        db.query("UPDATE sources SET name = ?, category = ?, enabled = ? WHERE url = ?",
                 (s.name, s.category, int(s.enabled), s.url))
    # 設定ファイルから消えた配信元は停止（記事は残す）
    urls = [s.url for s in sources]
    rows = db.query("SELECT id, url FROM sources")
    known = {r["url"]: r["id"] for r in rows}
    removed = [r["id"] for r in rows if r["url"] not in set(urls)]
    if removed:
        db.select_in("UPDATE sources SET enabled = 0 WHERE id IN ({in}) RETURNING id", removed)
    for s in sources:
        s.id = known[s.url]
    return [s for s in sources if s.enabled]


# ---------------------------------------------------------------- 記事の整形

@dataclass
class Article:
    source_id: int
    url: str
    title: str
    category: str
    publisher: str | None = None
    description: str | None = None
    image_url: str | None = None
    published_at: str | None = None
    needs_ogp: bool = field(default=False, repr=False)


def clean_url(url: str) -> str:
    """トラッキング用のパラメータとフラグメントを外す。"""
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(query), ""))


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    if not text:
        return None
    return text[:DESCRIPTION_MAX] + ("…" if len(text) > DESCRIPTION_MAX else "")


def to_iso(struct) -> str | None:
    if not struct:
        return None
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def absolute_image(url: str | None, base: str) -> str | None:
    if not url:
        return None
    url = html.unescape(url.strip())
    if url.startswith("//"):
        url = "https:" + url
    url = urljoin(base, url)
    return url if url.startswith(("http://", "https://")) else None


def image_from_entry(entry, base: str) -> str | None:
    """RSS の記事情報から画像URLを探す。"""
    for key in ("media_thumbnail", "media_content"):
        for m in entry.get(key) or []:
            if m.get("url") and (key == "media_thumbnail" or m.get("medium") == "image"
                                 or str(m.get("type", "")).startswith("image/")):
                return absolute_image(m["url"], base)
    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
            return absolute_image(enc["href"], base)
    if entry.get("hatena_imageurl"):
        return absolute_image(entry["hatena_imageurl"], base)
    bodies = [c.get("value", "") for c in entry.get("content") or []] + [entry.get("summary", "")]
    for body in bodies:
        m = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", body or "", re.I)
        if m and not re.search(r"(feeds\.feedburner|pixel|spacer|1x1)", m.group(1)):
            return absolute_image(m.group(1), base)
    return None


OGP_PATTERNS = [
    re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:image(?::secure_url)?|twitter:image(?::src)?)[\"'][^>]*content=[\"']([^\"']+)", re.I),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*(?:property|name)=[\"'](?:og:image(?::secure_url)?|twitter:image(?::src)?)[\"']", re.I),
]


def fetch_ogp_image(url: str) -> str | None:
    try:
        head = http_get(url, max_bytes=OGP_MAX_BYTES, timeout=10).decode("utf-8", errors="ignore")
    except Exception:
        return None
    for pat in OGP_PATTERNS:
        m = pat.search(head)
        if m:
            return absolute_image(m.group(1), url)
    return None


def parse_feed(source: Source, raw: bytes, now: datetime) -> list[Article]:
    feed = feedparser.parse(raw)
    is_google = urlparse(source.url).netloc == GOOGLE_NEWS_HOST
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    articles = []
    entries = sorted(feed.entries,
                     key=lambda e: e.get("published_parsed") or e.get("updated_parsed") or time.gmtime(0),
                     reverse=True)
    for e in entries[:MAX_ENTRIES_PER_FEED]:
        link, title = e.get("link"), strip_html(e.get("title"))
        if not link or not title:
            continue
        published = e.get("published_parsed") or e.get("updated_parsed")
        if published and datetime.fromtimestamp(calendar.timegm(published), tz=timezone.utc) < cutoff:
            continue
        publisher = source.name
        if is_google:
            publisher = (e.get("source") or {}).get("title") or None
            if publisher and title.endswith(f" - {publisher}"):
                title = title[: -len(publisher) - 3].strip()
        url = link if is_google else clean_url(link)
        image = image_from_entry(e, url)
        articles.append(Article(
            source_id=source.id, url=url, title=title, category=source.category,
            publisher=publisher,
            # Googleニュースの説明文は見出しと媒体名の繰り返しなので使わない
            description=None if is_google else strip_html(e.get("summary")),
            image_url=image, published_at=to_iso(published),
            # Googleニュースのリンクは中継ページなので OGP を取りにいかない
            needs_ogp=image is None and not is_google,
        ))
    return articles


# ---------------------------------------------------------------- 保存・削除

ARTICLE_COLUMNS = ["source_id", "url", "title", "publisher", "description",
                   "image_url", "category", "published_at"]


def save_articles(db: Database, articles: list[Article]) -> int:
    rows = [(a.source_id, a.url, a.title, a.publisher, a.description,
             a.image_url, a.category, a.published_at) for a in articles]
    return db.insert_many("articles", ARTICLE_COLUMNS, rows)


OLD_ARTICLES = f"""
  SELECT id FROM articles
  WHERE fetched_at < datetime('now', '-{RETENTION_DAYS} days')
    AND id NOT IN (SELECT article_id FROM ratings)
    AND id NOT IN (SELECT article_id FROM article_states WHERE favorite = 1)
"""


def cleanup(db: Database) -> int:
    """評価・お気に入りのない古い記事を、参照している行ごと削除する。"""
    old = db.query(f"SELECT COUNT(*) AS n FROM ({OLD_ARTICLES})")[0]["n"]
    if old:
        db.query(f"DELETE FROM daily_picks WHERE article_id IN ({OLD_ARTICLES})")
        db.query(f"DELETE FROM article_states WHERE article_id IN ({OLD_ARTICLES})")
        db.query(f"DELETE FROM articles WHERE id IN ({OLD_ARTICLES})")
    return old


# ---------------------------------------------------------------- 本体

def run(db: Database, config: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sources = sync_sources(db, load_sources(config))
    report = {"sources": len(sources), "failed": [], "fetched": 0, "new": 0,
              "with_image": 0, "deleted": 0}

    def fetch(s: Source):
        try:
            return s, parse_feed(s, http_get(s.url, timeout=20), now), None
        except Exception as e:  # 1つの配信元の失敗で全体を止めない
            return s, [], f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch, sources))

    candidates: dict[str, Article] = {}
    for s, arts, err in results:
        if err:
            report["failed"].append({"name": s.name, "url": s.url, "error": err[:200]})
        for a in arts:
            candidates.setdefault(a.url, a)  # 同じ記事が複数の配信元にあれば最初の1件
    report["fetched"] = len(candidates)

    existing = {r["url"] for r in db.select_in("SELECT url FROM articles WHERE url IN ({in})",
                                               list(candidates))}
    new = [a for url, a in candidates.items() if url not in existing]

    targets = [a for a in new if a.needs_ogp]
    with ThreadPoolExecutor(max_workers=OGP_WORKERS) as pool:
        for a, img in zip(targets, pool.map(lambda a: fetch_ogp_image(a.url), targets)):
            a.image_url = img

    report["new"] = save_articles(db, new) if new else 0
    report["with_image"] = sum(1 for a in new if a.image_url)
    report["deleted"] = cleanup(db)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(ROOT / "config" / "sources.yaml"))
    args = parser.parse_args()

    report = run(connect(), Path(args.config))
    print(f"配信元 {report['sources']} 件 / 取得 {report['fetched']} 件 / 新着 {report['new']} 件"
          f"（画像あり {report['with_image']} 件） / 削除 {report['deleted']} 件")
    if report["failed"]:
        print(f"取得に失敗した配信元 {len(report['failed'])} 件:")
        for f in report["failed"]:
            print(f"  - {f['name']} ({f['url']}): {f['error']}")
    print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
    # すべての配信元で失敗したときだけエラー終了（通知を飛ばすため）
    return 1 if report["sources"] and len(report["failed"]) == report["sources"] else 0


if __name__ == "__main__":
    sys.exit(main())
