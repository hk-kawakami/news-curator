"""収集バッチのローカル確認（ネットワークを使わず、サンプルRSSと SQLite で動かす）。

実行: python tests/test_collect.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "batch"))

import collect  # noqa: E402
from db import SqliteDatabase  # noqa: E402

NOW = datetime(2026, 11, 5, 21, 0, tzinfo=timezone.utc)
RECENT = format_datetime(NOW - timedelta(hours=3))
OLD = format_datetime(NOW - timedelta(days=10))

RSS_MEDIA = f"""<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>A</title>
<item><title>サムネ付きの記事</title><link>https://a.example/1?utm_source=rss&amp;id=7</link>
<description>&lt;p&gt;説明文&lt;/p&gt;</description><pubDate>{RECENT}</pubDate>
<media:thumbnail url="https://a.example/img1.jpg"/></item>
<item><title>画像なしの記事（OGPで補う）</title><link>https://a.example/2</link><pubDate>{RECENT}</pubDate></item>
<item><title>古い記事</title><link>https://a.example/old</link><pubDate>{OLD}</pubDate></item>
</channel></rss>"""

ATOM_IMG = f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>B</title>
<entry><title>本文に画像がある記事</title><link href="https://b.example/post"/>
<updated>{(NOW - timedelta(hours=1)).isoformat()}</updated>
<content type="html">&lt;img src="/images/p.png"&gt;本文</content></entry>
<entry><title>サムネ付きの記事</title><link href="https://a.example/1?id=7"/>
<updated>{(NOW - timedelta(hours=1)).isoformat()}</updated></entry>
</feed>"""

HATENA_RDF = f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns="http://purl.org/rss/1.0/" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:hatena="http://www.hatena.ne.jp/info/xmlns#">
<channel rdf:about="https://b.hatena.ne.jp/"><title>はてブ</title><link>https://b.hatena.ne.jp/</link><description>d</description></channel>
<item rdf:about="https://c.example/hot"><title>はてブの人気記事</title><link>https://c.example/hot</link>
<description>はてブの説明</description><dc:date>{(NOW - timedelta(hours=2)).isoformat()}</dc:date>
<hatena:imageurl>https://c.example/ogp.jpg</hatena:imageurl></item>
</rdf:RDF>"""

GOOGLE = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Google News</title>
<item><title>名古屋で新しい施設がオープン - 中日新聞</title>
<link>https://news.google.com/rss/articles/CBMiXYZ?oc=5</link><pubDate>{RECENT}</pubDate>
<description>&lt;a href="x"&gt;名古屋で新しい施設がオープン&lt;/a&gt; 中日新聞</description>
<source url="https://www.chunichi.co.jp">中日新聞</source></item>
</channel></rss>"""

OGP_PAGE = b'<html><head><meta content="https://a.example/og2.jpg" property="og:image"></head></html>'

CONFIG = """
feeds:
  - { name: "A", url: "https://feeds.example/a.xml", category: it }
  - { name: "B", url: "https://feeds.example/b.xml", category: trivia }
  - { name: "はてブ", url: "https://feeds.example/hatena.rss", category: lifehack }
  - { name: "壊れた配信元", url: "https://feeds.example/broken.xml", category: it }
google_news:
  society: ["名古屋"]
"""

RESPONSES = {
    "https://feeds.example/a.xml": RSS_MEDIA.encode(),
    "https://feeds.example/b.xml": ATOM_IMG.encode(),
    "https://feeds.example/hatena.rss": HATENA_RDF.encode(),
    collect.google_news_url("名古屋"): GOOGLE.encode(),
    "https://a.example/2": OGP_PAGE,
}
requested = []


def fake_get(url, max_bytes=None, timeout=15):
    requested.append(url)
    if url not in RESPONSES:
        raise ConnectionError("404")
    return RESPONSES[url]


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label)
    if not cond:
        check.failed += 1


check.failed = 0


