"use strict";
// romaji-slm 体験版（V0.2）のステップ式ページ。docs/TRIAL.md「画面」。
// 画面の状態は記録（kind=compare/answer/feel）から組み立てる。記録は押した時点で /api/record に送り、
// 保存の完了を待ってから次へ進む。送れなかった記録は localStorage に残し、結果ブロックに *未保存 で出す。

const $ = (id) => document.getElementById(id);
const CTX_MAX = 60;
const RETRY_MS = [500, 1000, 2000];
const STEP_NAMES = ["準備", "句読点", "英字", "体感", "結果"];

const S = {
  info: null, cfg: null, ex: null, session: null,
  recs: {},        // id → 記録（最新の版）
  pending: [],     // サーバに保存できていない記録の id（送る順）
  started: false,  // 準備の画面で「はじめる」を押した
  step: 0,
  measures: [],
  busy: null,      // 変換中の例のキー
  measuring: false,
  stale: false,    // サーバが再起動してセッションが変わった
};

// ---- 小物 ------------------------------------------------------------------
function el(tag, attrs, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c !== null && c !== undefined) e.append(c);
  return e;
}

function shuffle(xs) {
  const a = xs.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let r;
  try {
    r = await fetch(path, opt);
  } catch (e) {
    throw new ApiError(`サーバにつながらない（黒いウィンドウが閉じていないか確かめてください）: ${e.message}`, 0);
  }
  const ct = r.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (r.status === 409) markStale();
  if (!r.ok) throw new ApiError((data && data.error) || `HTTP ${r.status}`, r.status);
  return data;
}

function setStatus(id, text, isError) {
  const e = $(id);
  e.textContent = text || "";
  e.classList.toggle("error", !!isError);
}

function banner(text, kind, action) {
  const b = $("banner");
  b.replaceChildren();
  if (!text) { b.classList.add("hidden"); return; }
  b.append(text);
  if (action) b.append(" ", el("button", { onclick: action.fn }, action.label));
  b.className = `banner ${kind || ""}`;
}

function markStale() {
  S.stale = true;
  banner("サーバが再起動しました。ページを再読込してください（今回の選択は新しいセッションとして始まります）。", "error",
    { label: "再読込", fn: () => location.reload() });
}

// ---- 保存（localStorage と /api/record） ------------------------------------
const lsKey = () => `romaji-slm-trial:${S.session}`;

function saveLocal() {
  try {
    localStorage.setItem(lsKey(), JSON.stringify({ recs: S.recs, pending: S.pending, started: S.started, step: S.step }));
  } catch (_) { /* 使えないブラウザでもサーバ側の記録で続けられる */ }
}

function loadLocal() {
  try {
    return JSON.parse(localStorage.getItem(lsKey()) || "null");
  } catch (_) {
    return null;
  }
}

async function postWithRetry(path, body) {
  let last;
  for (let i = 0; i <= RETRY_MS.length; i++) {
    try {
      return await api(path, body);
    } catch (e) {
      last = e;
      if (e.status === 409 || e.status === 400 || i === RETRY_MS.length) break;
      await sleep(RETRY_MS[i]);
    }
  }
  throw last;
}

let flushing = Promise.resolve(true);

function flushPending() {
  // 送る順を守るため、前の送信が終わってから次を送る
  flushing = flushing.then(async () => {
    while (S.pending.length) {
      const id = S.pending[0];
      try {
        await postWithRetry("/api/record", S.recs[id]);
      } catch (e) {
        if (!S.stale) {
          banner(`保存に失敗しました（${e.message}）。選択はこのブラウザに残しているので、結果ブロックにも出ます。`, "error",
            { label: "もう一度保存", fn: () => flushPending().then((ok) => ok && banner("")) });
        }
        return false;
      }
      S.pending.shift();
      saveLocal();
    }
    return true;
  });
  return flushing;
}

async function save(rec) {
  const prev = S.recs[rec.id];
  const r = { ...rec, session: S.session, rev: ((prev && prev.rev) || 0) + 1, client_time: new Date().toISOString() };
  S.recs[r.id] = r;
  if (!S.pending.includes(r.id)) S.pending.push(r.id);
  saveLocal();
  return flushPending();
}

// ---- 記録から読む ------------------------------------------------------------
function answers() {
  const a = {};
  for (const r of Object.values(S.recs)) if (r.kind === "answer") a[r.question] = r.value;
  return a;
}

function option(q, value) {
  return S.cfg.questions[q].options.find((o) => o.value === value) || null;
}

