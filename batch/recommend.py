"""推薦バッチ: 好みの重みで記事にスコアを付け、その回（朝・夕）に表示する記事を daily_picks に保存する。

使い方:
  python batch/recommend.py                  # 現在時刻（日本時間）から朝・夕を判定
  python batch/recommend.py --edition morning

スコア = 分野の重み + タグの重みの合計 × 0.5 + 配信元の重み × 0.5 + 新しさ
（重みの増減は API 側で、評価やお気に入りの操作のたびに行う）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import Database, connect  # noqa: E402

JST = timezone(timedelta(hours=9))

PAGE_SIZE = 30                  # 画面に1回で表示する件数（「もっと見る」で次の30件）
PAGES = 3                       # 1回の実行で選んでおくページ数（30件×3＝90件）
MAIN_SLOTS = 24 * PAGES         # 通常枠（1ページあたり24件）
DISCOVERY_SLOTS = 6 * PAGES     # 発見枠（1ページあたり6件）
CATEGORY_CAP = 12 * PAGES       # 1分野が通常枠に入れる上限（全体の4割）
CANDIDATE_HOURS = 48            # 候補にする記事の取得期間
FRESH_HALF_LIFE_HOURS = 12      # 新しさの加点が半分になる時間
FRESH_MAX = 1.0                 # 取得直後の新しさの加点
TAG_FACTOR = 0.5
SOURCE_FACTOR = 0.5
DAILY_DECAY = 0.98              # 1日1回（朝の回）、すべての重みに掛ける
DISCOVERY_CATEGORIES = 4        # 発見枠に使う「普段の関心から遠い分野」の数
DISCOVERY_MAX_WEIGHT = 0.5      # 分野の重みがこれ以下なら「関心から遠い」とみなす

CATEGORIES = ["it", "keiba", "stock", "society", "entame", "trivia",
              "lifehack", "science", "sports", "other"]


def now_jst() -> datetime:
    return datetime.now(JST)


def edition_of(t: datetime) -> str:
    return "morning" if t.hour < 12 else "evening"


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def norm_title(title: str) -> str:
    """似た見出しの重複を見分けるための正規化（記号・空白を除いた先頭30文字）。"""
    return re.sub(r"[\s\W_]+", "", title.lower())[:30]


def is_dup(key: str, seen: list[str]) -> bool:
    """同じ見出し、または一方がもう一方の先頭部分（8文字以上）なら重複とみなす。"""
    for k in seen:
        short, long_ = (key, k) if len(key) <= len(k) else (k, key)
        if short == long_ or (len(short) >= 8 and long_.startswith(short)):
            return True
    return False


def load_weights(db: Database) -> dict[tuple[str, str], dict]:
    return {(r["feature_type"], r["feature"]): r for r in db.query("SELECT * FROM weights")}


def apply_daily_decay(db: Database, today: str) -> bool:
    """その日まだ減衰していなければ、すべての重みを DAILY_DECAY 倍する。"""
    done = db.query("SELECT 1 FROM weights WHERE feature_type = 'meta' AND feature = ?",
                    [f"decayed:{today}"])
    if done:
        return False
    db.query("UPDATE weights SET weight = weight * ? WHERE feature_type != 'meta'", [DAILY_DECAY])
    db.query("DELETE FROM weights WHERE feature_type = 'meta' AND feature LIKE 'decayed:%'")
    db.query("INSERT INTO weights (feature_type, feature, weight) VALUES ('meta', ?, 0)",
             [f"decayed:{today}"])
    return True


def load_candidates(db: Database, date: str, edition: str) -> list[dict]:
    # 以前の回で1ページ目（必ず表示される30件）に入った記事は除く。
    # 2ページ目以降は見ていない可能性があるので、次の回の候補に残す。
    return db.query(f"""
      SELECT a.* FROM articles a
      WHERE a.fetched_at >= datetime('now', '-{CANDIDATE_HOURS} hours')
        AND a.id NOT IN (SELECT article_id FROM daily_picks
                         WHERE rank <= {PAGE_SIZE} AND NOT (date = ? AND edition = ?))
        AND a.id NOT IN (SELECT article_id FROM ratings)
        AND a.id NOT IN (SELECT article_id FROM article_states WHERE is_read = 1 OR opened_at IS NOT NULL)
        AND COALESCE(a.publisher, '') NOT IN (SELECT value FROM mutes WHERE type = 'source')
    """, [date, edition])


def muted_keywords(db: Database) -> list[str]:
    return [r["value"].lower() for r in db.query("SELECT value FROM mutes WHERE type = 'keyword'")]


def score_article(a: dict, weights: dict, now_utc: datetime) -> float:
    w = lambda t, f: (weights.get((t, f)) or {}).get("weight", 0.0)  # noqa: E731
    tags = json.loads(a["tags"]) if a.get("tags") else []
    score = w("category", a["category"])
    score += TAG_FACTOR * sum(w("tag", t) for t in tags)
    score += SOURCE_FACTOR * w("source", a.get("publisher") or "")
    t = parse_time(a.get("published_at")) or parse_time(a.get("fetched_at")) or now_utc
    fetched = parse_time(a.get("fetched_at")) or now_utc
    t = min(t, fetched)  # 配信時刻が未来になっている記事への対策
    hours = max(0.0, (now_utc - t).total_seconds() / 3600)
    score += FRESH_MAX * 0.5 ** (hours / FRESH_HALF_LIFE_HOURS)
    # 要約・画像のある記事を少しだけ優先（同点時の並びを良くする）
    score += 0.05 * bool(a.get("summary")) + 0.05 * bool(a.get("image_url"))
    return round(score, 4)


def pick(candidates: list[dict], weights: dict, keywords: list[str], now_utc: datetime):
    def is_muted(a):
        tags = json.loads(a["tags"]) if a.get("tags") else []
        text = " ".join([a["title"], a.get("summary") or "", *tags]).lower()
        return any(k in text for k in keywords)

    scored = []
    for a in candidates:
        if is_muted(a):
            continue
        scored.append((score_article(a, weights, now_utc), a))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))

    # 似た見出しの重複を除く（スコアの高い方を残す）
    unique, seen_titles = [], []
    for s, a in scored:
        key = norm_title(a["title"])
        if not is_dup(key, seen_titles):
            seen_titles.append(key)
            unique.append((s, a))

    # 発見枠を先に確保する: 普段の関心から遠い分野（重みが低く、評価回数の少ない分野）から、
    # 好みの重みではなく新しさと要約・画像の有無で選ぶ
    def rated(c):
        return (weights.get(("category", c)) or {}).get("rated_count", 0)

    def weight(c):
        return (weights.get(("category", c)) or {}).get("weight", 0.0)

    present = {a["category"] for _, a in unique}
    ranked = sorted((c for c in CATEGORIES if c in present),
                    key=lambda c: (weight(c) > DISCOVERY_MAX_WEIGHT, rated(c), weight(c), CATEGORIES.index(c)))
    far = [c for c in ranked if weight(c) <= DISCOVERY_MAX_WEIGHT]
    low = (far if len(far) >= 2 else ranked)[:DISCOVERY_CATEGORIES]
    pools = {c: sorted((x for x in unique if x[1]["category"] == c),
                       key=lambda x: (bool(x[1].get("summary")), bool(x[1].get("image_url")),
                                      x[1].get("fetched_at") or ""), reverse=True) for c in low}
    discovery = []
    while len(discovery) < DISCOVERY_SLOTS and any(pools.values()):
        for c in low:
            if pools[c] and len(discovery) < DISCOVERY_SLOTS:
                s, a = pools[c].pop(0)
                discovery.append((s, a, "discovery"))
    taken = {a["id"] for _, a, _ in discovery}

    # 通常枠: 残りをスコア順に、1分野の上限を守って選ぶ
    per_category: dict[str, int] = {}
    main = []
    for s, a in unique:
        if len(main) >= MAIN_SLOTS:
            break
        if a["id"] in taken or per_category.get(a["category"], 0) >= CATEGORY_CAP:
            continue
        per_category[a["category"]] = per_category.get(a["category"], 0) + 1
        main.append((s, a, "main"))

    # 発見枠は通常枠4件ごとに1件ずつ差し込む（まとめて末尾に置かない）
    ordered, d = [], list(discovery)
    for i, item in enumerate(main, 1):
        ordered.append(item)
        if i % 4 == 0 and d:
            ordered.append(d.pop(0))
    ordered.extend(d)
    return ordered


def run(db: Database, now: datetime | None = None, edition: str | None = None) -> dict:
    now = now or now_jst()
    date = now.strftime("%Y-%m-%d")
    edition = edition or edition_of(now)
    decayed = apply_daily_decay(db, date) if edition == "morning" else False
    weights = load_weights(db)
    candidates = load_candidates(db, date, edition)
    picks = pick(candidates, weights, muted_keywords(db), now.astimezone(timezone.utc))

    db.query("DELETE FROM daily_picks WHERE date = ? AND edition = ?", [date, edition])
    rows = [(date, edition, a["id"], rank, s, slot) for rank, (s, a, slot) in enumerate(picks, 1)]
    if rows:
        db.insert_many("daily_picks", ["date", "edition", "article_id", "rank", "score", "slot"], rows,
                       on_conflict="OR REPLACE")
    return {"date": date, "edition": edition, "candidates": len(candidates), "picked": len(rows),
            "discovery": sum(1 for r in rows if r[5] == "discovery"), "decayed": decayed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", choices=["morning", "evening"])
    args = parser.parse_args()
    r = run(connect(), edition=args.edition)
    label = "朝" if r["edition"] == "morning" else "夕"
    print(f"{r['date']} {label}の回 / 候補 {r['candidates']} 件 / 表示 {r['picked']} 件"
          f"（うち発見枠 {r['discovery']} 件）{' / 重みを減衰' if r['decayed'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
