// ニュース管理サイトの API
// /api/* へのリクエストをここで受ける。D1 は env.DB。

const RATING_DELTA = { 1: -2, 2: -1, 3: 0, 4: 1, 5: 2 };
const TAG_FACTOR = 0.5;
const SOURCE_FACTOR = 0.5;
const FAVORITE_DELTA = 2;
const READ_LATER_DELTA = 1;
const OPEN_DELTA = 0.5;
const INITIAL_CATEGORIES = ["it", "keiba", "stock", "society", "entame", "trivia", "lifehack"];

const ARTICLE_COLUMNS = `
  a.id, a.url, a.title, a.publisher, a.summary, a.description, a.image_url, a.category, a.tags,
  a.published_at, a.fetched_at,
  r.rating, COALESCE(s.read_later, 0) AS read_later, COALESCE(s.favorite, 0) AS favorite,
  COALESCE(s.is_read, 0) AS is_read`;
const ARTICLE_JOINS = `
  LEFT JOIN ratings r ON r.article_id = a.id
  LEFT JOIN article_states s ON s.article_id = a.id`;

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function toItem(row) {
  let tags = [];
  try { tags = row.tags ? JSON.parse(row.tags) : []; } catch { tags = []; }
  return { ...row, tags, read_later: !!row.read_later, favorite: !!row.favorite, is_read: !!row.is_read };
}

async function getArticle(db, id) {
  const a = await db.prepare("SELECT id, category, tags, publisher FROM articles WHERE id = ?").bind(id).first();
  if (!a) throw new HttpError(404, "記事が見つかりません");
  return a;
}

// 記事の分野・タグ・配信元の重みを delta だけ動かす文を作る
function weightStatements(db, article, delta, countInc = 0) {
  if (!delta && !countInc) return [];
  let tags = [];
  try { tags = article.tags ? JSON.parse(article.tags) : []; } catch { tags = []; }
  const upsert = db.prepare(
    `INSERT INTO weights (feature_type, feature, weight, rated_count, updated_at)
     VALUES (?, ?, ?, ?, datetime('now'))
     ON CONFLICT(feature_type, feature) DO UPDATE SET
       weight = weight + excluded.weight,
       rated_count = rated_count + excluded.rated_count,
       updated_at = excluded.updated_at`);
  const stmts = [upsert.bind("category", article.category, delta, countInc)];
  for (const t of tags) stmts.push(upsert.bind("tag", t, delta * TAG_FACTOR, countInc));
  if (article.publisher) stmts.push(upsert.bind("source", article.publisher, delta * SOURCE_FACTOR, countInc));
  return stmts;
}

async function ensureState(db, id) {
  await db.prepare("INSERT OR IGNORE INTO article_states (article_id) VALUES (?)").bind(id).run();
  return db.prepare("SELECT * FROM article_states WHERE article_id = ?").bind(id).first();
}

// ---------------------------------------------------------------- 各API

const PAGE_SIZE = 30;

