"use strict";
// romaji-slm 体験版のページ。サーバは状態を持たないので、左文脈と画面の状態はここで持つ。
// 記録は選択のたびに /api/record に送る（同じ試行は同じ id で、最後の行が最終の状態）。

const $ = (id) => document.getElementById(id);
const CTX_MAX = 60;
const LABELS = "ABCDEFGH";

let info = null;
let axisId = null;
let singleId = null;
let exampleIdx = 0;
let trial = null; // 比べる: いま表示中の試行
let ctxStart = 0; // 打ってみる: 確定欄のこの位置より後ろだけを左文脈にする

function newId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function shuffle(xs) {
  const a = xs.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const ct = r.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

function setStatus(id, text, isError) {
  const el = $(id);
  el.textContent = text;
  el.classList.toggle("error", !!isError);
}

function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c);
  return e;
}

async function copyText(text, statusId) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const t = el("textarea", {});
    t.value = text;
    document.body.append(t);
    t.select();
    document.execCommand("copy");
    t.remove();
  }
  setStatus(statusId, "コピーしました");
}

function record(obj) {
  return api("/api/record", obj).catch((e) => setStatus("cmp-status", `記録に失敗: ${e.message}`, true));
}

// ---- タブ ---------------------------------------------------------------
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  if (name === "summary") refreshSummary();
  if (name === "compare") $("cmp-input").focus();
  if (name === "try") $("try-input").focus();
}

// ---- 起動・読み込み待ち ------------------------------------------------
async function loadInfo() {
  info = await api("/api/info");
  $("version").textContent = `${info.version}（${info.engine.name}）`;
  const b = $("banner");
  if (info.load_error) {
    b.textContent = `モデルの読み込みに失敗しました: ${info.load_error}（変換するとエラーが出ます）`;
    b.className = "banner error";
  } else if (!info.ready) {
    b.textContent = "モデルを読み込み中です。終わるまで変換は待たされます…";
    b.className = "banner";
    setTimeout(loadInfo, 1000);
  } else {
    b.className = "banner hidden";
  }
}

// ---- 比べる -------------------------------------------------------------
function renderAxes() {
  const box = $("axes");
  box.replaceChildren();
  for (const a of info.configs.axes) {
    box.append(el("button", {
      class: a.id === axisId ? "selected" : "",
      onclick: () => { axisId = a.id; renderAxes(); clearCards(); $("cmp-input").focus(); },
    }, a.label));
  }
  const axis = currentAxis();
  const ignored = new Set();
  for (const c of axis.configs) for (const k of info.engine.ignored[c] || []) ignored.add(k);
  $("axis-note").textContent = ignored.size
    ? `注意: この版の変換コアは ${[...ignored].join(", ")} の設定をまだ受け付けません。この軸のカードは同じ動作になることがあります。`
    : "";
}

function currentAxis() {
  return info.configs.axes.find((a) => a.id === axisId);
}

function clearCards() {
  trial = null;
  $("cards").replaceChildren();
  $("all-ok").classList.add("hidden");
  setStatus("cmp-status", "");
}

async function compareConvert() {
  const text = $("cmp-input").value.trim();
  if (!text) return;
  const axis = currentAxis();
  $("cmp-convert").disabled = true;
  setStatus("cmp-status", `変換中…（${axis.configs.length} 設定を順に実行）`);
  try {
    const res = await api("/api/convert", { text, left_context: "", config_ids: axis.configs });
    const cards = shuffle(res.results).map((r, i) => ({
      label: LABELS[i], config_id: r.config_id, text: r.text, ms: r.ms, error: r.error,
    }));
    trial = { kind: "compare", id: newId(), axis: axis.id, input: text, left_context: "", cards,
              best: null, fatal: [], all_ok: false, total_ms: res.total_ms };
    renderCards();
    setStatus("cmp-status", `待ち時間 合計 ${Math.round(res.total_ms)} ms。いちばん良いもの・致命的なものを押してください。`);
  } catch (e) {
    setStatus("cmp-status", `変換できませんでした: ${e.message}`, true);
  } finally {
    $("cmp-convert").disabled = false;
  }
}

