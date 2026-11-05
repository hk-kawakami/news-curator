"use strict";

const CATEGORY_LABELS = {
  it: "IT", keiba: "競馬", stock: "株", society: "社会", entame: "エンタメ", trivia: "豆知識",
  lifehack: "ライフハック", science: "科学", sports: "スポーツ", other: "その他",
};
const CATEGORY_ORDER = Object.keys(CATEGORY_LABELS);
const WEEKDAYS = ["日", "月", "火", "水", "木", "金", "土"];

const state = { tab: "today", feed: null, list: [], filter: "all", sheetItem: null, edition: null };
const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- 共通

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const res = await fetch(`/api${path}`, body === undefined ? {} : {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2400);
}

// D1 の時刻（UTC "YYYY-MM-DD HH:MM:SS"）を日本時間の短い表記に
function timeLabel(s) {
  if (!s) return "";
  const d = new Date(s.replace(" ", "T") + "Z");
  if (isNaN(d)) return "";
  const jst = new Date(d.getTime() + 9 * 3600e3);
  const now = new Date(Date.now() + 9 * 3600e3);
  const hm = `${jst.getUTCHours()}:${String(jst.getUTCMinutes()).padStart(2, "0")}`;
  const sameDay = jst.toISOString().slice(0, 10) === now.toISOString().slice(0, 10);
  return sameDay ? hm : `${jst.getUTCMonth() + 1}/${jst.getUTCDate()} ${hm}`;
}

function dateLabel(date) {
  const [y, m, d] = date.split("-").map(Number);
  const wd = WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
  return `${m}月${d}日（${wd}）`;
}
const editionLabel = (e) => (e === "morning" ? "朝" : "夕");

// ---------------------------------------------------------------- 記事カード

const ICON = {
  star: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5l2.6 5.3 5.8.8-4.2 4.1 1 5.8L12 16.8l-5.2 2.7 1-5.8-4.2-4.1 5.8-.8z"/></svg>',
  later: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
  fav: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3.5h12v17l-6-4-6 4z"/></svg>',
  more: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg>',
};

function placeholder(item) {
  return `<div class="thumb ph c-${esc(item.category)}" aria-hidden="true">${esc(CATEGORY_LABELS[item.category] || "")}</div>`;
}

function cardHtml(item) {
  const cat = CATEGORY_LABELS[item.category] || item.category;
  const tag = item.slot === "discovery"
    ? `<span class="tag disc">発見 · ${esc(cat)}</span>` : `<span class="tag">${esc(cat)}</span>`;
  const text = item.summary || item.description || "";
  const thumb = item.image_url
    ? `<img class="thumb" src="${esc(item.image_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-cat="${esc(item.category)}">`
    : placeholder(item);
  const stars = [1, 2, 3, 4, 5].map((n) =>
    `<button type="button" class="icon-btn star${item.rating >= n ? " on" : ""}" data-action="rate" data-id="${item.id}" data-n="${n}"
       aria-label="${n}点で評価" aria-pressed="${item.rating === n}">${ICON.star}</button>`).join("");
  return `
  <article class="card${item.is_read ? " read" : ""}" data-id="${item.id}">
    <div class="card-body">
      <div class="card-text">
        <div class="meta">${tag}<span>${esc(item.publisher || "")}${item.publisher ? " · " : ""}${esc(timeLabel(item.published_at || item.fetched_at))}</span></div>
        <a class="title" href="${esc(item.url)}" target="_blank" rel="noopener" data-action="open" data-id="${item.id}">${esc(item.title)}</a>
        ${text ? `<p class="summary">${esc(text)}</p>` : ""}
      </div>
      ${thumb}
    </div>
    <div class="actions">
      <div class="stars" role="group" aria-label="評価">${stars}</div>
      <div class="right-actions">
        <button type="button" class="icon-btn${item.read_later ? " on" : ""}" data-action="later" data-id="${item.id}"
          aria-label="あとで読む" aria-pressed="${item.read_later}">${ICON.later}</button>
        <button type="button" class="icon-btn${item.favorite ? " on" : ""}" data-action="fav" data-id="${item.id}"
          aria-label="お気に入り" aria-pressed="${item.favorite}">${ICON.fav}</button>
        <button type="button" class="icon-btn" data-action="menu" data-id="${item.id}" aria-label="その他の操作">${ICON.more}</button>
      </div>
    </div>
  </article>`;
}

// ---------------------------------------------------------------- 画面ごとの描画

function currentItems() {
  return state.tab === "today" ? (state.feed?.items || []) : state.list;
}
const findItem = (id) => currentItems().find((it) => it.id === id);

function renderHeader() {
  const top = $("top");
  if (state.tab === "today") {
    const f = state.feed;
    const options = (f?.editions || []).map((e) =>
      `<option value="${e.date}|${e.edition}"${e.date === f.date && e.edition === f.edition ? " selected" : ""}>
        ${dateLabel(e.date)} ${editionLabel(e.edition)}の更新（${e.n}件）</option>`).join("");
    top.innerHTML = `
      <p class="sub">${f?.date ? `<label><span class="visually-hidden">表示する回</span>
        <select class="edition-select" id="edition-select" aria-label="表示する回">${options}</select></label>` : "まだ一覧がありません"}</p>
      <h1>今日のニュース</h1>`;
  } else {
    const titles = { later: "あとで読む", favorite: "お気に入り", settings: "設定" };
    top.innerHTML = `<h1>${titles[state.tab]}</h1>`;
  }
}