function comboId(base) {
  // 未回答は最後の選択肢（まだ決められない＝暫定値）の値（trial/summary.py の combo_id と同じ）
  const a = answers();
  const parts = [base];
  for (const key of ["punct", "en_case"]) {
    const opts = S.cfg.questions[key].options;
    parts.push((option(key, a[key]) || opts[opts.length - 1])[key]);
  }
  return parts.join("/");
}

const withBase = (cid, base) => [base, ...cid.split("/").slice(1)].join("/");

const cmpId = (stepId, i) => `cmp-${stepId}-${i}`;

function cmpDone(r) {
  return !!(r && r.choice && (r.choice !== "neither" || r.reason));
}

function feelRecs() {
  return Object.values(S.recs).filter((r) => r.kind === "feel").sort((a, b) => a.seq - b.seq);
}

function committedText() {
  return feelRecs().filter((r) => r.source !== "speed").map((r) => r.text).join("");
}

function stepDone(n) {
  const a = answers();
  if (n === 0) return S.started || Object.keys(S.recs).length > 0;
  if (n === 1) return "punct" in a;
  if (n === 2) return "en_case" in a;
  if (n === 3) return "use" in a;
  return false;
}

function firstOpenStep() {
  for (let n = 0; n < 4; n++) if (!stepDone(n)) return n;
  return 4;
}

// ---- 画面の切り替え ------------------------------------------------------------
function goStep(n) {
  S.step = Math.max(0, Math.min(4, n));
  saveLocal();
  render();
  window.scrollTo(0, 0);
  if (S.step === 4) runMeasure();
}

function render() {
  for (let n = 0; n <= 4; n++) $(`step-${n}`).classList.toggle("hidden", n !== S.step);
  $("stepper").replaceChildren(...STEP_NAMES.map((name, n) => el("li", {
    class: [n === S.step ? "current" : "", stepDone(n) ? "done" : ""].join(" "),
  }, `${n === 0 ? "" : n + ". "}${name}${stepDone(n) && n !== S.step ? " ✓" : ""}`)));
  $("back").classList.toggle("hidden", S.step === 0);
  const canNext = S.step >= 1 && S.step < 4 && stepDone(S.step);
  $("next").classList.toggle("hidden", !canNext);
  if (S.step === 1) renderCompare("punct", $("cmp-punct"), $("q-punct"));
  if (S.step === 2) renderCompare("en_case", $("cmp-en_case"), $("q-en_case"));
  if (S.step === 3) renderFeel();
}

// ---- ステップ 1・2: 比較 -----------------------------------------------------
function renderCompare(stepId, box, qbox) {
  const step = S.cfg.steps[stepId];
  const exs = S.ex[stepId];
  const two = step.configs.length === 2;
  box.replaceChildren();
  let allDone = true;
  for (let i = 0; i < exs.length; i++) {
    const r = S.recs[cmpId(stepId, i)];
    box.append(renderExample(stepId, step, exs, i, r, two));
    if (!cmpDone(r)) { allDone = false; break; }
  }
  qbox.replaceChildren();
  if (allDone) qbox.append(renderQuestion(step.question));
}

function renderExample(stepId, step, exs, i, r, two) {
  const key = cmpId(stepId, i);
  const ex = exs[i];
  const head = el("div", { class: "ex-head" }, `例 ${i + 1}/${exs.length}`, ex.kind ? el("span", { class: "kind" }, `（${ex.kind}）`) : null);
  const input = el("div", { class: "ex-input" }, ex.romaji);
  const block = el("div", { class: `example${cmpDone(r) ? " done" : ""}`, id: `ex-${key}` }, head, input);
  const cards = r && r.cards;
  const allFailed = cards && cards.every((c) => c.error);
  if (!cards || allFailed) {
    const busy = S.busy === key;
    block.append(el("div", { class: "row" }, el("button", {
      class: "big primary", disabled: busy || !!S.busy,
      onclick: () => convertExample(stepId, i),
    }, busy ? "変換中…" : allFailed ? "もう一度変換" : "この例を変換")));
    if (busy) block.append(el("p", { class: "status" }, "変換しています（数秒かかることがあります）"));
  }
  block.append(el("p", { class: "status error", id: `err-${key}` }));
  if (!cards) return block;

  const grid = el("div", { class: "cards" });
  for (const c of cards) {
    const chosen = r.choice === c.config_id;
    grid.append(el("div", { class: `card${chosen ? " best" : ""}` },
      el("div", { class: "label" }, c.label),
      el("div", { class: `out${c.error ? " error" : ""}` }, c.text),
      c.error ? el("div", { class: "err" }, `変換できませんでした: ${c.error}`) : null,
      el("button", { class: `pick${chosen ? " selected" : ""}`, onclick: () => choose(stepId, i, c.config_id) }, "こちらがいい")));
  }
  block.append(grid);
  block.append(el("div", { class: "row center" },
    el("button", { class: r.choice === "all_ok" ? "selected" : "", onclick: () => choose(stepId, i, "all_ok") },
      two ? "どちらも問題ない" : "どれも問題ない"),
    el("button", { class: `fatal${r.choice === "neither" ? " selected" : ""}`, onclick: () => choose(stepId, i, "neither") },
      two ? "どちらも使えない" : "どれも使えない")));
  if (r.choice === "neither") {
    block.append(el("div", { class: "row center reasons" }, el("span", {}, "使えない理由:"),
      ...S.cfg.reasons.map((x) => el("button", {
        class: r.reason === x.value ? "selected" : "", onclick: () => choose(stepId, i, "neither", x.value),
      }, x.label))));
  }
  return block;
}

