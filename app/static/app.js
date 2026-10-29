// FixBridge Repair Assistant UI. Plain JS; all server text is escaped before rendering.
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const STAGES = ["context_preparation", "knowledge_retrieval", "ai_analysis", "safety_validation", "done"];
const STAGE_LABEL = { context_preparation: "Preparing context", knowledge_retrieval: "Searching repair knowledge",
  ai_analysis: "AI analysis", safety_validation: "Evidence & safety checks", done: "Done" };

// Relative URLs let the assistant live below the homeowner panel's own route.
const APP_BASE = new URL(".", window.location.href).pathname.replace(/\/$/, "");
const endpoint = (path) => `${APP_BASE}${path}`;
const embedded = new URLSearchParams(window.location.search).get("embed") === "1";
let token = null; if (!embedded) { try { token = localStorage.getItem("fb_token"); } catch {} }
if (embedded) {
  document.querySelector(".topbar").hidden = true;
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent || event.origin !== window.location.origin) return;
    if (event.data?.type === "fixbridge:authenticate" && typeof event.data.token === "string"
        && event.data.token.length > 0 && event.data.token.length < 8192) saveToken(event.data.token);
    if (event.data?.type === "fixbridge:logout") logout();
  });
  window.parent.postMessage({type: "fixbridge:ready"}, window.location.origin);
}
let currentId = null, pollTimer = null;

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
  if (opts.json) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(endpoint(path), { ...opts, headers });
  if (res.status === 401) { logout(); throw new Error("Please sign in again"); }
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(typeof body.detail === "string" ? body.detail : (body.detail?.message || JSON.stringify(body.detail || body)));
  return body;
}

function saveToken(t) { token = t; if (!embedded) { try { localStorage.setItem("fb_token", t); } catch {} } boot(); }
function logout() { token = null; if (!embedded) { try { localStorage.removeItem("fb_token"); } catch {} } $("#app").classList.add("hidden"); $("#login").classList.toggle("hidden", embedded); $("#who").textContent = ""; }

$("#token-save").onclick = () => saveToken($("#token-input").value.trim());
$("#dev-login").onclick = async () => {
  const fd = new FormData(); fd.append("email", $("#dev-email").value);
  const r = await fetch(endpoint("/api/dev/login"), { method: "POST", body: fd });
  if (!r.ok) { $("#login-error").textContent = "Dev login is disabled (set DEV_AUTH=true)"; return; }
  saveToken((await r.json()).token);
};

async function boot() {
  if (!token) return logout();
  try {
    const me = await api("/api/me");
    $("#who").innerHTML = `${esc(me.email)} <button class="secondary small" id="logout">Sign out</button>`;
    $("#logout").onclick = logout;
    $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
    const cats = await api("/api/service-categories");
    $("#category").innerHTML = cats.map((c) => `<option>${esc(c)}</option>`).join("");
    await loadProperties(); await loadRequests();
  } catch (e) { $("#login-error").textContent = e.message; }
}

async function loadProperties() {
  const props = await api("/api/properties");
  $("#property").innerHTML = props.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("") || `<option value="">Add a property first</option>`;
  await loadEquipment();
}
async function loadEquipment() {
  const pid = $("#property").value; const sel = $("#equipment");
  sel.innerHTML = `<option value="">— Not sure / not listed —</option>`;
  if (!pid) return;
  const { equipment } = await api(`/api/properties/${pid}`);
  sel.innerHTML += equipment.map((e) => `<option value="${esc(e.id)}">${esc(e.kind)} ${esc(e.brand || "")} ${esc(e.model_number || "")}</option>`).join("");
}
$("#property").onchange = loadEquipment;

$("#property-form").onsubmit = async (ev) => {
  ev.preventDefault(); const f = new FormData(ev.target);
  await api("/api/properties", { method: "POST", json: { name: f.get("name"), property_type: f.get("property_type"), year_built: f.get("year_built") ? +f.get("year_built") : null } });
  ev.target.reset(); await loadProperties();
};

async function loadRequests() {
  const list = await api("/api/repair-requests");
  $("#request-list").innerHTML = list.map((r) => `<li data-id="${esc(r.id)}" class="${r.id === currentId ? "active" : ""}">
    <span class="badge ${esc(r.status)}">${esc(r.status.replaceAll("_", " "))}</span>
    <div class="desc">${esc(r.description)}</div><div class="muted">${esc(r.property)} · ${new Date(r.created_at).toLocaleString()}</div></li>`).join("") || `<li class="muted">No requests yet</li>`;
  document.querySelectorAll("#request-list li[data-id]").forEach((li) => (li.onclick = () => openRequest(li.dataset.id)));
}

