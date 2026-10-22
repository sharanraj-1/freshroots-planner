// Freshroots planner workbench. Reads and writes through the planner API; holds no business data itself.
"use strict";

const $ = s => document.querySelector(s);
const fmt = n => n == null ? "–" : Number(n).toLocaleString("en-IN");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const SWITCH_LABELS = {
  planner_enabled: "Planner", scheduled_runs_enabled: "Scheduled runs", ai_assistant_enabled: "AI assistant",
  zoho_api_enabled: "Zoho API", pre_po_check_enabled: "Pre-PO check",
};

const state = {
  meta: {decision_reasons: [], switch_admins: []}, runs: [], run: null, recs: [], status: null,
  view: "run", filter: "all", open: new Set(), briefs: {}, user: "",
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
    const msg = Array.isArray(detail) ? detail.map(d => d.msg).join("; ") : (detail || body || res.statusText);
    throw new Error(msg);
  }
  return body;
}

function showError(msg) {
  $("#err").textContent = msg || "";
  $("#err").classList.toggle("hidden", !msg);
}

// ---------- loading ----------
async function loadRuns(selectId) {
  state.runs = await api("/runs");
  $("#runPick").innerHTML = state.runs.map(r =>
    `<option value="${r.id}">${esc(r.run_date)} · ${esc(r.status)}</option>`).join("") ||
    `<option value="">No runs yet</option>`;
  const pick = selectId || state.runs.find(r => r.status !== "failed")?.id || state.runs[0]?.id;
  if (pick) { $("#runPick").value = pick; await loadRun(pick); } else { render(); }
}

