"""D1 への接続。

本番は Cloudflare D1 の HTTP API を使い、ローカル確認では SQLite ファイルを使う。
どちらも D1 と同じ SQLite の SQL がそのまま通る。

環境変数:
  DB_BACKEND        d1（既定） / sqlite
  CF_ACCOUNT_ID     Cloudflare のアカウントID（d1）
  CF_D1_DATABASE_ID D1 のデータベースID（d1）
  CF_API_TOKEN      D1 の編集権限を持つ APIトークン（d1）
  SQLITE_PATH       SQLite ファイルのパス（sqlite）
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Iterable, Sequence

import requests

# D1 は 1 クエリあたりのバインド変数が 100 個まで
MAX_PARAMS = 100


class D1Error(RuntimeError):
    pass


class Database:
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        raise NotImplementedError

    def insert_many(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]],
                    on_conflict: str = "OR IGNORE") -> int:
        """複数行をまとめて INSERT する（バインド変数の上限に合わせて分割）。"""
        rows = list(rows)
        per_stmt = max(1, MAX_PARAMS // len(columns))
        placeholders = "(" + ",".join("?" * len(columns)) + ")"
        total = 0
        for i in range(0, len(rows), per_stmt):
            chunk = rows[i:i + per_stmt]
            sql = (f"INSERT {on_conflict} INTO {table} ({','.join(columns)}) VALUES "
                   + ",".join([placeholders] * len(chunk)))
            params = [v for row in chunk for v in row]
            self.query(sql, params)
            total += len(chunk)
        return total

    def select_in(self, sql_template: str, values: Sequence[Any]) -> list[dict]:
        """`... IN ({in})` を含む SELECT を、上限に合わせて分割して実行する。"""
        out: list[dict] = []
        values = list(values)
        for i in range(0, len(values), MAX_PARAMS - 10):
            chunk = values[i:i + MAX_PARAMS - 10]
            out.extend(self.query(sql_template.format(**{"in": ",".join("?" * len(chunk))}), chunk))
        return out


class D1Database(Database):
    def __init__(self, account_id: str, database_id: str, api_token: str):
        self.url = (f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                    f"/d1/database/{database_id}/query")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_token}",
                                     "Content-Type": "application/json"})

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        last_err = None
        for attempt in range(3):
            try:
                res = self.session.post(self.url, json={"sql": sql, "params": list(params)}, timeout=30)
                body = res.json()
                if res.status_code == 200 and body.get("success"):
                    results = body.get("result") or []
                    return results[0].get("results", []) if results else []
                last_err = D1Error(f"HTTP {res.status_code}: {body.get('errors')}")
                if res.status_code < 500 and res.status_code != 429:
                    break  # SQL の誤りなどは再試行しない
            except (requests.RequestException, ValueError) as e:
                last_err = e
            time.sleep(2 ** attempt)
        raise D1Error(f"D1 query failed: {last_err}\nSQL: {sql[:200]}")


class SqliteDatabase(Database):
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        cur = self.conn.execute(sql, list(params))
        rows = [dict(r) for r in cur.fetchall()]
        self.conn.commit()
        return rows


def connect() -> Database:
    backend = os.environ.get("DB_BACKEND", "d1")
    if backend == "sqlite":
        return SqliteDatabase(os.environ.get("SQLITE_PATH", "local.db"))
    missing = [k for k in ("CF_ACCOUNT_ID", "CF_D1_DATABASE_ID", "CF_API_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"環境変数が未設定です: {', '.join(missing)}")
    return D1Database(os.environ["CF_ACCOUNT_ID"], os.environ["CF_D1_DATABASE_ID"],
                      os.environ["CF_API_TOKEN"])