$("#new-request").onclick = () => { currentId = null; clearTimeout(pollTimer); $("#result").classList.add("hidden"); $("#new").classList.remove("hidden"); loadRequests(); };

// Phase 1 -> submit repair request
$("#request-form").onsubmit = async (ev) => {
  ev.preventDefault(); $("#form-error").textContent = ""; $("#submit-btn").disabled = true;
  try {
    const fd = new FormData(ev.target);
    if (!fd.get("equipment_id")) fd.delete("equipment_id");
    const req = await api("/api/repair-requests", { method: "POST", body: fd, headers: { "Idempotency-Key": crypto.randomUUID() } });
    ev.target.reset(); await loadEquipment(); openRequest(req.id);
  } catch (e) { $("#form-error").textContent = e.message; }
  finally { $("#submit-btn").disabled = false; }
};

async function openRequest(id) {
  currentId = id; clearTimeout(pollTimer);
  $("#new").classList.add("hidden"); $("#result").classList.remove("hidden");
  const req = await api(`/api/repair-requests/${id}`);
  render(req); loadRequests();
  if (["received", "processing"].includes(req.status)) pollTimer = setTimeout(() => openRequest(id), 1500);
}

function progress(stage, status) {
  const idx = STAGES.indexOf(stage);
  return `<div class="progress">${STAGES.map((s, i) => `<span class="${i < idx || status !== "processing" && i <= idx ? "done" : i === idx ? "now" : ""}">${STAGE_LABEL[s]}</span>`).join("")}</div>`;
}

function cites(refs) { return (refs || []).map((r) => `<span class="cite">${esc(r)}</span>`).join(""); }

function sourcesBlock(sources) {
  if (!sources?.length) return "";
  return `<h4>Sources</h4><table><tr><th>Ref</th><th>Document</th><th>Section</th><th>Page</th><th>Version</th></tr>${sources.map((s) =>
    `<tr><td>${esc(s.ref)}</td><td>${esc(s.title)}</td><td>${esc(s.section)}</td><td>${esc(s.page ?? "")}</td><td>${esc(s.version)}</td></tr>`).join("")}</table>`;
}

function causesBlock(r) {
  if (!r.likely_causes?.length) return "";
  return `<h4>Likely causes</h4><ul>${r.likely_causes.map((c) => `<li><b>${esc(c.cause)}</b> — ${Math.round(c.likelihood * 100)}% ${cites(c.evidence_refs)}<div class="muted">${esc(c.reasoning || "")}</div></li>`).join("")}</ul>`;
}

function warningsBlock(safety, withChecks) {
  return (safety?.warnings || []).map((w) => `<div class="banner ${w.severity === "advisory" || w.severity === "warning" ? "warn" : "danger"}">
    <h4>${withChecks && w.severity !== "advisory" ? `<input type="checkbox" class="ack" value="${esc(w.code)}">` : ""}⚠ ${esc(w.title)}</h4>
    <div>${esc(w.message)}</div><div class="muted">Risks: ${esc(w.risks.join(", "))}</div></div>`).join("");
}

function proBlock(p) {
  if (!p) return "";
  return `<h4>Recommended next step</h4><p>Contact a <b>${esc(p.recommended_trade)}</b> (urgency: <b>${esc(p.urgency)}</b>).</p>
    <ul>${p.safe_actions_now.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
    ${p.book_via_fixbridge ? `<p class="muted">Contact a qualified local professional to arrange service. Online booking is not available in this demo.</p>` : ""}`;
}

