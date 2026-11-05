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


def random_title(category, n):
    # 重複判定に引っかからないよう、記事ごとにばらばらの漢字で見出しを作る
    seed = sum(map(ord, category)) * 7919 + n * 104729
    return category + "".join(chr(0x4E00 + (seed * (k + 3) * 2654435761 >> 7) % 6000) for k in range(12))


def add(db, n, category, title=None, hours_ago=1, tags=None, publisher="媒体A", summary="要約"):
    db.query("INSERT INTO articles (source_id, url, title, publisher, summary, category, tags, published_at, fetched_at) "
             "VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now', ?), datetime('now', ?))",
             [f"https://a.example/{category}/{n}/{hours_ago}", title or random_title(category, n), publisher, summary,
              category, json.dumps(tags or []), f"-{hours_ago} hours", f"-{hours_ago} hours"])
    return db.query("SELECT last_insert_rowid() AS id")[0]["id"]


def picks(db, date, edition):
    return db.query("SELECT p.*, a.category, a.title FROM daily_picks p JOIN articles a ON a.id = p.article_id "
                    "WHERE date = ? AND edition = ? ORDER BY rank", [date, edition])


def main():
    now = datetime.now(JST).replace(hour=6, minute=5)
    date = now.strftime("%Y-%m-%d")

    db = new_db()
    for i in range(100):
        add(db, i, "it")
    for i in range(40):
        add(db, i, "keiba")
    for c in ["science", "sports", "trivia", "entame", "other"]:
        for i in range(10):
            add(db, i, c)
    add(db, 999, "it", hours_ago=60)                      # 48時間より古い
    rated = add(db, 998, "it", title="評価済みの記事")
    db.query("INSERT INTO ratings (article_id, rating) VALUES (?, 4)", [rated])
    muted_src = add(db, 997, "it", title="非表示媒体の記事", publisher="非表示媒体")
    db.query("INSERT INTO mutes (type, value) VALUES ('source', '非表示媒体')")
    add(db, 996, "it", title="ミュート語を含む記事", tags=["ガチャ"])
    db.query("INSERT INTO mutes (type, value) VALUES ('keyword', 'ガチャ')")
    add(db, 995, "keiba", title="天皇賞の結果 - 速報")
    add(db, 994, "keiba", title="天皇賞の結果 - 速報（別媒体）", publisher="媒体B")

    r = recommend.run(db, now=now)
    print(r)
    p = picks(db, date, "morning")
    titles = [x["title"] for x in p]
    page1 = [x for x in p if x["rank"] <= 30]
    check(r["edition"] == "morning" and len(p) == 90, "朝の回として90件（30件×3ページ）を選ぶ")
    check(sum(1 for x in page1 if x["slot"] == "discovery") == 6, "1ページ目30件のうち発見枠は6件")
    check(sum(1 for x in p if x["slot"] == "discovery") == 18, "全体では発見枠18件")
    page1_main = [x for x in page1 if x["slot"] == "main"]
    top_cat = max(sum(1 for x in page1_main if x["category"] == c) for c in recommend.CATEGORIES)
    check(top_cat <= round(len(page1_main) * recommend.MAX_CATEGORY_SHARE) + 1,
          f"1ページ目の通常枠で1分野が3割を大きく超えない（最大 {top_cat}/{len(page1_main)} 件）")
    check("評価済みの記事" not in titles and "非表示媒体の記事" not in titles, "評価済み・非表示媒体の記事は出さない")
    check("ミュート語を含む記事" not in titles, "非表示キーワードを含む記事は出さない")
    check(sum(1 for t in titles if t.startswith("天皇賞の結果")) == 1, "似た見出しの重複は1件だけ")
    check(not any("記事999" in t for t in titles), "48時間より古い記事は出さない")
    check(p[4]["slot"] == "discovery", "発見枠は通常枠4件ごとに差し込む")
    disc_cats = {x["category"] for x in p if x["slot"] == "discovery"}
    check(disc_cats == {"science", "sports", "other"}, f"発見枠は関心分野以外（科学・スポーツ・その他）から選ぶ: {disc_cats}")

    # 好みの重みが効く: keiba の重みを上げると keiba が上位に来る
    db.query("INSERT INTO weights (feature_type, feature, weight, rated_count) VALUES ('category', 'keiba', 5, 3) "
             "ON CONFLICT(feature_type, feature) DO UPDATE SET weight = 5, rated_count = 3")
    r = recommend.run(db, now=now)  # 同じ回をやり直し
    p = picks(db, date, "morning")
    check(p[0]["category"] == "keiba", "重みの高い分野が上位に来る")
    check(len(p) == 90, "同じ回をやり直しても件数は同じ（前の結果は置き換え）")
    check(r["decayed"] is False, "減衰は1日1回だけ")
    w = db.query("SELECT weight FROM weights WHERE feature_type='category' AND feature='it'")[0]["weight"]
    check(abs(w - 0.98) < 1e-9, "朝の回で重みを0.98倍に減衰")

    # 夕方の回: 朝に出した記事は出さない
    evening = now.replace(hour=18)
    shown = {x["article_id"] for x in p if x["rank"] <= 30}
    later_pages = {x["article_id"] for x in p if x["rank"] > 30}
    for i in range(5):
        add(db, 200 + i, "it", hours_ago=0)
    r = recommend.run(db, now=evening)
    pe = picks(db, date, "evening")
    ids_e = {x["article_id"] for x in pe}
    check(r["edition"] == "evening" and not (shown & ids_e), "夕方の回は、朝の1ページ目に出した記事を除く")
    check(bool(later_pages & ids_e), "朝の2ページ目以降（見ていないかもしれない記事）は夕方の候補に残す")
    check(r["decayed"] is False, "夕方の回では減衰しない")

    # 評価が効きすぎない: 競馬と野球（エンタメ）を強く評価しても、ITなど他の関心分野も毎ページ出る
    db3 = new_db()
    for c, n in [("it", 60), ("keiba", 60), ("entame", 60), ("stock", 30), ("society", 30), ("lifehack", 30), ("trivia", 30), ("science", 10)]:
        for i in range(n):
            add(db3, i, c)
    db3.query("UPDATE weights SET weight = 9, rated_count = 5 WHERE feature_type = 'category' AND feature = 'keiba'")
    db3.query("UPDATE weights SET weight = 7, rated_count = 4 WHERE feature_type = 'category' AND feature = 'entame'")
    recommend.run(db3, now=now)
    p3 = [x for x in picks(db3, date, "morning") if x["rank"] <= 30]
    cnt = {c: sum(1 for x in p3 if x["category"] == c) for c in ["keiba", "entame", "it"]}
    check(cnt["keiba"] >= cnt["it"] and cnt["keiba"] <= 8 and cnt["it"] >= 3,
          f"強く評価した分野は多めでも偏りすぎない（1ページ目: 競馬{cnt['keiba']}・エンタメ{cnt['entame']}・IT{cnt['it']}件）")
    check(p3[0]["category"] in ("keiba", "entame"), "強く評価した分野が先頭付近に来る")

    # 別の配信元の同じ話題は1件だけ
    db4 = new_db()
    add(db4, 1, "keiba", title="天皇賞・秋はイクイノックスが優勝 レコードで連覇", tags=["天皇賞", "イクイノックス", "G1"], publisher="A紙")
    add(db4, 2, "keiba", title="イクイノックスが天皇賞・秋を連覇、レコード決着", tags=["天皇賞", "イクイノックス"], publisher="B紙")
    add(db4, 3, "keiba", title="【天皇賞秋】イクイノックス圧勝 ルメール「強かった」", tags=["イクイノックス", "天皇賞", "ルメール"], publisher="C紙")
    add(db4, 4, "keiba", title="来週のジャパンカップ 出走予定馬が判明", tags=["ジャパンカップ", "G1"], publisher="A紙")
    recommend.run(db4, now=now)
    t4 = [x["title"] for x in picks(db4, date, "morning")]
    sx = {"_bigrams": recommend.bigrams("日経平均、続伸"), "_tags": {"日経平均"}}
    sy = {"_bigrams": recommend.bigrams("日経平均、続落"), "_tags": {"日経平均"}}
    check(not recommend.same_story(sx, sy), "短い見出しの別の話題（続伸／続落）は別の記事として扱う")
    disc3 = {x["category"] for x in picks(db3, date, "morning") if x["slot"] == "discovery"}
    check(disc3 == {"science"}, f"関心から遠い分野が1つでも、発見枠には関心分野を混ぜない: {disc3}")
    check(sum(1 for t in t4 if "イクイノックス" in t) == 1 and any("ジャパンカップ" in t for t in t4),
          f"別の配信元の同じ話題は1件だけ、別の話題は残す（{len(t4)}件）")

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
