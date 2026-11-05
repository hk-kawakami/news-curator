"""要約バッチ: まだ要約のない記事を Gemini（無料枠）に送り、1行要約・分野・タグを保存する。

使い方:
  python batch/summarize.py

環境変数:
  GEMINI_API_KEY   Google AI Studio で作った APIキー（請求先アカウントは紐づけない）
  GEMINI_MODELS    使うモデルをカンマ区切りで優先順に（省略時は下の DEFAULT_MODELS）
  ほか D1 への接続は db.py を参照
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import Database, connect  # noqa: E402

# 無料枠で使えるモデル。先頭から試し、存在しない・無料枠外なら次へ
DEFAULT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

BATCH_SIZE = 20              # 1リクエストで送る記事数
MAX_ARTICLES_PER_RUN = 200   # 1回の実行で要約する上限
TIME_BUDGET = 600            # 要約にかける時間の上限（秒）。超えたら残りは次回
SAVE_CHUNK = 10              # D1 へまとめて書き込む件数
TARGET_DAYS = 3              # この日数以内に取得した記事だけを対象にする
REQUEST_INTERVAL = 5.0       # リクエスト間隔（秒）。無料枠の1分あたり上限に収める
RATE_LIMIT_WAIT = 60         # 429 のときに待つ秒数
MAX_RATE_LIMIT_RETRIES = 2   # 429 が続いたらその回は終了（1日の上限の可能性）
OVERLOAD_WAITS = [15, 30]    # 503（混雑）のときに待つ秒数。使い切ったら次のモデルへ
MAX_OVERLOADED_BATCHES = 3   # 全モデルが混雑したまとまりがこの数続いたら、その回は終了
DESCRIPTION_MAX = 200

CATEGORIES = {
    "it": "IT・テクノロジー・AI・業務効率化",
    "keiba": "競馬",
    "stock": "株・マーケット・経済・企業決算",
    "society": "社会・政治・事件・地域（名古屋・愛知など）",
    "entame": "エンタメ（映画・ドラマ・音楽・ゲーム・アニメ・野球）",
    "trivia": "豆知識・雑学・歴史",
    "lifehack": "ライフハック・暮らし・お金の知恵・健康習慣",
    "travel": "旅行・観光・ホテル・交通（旅先の話題）",
    "gourmet": "グルメ・飲食店・食べ物・料理・新商品の食品",
    "science": "科学・研究・宇宙・自然",
    "sports": "スポーツ（野球以外）",
    "other": "上のどれにも当てはまらないもの",
}

PROMPT = """あなたはニュースの整理係です。次の記事一覧それぞれについて、日本語で以下を作ってください。

- summary: 記事の要点を60文字以内の1文で。見出しの言い換えではなく、何が起きたか・何が分かるかを書く。情報が見出ししかない場合は見出しから分かる範囲で書き、推測で事実を足さない。
- category: 次のうち最も近いものを1つ（キーのみ）
{categories}
- tags: 内容を表す短い名詞を3〜5個（固有名詞・技術名・分野名など。「ニュース」「話題」のような一般語は避ける）