async function convertExample(stepId, i) {
  const key = cmpId(stepId, i);
  const step = S.cfg.steps[stepId];
  const ex = S.ex[stepId][i];
  S.busy = key;
  render();
  try {
    // 実行順も並びも毎回入れ替える（待ち時間や位置から設定を当てられないように）
    const res = await api("/api/convert", { text: ex.romaji, left_context: "", config_ids: shuffle(step.configs) });
    const cards = shuffle(res.results).map((x, j) => ({
      label: `案${j + 1}`, config_id: x.config_id, text: x.text, ms: x.ms, error: x.error,
    }));
    S.busy = null;
    await save({ kind: "compare", id: key, step: stepId, index: i, total: S.ex[stepId].length, input: ex.romaji,
      cards, choice: null, reason: null });
    render();
  } catch (e) {
    S.busy = null;
    render();
    setStatus(`err-${key}`, `変換に失敗しました: ${e.message}`, true);
  }
}

async function choose(stepId, i, choice, reason) {
  const key = cmpId(stepId, i);
  const r = S.recs[key];
  const wasDone = cmpDone(r);
  render();
  setStatus(`err-${key}`, "保存しています…");
  await save({ ...r, choice, reason: reason || null });
  render();
  // 新しく決まったときだけ次の例（なければ質問カード）へ送る
  if (!wasDone && cmpDone(S.recs[key])) {
    const next = $(`ex-${cmpId(stepId, i + 1)}`) || $(`q-${S.cfg.steps[stepId].question}`);
    if (next) next.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// ---- 質問カード ---------------------------------------------------------------
function renderQuestion(qid) {
  const q = S.cfg.questions[qid];
  const cur = answers()[qid];
  return el("div", { class: "qcard" },
    el("p", { class: "qtext" }, q.text),
    el("div", { class: "row" }, ...q.options.map((o) => el("button", {
      class: `big${cur === o.value ? " selected" : ""}`, onclick: () => answer(qid, o.value),
    }, o.label))),
    q.note ? el("p", { class: "note" }, q.note) : null,
    el("p", { class: "status", id: `qs-${qid}` }));
}

async function answer(qid, value) {
  setStatus(`qs-${qid}`, "保存しています…");
  await save({ kind: "answer", id: `answer-${qid}`, question: qid, value });
  if (qid === "en_case") {
    // 体感と計測で使う組合せを先に作って空回ししておく（応答は待たない）
    const cid = comboId(S.cfg.feel_base);
    api("/api/prepare", { config_ids: [cid, withBase(cid, S.cfg.speed_base)] }).catch(() => {});
  }
  goStep(S.step + 1);
}

// ---- ステップ 3: 体感 -----------------------------------------------------------
function comboText() {
  const a = answers();
  const part = (key) => {
    const q = S.cfg.questions[key];
    const o = option(key, a[key]);
    const fallback = q.options[q.options.length - 1];
    const chosen = o || fallback;
    const set = q.options.find((x) => !x.provisional && x[key] === chosen[key]);
    return chosen.provisional || !o ? `${set ? set.label : chosen[key]}（暫定）` : chosen.label;
  };
  return `この設定で変換します — 句読点: ${part("punct")} ／ 英字: ${part("en_case")}`;
}

function feelInput(i) {
  const form = comboId(S.cfg.feel_base).split("/")[1] === "insert" ? "nopunct" : "punct";
  return S.ex.feel[i][form];
}

function renderFeel() {
  $("feel-combo").textContent = comboText();
  const recs = feelRecs();
  const box = $("feel-examples");
  box.replaceChildren();
  let examplesDone = true;
  for (let i = 0; i < S.ex.feel.length; i++) {
    const src = `example${i + 1}`;
    const done = recs.some((r) => r.source === src);
    const busy = S.busy === src;
    box.append(el("div", { class: `example${done ? " done" : ""}` },
      el("div", { class: "ex-head" }, i === 0 ? "例 1" : `例 ${i + 1}（前の文の続き。左文脈あり）`),
      el("div", { class: "ex-input" }, feelInput(i)),
      done ? null : el("div", { class: "row" }, el("button", {
        class: "big primary", disabled: !!S.busy, onclick: () => convertFeel(feelInput(i), src),
      }, busy ? "変換中…" : "変換"))));
    if (!done) { examplesDone = false; break; }
  }
  $("committed").value = committedText();
  $("feel-more").classList.toggle("hidden", !examplesDone);
  $("own-convert").disabled = !!S.busy;
  $("try-speed").disabled = !!S.busy || !recs.some((r) => r.source !== "speed");
  $("feel-history").replaceChildren(...recs.map((r) => el("li", {},
    el("span", { class: "in" }, r.input),
    el("span", { class: `res${r.error ? " error" : ""}` }, r.text),
    el("span", { class: "ms" }, `${Math.round(r.ms)} ms${r.source === "speed" ? "（速度優先 xsmall）" : ""}`),
    r.error ? el("span", { class: "error" }, ` ${r.error}`) : null)));
  $("q-use").replaceChildren();
  if (examplesDone) $("q-use").append(renderQuestion("use"));
}

async function convertFeel(text, source) {
  if (S.busy) return;
  text = text.trim();
  const recs = feelRecs();
  let cid = comboId(S.cfg.feel_base);
  let ctx = committedText().slice(-CTX_MAX);
  if (source === "speed") {
    // 直前に変換した文を、同じ左文脈で速度優先の設定にかける
    const last = recs.filter((r) => r.source !== "speed").pop();
    if (!last) return;
    text = last.input;
    ctx = last.left_context;
    cid = withBase(last.config_id, S.cfg.speed_base);
  }
  if (!text) return;
  S.busy = source;
  setStatus("feel-status", "変換しています…");
  render();
  try {
    const res = await api("/api/convert", { text, left_context: ctx, config_ids: [cid] });
    const x = res.results[0];
    const seq = recs.length ? recs[recs.length - 1].seq + 1 : 1;
    S.busy = null;
    await save({ kind: "feel", id: `feel-${seq}`, seq, source, config_id: cid, input: text, left_context: ctx,
      text: x.text, ms: x.ms, error: x.error });
    setStatus("feel-status", x.error ? `変換できませんでした（入力をそのまま残しました）: ${x.error}` : "", !!x.error);
    if (source === "own") $("own-input").value = "";
  } catch (e) {
    S.busy = null;
    setStatus("feel-status", `変換に失敗しました: ${e.message}`, true);
  }
  render();
}

// ---- ステップ 4: 計測と結果 -----------------------------------------------------
function measurePlan() {
  const fin = comboId(S.cfg.feel_base);
  const fast = withBase(fin, S.cfg.speed_base);
  const m = S.ex.measure;
  return [
    ...m.final.map((text, index) => ({ phase: "final", config_id: fin, index, text })),
    ...m.speed.map((text, index) => ({ phase: "speed", config_id: fin, index, text })),
    ...m.speed.map((text, index) => ({ phase: "speed", config_id: fast, index, text })),
  ];
}

const sameItem = (a, b) => a.phase === b.phase && a.config_id === b.config_id && a.index === b.index;

async function runMeasure() {
  if (S.measuring) return;
  S.measuring = true;
  const plan = measurePlan();
  const todo = plan.filter((p) => !S.measures.some((m) => sameItem(m, p) && m.input === p.text));
  const bar = $("measure-progress");
  bar.max = plan.length;
  bar.value = plan.length - todo.length;
  bar.classList.remove("hidden");
  $("measure-lead").textContent = "短い計測をしています。そのままお待ちください（操作は要りません）。";
  const warmed = new Set();
  try {
    for (const p of todo) {
      if (S.step !== 4) return; // 戻ったら止める（続きは次に開いたとき）
      const label = p.config_id.split("/")[0];
      if (!warmed.has(p.config_id)) {
        setStatus("measure-status", `計測の準備（${label}）…`);
        await postWithRetry("/api/measure", { session: S.session, ...p, warmup: true });
        warmed.add(p.config_id);
      }
      setStatus("measure-status", `計測中 ${bar.value + 1}/${plan.length}（${label}）`);
      const res = await postWithRetry("/api/measure", { session: S.session, ...p });
      S.measures.push({ ...p, input: p.text, ...res.result });
      bar.value += 1;
    }
    setStatus("measure-status", `計測が終わりました（${plan.length} 文）。`);
    $("measure-lead").textContent = "終わりました。「結果をコピー」を押して、そのまま貼って返してください。";
  } catch (e) {
    setStatus("measure-status", `計測に失敗しました: ${e.message}（結果ブロックには計測できた分だけ出ます）`, true);
  } finally {
    S.measuring = false;
  }
  if (S.step === 4) await showSummary();
}

async function showSummary() {
  await flushPending();
  try {
    const text = await api("/api/summary", { session: S.session, client_records: Object.values(S.recs) });
    $("summary").value = text;
  } catch (e) {
    $("summary").value = `結果ブロックを作れませんでした: ${e.message}\n\n` + JSON.stringify(Object.values(S.recs));
  }
  $("result-box").classList.remove("hidden");
}

async function copySummary() {
  const text = $("summary").value;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    $("summary").select();
    document.execCommand("copy");
  }
  setStatus("copy-status", "コピーしました。貼って返してください。");
}

// ---- 起動 ----------------------------------------------------------------------
async function waitReady() {
  for (;;) {
    try {
      const info = await api("/api/info");
      if (info.session !== S.session) { markStale(); return; }
      if (info.ready) {
        if (info.load_error) {
          banner(`モデルの読み込みに失敗しました: ${info.load_error}（変換は失敗として記録されます）`, "error");
        }
        const ignored = Object.entries(info.engine.ignored || {}).filter(([, v]) => v.length);
        if (ignored.length) banner(`注意: 変換コアが受け付けない設定項目があります: ${ignored.map(([k, v]) => `${k}(${v})`).join(", ")}`);
        setStatus("ready-status", "準備できました。");
        $("start").disabled = false;
        return;
      }
    } catch (e) {
      setStatus("ready-status", `サーバの応答を待っています… ${e.message}`, true);
    }
    await sleep(1000);
  }
}

async function init() {
  const info = await api("/api/info");
  S.info = info;
  S.cfg = info.configs;
  S.ex = info.examples;
  S.session = info.session;
  $("version").textContent = `${info.version}・セッション ${info.session}${info.engine.name === "mock" ? "・偽の変換（mock）" : ""}`;
  $("try-speed").textContent = `速度優先（${S.cfg.speed_base}）でも同じ文を試す`;

  // サーバの記録とブラウザの記録を合わせる（新しい版が勝つ。ブラウザだけにある新しい版は送り直す）
  const local = loadLocal();
  const state = await api("/api/state");
  for (const r of state.records) S.recs[r.id] = r;
  if (local) {
    for (const r of Object.values(local.recs || {})) {
      const s = S.recs[r.id];
      if (!s || (r.rev || 0) > (s.rev || 0)) {
        S.recs[r.id] = r;
        if (!S.pending.includes(r.id)) S.pending.push(r.id);
      }
    }
    S.started = !!local.started;
  }
  S.measures = state.measures;
  S.step = local && Number.isInteger(local.step) ? Math.min(local.step, firstOpenStep()) : firstOpenStep();
  if (S.pending.length) flushPending();

  $("start").addEventListener("click", () => { S.started = true; goStep(1); });
  $("back").addEventListener("click", () => goStep(S.step - 1));
  $("next").addEventListener("click", () => goStep(S.step + 1));
  $("own-convert").addEventListener("click", () => convertFeel($("own-input").value, "own"));
  $("own-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); convertFeel($("own-input").value, "own"); }
  });
  $("try-speed").addEventListener("click", () => convertFeel("", "speed"));
  $("copy").addEventListener("click", copySummary);

  goStep(S.step);
  waitReady();
}

init().catch((e) => banner(`ページを開けませんでした: ${e.message}`, "error", { label: "再読込", fn: () => location.reload() }));
