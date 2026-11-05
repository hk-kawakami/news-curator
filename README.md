# news-curator

自分用のニュース管理サイト。毎日2回（6時・18時）RSSを集めて Cloudflare D1 に保存し、評価から好みを学習して並べ替える。

このリポジトリでできていること

- ステップ2：収集バッチ
  - `config/sources.yaml` の配信元（RSS 15件＋Googleニュース検索 33キーワード）からの取得
  - 画像URLの取得（RSSの画像 → 記事ページのOGP画像。Googleニュース経由の記事は取得しない）
  - 重複除外、3日より古い記事の除外、評価もお気に入りもない90日超の記事の削除
- ステップ3：要約バッチ（Gemini 無料枠）
  - 3日以内に取得した未要約の記事を20件ずつ送り、1行要約・分野・タグを保存
  - 無料枠の上限（HTTP 429）に達したらその回は終了し、残りは次回に処理
  - 使えないモデル名は飛ばして次の候補で実行

- ステップ4：推薦バッチと画面
  - 収集・要約の後に、好みの重みで表示する記事（通常24件＋発見枠6件）を選んで `daily_picks` に保存
  - 画面と API は Cloudflare Workers（`web/`）。GitHub と連携し、push すると自動で反映

## 構成

```
schema.sql                     D1 のテーブル定義（7テーブル＋初期の重み）
config/sources.yaml            配信元とGoogleニュース検索キーワード
batch/collect.py               収集バッチ本体
batch/summarize.py             要約バッチ本体（Gemini）
batch/recommend.py             推薦バッチ本体（表示する記事を選ぶ）
batch/db.py                    D1（HTTP API）／ローカルSQLite への接続
web/wrangler.toml              Workers の設定（D1 の接続先など）
web/src/                       API（評価・あとで読む・非表示などの保存）
web/public/                    画面（HTML / CSS / JavaScript）
tests/test_collect.py          サンプルRSSでの動作確認（ネットワーク不要）
tests/test_summarize.py        模擬のGemini応答での動作確認（ネットワーク不要）
tests/test_recommend.py        推薦バッチの動作確認（ネットワーク不要）
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

### 6. Gemini の APIキーを登録する（要約バッチ用）

[Google AI Studio](https://aistudio.google.com/apikey) で APIキーを作り、Secrets に `GEMINI_API_KEY` として登録します。請求先アカウント（Set up billing）は紐づけないでください。紐づけなければ無料枠のまま、料金はかかりません。

要約の結果は、Actions のログの「Geminiで要約・分類」に次のように出ます。

```
要約待ち ○○ 件 / 要約 ○○ 件 / 次回へ ○ 件 / リクエスト ○○ 回 / モデル gemini-…
```

「使えるモデルがありません」と出たら、AI Studio で無料枠のモデル名を確認し、「Settings → Secrets and variables → Actions → Variables」に `GEMINI_MODELS`（カンマ区切り、例 `gemini-3.5-flash-lite,gemini-3.8-flash`）を登録してください。

### 7. 画面を公開する（Cloudflare Workers と GitHub の連携）

1. Cloudflare ダッシュボードで「Workers & Pages」→「作成」（Create）→「リポジトリをインポート」（Import a repository）を選ぶ
2. GitHub と連携し、`news-curator` リポジトリを選ぶ
3. プロジェクト名は `news-curator`（`web/wrangler.toml` の name と同じにする）
4. 詳細設定で「ルートディレクトリ」（Root directory / Path）を `web` にする。ビルドコマンドは空欄、デプロイコマンドは `npx wrangler deploy` のまま
5. 「デプロイ」を押す。完了すると `https://news-curator.<サブドメイン>.workers.dev` で開ける

以降は push するたびに自動で反映されます。一覧は推薦バッチの実行後（毎日6時・18時）に更新されます。

## 運用メモ

- **配信元の追加・停止**：`config/sources.yaml` を編集して push すると、次回の収集から反映されます。ファイルから消した配信元は停止扱いになり、記事は残ります。
- **定期実行が止まる場合**：公開リポジトリでは、60日間リポジトリに更新がないと定期実行が自動で無効になります。止まったら Actions 画面から再度有効にしてください。
- **ローカル確認**：`pip install -r requirements.txt` のあと `python tests/test_collect.py`。
- **ローカルの SQLite で実際に収集してみる**：

  ```bash
  sqlite3 local.db < schema.sql
  DB_BACKEND=sqlite SQLITE_PATH=local.db python batch/collect.py
  ```