async function feed(db, url) {
  const editions = (await db.prepare(
    `SELECT date, edition, COUNT(*) AS n FROM daily_picks
     GROUP BY date, edition ORDER BY date DESC, edition DESC LIMIT 14`).all()).results;
  if (!editions.length) return { date: null, edition: null, editions: [], items: [], next: null };
  const date = url.searchParams.get("date") || editions[0].date;
  const edition = url.searchParams.get("edition") || editions.find((e) => e.date === date)?.edition || editions[0].edition;
  const after = Number(url.searchParams.get("after") || 0);  // 「もっと見る」: この順位より後を返す
  const keywords = (await db.prepare("SELECT value FROM mutes WHERE type = 'keyword'").all()).results
    .map((r) => r.value.toLowerCase());
  const keep = (it) => {
    const text = [it.title, it.summary || "", ...it.tags].join(" ").toLowerCase();
    return !keywords.some((k) => text.includes(k));
  };
  // 評価済み・非表示を除いたあと PAGE_SIZE 件になるまで順位順に読む
  const items = [];
  let cursor = after, done = false;
  while (items.length < PAGE_SIZE && !done) {
    const rows = (await db.prepare(
      `SELECT ${ARTICLE_COLUMNS}, p.slot, p.rank, p.score FROM daily_picks p
       JOIN articles a ON a.id = p.article_id ${ARTICLE_JOINS}
       WHERE p.date = ? AND p.edition = ? AND p.rank > ?
         AND r.rating IS NULL
         AND COALESCE(a.publisher, '') NOT IN (SELECT value FROM mutes WHERE type = 'source')
       ORDER BY p.rank LIMIT ?`).bind(date, edition, cursor, PAGE_SIZE).all()).results;
    if (rows.length < PAGE_SIZE) done = true;
    if (rows.length) cursor = rows[rows.length - 1].rank;
    for (const it of rows.map(toItem).filter(keep)) if (items.length < PAGE_SIZE) items.push(it);
  }
  const lastRank = items.length ? items[items.length - 1].rank : cursor;
  const rest = await db.prepare(
    `SELECT COUNT(*) AS n FROM daily_picks p LEFT JOIN ratings r ON r.article_id = p.article_id
     WHERE p.date = ? AND p.edition = ? AND p.rank > ? AND r.rating IS NULL`).bind(date, edition, lastRank).first();
  return { date, edition, editions, items, next: rest.n > 0 ? lastRank : null };
}

async function list(db, url) {
  const type = url.searchParams.get("type");
  if (type === "rated") {
    // 評価済みの記事（評価した日時の新しい順）
    const rows = (await db.prepare(
      `SELECT ${ARTICLE_COLUMNS}, r.rated_at FROM articles a ${ARTICLE_JOINS}
       WHERE r.rating IS NOT NULL ORDER BY r.rated_at DESC LIMIT 300`).all()).results;
    return { items: rows.map(toItem) };
  }
  const column = { later: "read_later", favorite: "favorite" }[type];
  if (!column) throw new HttpError(400, "type は later・favorite・rated のいずれかです");
  const rows = (await db.prepare(
    `SELECT ${ARTICLE_COLUMNS} FROM articles a ${ARTICLE_JOINS}
     WHERE s.${column} = 1 ORDER BY a.fetched_at DESC LIMIT 200`).all()).results;
  return { items: rows.map(toItem) };
}

async function rate(db, body) {
  const id = Number(body.id), rating = Number(body.rating);
  if (rating !== 0 && !RATING_DELTA.hasOwnProperty(rating)) throw new HttpError(400, "rating は1〜5（取り消しは0）です");
  const article = await getArticle(db, id);
  const old = await db.prepare("SELECT rating FROM ratings WHERE article_id = ?").bind(id).first();
  if (rating === 0) {
    // 評価の取り消し: 評価を消し、重みと評価回数を元に戻す
    if (!old) return { id, rating: null };
    await db.batch([
      db.prepare("DELETE FROM ratings WHERE article_id = ?").bind(id),
      ...weightStatements(db, article, -RATING_DELTA[old.rating], -1),
    ]);
    return { id, rating: null };
  }
  const delta = RATING_DELTA[rating] - (old ? RATING_DELTA[old.rating] : 0);
  await db.batch([
    db.prepare(`INSERT INTO ratings (article_id, rating, rated_at) VALUES (?, ?, datetime('now'))
                ON CONFLICT(article_id) DO UPDATE SET rating = excluded.rating, rated_at = excluded.rated_at`)
      .bind(id, rating),
    ...weightStatements(db, article, delta, old ? 0 : 1),
  ]);
  return { id, rating };
}