def main():
    collect.http_get = fake_get
    tmp = Path(tempfile.mkdtemp())
    db_path, cfg = tmp / "test.db", tmp / "sources.yaml"
    cfg.write_text(CONFIG, encoding="utf-8")
    sqlite3.connect(db_path).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    db = SqliteDatabase(str(db_path))

    report = collect.run(db, cfg, now=NOW)
    print(report)
    arts = {r["url"]: r for r in db.query("SELECT * FROM articles")}

    check(report["sources"] == 5, "配信元5件（RSS4＋Googleニュース1）を同期")
    check(len(report["failed"]) == 1 and report["failed"][0]["name"] == "壊れた配信元",
          "失敗した配信元だけがログに出て、他は取り込まれる")
    check("https://a.example/1?id=7" in arts, "utm_ パラメータを外し、他のパラメータは残す")
    check(sum(1 for u in arts if u.startswith("https://a.example/1")) == 1, "別の配信元にある同じ記事は1件だけ保存")
    check("https://a.example/old" not in arts, "3日より古い記事は取り込まない")
    check(arts["https://a.example/1?id=7"]["image_url"] == "https://a.example/img1.jpg", "media:thumbnail から画像を取得")
    check(arts["https://a.example/1?id=7"]["description"] == "説明文", "説明文のHTMLタグを除去")
    check(arts["https://a.example/2"]["image_url"] == "https://a.example/og2.jpg", "画像がない記事はOGP画像で補う")
    check(arts["https://b.example/post"]["image_url"] == "https://b.example/images/p.png", "本文の<img>を絶対URLにして取得")
    check(arts["https://c.example/hot"]["image_url"] == "https://c.example/ogp.jpg", "はてブの hatena:imageurl を取得")
    g = arts["https://news.google.com/rss/articles/CBMiXYZ?oc=5"]
    check(g["title"] == "名古屋で新しい施設がオープン" and g["publisher"] == "中日新聞",
          "Googleニュースは見出しから媒体名を分け、publisher に保存")
    check(g["category"] == "society" and g["description"] is None, "Googleニュースの分野と説明文の扱い")
    check(not any(u.startswith("https://news.google.com/rss/articles") for u in requested),
          "Googleニュースの記事ページにはOGPを取りにいかない")

    # 2回目: 既存記事は再保存せず、OGP も取り直さない
    requested.clear()
    report2 = collect.run(db, cfg, now=NOW)
    check(report2["new"] == 0, "2回目の実行では新着0件（重複保存しない）")
    check("https://a.example/2" not in requested, "既存記事のOGPは取り直さない")

    # 配信元を設定から外すと停止になる
    cfg.write_text(CONFIG.replace('  - { name: "B", url: "https://feeds.example/b.xml", category: trivia }\n', ""),
                   encoding="utf-8")
    collect.run(db, cfg, now=NOW)
    b = db.query("SELECT enabled FROM sources WHERE url = 'https://feeds.example/b.xml'")[0]
    check(b["enabled"] == 0, "設定から消した配信元は停止（記事は残る）")
    check("https://b.example/post" in {r["url"] for r in db.query("SELECT url FROM articles")}, "停止した配信元の記事は残る")

    # 90日より古い記事の削除（評価・お気に入りがあれば残す）
    ids = {r["url"]: r["id"] for r in db.query("SELECT id, url FROM articles")}
    db.query("UPDATE articles SET fetched_at = datetime('now', '-100 days')")
    db.query("INSERT INTO ratings (article_id, rating) VALUES (?, 5)", [ids["https://c.example/hot"]])
    db.query("INSERT INTO article_states (article_id, favorite) VALUES (?, 1)", [ids["https://b.example/post"]])
    db.query("INSERT INTO article_states (article_id, read_later) VALUES (?, 1)", [ids["https://a.example/2"]])
    db.query("INSERT INTO daily_picks VALUES ('2026-08-01','morning',?,1,1.0,'main')", [ids["https://a.example/2"]])
    deleted = collect.cleanup(db)
    left = {r["url"] for r in db.query("SELECT url FROM articles")}
    check(deleted == len(ids) - 2 and left == {"https://c.example/hot", "https://b.example/post"},
          "古い記事を削除し、評価済み・お気に入りは残す")
    check(db.query("SELECT COUNT(*) n FROM daily_picks")[0]["n"] == 0, "削除した記事の daily_picks も削除")

    print(f"\n{'すべて成功' if not check.failed else f'{check.failed} 件失敗'}")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