function renderCards() {
  const box = $("cards");
  box.replaceChildren();
  for (const c of trial.cards) {
    const out = el("div", { class: "out" + (c.error ? " error" : "") }, c.text);
    const card = el("div", { class: "card" + (trial.best === c.label ? " best" : "") },
      el("div", { class: "head" },
        el("span", { class: "label" }, c.label),
        el("span", { class: "ms" }, `${Math.round(c.ms)} ms`)),
      out);
    if (c.error) card.append(el("div", { class: "err" }, `変換失敗（原入力のまま）: ${c.error}`));
    card.append(el("div", { class: "btns" },
      el("button", { class: "pick" + (trial.best === c.label ? " selected" : ""), onclick: () => choose("best", c.label) },
        "一番いい"),
      el("button", { class: "fatal" + (trial.fatal.includes(c.label) ? " selected" : ""),
                     onclick: () => choose("fatal", c.label) }, "致命的")));
    box.append(card);
  }
  const ok = $("all-ok");
  ok.classList.remove("hidden");
  ok.classList.toggle("selected", trial.all_ok);
}

function choose(kind, label) {
  if (!trial) return;
  if (kind === "best") {
    trial.best = trial.best === label ? null : label;
    trial.all_ok = false;
  } else if (kind === "fatal") {
    const i = trial.fatal.indexOf(label);
    if (i >= 0) trial.fatal.splice(i, 1); else trial.fatal.push(label);
    trial.all_ok = false;
  } else {
    trial.all_ok = !trial.all_ok;
    if (trial.all_ok) { trial.best = null; trial.fatal = []; }
  }
  renderCards();
  const { total_ms, ...rec } = trial;
  record(rec).then(() => setStatus("cmp-status", "記録しました。次の文をどうぞ（選び直しもできます）。"));
  $("cmp-input").value = "";
  $("cmp-input").focus();
}

function putExample() {
  const ex = info.bench_sentences.slice(0, 10);
  if (!ex.length) return;
  $("cmp-input").value = ex[exampleIdx % ex.length].romaji;
  exampleIdx++;
  $("cmp-input").focus();
}

// ---- 打ってみる ---------------------------------------------------------
function renderSingles() {
  const box = $("singles");
  box.replaceChildren();
  for (const [cid, c] of Object.entries(info.configs.configs)) {
    box.append(el("button", {
      class: cid === singleId ? "selected" : "",
      onclick: () => { singleId = cid; renderSingles(); $("try-input").focus(); },
    }, c.label || cid));
  }
}

function leftContext() {
  const v = $("committed").value;
  if (ctxStart > v.length) ctxStart = v.length;
  return v.slice(ctxStart).slice(-CTX_MAX);
}

function renderCtx() {
  const c = leftContext();
  $("ctx-view").textContent = c ? `左文脈: …${c.slice(-20)}` : "左文脈: なし";
}

async function tryConvert() {
  const text = $("try-input").value.trim();
  if (!text) return;
  const cid = singleId;
  const ctx = leftContext();
  $("try-convert").disabled = true;
  setStatus("try-status", "変換中…");
  try {
    const res = await api("/api/convert", { text, left_context: ctx, config_ids: [cid] });
    const r = res.results[0];
    const rec = { kind: "single", id: newId(), config_id: cid, input: text, left_context: ctx,
                  text: r.text, ms: r.ms, error: r.error, verdict: null };
    $("committed").value += r.text;
    $("try-input").value = "";
    addHistory(rec);
    record(rec);
    setStatus("try-status", r.error ? `変換失敗（原入力のまま追記）: ${r.error}` : `${Math.round(r.ms)} ms`, !!r.error);
  } catch (e) {
    setStatus("try-status", `変換できませんでした: ${e.message}`, true);
  } finally {
    $("try-convert").disabled = false;
    renderCtx();
    $("try-input").focus();
  }
}