async function loadRun(id) {
  state.open.clear();
  const [run, recs] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/recommendations`)]);
  state.run = run;
  state.recs = recs.map(r => ({...r.detail, ...r}));
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
  ? {decision: r.comparison.decision, qty: r.comparison.recommended_qty, flags: r.comparison.review_flags || []}
  : {decision: r.decision, qty: r.recommended_qty, flags: [...r.exception_reasons, ...r.review_flags]};
const canDecide = () => state.view === "run" && state.run?.status === "completed" && state.status?.planner_enabled;

function strip(r) {
  if (r.weeks_cover == null) return `<span class="note">No cover figure: ${esc(r.exception_reasons.join("; "))}</span>`;
  const S = 16, W = 300, x = v => Math.min(v, S) / S * W;
  const lo = x(r.min_cover), hi = x(r.max_cover), c = x(r.weeks_cover), u = x(r.weeks_cover_unadjusted);
  const post = r.post_order_cover != null
    ? `<line x1="${c}" y1="17" x2="${x(r.post_order_cover)}" y2="17" stroke="var(--short)" stroke-width="3"/><circle cx="${x(r.post_order_cover)}" cy="17" r="3" fill="var(--short)"/>` : "";
  const ticks = [0, 4, 8, 12, 16].map(t => `<text x="${x(t)}" y="34" font-size="9" fill="var(--muted)" text-anchor="${t === 0 ? "start" : t === 16 ? "end" : "middle"}">${t}${t === 16 ? "+ wks" : ""}</text>`).join("");
  return `<svg class="strip" viewBox="0 0 ${W} 36" preserveAspectRatio="xMinYMid meet" role="img" aria-label="${r.weeks_cover} weeks of cover; target ${r.min_cover} to ${r.max_cover}">
    <rect x="${lo}" y="9" width="${Math.max(hi - lo, 2)}" height="16" fill="var(--band)"/>
    ${r.festival ? `<circle cx="${u}" cy="17" r="4.5" fill="none" stroke="var(--muted)" stroke-width="1.5"/>` : ""}
    ${post}
    <circle cx="${c}" cy="17" r="6" fill="var(--ink)"/>
    ${r.weeks_cover > S ? `<text x="${W - 2}" y="8" font-size="10" text-anchor="end" fill="var(--long)" font-weight="700">${r.weeks_cover} wks</text>` : ""}
    ${ticks}</svg>`;
}

function spark(r) {
  const W = 320, H = 90, n = r.units.length, max = Math.max(...r.units, r.adjusted_weekly || 0) * 1.08 || 1;
  const bw = W / n, y = v => H - v / max * (H - 12);
  const bars = r.units.map((u, i) => `<rect x="${i * bw + 1.5}" y="${y(u)}" width="${bw - 3}" height="${H - y(u)}" fill="${i >= n - 4 ? "var(--ink)" : "var(--band)"}"><title>${esc(r.weeks[i])}: ${u} units</title></rect>`).join("");
  return `<svg viewBox="0 0 ${W} ${H + 14}" style="width:100%;max-width:440px" role="img" aria-label="${n} weeks of unit sales, last 4 highlighted">
    ${bars}
    <line x1="0" x2="${W}" y1="${y(r.avg_12w)}" y2="${y(r.avg_12w)}" stroke="var(--long)" stroke-dasharray="4 3"/>
    ${r.festival ? `<line x1="0" x2="${W}" y1="${y(r.adjusted_weekly)}" y2="${y(r.adjusted_weekly)}" stroke="var(--turmeric)" stroke-width="1.5"/>` : ""}
    <text x="0" y="${H + 12}" font-size="9" fill="var(--muted)">${esc(r.weeks[0])}</text>
    <text x="${W}" y="${H + 12}" font-size="9" fill="var(--muted)" text-anchor="end">${esc(r.weeks[n - 1])}</text>
  </svg>
  <p class="note">Dashed: 12-week average (${fmt(r.avg_12w)}/wk). ${r.festival ? `Solid: with festival uplift (${fmt(r.adjusted_weekly)}/wk). ` : ""}Dark bars: last 4 weeks (${fmt(r.avg_4w)}/wk).</p>`;
}

function qtyText(decision, qty) {
  return decision === "REORDER" ? `${fmt(qty)} units` : decision === "PAUSE" ? "skip next order"
    : decision === "HOLD" ? "no order" : "planner decides";
}

function actHTML(r) {
  const d = r.current_decision;
  if (state.view === "compare") return `<span class="compare-tag">Comparison only</span>`;
  if (d && !state.open.has(r.sku_id)) {
    const label = d.action === "approve" ? `Approved${d.qty ? `: ${fmt(d.qty)} units` : ""}`
      : d.action === "edit" ? `Changed to ${fmt(d.qty)} units` : "Rejected";
    return `<div><div class="status-text ${d.action}">${label}</div><div class="who-line">${esc(d.decided_by)}${d.reason ? ` · ${esc(d.reason)}` : ""}</div></div>
      ${canDecide() ? `<button class="btn" data-toggle="${r.sku_id}">Change</button>` : ""}`;
  }
  if (!canDecide()) return `<span class="note">${state.status?.planner_enabled ? "Read only" : "Paused"}</span>`;
  const open = state.open.has(r.sku_id);
  return `<button class="btn primary" data-approve="${r.sku_id}">Approve</button>
    <button class="btn" data-toggle="${r.sku_id}" aria-expanded="${open}">${open ? "Close" : "Review"}</button>`;
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
      ${flags.length ? `<h3>Why this needs your eye</h3><ul class="flags">${flags.map(f => `<li>${esc(f)}</li>`).join("")}</ul>`
        : `<h3>No flags</h3><p class="note">All values sit clearly inside the documented rules.</p>`}
      ${r.decision === "REORDER" ? `<p class="note">After this order: ${r.post_order_cover} weeks of cover. To reach ${r.min_cover} weeks: ${fmt(r.qty_to_min_cover)} units. Mid-window: ${fmt(r.qty_to_mid_cover)} units.</p>` : ""}
      ${canDecide() ? `<div class="editbox">
        <label class="field">Order qty <input type="number" min="1" step="1" id="q-${r.sku_id}" value="${r.recommended_qty || ""}"></label>
        <label class="field">Reason <select id="rs-${r.sku_id}">${reasons}</select></label>
        <button class="btn" data-edit="${r.sku_id}">Save change</button>
        <button class="btn" data-reject="${r.sku_id}">Reject</button>
        ${r.current_decision ? "" : `<button class="btn primary" data-approve="${r.sku_id}">Approve ${fmt(r.recommended_qty)}</button>`}
      </div>` : ""}
      ${r.needs_review && aiOn && canDecide() ? `<div>
        <button class="btn" data-brief="${r.id}" style="margin-top:12px">${ai ? "Ask again" : "Get AI brief"}</button>
        ${ai ? `<div class="ai">${ai}</div>` : ""}</div>` : ""}
    </div></div>`;
}