function renderChips() {
  const chips = $("chips");
  if (state.tab !== "today" || !state.feed?.items?.length) { chips.hidden = true; return; }
  const present = new Set(state.feed.items.map((it) => it.category));
  const cats = ["all", ...CATEGORY_ORDER.filter((c) => present.has(c))];
  if (!cats.includes(state.filter)) state.filter = "all";
  chips.hidden = false;
  chips.innerHTML = cats.map((c) =>
    `<button type="button" class="chip" data-filter="${c}" aria-pressed="${state.filter === c}">${c === "all" ? "すべて" : CATEGORY_LABELS[c]}</button>`).join("");
}

function renderList() {
  const items = currentItems().filter((it) => state.tab !== "today" || state.filter === "all" || it.category === state.filter);
  const empty = {
    today: "まだ表示できるニュースがありません。<br>朝6時・夕方18時の収集後に表示されます。",
    later: "「あとで読む」に入れた記事はまだありません。",
    favorite: "お気に入りに入れた記事はまだありません。",
  };
  $("main").innerHTML = items.length ? items.map(cardHtml).join("") : `<p class="empty">${empty[state.tab]}</p>`;
}

function weightPills(rows, low) {
  if (!rows.length) return `<p class="muted-text">まだありません。評価が増えると表示されます。</p>`;
  return `<div class="pill-row">${rows.map((r) => {
    const name = r.feature_type === "category" ? CATEGORY_LABELS[r.feature] || r.feature
      : r.feature_type === "source" ? `配信元: ${r.feature}` : r.feature;
    return `<span class="pill${low ? " low" : ""}">${esc(name)}<small>${r.weight > 0 ? "+" : ""}${r.weight.toFixed(1)}</small></span>`;
  }).join("")}</div>`;
}

function renderSettings(s) {
  const mutes = s.mutes.length ? s.mutes.map((m) => `
    <div class="row"><span><span class="name">${esc(m.value)}</span><br><span class="note">${m.type === "source" ? "配信元" : "キーワード"}</span></span>
      <button type="button" class="small-btn" data-action="unmute" data-type="${esc(m.type)}" data-value="${esc(m.value)}">解除</button></div>`).join("")
    : `<p class="muted-text" style="padding:12px 0">非表示にしているものはありません。</p>`;
  const sources = s.sources.map((src) => `
    <div class="row"><span><span class="name">${esc(src.name)}</span><br><span class="note">${esc(CATEGORY_LABELS[src.category] || src.category)}${src.enabled ? "" : " · 停止中"}</span></span></div>`).join("");
  $("main").innerHTML = `
    <section class="section"><h2>学習した好み</h2>
      <div class="panel pad">
        <p class="label">よく評価している</p>${weightPills(s.top, false)}
        <p class="label low">あまり評価していない</p>${weightPills(s.bottom, true)}
        <p class="muted-text">評価 ${s.stats.ratings} 件 / 保存している記事 ${s.stats.articles} 件 / 最終取得 ${esc(timeLabel(s.stats.last_fetched))}</p>
        <button type="button" class="wide-btn" data-action="reset">学習をリセット</button>
      </div></section>
    <section class="section"><h2>非表示にしているもの</h2><div class="panel">${mutes}</div></section>
    <section class="section"><h2>配信元（${s.sources.length}件）</h2>
      <div class="panel">${sources}</div>
      <p class="muted-text">配信元の追加・停止は、GitHub の config/sources.yaml を編集して行います。</p></section>`;
}

async function render() {
  document.querySelectorAll(".tabs a").forEach((a) =>
    a.setAttribute("aria-current", a.dataset.tab === state.tab ? "page" : "false"));
  $("main").innerHTML = `<p class="loading">読み込み中…</p>`;
  try {
    if (state.tab === "today") {
      const q = state.edition ? `?date=${state.edition.date}&edition=${state.edition.edition}` : "";
      state.feed = await api(`/feed${q}`);
    } else if (state.tab === "later" || state.tab === "favorite") {
      state.list = (await api(`/list?type=${state.tab}`)).items;
    }
    renderHeader();
    renderChips();
    if (state.tab === "settings") renderSettings(await api("/settings"));
    else renderList();
  } catch (e) {
    $("main").innerHTML = `<p class="empty">読み込みに失敗しました。<br>${esc(e.message)}</p>`;
  }
}

// ---------------------------------------------------------------- 操作

async function act(fn, rollback) {
  try { await fn(); } catch (e) { rollback?.(); renderList(); toast(`保存できませんでした: ${e.message}`); }
}