記事一覧（JSON）:
{articles}
"""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "INTEGER"},
            "summary": {"type": "STRING"},
            "category": {"type": "STRING", "enum": list(CATEGORIES)},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["id", "summary", "category", "tags"],
    },
}


class RateLimited(Exception):
    pass


class ModelUnavailable(Exception):
    pass


class Overloaded(Exception):
    """Gemini 側の一時的な混雑（500 / 503）。"""


class Blocked(Exception):
    """安全フィルターで拒否された（どれかの記事が原因）。"""


def http_post(url: str, payload: dict, api_key: str) -> requests.Response:
    """Gemini API を呼ぶ（テストでは差し替える）。"""
    return requests.post(url, json=payload, timeout=90,
                         headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})


def build_payload(articles: list[dict]) -> dict:
    items = [{"id": a["id"], "title": a["title"],
              "description": (a.get("description") or "")[:DESCRIPTION_MAX],
              "source": a.get("publisher") or ""} for a in articles]
    prompt = PROMPT.format(
        categories="\n".join(f"  - {k}: {v}" for k, v in CATEGORIES.items()),
        articles=json.dumps(items, ensure_ascii=False))
    return {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "responseSchema": RESPONSE_SCHEMA, "temperature": 0.2},
    }


def call_gemini(model: str, api_key: str, articles: list[dict]) -> list[dict]:
    res = http_post(API_URL.format(model=model), build_payload(articles), api_key)
    if res.status_code == 429:
        raise RateLimited(res.text[:300])
    if res.status_code in (500, 503):
        raise Overloaded(f"{model}: HTTP {res.status_code}")
    if res.status_code in (400, 403, 404) and ("model" in res.text.lower() or res.status_code == 404):
        raise ModelUnavailable(f"{model}: HTTP {res.status_code} {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {res.status_code}: {res.text[:300]}")
    body = res.json()
    feedback = body.get("promptFeedback") or {}
    finish = ((body.get("candidates") or [{}])[0]).get("finishReason")
    if feedback.get("blockReason") or finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"):
        raise Blocked(feedback.get("blockReason") or finish)
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Gemini の応答を読めませんでした: {e}: {str(body)[:300]}")


def clean_result(item: dict, fallback_category: str) -> tuple[str, str, str] | None:
    summary = str(item.get("summary") or "").strip()
    if not summary:
        return None
    category = item.get("category") if item.get("category") in CATEGORIES else fallback_category
    tags, seen = [], set()
    for t in item.get("tags") or []:
        t = str(t).strip()
        if t and t not in seen and len(t) <= 30:
            seen.add(t)
            tags.append(t)
    return summary[:120], category, json.dumps(tags[:5], ensure_ascii=False)


def pending_articles(db: Database, limit: int) -> list[dict]:
    return db.query(
        "SELECT id, title, description, publisher, category FROM articles "
        f"WHERE summary IS NULL AND fetched_at >= datetime('now', '-{TARGET_DAYS} days') "
        "ORDER BY fetched_at DESC, id DESC LIMIT ?", [limit])


def save_results(db: Database, rows: list[tuple]) -> None:
    """(summary, category, tags, id) の行を、まとめて UPDATE する（D1 への通信回数を減らす）。"""
    for i in range(0, len(rows), SAVE_CHUNK):
        chunk = rows[i:i + SAVE_CHUNK]
        params: list = []
        cases = []
        for col_index, col in enumerate(("summary", "category", "tags")):
            whens = " ".join("WHEN ? THEN ?" for _ in chunk)
            cases.append(f"{col} = CASE id {whens} END")
            for r in chunk:
                params += [r[3], r[col_index]]
        ids = [r[3] for r in chunk]
        params += ids
        db.query(f"UPDATE articles SET {', '.join(cases)} WHERE id IN ({','.join('?' * len(ids))})", params)


def run(db: Database, api_key: str, models: list[str] | None = None,
        sleep=time.sleep, clock=time.monotonic) -> dict:
    models = list(models or DEFAULT_MODELS)
    articles = pending_articles(db, MAX_ARTICLES_PER_RUN)
    report = {"pending": len(articles), "summarized": 0, "skipped": 0, "blocked": 0,
              "requests": 0, "model": None, "stopped": None, "overloaded": 0}
    started = clock()
    queue = deque(articles[i:i + BATCH_SIZE] for i in range(0, len(articles), BATCH_SIZE))
    overloaded_in_row, first = 0, True

    def stop(reason: str) -> dict:
        report["skipped"] += sum(len(b) for b in queue)
        report["stopped"] = reason
        report["rate_limited"] = True  # エラー扱いにしない停止
        return report

    while queue:
        if clock() - started > TIME_BUDGET:
            return stop(f"{TIME_BUDGET // 60}分を超えたため終了（残りは次回）")
        batch = queue.popleft()
        if not first:
            sleep(REQUEST_INTERVAL)
        first = False
        results, retries, overload_waits, models_tried = None, 0, 0, 1
        batch_overloaded = False
        while results is None:
            if not models:
                queue.appendleft(batch)
                report["skipped"] += sum(len(b) for b in queue)
                report["stopped"] = "使えるモデルがありません（GEMINI_MODELS を確認してください）"
                return report
            try:
                report["requests"] += 1
                results = call_gemini(models[0], api_key, batch)
                report["model"] = models[0]
            except ModelUnavailable as e:
                print(f"モデルを切り替えます: {e}")
                models.pop(0)
            except Blocked as e:
                if len(batch) > 1:
                    # 原因の記事を絞り込むため、半分ずつに分けて送り直す
                    half = len(batch) // 2
                    queue.appendleft(batch[half:])
                    queue.appendleft(batch[:half])
                else:
                    # 1件でも拒否される記事は要約なし（空文字）にして、次回以降は送らない
                    print(f"安全フィルターで拒否されたため要約なしにします: id={batch[0]['id']} {e}")
                    db.query("UPDATE articles SET summary = '' WHERE id = ?", [batch[0]["id"]])
                    report["blocked"] += 1
                results = []
                batch = []
            except Overloaded as e:
                if overload_waits < len(OVERLOAD_WAITS):
                    sleep(OVERLOAD_WAITS[overload_waits])
                    overload_waits += 1
                elif models_tried < len(models):
                    # 混雑中のモデルを後ろに回し、次のモデルで試す
                    print(f"混雑のため次のモデルで試します: {e}")
                    models.append(models.pop(0))
                    overload_waits, models_tried = 0, models_tried + 1
                else:
                    print(f"全モデルが混雑しているため、このまとまりは次回に回します: {e}")
                    report["overloaded"] += 1
                    batch_overloaded = True
                    results = []
            except RateLimited as e:
                retries += 1
                if retries > MAX_RATE_LIMIT_RETRIES:
                    queue.appendleft(batch)
                    return stop(f"無料枠の上限に達したため終了（残りは次回）: {str(e)[:120]}")
                sleep(RATE_LIMIT_WAIT)
            except Exception as e:  # 応答の形式が崩れたなどは、このまとまりだけ飛ばす
                print(f"要約に失敗したため次のまとまりへ進みます: {e}")
                results = []

        by_id = {a["id"]: a for a in batch}
        rows = []
        for item in results:
            a = by_id.pop(item.get("id"), None) if isinstance(item, dict) else None
            cleaned = clean_result(item, a["category"]) if a else None
            if cleaned:
                rows.append((*cleaned, a["id"]))
        if rows:
            save_results(db, rows)
            report["summarized"] += len(rows)
        report["skipped"] += len(by_id)  # 応答に含まれなかった記事は次回に再挑戦

        overloaded_in_row = overloaded_in_row + 1 if batch_overloaded else 0
        if overloaded_in_row >= MAX_OVERLOADED_BATCHES:
            return stop("Gemini が混雑しているため終了（残りは次回）")
    return report


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("環境変数 GEMINI_API_KEY が未設定です")
    models = [m.strip() for m in os.environ.get("GEMINI_MODELS", "").split(",") if m.strip()] or None
    report = run(connect(), api_key, models)
    print(f"要約待ち {report['pending']} 件 / 要約 {report['summarized']} 件 / 次回へ {report['skipped']} 件"
          f" / 要約なし（拒否） {report['blocked']} 件"
          f" / リクエスト {report['requests']} 回 / モデル {report['model']}")
    if report["stopped"]:
        print(report["stopped"])
    print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
    # 要約待ちがあるのに1件も要約できなかったとき（無料枠の上限による停止は除く）はエラー終了（通知を飛ばすため）
    failed = report["pending"] and not report["summarized"] and not report.get("rate_limited")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