function rowHTML(r) {
  const s = shown(r);
  return `<div class="line">
      <div><div class="name">${esc((r.sku_name || r.sku_id).replace("Freshroots ", ""))}${r.needs_review ? '<span class="flagdot" title="Needs review"></span>' : ""}</div>
        <div class="meta">${esc(r.sku_id)}, ${fmt(r.stock_on_hand)} in stock${r.open_po_units ? `, ${fmt(r.open_po_units)} on order` : ""}${r.perishability === "High" ? ', <span class="perish">perishable</span>' : ""}</div></div>
      <div class="strip-cell">${strip(r)}</div>
      <div><span class="dec ${s.decision}">${s.decision.charAt(0) + s.decision.slice(1).toLowerCase()}</span><div class="qty">${qtyText(s.decision, s.qty)}</div></div>
      <div class="act">${actHTML(r)}</div>
    </div>
    ${state.open.has(r.sku_id) ? detailHTML(r) : ""}`;
}

function render(only) {
  const run = state.run, rs = state.recs;
  $("#paused").classList.toggle("hidden", !state.status || state.status.planner_enabled);
  const plannerSw = state.status?.switches?.find?.(s => s.name === "planner_enabled");
  $("#pausedWhy").textContent = plannerSw ? `${plannerSw.updated_by}: ${plannerSw.reason}` : "";
  if (!run) { $("#list").innerHTML = `<p class="empty">No planning runs yet.</p>`; return; }

  const d = new Date(run.run_date + "T00:00:00");
  $("#title").textContent = `Planning run, ${d.toLocaleDateString("en-GB", {weekday: "long", day: "numeric", month: "long"})}`;
  document.title = `Freshroots planning run – ${run.run_date}`;
  $("#sub").textContent = run.status === "failed"
    ? `This run stopped on data errors, so it made no recommendations. ${run.error || ""}`
    : `${rs.length} SKUs checked against Raghav’s reorder rules.${run.festival_month ? ` This is a festival month, so the demand baseline is raised ${Math.round((run.config.festival_uplift || 0) * 100)}%.` : ""} Nothing reaches Zoho until you approve it, and then only as a draft.`;

  $("#held").classList.toggle("hidden", run.status !== "held");
  $("#heldWhy").textContent = (run.hold_reasons || []).join("; ") + " Nobody can act on it until an admin releases it.";
  $("#releaseBtn").classList.toggle("hidden", !isAdmin());

  const basis = run.trend_basis, other = basis === "unadjusted" ? "adjusted" : "unadjusted";
  document.querySelectorAll(".seg button").forEach(b => b.setAttribute("aria-pressed", b.dataset.view === state.view));
  $("#basisNote").textContent = state.view === "run"
    ? `This run compares demand with the ${basis === "unadjusted" ? "normal" : "festival-raised"} baseline.`
    : `Showing what the ${other === "adjusted" ? "festival-raised" : "normal"} baseline would give. Read only.`;

  const vis = rs.filter(r => state.filter === "all" || (state.filter === "review" ? r.needs_review : shown(r).decision === state.filter));
  const count = k => k === "all" ? rs.length : k === "review" ? rs.filter(r => r.needs_review).length : rs.filter(r => shown(r).decision === k).length;
  const pending = rs.filter(r => !r.current_decision).length;
  const units = rs.reduce((a, r) => a + (shown(r).qty || 0), 0);
  $("#tally").innerHTML = [["all", "All SKUs", `${pending} awaiting decision`], ["review", "Need review", "flagged by the rules"],
    ["REORDER", "Reorder", `${fmt(units)} units proposed`], ["PAUSE", "Pause", "skip next order"], ["HOLD", "Hold", "inside window"],
    ...(count("EXCEPTION") ? [["EXCEPTION", "Exception", "planner decides"]] : [])]
    .map(([k, l, s]) => `<button data-filter="${k}" aria-pressed="${state.filter === k}"><b>${count(k)}</b>${l}<br><span>${s}</span></button>`).join("");

  if (only && $(`#row-${only}`) && vis.some(r => r.sku_id === only)) {
    const r = rs.find(x => x.sku_id === only);
    $(`#row-${only}`).innerHTML = rowHTML(r);
    $(`#row-${only}`).classList.toggle("open", state.open.has(only));
  } else {
    $("#list").innerHTML = vis.length
      ? vis.map(r => `<div class="row${state.open.has(r.sku_id) ? " open" : ""}" id="row-${r.sku_id}">${rowHTML(r)}</div>`).join("")
      : `<p class="empty">No SKUs in this view.</p>`;
  }

  const lines = rs.filter(r => ["approve", "edit"].includes(r.current_decision?.action) && r.current_decision.qty > 0);
  const zohoTag = d => d.zoho_po_id ? ` <span class="note">in Zoho, draft ${esc(d.zoho_po_id)}</span>`
    : d.zoho_status === "failed" ? ` <span class="note">Zoho failed: CSV</span>` : d.zoho_status === "csv_only" ? ` <span class="note">no vendor: CSV</span>` : "";
  $("#po").innerHTML = lines.length
    ? lines.map(r => `<tr><td>${esc(r.sku_name.replace("Freshroots ", ""))}${zohoTag(r.current_decision)}</td><td>${fmt(r.current_decision.qty)}</td></tr>`).join("")
      + `<tr><td><b>Total units</b></td><td>${fmt(lines.reduce((a, r) => a + r.current_decision.qty, 0))}</td></tr>`
    : `<tr><td class="empty">Approve a reorder line to add it here.</td></tr>`;
  $("#savePo").href = `/api/runs/${run.id}/export/zoho.csv`;
  const unsent = lines.filter(r => !r.current_decision.zoho_po_id);
  const zohoOn = state.status?.zoho_api_configured && state.status?.switches?.find?.(s => s.name === "zoho_api_enabled")?.enabled;
  $("#savePo").classList.toggle("hidden", !unsent.length || run.status !== "completed");
  $("#pushZoho").classList.toggle("hidden", !zohoOn || !unsent.length || !canDecide());
  if (!$("#zohoMsg").dataset.sticky) $("#zohoMsg").textContent = zohoOn ? "" : "Zoho API is off: download the CSV and import it into Zoho.";
  $("#saveLog").href = `/api/runs/${run.id}/export/decisions.csv`;

  const checks = (run.data_checks || []).filter(c => c.level === "warn");
  $("#checks").innerHTML = checks.length ? checks.map(c => `<li>${esc(c.message)}</li>`).join("") : `<li>No data warnings on this run.</li>`;
}

