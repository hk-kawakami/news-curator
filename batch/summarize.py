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
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import Database, connect  # noqa: E402

# 無料枠で使えるモデル。先頭から試し、存在しない・無料枠外なら次へ
DEFAULT_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

BATCH_SIZE = 20              # 1リクエストで送る記事数
MAX_ARTICLES_PER_RUN = 600   # 1回の実行で要約する上限
TARGET_DAYS = 3              # この日数以内に取得した記事だけを対象にする
REQUEST_INTERVAL = 5.0       # リクエスト間隔（秒）。無料枠の1分あたり上限に収める
RATE_LIMIT_WAIT = 60         # 429 のときに待つ秒数
MAX_RATE_LIMIT_RETRIES = 2   # 429 が続いたらその回は終了（1日の上限の可能性）
DESCRIPTION_MAX = 200

CATEGORIES = {
    "it": "IT・テクノロジー・AI・業務効率化",
    "keiba": "競馬",
    "stock": "株・マーケット・経済・企業決算",
    "society": "社会・政治・事件・地域（名古屋・愛知など）",
    "entame": "エンタメ（映画・ドラマ・音楽・ゲーム・アニメ・野球）",
    "trivia": "豆知識・雑学・歴史",
    "lifehack": "ライフハック・暮らし・お金の知恵・健康習慣",
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
    if res.status_code in (400, 403, 404) and ("model" in res.text.lower() or res.status_code == 404):
        raise ModelUnavailable(f"{model}: HTTP {res.status_code} {res.text[:200]}")
    if res.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {res.status_code}: {res.text[:300]}")
    body = res.json()
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


def run(db: Database, api_key: str, models: list[str] | None = None,
        sleep=time.sleep) -> dict:
    models = list(models or DEFAULT_MODELS)
    articles = pending_articles(db, MAX_ARTICLES_PER_RUN)
    report = {"pending": len(articles), "summarized": 0, "skipped": 0, "requests": 0,
              "model": None, "stopped": None}

    for i in range(0, len(articles), BATCH_SIZE):
        batch = articles[i:i + BATCH_SIZE]
        if i:
            sleep(REQUEST_INTERVAL)
        results, retries = None, 0
        while results is None:
            if not models:
                report["stopped"] = "使えるモデルがありません（GEMINI_MODELS を確認してください）"
                return report
            try:
                report["requests"] += 1
                results = call_gemini(models[0], api_key, batch)
                report["model"] = models[0]
            except ModelUnavailable as e:
                print(f"モデルを切り替えます: {e}")
                models.pop(0)
            except RateLimited as e:
                retries += 1
                if retries > MAX_RATE_LIMIT_RETRIES:
                    report["stopped"] = f"無料枠の上限に達したため終了（残りは次回）: {str(e)[:120]}"
                    report["rate_limited"] = True
                    return report
                sleep(RATE_LIMIT_WAIT)
            except Exception as e:  # 応答の形式が崩れたなどは、このまとまりだけ飛ばす
                print(f"要約に失敗したため次のまとまりへ進みます: {e}")
                results = []

        by_id = {a["id"]: a for a in batch}
        for item in results:
            a = by_id.pop(item.get("id"), None) if isinstance(item, dict) else None
            cleaned = clean_result(item, a["category"]) if a else None
            if not cleaned:
                continue
            db.query("UPDATE articles SET summary = ?, category = ?, tags = ? WHERE id = ?",
                     [*cleaned, a["id"]])
            report["summarized"] += 1
        report["skipped"] += len(by_id)  # 応答に含まれなかった記事は次回に再挑戦
    return report


def main() -> int:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("環境変数 GEMINI_API_KEY が未設定です")
    models = [m.strip() for m in os.environ.get("GEMINI_MODELS", "").split(",") if m.strip()] or None
    report = run(connect(), api_key, models)
    print(f"要約待ち {report['pending']} 件 / 要約 {report['summarized']} 件 / 次回へ {report['skipped']} 件"
          f" / リクエスト {report['requests']} 回 / モデル {report['model']}")
    if report["stopped"]:
        print(report["stopped"])
    print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
    # 要約待ちがあるのに1件も要約できなかったとき（無料枠の上限による停止は除く）はエラー終了（通知を飛ばすため）
    failed = report["pending"] and not report["summarized"] and not report.get("rate_limited")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
