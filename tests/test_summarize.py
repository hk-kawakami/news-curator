"""要約バッチのローカル確認（Gemini を呼ばず、模擬の応答と SQLite で動かす）。

実行: python tests/test_summarize.py
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "batch"))

import summarize  # noqa: E402
from db import SqliteDatabase  # noqa: E402


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body


def gemini_ok(items):
    return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(items, ensure_ascii=False)}]}}]})


def check(cond, label):
    print(("OK  " if cond else "NG  ") + label)
    if not cond:
        check.failed += 1


check.failed = 0


def new_db(n_articles):
    tmp = Path(tempfile.mkdtemp())
    path = tmp / "t.db"
    sqlite3.connect(path).executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    db = SqliteDatabase(str(path))
    db.query("INSERT INTO sources (name, url, category) VALUES ('s', 'https://s.example/rss', 'it')")
    for i in range(1, n_articles + 1):
        db.query("INSERT INTO articles (source_id, url, title, description, category) VALUES (1, ?, ?, ?, 'it')",
                 [f"https://a.example/{i}", f"見出し{i}", f"説明{i}"])
    # 古い記事（対象外）
    db.query("INSERT INTO articles (source_id, url, title, category, fetched_at) "
             "VALUES (1, 'https://a.example/old', '古い記事', 'it', datetime('now', '-5 days'))")
    return db


def main():
    # --- 通常: 45件 → 20/20/5 の3リクエスト。不正な分野は元の分野、欠けた記事は次回へ
    db = new_db(45)
    sent, sleeps = [], []

    def fake_post(url, payload, api_key):
        sent.append((url, payload))
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        out = []
        for it in items:
            if it["id"] == 7:
                continue  # 応答から欠ける
            out.append({"id": it["id"], "summary": f"{it['title']}の要約",
                        "category": "unknown" if it["id"] == 3 else "science",
                        "tags": ["Python", "Python", "AI", " ", "D1"]})
        return gemini_ok(out)

    summarize.http_post = fake_post
    report = summarize.run(db, "key", ["model-a"], sleep=sleeps.append)
    print(report)
    rows = {r["id"]: r for r in db.query("SELECT * FROM articles")}
    check(report["pending"] == 45 and len(sent) == 3, "3日以内の未要約45件を20件ずつ3回で送る（古い記事は対象外）")
    check(sleeps == [summarize.REQUEST_INTERVAL] * 2, "リクエストの間に間隔を空ける")
    check(report["summarized"] == 44 and report["skipped"] == 1, "応答から欠けた1件は次回に回す")
    check(rows[7]["summary"] is None, "欠けた記事は summary が空のまま")
    check(rows[1]["summary"] == "見出し1の要約" and rows[1]["category"] == "science", "要約と分野を保存")
    check(rows[3]["category"] == "it", "候補にない分野が返ったら元の分野のまま")
    check(json.loads(rows[1]["tags"]) == ["Python", "AI", "D1"], "タグの重複・空白を除いて保存")
    check(sent[0][1]["generationConfig"]["responseMimeType"] == "application/json", "JSONで返すよう指定")
    check("/models/model-a:generateContent" in sent[0][0], "指定したモデルを呼ぶ")

    # --- 2回目は残りの1件だけ
    sent.clear()
    report2 = summarize.run(db, "key", ["model-a"], sleep=lambda s: None)
    check(report2["pending"] == 1 and len(sent) == 1, "2回目は要約が残っている記事だけを送る")

    # --- モデルが使えないときは次のモデルへ
    db = new_db(3)
    calls = []

    def fake_model_fallback(url, payload, api_key):
        calls.append(url)
        if "model-old" in url:
            return FakeResponse(404, {"error": {"message": "models/model-old is not found"}})
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": ["a"]} for it in items])

    summarize.http_post = fake_model_fallback
    report = summarize.run(db, "key", ["model-old", "model-new"], sleep=lambda s: None)
    check(report["model"] == "model-new" and report["summarized"] == 3, "存在しないモデルは飛ばして次のモデルで要約")

    # --- 429 が続いたら終了し、残りは次回へ
    db = new_db(30)
    waits = []
    state = {"n": 0}

    def fake_rate_limit(url, payload, api_key):
        state["n"] += 1
        if state["n"] == 1:
            items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
            return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": []} for it in items])
        return FakeResponse(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})

    summarize.http_post = fake_rate_limit
    report = summarize.run(db, "key", ["m"], sleep=waits.append)
    check(report["summarized"] == 20 and report.get("rate_limited"), "429が続いたら停止（それまでの20件は保存済み）")
    check(waits.count(summarize.RATE_LIMIT_WAIT) == summarize.MAX_RATE_LIMIT_RETRIES, "停止前に決められた回数だけ待って再試行")
    left = db.query("SELECT COUNT(*) n FROM articles WHERE summary IS NULL AND title != '古い記事'")[0]["n"]
    check(left == 10, "残り10件は次回に要約")

    # --- 応答が壊れていたら、そのまとまりだけ飛ばして続行
    db = new_db(25)
    state = {"n": 0}

    def fake_broken(url, payload, api_key):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": []} for it in items])

    summarize.http_post = fake_broken
    report = summarize.run(db, "key", ["m"], sleep=lambda s: None)
    check(report["summarized"] == 5 and report["skipped"] == 20, "壊れた応答のまとまりは次回に回し、残りは要約")

    # --- 503（混雑）: 待って再試行すれば成功
    db = new_db(5)
    waits, state = [], {"n": 0}

    def fake_503_then_ok(url, payload, api_key):
        state["n"] += 1
        if state["n"] <= 2:
            return FakeResponse(503, {"error": {"status": "UNAVAILABLE"}})
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": []} for it in items])

    summarize.http_post = fake_503_then_ok
    report = summarize.run(db, "key", ["m"], sleep=waits.append)
    check(report["summarized"] == 5 and waits == summarize.OVERLOAD_WAITS, "503は待って再試行し、空いたら要約する")

    # --- 503 が続くモデルは次のモデルに切り替える
    db = new_db(5)
    used = []

    def fake_503_model(url, payload, api_key):
        used.append(url.split("/models/")[1].split(":")[0])
        if "busy" in url:
            return FakeResponse(503, {"error": {"status": "UNAVAILABLE"}})
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": []} for it in items])

    summarize.http_post = fake_503_model
    report = summarize.run(db, "key", ["busy", "free"], sleep=lambda s: None)
    check(report["summarized"] == 5 and report["model"] == "free", "混雑が続くモデルから別のモデルに切り替えて要約")

    # --- 全モデルが混雑し続けたら、決められたまとまり数で打ち切る
    db = new_db(100)
    summarize.http_post = lambda url, payload, api_key: FakeResponse(503, {"error": {"status": "UNAVAILABLE"}})
    report = summarize.run(db, "key", ["a", "b"], sleep=lambda s: None)
    check(report["overloaded"] == summarize.MAX_OVERLOADED_BATCHES and report["skipped"] == 100
          and report.get("rate_limited"), "全モデルが混雑し続けたら3まとまりで打ち切り、残りは次回へ（エラー扱いにしない）")

    # --- 安全フィルターで拒否されたら半分ずつに分けて送り直し、原因の1件だけ要約なしにする
    db = new_db(20)
    state = {"n": 0}

    def fake_blocked(url, payload, api_key):
        state["n"] += 1
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        if any(it["id"] == 13 for it in items):
            return FakeResponse(200, {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})
        return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": []} for it in items])

    summarize.http_post = fake_blocked
    report = summarize.run(db, "key", ["m"], sleep=lambda s: None)
    rows = {r["id"]: r for r in db.query("SELECT id, summary FROM articles")}
    check(report["summarized"] == 19 and report["blocked"] == 1, "拒否されたまとまりを分割し、原因の1件以外の19件を要約")
    check(rows[13]["summary"] == "", "原因の記事は要約なし（空文字）にして次回以降は送らない")
    check(state["n"] <= 12, f"分割の送り直しは少ない回数で済む（{state['n']}回）")
    report2 = summarize.run(db, "key", ["m"], sleep=lambda s: None)
    check(report2["pending"] == 0, "次回は拒否された記事を対象にしない")

    # --- 時間の上限を超えたら、残りは次回に回す（エラー扱いにしない）
    db = new_db(100)
    t = {"now": 0.0}

    def fake_slow(url, payload, api_key):
        t["now"] += 250  # 1リクエスト250秒かかったことにする
        items = json.loads(payload["contents"][0]["parts"][0]["text"].split("記事一覧（JSON）:\n")[1])
        return gemini_ok([{"id": it["id"], "summary": "要約", "category": "it", "tags": []} for it in items])

    summarize.http_post = fake_slow
    report = summarize.run(db, "key", ["m"], sleep=lambda s: None, clock=lambda: t["now"])
    check(report["summarized"] == 60 and report["skipped"] == 40 and report.get("rate_limited"),
          "10分を超えたら打ち切り、残り40件は次回へ")

    # --- 1回の上限は200件
    db = new_db(250)
    summarize.http_post = fake_blocked
    report = summarize.run(db, "key", ["m"], sleep=lambda s: None)
    check(report["pending"] == 200, "1回に要約するのは200件まで")

    print(f"\n{'すべて成功' if not check.failed else f'{check.failed} 件失敗'}")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
