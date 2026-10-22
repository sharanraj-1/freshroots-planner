// Freshroots planner workbench. Reads and writes through the planner API; holds no business data itself.
"use strict";

const $ = s => document.querySelector(s);
const fmt = n => n == null ? "–" : Number(n).toLocaleString("en-IN");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
const finePointer = matchMedia("(pointer: fine)").matches;
const SWITCH_LABELS = {
  planner_enabled: "Planner", scheduled_runs_enabled: "Scheduled runs", ai_assistant_enabled: "AI assistant",
  zoho_api_enabled: "Zoho API", pre_po_check_enabled: "Pre-PO check",
};

const state = {
  meta: {decision_reasons: [], switch_admins: []}, runs: [], run: null, recs: [], status: null,
  view: "run", filter: "all", open: new Set(), briefs: {}, user: "", typed: {}, fresh: new Set(), drawn: new Set(),
};

// ---------- identity (a per-browser convenience; the API records who decided) ----------
try { state.user = localStorage.getItem("fr-user") || ""; } catch (e) { /* storage blocked */ }
$("#who").value = state.user;
$("#who").addEventListener("change", e => {
  state.user = e.target.value.trim();
  try { localStorage.setItem("fr-user", state.user); } catch (err) { /* storage blocked */ }
  render(); renderStatus();
});
const isAdmin = () => state.meta.switch_admins.includes(state.user);

async function api(path, opts = {}) {
  const headers = {"Content-Type": "application/json", ...(opts.headers || {})};
  if (state.user) headers["X-Planner-User"] = state.user;
  const res = await fetch("/api" + path, {...opts, headers});
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = body && body.detail;
    throw new Error(Array.isArray(detail) ? detail.map(d => d.msg).join("; ") : (detail || body || res.statusText));
  }
  return body;
}

function showError(msg) {
  $("#err").textContent = msg || "";
  $("#err").classList.toggle("hidden", !msg);
}