function renderPrePo() {
  const alerts = Array.isArray(state.status?.open_alerts) ? state.status.open_alerts.filter(a => a.kind === "pre_po") : [];
  $("#prepo").classList.toggle("hidden", !alerts.length);
  $("#prepo").innerHTML = alerts.map(a => `<div class="banner prepo" role="alert"><div class="wrap">
    <b>Check before releasing:</b> ${esc(a.message)}
    <button class="btn on-dark" data-resolve="${a.id}">Checked, dismiss</button></div></div>`).join("");
}

function renderStatus() {
  const s = state.status;
  renderPrePo();
  if (!s) return;
  if (s.error) { $("#status").innerHTML = `<p class="error">Status unavailable: ${esc(s.error)}. Treat the planner as paused.</p>`; return; }
  const sw = Array.isArray(s.switches)
    ? s.switches.map(x => `<div class="sw"><span>${esc(SWITCH_LABELS[x.name] || x.name)}</span>
        <span><span class="state ${x.enabled ? "on" : "off"}">${x.enabled ? "On" : "Off"}</span>
        ${isAdmin() ? ` <button class="btn" data-switch="${x.name}" data-to="${!x.enabled}">${x.enabled ? "Turn off" : "Turn on"}</button>` : ""}</span>
        <span class="why">${esc(x.updated_by || "")}${x.reason ? `: ${esc(x.reason)}` : ""}</span></div>`).join("")
    : `<p class="error">Switches unreadable (${esc(s.switches?.error)}): everything is treated as OFF.</p>`;
  const f = s.data_freshness || {};
  const fresh = f.error ? `<div class="stat-block bad">Freshness unknown: ${esc(f.error)}</div>`
    : `<div class="stat-block${f.fresh ? "" : " bad"}">Stock as of ${esc(f.latest_inventory_date || "–")} (${f.inventory_age_days ?? "–"} days old). Sales to the week of ${esc(f.latest_sales_week_start || "–")}. ${f.fresh ? "Fresh enough to plan." : "Too old to plan today."}</div>`;
  const last = s.last_run && !s.last_run.error
    ? `<div class="stat-block${s.last_run.status === "failed" ? " bad" : ""}">Last run: ${esc(s.last_run.run_date)}, ${esc(s.last_run.status)}${s.last_run.finished_at ? ` at ${esc(s.last_run.finished_at.slice(11, 16))}` : ""}.</div>` : "";
  const alerts = Array.isArray(s.open_alerts) && s.open_alerts.length
    ? `<h3>Open alerts</h3>` + s.open_alerts.slice(0, 5).map(a => `<p class="alert-item ${a.severity}"><b>${esc(a.severity)}</b> ${esc(a.message)}</p>`).join("") : "";
  $("#status").innerHTML = sw + last + fresh + alerts + (isAdmin() ? "" : `<p class="note">Only switch admins can change switches.</p>`);
}

