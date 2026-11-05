# news-curator

自分用のニュース管理サイト。毎日2回（6時・18時）RSSを集めて Cloudflare D1 に保存し、評価から好みを学習して並べ替える。

このリポジトリでできていること（ステップ2：収集バッチ）

- `config/sources.yaml` の配信元（RSS 15件＋Googleニュース検索 33キーワード）からの取得
- 画像URLの取得（RSSの画像 → 記事ページのOGP画像。Googleニュース経由の記事は取得しない）
- 重複除外、3日より古い記事の除外、評価もお気に入りもない90日超の記事の削除

AI要約・推薦・画面は次のステップで追加します。

## 構成

```
schema.sql                     D1 のテーブル定義（7テーブル＋初期の重み）
config/sources.yaml            配信元とGoogleニュース検索キーワード
batch/collect.py               収集バッチ本体
batch/db.py                    D1（HTTP API）／ローカルSQLite への接続
tests/test_collect.py          サンプルRSSでの動作確認（ネットワーク不要）
.github/workflows/collect.yml  毎日 6:00・18:00（日本時間）に収集
```

## セットアップ手順

### 1. GitHub にリポジトリを作る

公開リポジトリ（例：`news-curator`）を作り、このフォルダの中身を push します。

### 2. D1 データベースを作る

Node.js が入っていれば、手元のターミナルで次のコマンドを実行します。

```bash
npx wrangler login
npx wrangler d1 create news-curator
```

表示される `database_id` を控えておきます。続けてテーブルを作ります。

```bash
npx wrangler d1 execute news-curator --remote --file=schema.sql
```

### 3. Cloudflare の APIトークンを作る

Cloudflare ダッシュボードの「My Profile → API Tokens → Create Token → Create Custom Token」で作成します。

- Permissions：`Account` / `D1` / `Edit`
- Account Resources：自分のアカウント

アカウントIDは、ダッシュボードの「Workers & Pages」画面の右側に表示されています。

### 4. GitHub の Secrets に登録する

リポジトリの「Settings → Secrets and variables → Actions → New repository secret」で、次の3つを登録します。

| 名前 | 値 |
| --- | --- |
| `CF_ACCOUNT_ID` | Cloudflare のアカウントID |
| `CF_D1_DATABASE_ID` | 手順2の `database_id` |
| `CF_API_TOKEN` | 手順3の APIトークン |

公開リポジトリなので、トークンやIDはファイルに書かず、必ず Secrets に入れてください。

### 5. 手動で1回動かす

「Actions → ニュース収集 → Run workflow」で実行します。ログの最後に次のような結果が出ます。

```
配信元 48 件 / 取得 ○○ 件 / 新着 ○○ 件（画像あり ○○ 件） / 削除 0 件
取得に失敗した配信元 ○ 件:
  - （名前）(URL): （エラー内容）
```

取得に失敗した配信元は、`config/sources.yaml` でURLを直すか `enabled: false` にして push してください。

保存された件数は、次のコマンドで確認できます。

```bash
npx wrangler d1 execute news-curator --remote --command "SELECT category, COUNT(*) AS n, SUM(image_url IS NOT NULL) AS with_image FROM articles GROUP BY category"
```

## 運用メモ

- **配信元の追加・停止**：`config/sources.yaml` を編集して push すると、次回の収集から反映されます。ファイルから消した配信元は停止扱いになり、記事は残ります。
- **定期実行が止まる場合**：公開リポジトリでは、60日間リポジトリに更新がないと定期実行が自動で無効になります。止まったら Actions 画面から再度有効にしてください。
- **ローカル確認**：`pip install -r requirements.txt` のあと `python tests/test_collect.py`。
- **ローカルの SQLite で実際に収集してみる**：

  ```bash
  sqlite3 local.db < schema.sql
  DB_BACKEND=sqlite SQLITE_PATH=local.db python batch/collect.py
  ```