function addHistory(rec) {
  const used = el("button", {}, "使えた");
  const fixed = el("button", {}, "直した");
  const set = (v) => {
    rec.verdict = rec.verdict === v ? null : v;
    used.classList.toggle("selected", rec.verdict === "used");
    fixed.classList.toggle("selected", rec.verdict === "fixed");
    record(rec);
    $("try-input").focus();
  };
  used.addEventListener("click", () => set("used"));
  fixed.addEventListener("click", () => set("fixed"));
  const label = (info.configs.configs[rec.config_id] || {}).label || rec.config_id;
  const li = el("li", {},
    el("div", { class: "line" },
      el("span", { class: "res" + (rec.error ? " error" : "") }, rec.text),
      el("span", { class: "ms" }, `${Math.round(rec.ms)} ms・${label}`),
      used, fixed),
    el("div", { class: "in" }, rec.input + (rec.error ? `（失敗: ${rec.error}）` : "")));
  $("history").prepend(li);
}

// ---- まとめ -------------------------------------------------------------
async function refreshSummary() {
  try {
    $("summary").value = await api("/api/summary");
    setStatus("summary-status", "");
  } catch (e) {
    setStatus("summary-status", `まとめを取れませんでした: ${e.message}`, true);
  }
}

async function runBench() {
  const ids = [...new Set(info.configs.axes.flatMap((a) => a.configs))];
  const prog = $("bench-progress");
  const btn = $("bench-run");
  btn.disabled = true;
  prog.classList.remove("hidden");
  prog.max = ids.length;
  const t0 = Date.now();
  let cur = "";
  let done = 0;
  const tick = () => setStatus("bench-status",
    `${done}/${ids.length} 設定済み。計測中: ${cur}（経過 ${Math.round((Date.now() - t0) / 1000)} 秒）`);
  const timer = setInterval(tick, 1000);
  try {
    for (const cid of ids) {
      cur = cid;
      tick();
      await api("/api/bench", { config_ids: [cid] });
      done++;
      prog.value = done;
    }
    setStatus("bench-status", `完了（${Math.round((Date.now() - t0) / 1000)} 秒）`);
  } catch (e) {
    setStatus("bench-status", `計測が止まりました: ${e.message}`, true);
  } finally {
    clearInterval(timer);
    btn.disabled = false;
    refreshSummary();
  }
}

// ---- 配線 ---------------------------------------------------------------
function onEnter(id, fn) {
  $(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); fn(); }
  });
}

async function init() {
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("cmp-convert").addEventListener("click", compareConvert);
  $("cmp-example").addEventListener("click", putExample);
  $("all-ok").addEventListener("click", () => choose("all_ok"));
  onEnter("cmp-input", compareConvert);
  $("try-convert").addEventListener("click", tryConvert);
  onEnter("try-input", tryConvert);
  $("try-copy").addEventListener("click", () => copyText($("committed").value, "try-status"));
  $("try-reset").addEventListener("click", () => { ctxStart = $("committed").value.length; renderCtx(); });
  $("committed").addEventListener("input", renderCtx);
  $("bench-run").addEventListener("click", runBench);
  $("summary-copy").addEventListener("click", () => copyText($("summary").value, "summary-status"));
  $("summary-refresh").addEventListener("click", refreshSummary);
  try {
    await loadInfo();
  } catch (e) {
    const b = $("banner");
    b.textContent = `サーバにつながりません: ${e.message}`;
    b.className = "banner error";
    return;
  }
  axisId = info.configs.axes[0].id;
  singleId = info.configs.default_single;
  renderAxes();
  renderSingles();
  renderCtx();
  $("cmp-input").focus();
}

init();