function openSheet(item) {
  state.sheetItem = item;
  $("sheet-meta").textContent = `${item.publisher || ""} ${timeLabel(item.published_at || item.fetched_at)}`.trim();
  $("sheet-title").textContent = item.title;
  $("sheet-open").href = item.url;
  $("sheet-open").dataset.id = item.id;
  const src = $("sheet-mute-source");
  src.hidden = !item.publisher;
  src.querySelector("span").textContent = `この配信元（${item.publisher || ""}）を非表示`;
  $("sheet-tags-block").hidden = !item.tags.length;
  $("sheet-tags").innerHTML = item.tags.map((t) =>
    `<button type="button" class="tag-btn" data-tag="${esc(t)}" aria-pressed="false">${esc(t)}</button>`).join("");
  $("sheet").hidden = false;
  $("sheet-close").focus();
}
function closeSheet() { $("sheet").hidden = true; state.sheetItem = null; }

function removeWhere(pred) {
  if (state.feed) state.feed.items = state.feed.items.filter((it) => !pred(it));
  state.list = state.list.filter((it) => !pred(it));
}

$("main").addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-action]");
  if (!el) return;
  const id = Number(el.dataset.id);
  const item = findItem(id);
  const action = el.dataset.action;

  if (action === "open" && item) {
    // リンクはそのまま開き、裏で既読を記録する
    item.is_read = true;
    el.closest(".card")?.classList.add("read");
    api("/open", { id }).catch(() => {});
    return;
  }
  if (action === "rate" && item) {
    const n = Number(el.dataset.n), old = item.rating;
    item.rating = n;
    renderList();
    act(() => api("/rate", { id, rating: n }), () => { item.rating = old; });
  } else if ((action === "later" || action === "fav") && item) {
    const field = action === "later" ? "read_later" : "favorite";
    item[field] = !item[field];
    if (state.tab === (action === "later" ? "later" : "favorite") && !item[field]) state.list = state.list.filter((it) => it !== item);
    renderList();
    toast(item[field] ? (action === "later" ? "あとで読むに入れました" : "お気に入りに入れました")
      : (action === "later" ? "あとで読むから外しました" : "お気に入りから外しました"));
    act(() => api("/state", { id, field, value: item[field] }), () => { item[field] = !item[field]; });
  } else if (action === "menu" && item) {
    openSheet(item);
  } else if (action === "unmute") {
    act(async () => { await api("/unmute", { type: el.dataset.type, value: el.dataset.value }); toast("非表示を解除しました"); render(); });
  } else if (action === "reset") {
    if (confirm("学習した好みをリセットして、最初の状態に戻しますか？")) {
      act(async () => { await api("/reset-weights", {}); toast("学習をリセットしました"); render(); });
    }
  }
});

// 画像が読み込めなかったら分野の色に置き換える
$("main").addEventListener("error", (ev) => {
  const img = ev.target;
  if (img.tagName !== "IMG") return;
  img.outerHTML = placeholder({ category: img.dataset.cat });
}, true);

$("chips").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-filter]");
  if (!b) return;
  state.filter = b.dataset.filter;
  renderChips();
  renderList();
});

$("top").addEventListener("change", (ev) => {
  if (ev.target.id !== "edition-select") return;
  const [date, edition] = ev.target.value.split("|");
  state.edition = { date, edition };
  render();
});

$("sheet-close").addEventListener("click", closeSheet);
$("sheet").addEventListener("click", (ev) => { if (ev.target === $("sheet")) closeSheet(); });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !$("sheet").hidden) closeSheet(); });

$("sheet-open").addEventListener("click", () => {
  const item = state.sheetItem;
  if (item) { item.is_read = true; api("/open", { id: item.id }).catch(() => {}); }
  closeSheet();
  renderList();
});

$("sheet-mute-source").addEventListener("click", () => {
  const item = state.sheetItem;
  if (!item?.publisher) return;
  const pub = item.publisher;
  closeSheet();
  removeWhere((it) => it.publisher === pub);
  renderChips();
  renderList();
  toast(`「${pub}」を非表示にしました（設定から解除できます）`);
  act(() => api("/mute", { type: "source", value: pub }));
});

$("sheet-tags").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-tag]");
  if (!b) return;
  const tag = b.dataset.tag, on = b.getAttribute("aria-pressed") !== "true";
  b.setAttribute("aria-pressed", String(on));
  b.classList.toggle("on", on);
  b.textContent = on ? `${tag} を非表示中` : tag;
  act(() => api(on ? "/mute" : "/unmute", { type: "keyword", value: tag }));
  if (on) {
    const k = tag.toLowerCase();
    removeWhere((it) => it !== state.sheetItem && [it.title, it.summary || "", ...it.tags].join(" ").toLowerCase().includes(k));
    if (state.sheetItem) removeWhere((it) => it === state.sheetItem);
    renderChips();
    renderList();
  }
});

// ---------------------------------------------------------------- 画面の切り替え

function route() {
  const tab = (location.hash || "#today").slice(1);
  state.tab = ["today", "later", "favorite", "settings"].includes(tab) ? tab : "today";
  window.scrollTo(0, 0);
  render();
}
window.addEventListener("hashchange", route);
route();