function planBlock(plan) {
  const ce = plan.cost_estimate;
  return `
    <div class="grid2">
      <div><h4>Estimated parts cost</h4><div class="cost">${ce.high > 0 ? `$${ce.low.toFixed(2)} – $${ce.high.toFixed(2)}` : "No parts needed"}</div>
        <ul class="muted">${ce.assumptions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul></div>
      <div><h4>At a glance</h4><p>Skill: <b>${esc(plan.skill_level)}</b><br>Time: <b>${plan.estimated_time_minutes ? esc(plan.estimated_time_minutes) + " min" : "varies"}</b><br>Confidence: <b>${Math.round(plan.confidence * 100)}%</b></p></div>
    </div>
    ${plan.prerequisites.length ? `<h4>Before you start</h4><ul>${plan.prerequisites.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
    <div class="grid2">
      ${plan.tools.length ? `<div><h4>Tools</h4><ul>${plan.tools.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
      ${plan.materials.length ? `<div><h4>Materials</h4><ul>${plan.materials.map((t) => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
    </div>
    <h4>Steps</h4><ol class="steps">${plan.steps.map((s) => `<li><b>${esc(s.title)}</b> ${cites(s.sources)} ${s.verified ? "" : `<span class="unverified">general guidance</span>`}<div>${esc(s.detail)}</div></li>`).join("")}</ol>
    ${plan.parts.length ? `<h4>Compatible parts</h4><table><tr><th>Part</th><th>Product</th><th>Price</th><th>Fit</th></tr>${plan.parts.flatMap((p) => p.options.length ? p.options.map((o) =>
      `<tr><td>${esc(p.part_type)} ×${p.quantity}</td><td>${esc(o.name)}<div class="muted">${esc(o.sku)}</div></td><td>$${o.price.toFixed(2)} ${o.price_verified ? "" : "<span class='unverified'>(unverified)</span>"}${o.live_market_price ? "<div class='muted'>live market price</div>" : ""}</td><td>${o.model_specific ? "Exact model" : o.live_market_price ? "Check fit" : "Universal"}</td></tr>`) :
      [`<tr><td>${esc(p.part_type)}</td><td colspan="3" class="muted">No compatible product found</td></tr>`]).join("")}</table>` : ""}
    <p><button class="secondary small" id="refresh-prices">Get current prices</button> <span id="price-msg" class="muted"></span></p>
    <h4>Limitations</h4><ul class="muted">${plan.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
}

function render(req) {
  const r = req.result;
  let html = `<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><h2>${esc(req.service_category)} request</h2><span class="badge ${esc(req.status)}">${esc(req.status.replaceAll("_", " "))}</span></div>
    <p class="muted">${esc(req.description)}</p><div class="thumbs" id="thumbs"></div>`;

  if (["received", "processing"].includes(req.status)) {
    html += `<p><span class="spinner"></span>${esc(req.stage === "waiting_retry" ? "Our AI service is busy — retrying automatically…" : "Analysing your request…")}</p>${progress(req.stage, req.status)}`;
  } else if (r) {
    html += `<div class="muted">Version ${r.version}${r.provider ? ` · ${esc(r.provider)} ${esc(r.model || "")}` : ""}${r.confidence != null ? ` · confidence ${Math.round(r.confidence * 100)}%` : ""}</div>`;
    if (r.summary) html += `<p><b>${esc(r.summary)}</b></p>`;
    switch (r.outcome) {
      case "diy_plan":
        if (r.safety?.warnings?.length) html += warningsBlock(r.safety, false);
        html += causesBlock(r) + planBlock(r.repair_plan) + sourcesBlock(r.sources); break;
      case "safety_warning":
        html += `<div class="banner warn"><h4>Please review these safety warnings</h4>Tick each warning to confirm you understand it, then choose how to proceed.</div>`
          + warningsBlock(r.safety, true) + causesBlock(r)
          + `<div class="row"><button id="continue">I understand — continue with DIY</button><button id="cancel" class="secondary">Stop — I'd rather get a professional</button></div><p id="choice-error" class="error"></p>`;
        break;
      case "clarification":
        html += `<div class="banner warn"><h4>We need a bit more information</h4>Round ${r.clarification.round} of ${r.clarification.max_rounds}.</div>
          <form id="clarify-form">${r.clarification.questions.map((q) => `<label>${esc(q.question)}<div class="muted">${esc(q.reason)}</div>
          ${q.kind === "photo" ? `<input type="file" name="files" multiple accept="image/*,video/*,application/pdf"><input name="${esc(q.id)}" placeholder="Optional note">`
            : q.kind === "choice" ? `<select name="${esc(q.id)}"><option value="">—</option>${q.choices.map((c) => `<option>${esc(c)}</option>`).join("")}</select>`
            : `<input name="${esc(q.id)}">`}</label>`).join("")}
          <button>Send answers</button><p id="clarify-error" class="error"></p></form>` + causesBlock(r);
        break;
      case "professional": case "cancelled":
        html += `<div class="banner danger"><h4>${r.safety?.emergency ? "This may be an emergency" : r.outcome === "cancelled" ? "DIY stopped" : "We recommend a professional for this repair"}</h4></div>`
          + warningsBlock(r.safety, false) + proBlock(r.professional_recommendation)
          + (r.limitations?.length ? `<ul class="muted">${r.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : "")
          + causesBlock(r) + sourcesBlock(r.sources); break;
      case "failure":
        html += `<div class="banner danger"><h4>Diagnosis unavailable</h4>${esc(r.summary)}</div>`; break;
    }
    // Phase 9: outcome
    if (["completed", "cancelled", "blocked"].includes(req.status)) {
      html += req.outcome ? `<div class="banner ok">Thanks! Outcome recorded: <b>${esc(req.outcome.resolution.replaceAll("_", " "))}</b> (${esc(req.outcome.label.replace("_", " "))}).</div>` :
        `<h4>How did it go?</h4><form id="outcome-form" class="row">
          <label>Result<select name="resolution"><option value="fixed_diy">Fixed it myself</option><option value="fixed_professional">A professional fixed it</option><option value="not_fixed">Not fixed</option><option value="abandoned">Gave up</option></select></label>
          <label>Rating<select name="rating"><option value="">—</option>${[5, 4, 3, 2, 1].map((n) => `<option>${n}</option>`).join("")}</select></label>
          <label>Parts used<input name="parts" placeholder="e.g. 2in flapper"></label>
          <label>Total cost ($)<input name="cost" type="number" step="0.01" min="0"></label>
          <label style="grid-column:1/-1">Feedback<textarea name="feedback" rows="2"></textarea></label>
          <button>Submit outcome</button></form>`;
    }
  } else if (req.status === "failed") {
    html += `<div class="banner danger">${esc(req.error || "Diagnosis failed")}</div>`;
  }
  $("#result").innerHTML = html;
  bindResult(req); loadThumbs(req);
}

async function loadThumbs(req) {
  const imgs = req.media.filter((m) => m.kind === "image").slice(0, 8);
  for (const m of imgs) {
    try {
      const res = await fetch(endpoint(`/api/media/${m.id}?preview=true`), { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) continue;
      const img = document.createElement("img"); img.alt = m.filename; img.src = URL.createObjectURL(await res.blob());
      $("#thumbs")?.appendChild(img);
    } catch {}
  }
}

function bindResult(req) {
  const id = req.id;
  const choose = async (choice) => {
    const acks = [...document.querySelectorAll(".ack:checked")].map((c) => c.value);
    try { render(await api(`/api/repair-requests/${id}/safety-choice`, { method: "POST", json: { choice, acknowledged_warning_codes: acks } })); loadRequests(); }
    catch (e) { $("#choice-error").textContent = e.message; }
  };
  if ($("#continue")) $("#continue").onclick = () => choose("continue");
  if ($("#cancel")) $("#cancel").onclick = () => choose("cancel");

  const rp = $("#refresh-prices");
  if (rp) rp.onclick = async () => {
    rp.disabled = true; $("#price-msg").textContent = "Checking current prices…";
    try { render(await api(`/api/repair-requests/${id}/refresh-prices`, { method: "POST" })); }
    catch (e) { $("#price-msg").textContent = e.message; rp.disabled = false; }
  };

  const cf = $("#clarify-form");
  if (cf) cf.onsubmit = async (ev) => {
    ev.preventDefault(); const src = new FormData(cf); const fd = new FormData(); const answers = {};
    for (const [k, v] of src.entries()) { if (k === "files") { if (v.size) fd.append("files", v); } else if (v) answers[k] = v; }
    fd.append("answers", JSON.stringify(answers));
    try { await api(`/api/repair-requests/${id}/clarifications`, { method: "POST", body: fd }); openRequest(id); }
    catch (e) { $("#clarify-error").textContent = e.message; }
  };

  const of = $("#outcome-form");
  if (of) of.onsubmit = async (ev) => {
    ev.preventDefault(); const f = new FormData(of);
    await api(`/api/repair-requests/${id}/outcome`, { method: "POST", json: {
      resolution: f.get("resolution"), rating: f.get("rating") ? +f.get("rating") : null, feedback: f.get("feedback"),
      actual_cost: f.get("cost") ? +f.get("cost") : null,
      parts_used: f.get("parts") ? f.get("parts").split(",").map((p) => ({ name: p.trim() })) : [] } });
    openRequest(id);
  };
}

boot();
