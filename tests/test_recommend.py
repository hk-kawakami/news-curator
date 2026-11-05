"""推薦バッチのローカル確認（SQLite で動かす）。

実行: python tests/test_recommend.py
"""
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "batch"))

import recommend  # noqa: E402
from db import SqliteDatabase  # noqa: E402

JST = timezone(timedelta(hours=9))


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label)
    if not cond:
        check.failed += 1


check.failed = 0


def new_db():
    path = Path(tempfile.mkdtemp()) / "t.db"
    sqlite3.connect(path).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    db = SqliteDatabase(str(path))
    db.query("INSERT INTO sources (name, url, category) VALUES ('s', 'https://s.example/rss', 'it')")
    return db


def add(db, n, category, title=None, hours_ago=1, tags=None, publisher="媒体A", summary="要約"):
    db.query("INSERT INTO articles (source_id, url, title, publisher, summary, category, tags, published_at, fetched_at) "
             "VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now', ?), datetime('now', ?))",
             [f"https://a.example/{category}/{n}/{hours_ago}", title or f"{category}の記事{n}", publisher, summary,
              category, json.dumps(tags or []), f"-{hours_ago} hours", f"-{hours_ago} hours"])
    return db.query("SELECT last_insert_rowid() AS id")[0]["id"]


def picks(db, date, edition):
    return db.query("SELECT p.*, a.category, a.title FROM daily_picks p JOIN articles a ON a.id = p.article_id "
                    "WHERE date = ? AND edition = ? ORDER BY rank", [date, edition])


def main():
    now = datetime.now(JST).replace(hour=6, minute=5)
    date = now.strftime("%Y-%m-%d")

    db = new_db()
    for i in range(30):
        add(db, i, "it")
    for i in range(10):
        add(db, i, "keiba")
    for c in ["science", "sports", "trivia", "entame", "other"]:
        for i in range(3):
            add(db, i, c)
    add(db, 99, "it", hours_ago=60)                      # 48時間より古い
    rated = add(db, 98, "it", title="評価済みの記事")
    db.query("INSERT INTO ratings (article_id, rating) VALUES (?, 4)", [rated])
    muted_src = add(db, 97, "it", title="非表示媒体の記事", publisher="非表示媒体")
    db.query("INSERT INTO mutes (type, value) VALUES ('source', '非表示媒体')")
    add(db, 96, "it", title="ミュート語を含む記事", tags=["ガチャ"])
    db.query("INSERT INTO mutes (type, value) VALUES ('keyword', 'ガチャ')")
    add(db, 95, "keiba", title="天皇賞の結果 - 速報")
    add(db, 94, "keiba", title="天皇賞の結果 - 速報（別媒体）", publisher="媒体B")

    r = recommend.run(db, now=now)
    print(r)
    p = picks(db, date, "morning")
    titles = [x["title"] for x in p]
    check(r["edition"] == "morning" and len(p) == 30, "朝の回として30件を選ぶ")
    check(sum(1 for x in p if x["slot"] == "discovery") == 6, "うち発見枠は6件")
    check(max(sum(1 for x in p if x["category"] == c and x["slot"] == "main") for c in recommend.CATEGORIES) <= recommend.CATEGORY_CAP,
          "通常枠で1分野が12件を超えない")
    check("評価済みの記事" not in titles and "非表示媒体の記事" not in titles, "評価済み・非表示媒体の記事は出さない")
    check("ミュート語を含む記事" not in titles, "非表示キーワードを含む記事は出さない")
    check(sum(1 for t in titles if t.startswith("天皇賞の結果")) == 1, "似た見出しの重複は1件だけ")
    check(not any("記事99" in t for t in titles), "48時間より古い記事は出さない")
    check(p[4]["slot"] == "discovery", "発見枠は通常枠4件ごとに差し込む")
    disc_cats = {x["category"] for x in p if x["slot"] == "discovery"}
    check("it" not in disc_cats and "keiba" not in disc_cats, "発見枠は評価の少ない分野から選ぶ")

    # 好みの重みが効く: keiba の重みを上げると keiba が上位に来る
    db.query("INSERT INTO weights (feature_type, feature, weight, rated_count) VALUES ('category', 'keiba', 5, 3) "
             "ON CONFLICT(feature_type, feature) DO UPDATE SET weight = 5, rated_count = 3")
    r = recommend.run(db, now=now)  # 同じ回をやり直し
    p = picks(db, date, "morning")
    check(p[0]["category"] == "keiba", "重みの高い分野が上位に来る")
    check(len(p) == 30, "同じ回をやり直しても件数は同じ（前の結果は置き換え）")
    check(r["decayed"] is False, "減衰は1日1回だけ")
    w = db.query("SELECT weight FROM weights WHERE feature_type='category' AND feature='it'")[0]["weight"]
    check(abs(w - 0.98) < 1e-9, "朝の回で重みを0.98倍に減衰")

    # 夕方の回: 朝に出した記事は出さない
    evening = now.replace(hour=18)
    shown = {x["article_id"] for x in p}
    for i in range(5):
        add(db, 200 + i, "it", hours_ago=0)
    r = recommend.run(db, now=evening)
    pe = picks(db, date, "evening")
    check(r["edition"] == "evening" and not (shown & {x["article_id"] for x in pe}), "夕方の回は朝に出した記事を除く")
    check(r["decayed"] is False, "夕方の回では減衰しない")

    # タグ・配信元の重み
    db2 = new_db()
    a1 = add(db2, 1, "it", tags=["Tableau"], publisher="媒体X")
    a2 = add(db2, 2, "it", tags=["Excel"], publisher="媒体Y")
    db2.query("INSERT INTO weights (feature_type, feature, weight) VALUES ('tag', 'Tableau', 2), ('source', '媒体Y', -2)")
    recommend.run(db2, now=now)
    p2 = picks(db2, date, "morning")
    check([x["article_id"] for x in p2][:2] == [a1, a2], "タグの重みが高い記事が上、配信元の重みが低い記事が下")

    print(f"\n{'すべて成功' if not check.failed else f'{check.failed} 件失敗'}")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