// ---------- motion helpers ----------
function countUp(el, to) {
  const from = Number(el.dataset.count || 0);
  el.dataset.count = to;
  if (calm || from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = 700;
  const step = now => {
    const p = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(Math.round(from + (to - from) * e));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function splitTitle(text) {
  let i = 0;
  return text.split(" ").map(word => `<span class="w">${[...word].map(ch => `<span class="ch" style="--i:${i++}">${esc(ch)}</span>`).join("")}</span>`).join(" ");
}

function marquee(el, items, copies = 2) {
  const one = items.map(t => `<span>${esc(t)}</span>`).join("");
  el.innerHTML = one.repeat(copies * 2);  // two identical halves: the -50% loop is seamless
}

// Cards tilt towards the cursor and carry a light where it points.
if (finePointer && !calm) {
  document.addEventListener("pointermove", e => {
    const el = e.target.closest("[data-tilt], .tile");
    document.querySelectorAll(".tilting").forEach(x => { if (x !== el) { x.classList.remove("tilting"); x.style.removeProperty("--rx"); x.style.removeProperty("--ry"); } });
    if (!el) return;
    const b = el.getBoundingClientRect(), x = (e.clientX - b.left) / b.width, y = (e.clientY - b.top) / b.height;
    el.classList.add("tilting");
    el.style.setProperty("--mx", `${x * 100}%`); el.style.setProperty("--my", `${y * 100}%`);
    el.style.setProperty("--rx", `${(0.5 - y) * 5}deg`); el.style.setProperty("--ry", `${(x - 0.5) * 7}deg`);
  });
  // Buttons lean a few pixels towards the pointer.
  document.addEventListener("pointermove", e => {
    const b = e.target.closest(".btn");
    document.querySelectorAll(".btn.magnet").forEach(x => { if (x !== b) { x.classList.remove("magnet"); x.style.removeProperty("--mgx"); x.style.removeProperty("--mgy"); } });
    if (!b) return;
    const r = b.getBoundingClientRect();
    b.classList.add("magnet");
    b.style.setProperty("--mgx", `${((e.clientX - r.left) / r.width - 0.5) * 6}px`);
    b.style.setProperty("--mgy", `${((e.clientY - r.top) / r.height - 0.5) * 6}px`);
  });
}

function burst(x, y) {
  if (calm) return;
  const colours = ["var(--lime)", "var(--tangerine)", "var(--sky)", "var(--pink)"];
  for (let i = 0; i < 14; i++) {
    const p = document.createElement("span");
    p.className = "burst";
    p.style.left = `${x}px`; p.style.top = `${y}px`; p.style.background = colours[i % 4];
    document.body.appendChild(p);
    const a = (Math.PI * 2 * i) / 14 + Math.random() * 0.4, d = 60 + Math.random() * 70;
    p.animate([
      {transform: "translate(-50%,-50%) scale(1) rotate(0deg)", opacity: 1},
      {transform: `translate(calc(-50% + ${Math.cos(a) * d}px), calc(-50% + ${Math.sin(a) * d}px)) scale(.4) rotate(${180 + Math.random() * 180}deg)`, opacity: 0},
    ], {duration: 700 + Math.random() * 300, easing: "cubic-bezier(.2,.75,.2,1)"}).onfinish = () => p.remove();
  }
}

// ---------- loading ----------
async function loadRuns(selectId) {
  state.runs = await api("/runs");
  $("#runPick").innerHTML = state.runs.map(r => `<option value="${r.id}">${esc(r.run_date)} · ${esc(r.status)}</option>`).join("") ||
    `<option value="">No runs yet</option>`;
  const pick = selectId || state.runs.find(r => r.status !== "failed")?.id || state.runs[0]?.id;
  if (pick) { $("#runPick").value = pick; await loadRun(pick); } else { render(); }
}

async function loadRun(id) {
  state.open.clear();
  const [run, recs] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/recommendations`)]);
  const newRun = state.run?.id !== run.id;
  state.run = run;
  state.recs = recs.map(r => ({...r.detail, ...r}));
  if (newRun) renderHero();
  render();
}

async function loadStatus() {
  try { state.status = await api("/status"); }
  catch (e) { state.status = {error: e.message, planner_enabled: false}; }
  renderStatus();
  render();
}

// ---------- drawing ----------
const shown = r => state.view === "compare" && r.comparison
  ? {decision: r.comparison.decision, qty: r.comparison.recommended_qty}
  : {decision: r.decision, qty: r.recommended_qty};
const canDecide = () => state.view === "run" && state.run?.status === "completed" && state.status?.planner_enabled;

// Cover after ordering qty units: (on hand + on order + qty) / adjusted weekly demand.
const coverAfter = (r, qty) => r.adjusted_weekly ? (r.stock_on_hand + (r.open_po_units || 0) + qty) / r.adjusted_weekly : null;

function strip(r, qty) {
  if (r.weeks_cover == null) return `<span class="note">No cover figure: ${esc(r.exception_reasons.join("; "))}</span>`;
  const S = 16, W = 300, x = v => Math.min(v, S) / S * W;
  const lo = x(r.min_cover), hi = x(r.max_cover), c = x(r.weeks_cover), u = x(r.weeks_cover_unadjusted);
  const after = qty != null ? coverAfter(r, qty) : r.post_order_cover;
  // The first time a row is drawn its dot slides in from zero and the window grows into place.
  const intro = !calm && !state.drawn.has(r.sku_id);
  state.drawn.add(r.sku_id);
  const slide = (from, to) => intro ? `<animate attributeName="cx" from="${from}" to="${to}" dur=".9s" calcMode="spline" keySplines=".2 .75 .2 1" fill="freeze"/>` : "";
  const grow = intro ? `<animate attributeName="width" from="0" to="${Math.max(hi - lo, 2)}" dur=".7s" calcMode="spline" keySplines=".2 .75 .2 1" fill="freeze"/>` : "";
  const post = after != null && after !== r.weeks_cover
    ? `<line x1="${c}" y1="17" x2="${x(after)}" y2="17" stroke="var(--tangerine)" stroke-width="4" stroke-linecap="round"/><circle cx="${x(after)}" cy="17" r="5" fill="var(--tangerine)" stroke="var(--ink)" stroke-width="1.5">${slide(c, x(after))}</circle>` : "";
  const ticks = [0, 4, 8, 12, 16].map(t => `<text x="${x(t)}" y="42" font-size="11.5" fill="var(--soft)" text-anchor="${t === 0 ? "start" : t === 16 ? "end" : "middle"}">${t}${t === 16 ? "+wk" : ""}</text>`).join("");
  return `<svg class="strip" viewBox="0 0 ${W} 44" preserveAspectRatio="xMinYMid meet" role="img" aria-label="${r.weeks_cover} weeks of cover; target ${r.min_cover} to ${r.max_cover}">
    <line x1="0" y1="17" x2="${W}" y2="17" stroke="var(--ink)" stroke-width="2" stroke-linecap="round" opacity=".25"/>
    <rect x="${lo}" y="6" width="${Math.max(hi - lo, 2)}" height="22" rx="8" fill="var(--lime)" stroke="var(--ink)" stroke-width="2">${grow}</rect>
    ${r.festival ? `<circle cx="${u}" cy="17" r="5" fill="var(--card)" stroke="var(--ink)" stroke-width="2"/>` : ""}
    ${post}
    <circle cx="${c}" cy="17" r="7.5" fill="var(--ink)">${slide(0, c)}</circle>
    ${r.weeks_cover > S ? `<text x="${W - 2}" y="4" font-size="10" text-anchor="end" fill="var(--ink)" font-weight="600">${r.weeks_cover} wks</text>` : ""}
    ${ticks}</svg>`;
}

function spark(r) {
  const W = 320, H = 92, n = r.units.length, max = Math.max(...r.units, r.adjusted_weekly || 0) * 1.08 || 1;
  const bw = W / n, y = v => H - v / max * (H - 12);
  const bars = r.units.map((u, i) => `<rect x="${i * bw + 2}" y="${y(u)}" width="${bw - 4}" height="${H - y(u)}" rx="3" fill="${i >= n - 4 ? "var(--tangerine)" : "var(--sand)"}" stroke="var(--ink)" stroke-width="1.5"><title>${esc(r.weeks[i])}: ${u} units</title></rect>`).join("");
  return `<svg viewBox="0 0 ${W} ${H + 14}" style="width:100%;max-width:460px" role="img" aria-label="${n} weeks of unit sales, last 4 highlighted">
    ${bars}
    <line x1="0" x2="${W}" y1="${y(r.avg_12w)}" y2="${y(r.avg_12w)}" stroke="var(--ink)" stroke-dasharray="5 4" stroke-width="2"/>
    ${r.festival ? `<line x1="0" x2="${W}" y1="${y(r.adjusted_weekly)}" y2="${y(r.adjusted_weekly)}" stroke="#2f7d1a" stroke-width="2.5"/>` : ""}
    <text x="0" y="${H + 12}" font-size="11" fill="var(--soft)" font-family="Geist Mono">${esc(r.weeks[0])}</text>
    <text x="${W}" y="${H + 12}" font-size="11" fill="var(--soft)" text-anchor="end" font-family="Geist Mono">${esc(r.weeks[n - 1])}</text>
  </svg>
  <p class="spark-note">Dashed: 12-week average ${fmt(r.avg_12w)}/wk. ${r.festival ? `Green: with festival uplift ${fmt(r.adjusted_weekly)}/wk. ` : ""}Orange bars: last 4 weeks, ${fmt(r.avg_4w)}/wk.</p>`;
}

function qtyText(decision, qty) {
  return decision === "REORDER" ? `${fmt(qty)} units` : decision === "PAUSE" ? "skip next order"
    : decision === "HOLD" ? "no order" : "you decide";
}

function actHTML(r) {
  const d = r.current_decision;
  if (state.view === "compare") return `<span class="compare-tag">comparison only</span>`;
  if (d && !state.open.has(r.sku_id)) {
    const word = d.action === "approve" ? "Approved" : d.action === "edit" ? `Changed → ${fmt(d.qty)}` : "Rejected";
    const fresh = state.fresh.has(r.sku_id) ? " fresh" : "";
    return `<span class="stamp ${d.action}${fresh}"><b>${word}${d.action === "approve" && d.qty ? ` ${fmt(d.qty)}` : ""}</b><small>${esc(d.decided_by)}${d.reason ? ` · ${esc(d.reason)}` : ""}</small></span>
      ${canDecide() ? `<button class="btn" data-toggle="${r.sku_id}">Change</button>` : ""}`;
  }
  if (!canDecide()) return `<span class="compare-tag">${state.status?.planner_enabled ? "read only" : "paused"}</span>`;
  const open = state.open.has(r.sku_id);
  return `<button class="btn ink" data-approve="${r.sku_id}">Approve</button>
    <button class="btn" data-toggle="${r.sku_id}" aria-expanded="${open}">${open ? "Close" : "Review"}</button>`;
}

// What the planner has typed survives re-renders (a brief arriving, a status refresh).
const draftQty = r => state.typed[r.sku_id] ?? r.current_decision?.qty ?? r.recommended_qty;
const liveClass = (r, qty) => { const c = coverAfter(r, qty); return c == null || !qty ? "" : c < r.min_cover || c > r.max_cover ? "out" : "in"; };

// The live thing: type a quantity, see where the cover lands against Raghav's window before saving.
function liveText(r, qty) {
  if (!qty || r.adjusted_weekly == null) return "";
  const c = coverAfter(r, qty);
  const where = c < r.min_cover ? `still under the ${r.min_cover}-week minimum` : c > r.max_cover ? `over the ${r.max_cover}-week maximum` : "inside the window";
  return `With ${fmt(qty)} units: <b>${c.toFixed(1)} wks</b> of cover, ${where}.`;
}

function detailHTML(r) {
  const flags = [...r.exception_reasons, ...r.review_flags];
  const reasons = state.meta.decision_reasons.map(x => `<option>${esc(x)}</option>`).join("");
  const ai = state.briefs[r.id];
  const aiOn = state.status?.switches?.find?.(s => s.name === "ai_assistant_enabled")?.enabled;
  return `<div class="detail">
    <div>
      ${r.units ? `<h3>Last ${r.units.length} weeks</h3>${spark(r)}` : ""}
      <h3>How the rules decided</h3><ol>${r.rule_trace.map(t => `<li>${esc(t)}</li>`).join("")}</ol>
    </div>
    <div>
      ${flags.length ? `<h3>Why this needs your eye</h3><ul>${flags.map(f => `<li>${esc(f)}</li>`).join("")}</ul>`
        : `<h3>No flags</h3><p class="note">Everything sits clearly inside the documented rules.</p>`}
      ${r.qty_to_min_cover != null ? `<p class="hint">To reach ${r.min_cover} weeks: <b>${fmt(r.qty_to_min_cover)}</b> units. Middle of the window: <b>${fmt(r.qty_to_mid_cover)}</b>.</p>` : ""}
      ${canDecide() ? `<div class="editbox">
        <label>Order qty <input type="number" min="1" step="1" id="q-${r.sku_id}" data-live="${r.sku_id}" value="${draftQty(r) || ""}"></label>
        <label>Reason <select id="rs-${r.sku_id}">${reasons}</select></label>
        <button class="btn" data-edit="${r.sku_id}">Save change</button>
        <button class="btn" data-reject="${r.sku_id}">Reject</button>
        ${r.current_decision ? "" : `<button class="btn ink" data-approve="${r.sku_id}">Approve ${fmt(r.recommended_qty)}</button>`}
      </div>
      <p class="live ${liveClass(r, draftQty(r))}" id="live-${r.sku_id}" aria-live="polite">${liveText(r, draftQty(r))}</p>` : ""}
      ${r.needs_review && aiOn && canDecide() ? `<div>
        <button class="btn" data-brief="${r.id}" style="margin-top:12px">${ai ? "Ask again" : "✦ Get AI brief"}</button>
        ${ai ? `<div class="ai">${ai}</div>` : ""}</div>` : ""}
    </div></div>`;
}

function onQtyInput(input) {
  const r = state.recs.find(x => x.sku_id === input.dataset.live);
  const qty = Number(input.value) || 0;
  state.typed[r.sku_id] = qty;
  const cell = $(`#row-${r.sku_id} .strip-cell`);
  if (cell) cell.innerHTML = strip(r, qty);
  const live = $(`#live-${r.sku_id}`);
  live.innerHTML = liveText(r, qty);
  live.className = "live " + liveClass(r, qty);
}
document.addEventListener("input", e => { if (e.target.dataset?.live) onQtyInput(e.target); });

const RANK = {EXCEPTION: 0, REORDER: 1, PAUSE: 2, HOLD: 3};
const urgency = r => [RANK[shown(r).decision] ?? 4, r.needs_review ? 0 : 1, (r.weeks_cover ?? 0) / (r.min_cover || 1)];
const byUrgency = (a, b) => { const x = urgency(a), y = urgency(b); return x[0] - y[0] || x[1] - y[1] || x[2] - y[2]; };

function rowHTML(r) {
  const s = shown(r);
  const typed = state.open.has(r.sku_id) && state.typed[r.sku_id] != null ? state.typed[r.sku_id] : undefined;
  return `<div class="line">
      <div><div class="sku-name">${esc((r.sku_name || r.sku_id).replace("Freshroots ", ""))}</div>
        <div class="meta">${esc(r.sku_id)} · ${fmt(r.stock_on_hand)} in stock${r.open_po_units ? ` · ${fmt(r.open_po_units)} on order` : ""}${r.perishability === "High" ? ' · <span class="perish">perishable</span>' : ""}${r.needs_review ? ' <span class="review">needs review</span>' : ""}</div></div>
      <div class="strip-cell">${strip(r, typed)}</div>
      <div><span class="dec ${s.decision}">${s.decision.toLowerCase()}</span><div class="qty">${qtyText(s.decision, s.qty)}</div></div>
      <div class="act">${actHTML(r)}</div>
    </div>
    ${state.open.has(r.sku_id) ? detailHTML(r) : ""}`;
}

function renderHero() {
  const run = state.run;
  const d = new Date(run.run_date + "T00:00:00");
  const label = d.toLocaleDateString("en-GB", {weekday: "short", day: "numeric", month: "short"});
  $("#title").innerHTML = splitTitle(label);
  $("#title").setAttribute("aria-label", `Planning run, ${d.toLocaleDateString("en-GB", {weekday: "long", day: "numeric", month: "long"})}`);
  document.title = `Freshroots planning run – ${run.run_date}`;
  const uplift = Math.round((run.config.festival_uplift || 0) * 100);
  $("#festSticker").textContent = `Festival month · +${uplift}%`;
  $("#festSticker").classList.toggle("hidden", !run.festival_month);
  const win = run.config.cover_windows || {};
  marquee($("#bandA"), ["Mon + Thu · 06:00", "Recommends", "You decide", "Drafts only", "Freshroots"]);
  marquee($("#bandB"), [`${win.Low?.min_weeks ?? 6}–${win.Low?.max_weeks ?? 14} wks shelf-stable`, `${win.High?.min_weeks ?? 3}–${win.High?.max_weeks ?? 4} wks perishable`,
    run.festival_month ? `Festival +${uplift}%` : "No festival uplift", `${run.run_date}`]);
  marquee($("#footBand"), ["The system recommends.", "Humans decide.", "Nothing reaches Zoho until you say so.", "Freshroots planner, run " + run.run_date + "."]);
}

function renderTicker() {
  const run = state.run, rs = state.recs;
  if (!run) return;
  const units = rs.reduce((a, r) => a + (r.recommended_qty || 0), 0);
  const review = rs.filter(r => r.needs_review).length;
  const decided = rs.filter(r => r.current_decision).length;
  const f = state.status?.data_freshness || {};
  marquee($("#ticker"), [
    `Run ${run.run_date}`, `${rs.length} SKUs`, `${fmt(units)} units proposed`, `${review} need your eye`,
    `${decided}/${rs.length} decided`, run.festival_month ? `Festival month +${Math.round(run.config.festival_uplift * 100)}%` : "No festival uplift",
    f.latest_inventory_date ? `Stock as of ${f.latest_inventory_date}` : "Stock date unknown",
    state.status?.planner_enabled === false ? "Planner paused" : "Planner live", "Nothing hits Zoho till you say so",
  ]);
}

function render(only) {
  const run = state.run, rs = state.recs;
  $("#paused").classList.toggle("hidden", !state.status || state.status.planner_enabled);
  const plannerSw = state.status?.switches?.find?.(s => s.name === "planner_enabled");
  $("#pausedWhy").textContent = plannerSw ? `${plannerSw.updated_by}: ${plannerSw.reason}` : "";
  if (!run) { $("#list").innerHTML = `<p class="empty">No planning runs yet.</p>`; return; }

  $("#sub").innerHTML = run.status === "failed"
    ? `This run stopped on data errors, so it made no recommendations. ${esc(run.error || "")}`
    : `<mark>${rs.length} SKUs</mark> checked against Raghav’s reorder rules. Nothing reaches Zoho until you approve it, and then only as a draft.`;
  $("#held").classList.toggle("hidden", run.status !== "held");
  $("#heldWhy").textContent = (run.hold_reasons || []).join("; ") + " Nobody can act on it until an admin releases it.";
  $("#releaseBtn").classList.toggle("hidden", !isAdmin());

  const basis = run.trend_basis, other = basis === "unadjusted" ? "adjusted" : "unadjusted";
  document.querySelectorAll(".seg button").forEach(b => b.setAttribute("aria-pressed", b.dataset.view === state.view));
  $("#basisNote").textContent = state.view === "run"
    ? `Demand compared with the ${basis === "unadjusted" ? "normal" : "festival-raised"} baseline`
    : `Comparison: the ${other === "adjusted" ? "festival-raised" : "normal"} baseline. Read only`;

  const vis = rs.filter(r => state.filter === "all" || (state.filter === "review" ? r.needs_review : shown(r).decision === state.filter)).sort(byUrgency);
  const count = k => k === "all" ? rs.length : k === "review" ? rs.filter(r => r.needs_review).length : rs.filter(r => shown(r).decision === k).length;
  const pending = rs.filter(r => !r.current_decision).length;
  const units = rs.reduce((a, r) => a + (shown(r).qty || 0), 0);
  const tiles = [["all", "All SKUs", `${pending} awaiting you`], ["review", "Need review", "flagged by the rules"],
    ["REORDER", "Reorder", `${fmt(units)} units`], ["PAUSE", "Pause", "skip next order"], ["HOLD", "Hold", "inside window"],
    ...(count("EXCEPTION") ? [["EXCEPTION", "Exception", "you decide"]] : [])];
  if ($("#tally").children.length !== tiles.length) {
    $("#tally").innerHTML = tiles.map(([k], i) => `<button class="tile" data-filter="${k}" style="--i:${i}"><b data-count="0">0</b><span><span class="lbl"></span><span class="sub"></span></span></button>`).join("");
  }
  tiles.forEach(([k, l, s], i) => {
    const t = $("#tally").children[i];
    t.dataset.filter = k; t.setAttribute("aria-pressed", state.filter === k);
    t.querySelector(".lbl").textContent = l; t.querySelector(".sub").textContent = s;
    countUp(t.querySelector("b"), count(k));
  });

  if (only && $(`#row-${only}`) && vis.some(r => r.sku_id === only)) {
    const r = rs.find(x => x.sku_id === only), row = $(`#row-${only}`);
    row.innerHTML = rowHTML(r);
    row.className = `row ${shown(r).decision}${state.open.has(only) ? " open" : ""}`;
  } else {
    const key = `${run.id}|${state.filter}|${state.view}`, enter = key !== state.listKey ? " enter" : "";
    state.listKey = key;
    $("#list").innerHTML = vis.length
      ? vis.map((r, i) => `<div class="row${enter} ${shown(r).decision}${state.open.has(r.sku_id) ? " open" : ""}" id="row-${r.sku_id}" style="--i:${i}">${rowHTML(r)}</div>`).join("")
      : `<p class="empty">No SKUs in this view.</p>`;
  }

  const lines = rs.filter(r => ["approve", "edit"].includes(r.current_decision?.action) && r.current_decision.qty > 0);
  const zohoTag = d => d.zoho_po_id ? ` <span class="note">in Zoho · ${esc(d.zoho_po_id)}</span>`
    : d.zoho_status === "failed" ? ` <span class="note">Zoho failed → CSV</span>` : d.zoho_status === "csv_only" ? ` <span class="note">no vendor → CSV</span>` : "";
  const total = lines.reduce((a, r) => a + r.current_decision.qty, 0);
  $("#po").innerHTML = lines.length
    ? lines.map(r => `<tr><td>${esc(r.sku_name.replace("Freshroots ", ""))}${zohoTag(r.current_decision)}</td><td>${fmt(r.current_decision.qty)}</td></tr>`).join("")
      + `<tr><td>Total units</td><td>${fmt(total)}</td></tr>`
    : `<tr><td class="note">Approve a reorder line and it lands here.</td></tr>`;
  countUp($("#topUnits"), total);
  $("#savePo").href = `/api/runs/${run.id}/export/zoho.csv`;
  const unsent = lines.filter(r => !r.current_decision.zoho_po_id);
  const zohoOn = state.status?.zoho_api_configured && state.status?.switches?.find?.(s => s.name === "zoho_api_enabled")?.enabled;
  $("#savePo").classList.toggle("hidden", !unsent.length || run.status !== "completed");
  $("#pushZoho").classList.toggle("hidden", !zohoOn || !unsent.length || !canDecide());
  if (!$("#zohoMsg").dataset.sticky) $("#zohoMsg").textContent = zohoOn ? "" : "Zoho API is off: download the CSV and import it into Zoho.";
  $("#saveLog").href = `/api/runs/${run.id}/export/decisions.csv`;

  const checks = (run.data_checks || []).filter(c => c.level === "warn");
  $("#checks").innerHTML = checks.length ? checks.map(c => `<li>${esc(c.message)}</li>`).join("") : `<li>No data warnings on this run.</li>`;
  renderTicker();
}

function renderPrePo() {
  const alerts = Array.isArray(state.status?.open_alerts) ? state.status.open_alerts.filter(a => a.kind === "pre_po") : [];
  $("#prepo").innerHTML = alerts.map(a => `<div class="hazard prepo" role="alert"><div class="hazard-box">
    <b>Check before releasing.</b> ${esc(a.message)}
    <button class="btn ink" data-resolve="${a.id}">Checked, dismiss</button></div></div>`).join("");
}

function renderStatus() {
  const s = state.status;
  renderPrePo();
  if (!s) return;
  if (s.error) { $("#status").innerHTML = `<p class="error">Status unavailable: ${esc(s.error)}. Treat the planner as paused.</p>`; return; }
  const sw = Array.isArray(s.switches)
    ? s.switches.map(x => `<div class="sw"><span>${esc(SWITCH_LABELS[x.name] || x.name)}</span>
        <span><span class="pill ${x.enabled ? "on" : "off"}">${x.enabled ? "on" : "off"}</span>${isAdmin() ? `<button class="btn" data-switch="${x.name}" data-to="${!x.enabled}">${x.enabled ? "Turn off" : "Turn on"}</button>` : ""}</span>
        ${x.updated_by && x.updated_by !== "install" ? `<span class="why">${esc(x.updated_by)}: ${esc(x.reason)}</span>` : ""}</div>`).join("")
    : `<p class="error">Switches unreadable (${esc(s.switches?.error)}): everything counts as OFF.</p>`;
  const f = s.data_freshness || {};
  const fresh = f.error ? `<div class="stat-block bad">Freshness unknown: ${esc(f.error)}</div>`
    : `<div class="stat-block${f.fresh ? "" : " bad"}">Stock as of ${esc(f.latest_inventory_date || "–")} (${f.inventory_age_days ?? "–"} days old). Sales to the week of ${esc(f.latest_sales_week_start || "–")}. ${f.fresh ? "Fresh enough to plan." : "Too old to plan today."}</div>`;
  const last = s.last_run && !s.last_run.error
    ? `<div class="stat-block${s.last_run.status === "failed" ? " bad" : ""}">Last run ${esc(s.last_run.run_date)}, ${esc(s.last_run.status)}${s.last_run.finished_at ? ` at ${esc(s.last_run.finished_at.slice(11, 16))}` : ""}.</div>` : "";
  const alerts = Array.isArray(s.open_alerts) && s.open_alerts.length
    ? `<h3>Open alerts</h3>` + s.open_alerts.slice(0, 5).map(a => `<p class="alert-item"><span class="pill ${a.severity === "critical" ? "off" : "on"}">${esc(a.severity)}</span> ${esc(a.message)}</p>`).join("") : "";
  $("#status").innerHTML = sw + last + fresh + alerts + (isAdmin() ? "" : `<p class="note">Only switch admins can flip switches.</p>`);
  renderTicker();
}

// ---------- actions ----------
async function decide(sku, body, origin) {
  const r = state.recs.find(x => x.sku_id === sku);
  if (!state.user) { showError("Type your name in “You are” before deciding."); $("#who").focus(); return; }
  try {
    const out = await api(`/recommendations/${r.id}/decision`, {method: "POST", body: JSON.stringify(body)});
    Object.assign(r, {...out.detail, ...out});
    state.open.delete(sku);
    delete state.typed[sku];
    state.fresh.add(sku);
    showError("");
    render(sku);
    state.fresh.delete(sku);
    const stamp = $(`#row-${sku} .stamp`);
    const b = (stamp || origin)?.getBoundingClientRect();
    if (b && body.action !== "reject") burst(b.left + b.width / 2, b.top + b.height / 2);
  } catch (e) { showError(`${r.sku_id}: ${e.message}`); }
}

let pendingSwitch = null;
function askReason(title, note, onOk) {
  pendingSwitch = onOk;
  $("#dlgTitle").textContent = title; $("#dlgNote").textContent = note;
  $("#dlgReason").value = ""; $("#dlgErr").classList.add("hidden");
  $("#switchDlg").showModal();
}
$("#switchForm").addEventListener("submit", async e => {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  try { await pendingSwitch($("#dlgReason").value.trim()); $("#switchDlg").close(); }
  catch (err) { $("#dlgErr").textContent = err.message; $("#dlgErr").classList.remove("hidden"); }
});

document.addEventListener("click", async e => {
  const t = e.target.closest("button"); if (!t || t.closest("dialog")) return;
  if (t.dataset.filter) { state.filter = t.dataset.filter; render(); }
  else if (t.dataset.view) { state.view = t.dataset.view; state.open.clear(); render(); }
  else if (t.dataset.toggle) { const id = t.dataset.toggle; state.open.has(id) ? state.open.delete(id) : state.open.add(id); render(id); }
  else if (t.dataset.approve) { await decide(t.dataset.approve, {action: "approve"}, t); }
  else if (t.dataset.edit) {
    const id = t.dataset.edit, qty = Number($("#q-" + id).value);
    if (!Number.isInteger(qty) || qty < 1) { showError(`${id}: enter a whole number of 1 or more, or reject the line.`); return; }
    await decide(id, {action: "edit", qty, reason: $("#rs-" + id).value}, t);
  }
  else if (t.dataset.reject) { await decide(t.dataset.reject, {action: "reject", reason: $("#rs-" + t.dataset.reject).value}, t); }
  else if (t.dataset.resolve) {
    if (!state.user) { showError("Type your name in “You are” first."); return; }
    try { await api(`/alerts/${t.dataset.resolve}/resolve`, {method: "POST"}); await loadStatus(); }
    catch (err) { showError(err.message); }
  }
  else if (t.dataset.brief) { await getBrief(Number(t.dataset.brief), t); }
  else if (t.id === "pushZoho") {
    t.disabled = true; t.textContent = "Sending…";
    try {
      const r = await api(`/runs/${state.run.id}/push-to-zoho`, {method: "POST"});
      $("#zohoMsg").textContent = `${r.created.length} line(s) created as Zoho drafts. ${r.csv_only.length} without a vendor and ${r.failed.length} failed: those stay in the CSV.`;
    } catch (err) { $("#zohoMsg").textContent = err.message; }
    $("#zohoMsg").dataset.sticky = "1";
    t.disabled = false; t.textContent = "Send drafts to Zoho";
    await loadRun(state.run.id); await loadStatus();
  }
  else if (t.dataset.switch) {
    const name = t.dataset.switch, on = t.dataset.to === "true";
    askReason(`${on ? "Turn on" : "Turn off"} ${SWITCH_LABELS[name] || name}`,
      on ? "Only turn this back on once the cause is understood." : "This takes effect immediately for everyone.",
      async reason => { await api(`/switches/${name}`, {method: "POST", body: JSON.stringify({enabled: on, reason})}); await loadStatus(); });
  }
  else if (t.id === "releaseBtn") {
    askReason("Release this run", "Planners will be able to decide on it and export the PO.",
      async reason => { await api(`/runs/${state.run.id}/release`, {method: "POST", body: JSON.stringify({reason})}); await loadRuns(state.run.id); await loadStatus(); });
  }
});

async function getBrief(recId, btn) {
  btn.disabled = true; btn.textContent = "Thinking…";
  const r = state.recs.find(x => x.id === recId);
  try {
    state.briefs[recId] = briefHTML(await api(`/recommendations/${recId}/brief`, {method: "POST"}));
  } catch (e) {
    state.briefs[recId] = `<p>${esc(e.message)}</p><p class="note">Review the flags above and decide from the data.</p>`;
  }
  render(r.sku_id);
}

function briefHTML(b) {
  if (b.status !== "ok") {
    const why = {invalid_json: "the reply was not valid", grounding_failed: `it quoted figures not in the data (${esc((b.grounding?.ungrounded || []).join(", "))})`,
      injection_detected: "the SKU data contains instruction-like text, so it was not sent", error: esc(b.error || "the AI service failed")}[b.status] || esc(b.status);
    return `<p>No usable brief: ${why}.</p><p class="note">Review the flags above and decide from the data.</p>`;
  }
  const x = b.brief, q = x.suggested_qty_range;
  return `<p><b>What’s unusual.</b> ${esc(x.what_is_unusual)}</p>
    <ul>${(x.drivers || []).map(d => `<li>${esc(d.driver)} <span class="note">(${d.basis === "supported_by_data" ? "supported by data" : "hypothesis"}${d.evidence ? ": " + esc(d.evidence) : ""})</span></li>`).join("")}</ul>
    <p><b>Suggested quantity.</b> ${q && typeof q === "object" ? `${fmt(q.low)}–${fmt(q.high)} units. <span class="note">${esc(q.calculation)}</span>` : "Not enough data to suggest one."}</p>
    <p><b>Check before deciding.</b> ${(x.planner_should_verify || []).map(esc).join("; ")}</p>
    <p class="note">Confidence: ${esc(x.confidence)}. Advisory only; the call is yours. Model ${esc(b.model)}.</p>`;
}

$("#runPick").addEventListener("change", e => { state.drawn.clear(); loadRun(e.target.value).catch(err => showError(err.message)); });

(async () => {
  try {
    state.meta = await api("/meta");
    await loadStatus();
    await loadRuns();
  } catch (e) { showError(`Could not load the planner: ${e.message}`); }
  setTimeout(() => document.body.classList.add("settled"), 1600);  // the entrance plays once, not on every re-render
  setInterval(loadStatus, 60000);
})();