// ---------- actions ----------
async function decide(sku, body) {
  const r = state.recs.find(x => x.sku_id === sku);
  if (!state.user) { showError("Enter your name under “You are” before deciding."); $("#who").focus(); return; }
  try {
    const out = await api(`/recommendations/${r.id}/decision`, {method: "POST", body: JSON.stringify(body)});
    Object.assign(r, {...out.detail, ...out});
    state.open.delete(sku);
    showError("");
    render(sku);
    const row = $(`#row-${sku}`);
    if (row) { row.classList.add("just"); setTimeout(() => row.classList.remove("just"), 700); }
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
  else if (t.dataset.approve) { await decide(t.dataset.approve, {action: "approve"}); }
  else if (t.dataset.edit) {
    const id = t.dataset.edit, qty = Number($("#q-" + id).value);
    if (!Number.isInteger(qty) || qty < 1) { showError(`${id}: enter a whole number of 1 or more, or reject the line.`); return; }
    await decide(id, {action: "edit", qty, reason: $("#rs-" + id).value});
  }
  else if (t.dataset.reject) { await decide(t.dataset.reject, {action: "reject", reason: $("#rs-" + t.dataset.reject).value}); }
  else if (t.dataset.resolve) {
    if (!state.user) { showError("Enter your name under “You are” first."); return; }
    try { await api(`/alerts/${t.dataset.resolve}/resolve`, {method: "POST"}); await loadStatus(); }
    catch (e) { showError(e.message); }
  }
  else if (t.dataset.brief) { await getBrief(Number(t.dataset.brief), t); }
  else if (t.id === "pushZoho") {
    t.disabled = true; t.textContent = "Sending…";
    try {
      const r = await api(`/runs/${state.run.id}/push-to-zoho`, {method: "POST"});
      $("#zohoMsg").textContent = `${r.created.length} line(s) created as Zoho drafts. ${r.csv_only.length} without a vendor and ${r.failed.length} failed: those stay in the CSV.`;
    } catch (e) { $("#zohoMsg").textContent = e.message; }
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

// AI briefs arrive in Phase 4; until the endpoint exists the button reports it plainly.
async function getBrief(recId, btn) {
  btn.disabled = true; btn.textContent = "Thinking…";
  const r = state.recs.find(x => x.id === recId);
  try {
    const b = await api(`/recommendations/${recId}/brief`, {method: "POST"});
    state.briefs[recId] = briefHTML(b);
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
  return `<p><b>What is unusual.</b> ${esc(x.what_is_unusual)}</p>
    <ul>${(x.drivers || []).map(d => `<li>${esc(d.driver)} <span class="note">(${d.basis === "supported_by_data" ? "supported by data" : "hypothesis"}${d.evidence ? ": " + esc(d.evidence) : ""})</span></li>`).join("")}</ul>
    <p><b>Suggested quantity.</b> ${q && typeof q === "object" ? `${fmt(q.low)}–${fmt(q.high)} units. <span class="note">${esc(q.calculation)}</span>` : "Not enough data to suggest one."}</p>
    <p><b>Check before deciding.</b> ${(x.planner_should_verify || []).map(esc).join("; ")}</p>
    <p class="note">Confidence: ${esc(x.confidence)}. Advisory only; the decision is yours. Model ${esc(b.model)}.</p>`;
}

$("#runPick").addEventListener("change", e => loadRun(e.target.value).catch(err => showError(err.message)));

(async () => {
  try {
    state.meta = await api("/meta");
    await loadStatus();
    await loadRuns();
  } catch (e) { showError(`Could not load the planner: ${e.message}`); }
  setInterval(loadStatus, 60000);
})();