async function setState(db, body) {
  const id = Number(body.id), field = body.field, value = !!body.value;
  const deltas = { read_later: READ_LATER_DELTA, favorite: FAVORITE_DELTA };
  if (!(field in deltas)) throw new HttpError(400, "field は read_later か favorite です");
  const article = await getArticle(db, id);
  const state = await ensureState(db, id);
  if (!!state[field] === value) return { id, field, value };
  await db.batch([
    db.prepare(`UPDATE article_states SET ${field} = ? WHERE article_id = ?`).bind(value ? 1 : 0, id),
    ...weightStatements(db, article, value ? deltas[field] : -deltas[field]),
  ]);
  return { id, field, value };
}

async function open(db, body) {
  const id = Number(body.id);
  const article = await getArticle(db, id);
  const state = await ensureState(db, id);
  const stmts = [db.prepare(`UPDATE article_states SET is_read = 1, read_later = 0,
                             opened_at = COALESCE(opened_at, datetime('now')) WHERE article_id = ?`).bind(id)];
  if (!state.opened_at) stmts.push(...weightStatements(db, article, OPEN_DELTA));
  await db.batch(stmts);
  return { id, opened: true };
}

async function mute(db, body, on) {
  const type = body.type, value = String(body.value || "").trim();
  if (!["source", "keyword"].includes(type) || !value) throw new HttpError(400, "type と value を指定してください");
  if (on) await db.prepare("INSERT OR IGNORE INTO mutes (type, value) VALUES (?, ?)").bind(type, value).run();
  else await db.prepare("DELETE FROM mutes WHERE type = ? AND value = ?").bind(type, value).run();
  return { type, value, muted: on };
}

async function settings(db) {
  const top = (await db.prepare(
    `SELECT feature_type, feature, weight, rated_count FROM weights
     WHERE feature_type IN ('category', 'tag', 'source') AND weight > 0.05
     ORDER BY weight DESC LIMIT 12`).all()).results;
  const bottom = (await db.prepare(
    `SELECT feature_type, feature, weight, rated_count FROM weights
     WHERE feature_type IN ('category', 'tag', 'source') AND weight < -0.05
     ORDER BY weight ASC LIMIT 12`).all()).results;
  const sources = (await db.prepare(
    "SELECT name, category, enabled FROM sources ORDER BY enabled DESC, category, name").all()).results;
  const mutes = (await db.prepare("SELECT type, value, created_at FROM mutes ORDER BY created_at DESC").all()).results;
  const stats = await db.prepare(
    `SELECT (SELECT COUNT(*) FROM ratings) AS ratings, (SELECT COUNT(*) FROM articles) AS articles,
            (SELECT MAX(fetched_at) FROM articles) AS last_fetched`).first();
  return { top, bottom, sources, mutes, stats };
}

async function resetWeights(db) {
  const seed = db.prepare("INSERT INTO weights (feature_type, feature, weight) VALUES ('category', ?, 1)");
  await db.batch([
    db.prepare("DELETE FROM weights WHERE feature_type != 'meta'"),
    ...INITIAL_CATEGORIES.map((c) => seed.bind(c)),
  ]);
  return { reset: true };
}

// ---------------------------------------------------------------- 振り分け

export async function handleApi(request, env) {
  const url = new URL(request.url);
  const path = url.pathname.replace(/^\/api/, "").replace(/\/$/, "") || "/";
  const method = request.method;
  try {
    const db = env.DB;
    if (!db) throw new HttpError(500, "D1 のバインディング（DB）が設定されていません");
    const body = method === "POST" ? await request.json().catch(() => ({})) : {};
    const routes = {
      "GET /feed": () => feed(db, url),
      "GET /list": () => list(db, url),
      "GET /settings": () => settings(db),
      "POST /rate": () => rate(db, body),
      "POST /state": () => setState(db, body),
      "POST /open": () => open(db, body),
      "POST /mute": () => mute(db, body, true),
      "POST /unmute": () => mute(db, body, false),
      "POST /reset-weights": () => resetWeights(db),
    };
    const handler = routes[`${method} ${path}`];
    if (!handler) throw new HttpError(404, "Not found");
    return json(await handler());
  } catch (e) {
    return json({ error: e.message || String(e) }, e.status || 500);
  }
}
