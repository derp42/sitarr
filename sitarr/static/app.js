"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
} [c]));
let settings, rows = [],
  offset = 0,
  total = 0,
  activePage = "library",
  searchTimer;
let users = {};
const labels = {
  pending: "Waiting",
  unreviewed: "Needs review",
  review: "Needs attention",
  ready: "Ready",
  applied: "Applied",
  error: "Unavailable"
};
async function api(path, method = "GET", body) {
  const r = await fetch("/api/" + path, {
    method,
    headers: body ? {
      "Content-Type": "application/json"
    } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  if (!r.ok) {
    let text = "Request failed";
    try {
      const d = await r.json();
      text = typeof d.detail === "string" ? d.detail : text;
    } catch {}
    throw new Error(text);
  }
  return r.json();
}

function toast(text) {
  $("#toast").textContent = text;
  $("#toast").hidden = false;
  setTimeout(() => $("#toast").hidden = true, 5500);
}

function safely(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      toast(e.message);
    }
  };
}

function page(name) {
  activePage = name;
  document.querySelectorAll(".page").forEach(p => p.hidden = p.id !== "page-" + name);
  document.querySelectorAll("nav button").forEach(b => b.classList.toggle("active", b.dataset.page === name));
  const titles = {
    library: ["Make room for better movie nights.", "Age guidance that looks beyond the original rating."],
    activity: ["Every change has a story.", "Follow lookups, inspect changes, and undo what needs another look."],
    connections: ["A sitter for every screen.", "Connect your library and choose who gets age guidance."],
    rules: ["Guidance that fits your family.", "Keep the facts. Set the boundaries."]
  };
  $("#page-title").textContent = titles[name][0];
  $("#page-subtitle").textContent = titles[name][1];
  refresh();
}
async function loadItems() {
  const data = await api("items?" + new URLSearchParams({
    q: $("#search").value,
    status: $("#status-filter").value,
    limit: 75,
    offset
  }));
  rows = data.items;
  total = data.total;
  $("#library-body").innerHTML = rows.map((r, i) => {
    const g = r.guidance,
      s = r.snapshot;
    const url = g?.url?.startsWith("https://") ? g.url : null;
    return `<tr><td><div class="title-cell"><span class="media-icon ${esc(r.kind)}">${r.kind==="movie"?"▶":"▤"}</span><div><b>${esc(r.title)}</b><small>${esc(r.year||"Year unknown")} · ${r.kind==="movie"?"Movie":"TV series"}</small></div></div></td><td><span class="badge">${esc(s.official||"Unrated")}</span></td><td>${g?`<span class="badge age">Age ${g.age}+</span>${url?`<a class="source-link" href="${esc(url)}" target="_blank" rel="noreferrer">${esc(g.source)} ↗</a>`:`<small class="source-link">${esc(g.source)}</small>`}`:`<span class="muted">Not reviewed</span>`}</td><td><span class="badge">${esc(r.proposal.custom||"—")}</span></td><td><span class="badge ${esc(r.status)}" title="${esc(r.reason||"")}">${esc(labels[r.status]||r.status)}</span></td><td class="row-actions"><button data-row="${i}" data-action="review">Review</button>${g?`<button data-row="${i}" data-action="apply">Apply</button>`:`<button data-row="${i}" data-action="resolve">Check age</button>`}</td></tr>`;
  }).join("");
  $("#empty").hidden = total > 0;
  $("#item-count").textContent = total ? `${offset+1}–${Math.min(offset+75,total)} of ${total.toLocaleString()} titles` : "No matching titles";
  $("#previous").disabled = offset === 0;
  $("#next").disabled = offset + 75 >= total;
}
async function refresh() {
  const status = await api("status");
  const counts = status.counts;
  const stat = [
    ['Titles in your library', status.total, 'Movies and TV shows'],
    ['Guidance found', (counts.ready || 0) + (counts.applied || 0), 'Matched or reviewed recommendations'],
    ['Needs a second look', (counts.unreviewed || 0) + (counts.review || 0) + (counts.error || 0), 'No guesses. You make the call.'],
    ['Changes applied', status.changes, 'Each one recorded in your history']
  ];
  $("#stats").innerHTML = stat.map((s, i) => `<div class="stat ${i===1?"highlight":""}"><span class="stat-label">${s[0]}</span><b>${s[1].toLocaleString()}</b><small>${s[2]}</small></div>`).join("");
  $("#connection-status").textContent = status.servers.filter(s => s.enabled).length + " connected server(s) · " + (status.auto_apply ? "Automatic apply" : "Review first");
  const jobs = await api("jobs");
  const running = jobs.find(j => j.status === "running") || jobs.find(j => j.status === "queued");
  $("#job-banner").hidden = !running;
  if (running) $("#job-banner").textContent = (running.message || "Waiting to start…") + (running.total ? ` · ${running.progress}/${running.total}` : "");
  if (activePage === "library") await loadItems();
  if (activePage === "activity") {
    $("#jobs").innerHTML = jobs.map(j => `<div class="card"><div class="card-heading"><b>${esc(j.kind)} <span class="muted">#${j.id}</span></b><span class="badge ${esc(j.status)}">${esc(j.status)}</span></div><p>${esc(j.error||j.message||"Waiting")}</p>${j.total?`<progress value="${j.progress}" max="${j.total}"></progress>`:""}</div>`).join("") || '<p class="muted">No jobs yet.</p>';
    const changes = await api("changes");
    $("#changes").innerHTML = changes.map(c => {
      let before = JSON.parse(c.before_state),
        after = JSON.parse(c.after_state);
      return `<div class="card"><div class="card-heading"><div><b>${esc(c.title)}</b><p>${esc(before.custom||"No custom rating")} → ${esc(after.custom||"No custom rating")} · ${esc(new Date(c.created*1000).toLocaleString())}</p></div>${["applied","uncertain"].includes(c.status)?`<button data-undo="${c.id}">Undo</button>`:`<span class="badge">${esc(c.status)}</span>`}</div></div>`;
    }).join("") || '<p class="muted">Changes will appear here after you apply guidance.</p>';
  }
}
async function loadSettings() {
  settings = await api("settings");
  $("#auto-apply").checked = settings.auto_apply;
  $("#csm-enabled").checked = settings.csm_enabled;
  $("#sync-hours").value = settings.sync_hours;
  $("#batch-size").value = settings.batch_size;
  $("#cache-days").value = settings.cache_days;
  $("#mdblist-token").value = settings.mdblist_token;
  renderServers();
}

function renderServers() {
  $("#servers").innerHTML = settings.servers.map((s, i) => `<div class="card"><div class="card-heading"><div><b>${esc(s.name)}</b><p>${esc(s.kind)} · ${esc(s.url)} · ${s.enabled?"Enabled":"Disabled"}</p></div><div><button data-test="${i}">Test connection</button> <button data-edit="${i}">Edit</button></div></div><p id="server-result-${i}"></p></div>`).join("") || '<div class="card muted">No servers connected yet.</div>';
  $("#viewers").innerHTML = settings.protected_users.map(u => `<div class="card"><div class="card-heading"><b>${esc(u.name)}</b><span class="badge age">Up to age ${u.max_age}</span></div><p>${u.block_unreviewed?"Unreviewed titles blocked":"Original parental controls apply to unreviewed titles"}</p></div>`).join("") || '<p class="muted">No viewers configured. Test a Jellyfin connection to load its accounts.</p>';
  $("#protect-server").innerHTML = settings.servers.filter(s => s.kind === "jellyfin").map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
}
async function job(kind, key) {
  const r = await api("jobs", "POST", {
    kind,
    ...(key ? {
      key
    } : {})
  });
  toast(`Queued ${kind} · job #${r.id}`);
  await refresh();
}
document.querySelectorAll("nav button").forEach(b => b.onclick = () => page(b.dataset.page));
$("#search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(safely(async () => {
    offset = 0;
    await loadItems();
  }), 250);
};
$("#status-filter").onchange = safely(async () => {
  offset = 0;
  await loadItems();
});
$("#previous").onclick = safely(async () => {
  offset = Math.max(0, offset - 75);
  await loadItems();
});
$("#next").onclick = safely(async () => {
  offset += 75;
  await loadItems();
});
$("#scan").onclick = safely(() => job("scan"));
$("#lookup").onclick = safely(() => job("resolve"));
$("#sync").onclick = safely(() => job("sync"));
$("#apply-all").onclick = safely(() => job("apply"));
$("#empty-connect").onclick = () => page("connections");
$("#library-body").onclick = safely(async e => {
  const b = e.target.closest("button");
  if (!b) return;
  const row = rows[Number(b.dataset.row)];
  if (b.dataset.action === "review") {
    $("#review-key").value = row.key;
    $("#review-title").textContent = row.title;
    $("#review-info").textContent = `${row.year||"Unknown year"} · ${row.snapshot.official||"Unrated"} · ${row.reason||"Review the recommendation or add your household guidance."}`;
    $("#review-age").value = row.guidance?.age ?? "";
    $("#review-reason").value = row.guidance?.details?.reason || "";
    $("#review-dialog").showModal();
  } else await job(b.dataset.action, row.key);
});
$("#close-review").onclick = () => $("#review-dialog").close();
$("#review-form").onsubmit = safely(async e => {
  e.preventDefault();
  await api("items/" + encodeURIComponent($("#review-key").value) + "/override", "POST", {
    age: Number($("#review-age").value),
    reason: $("#review-reason").value
  });
  $("#review-dialog").close();
  toast("Recommendation saved. Apply it when ready.");
  await refresh();
});
$("#changes").onclick = safely(async e => {
  const b = e.target.closest("[data-undo]");
  if (b) {
    await api(`changes/${b.dataset.undo}/undo`, "POST", {});
    toast("Undo queued. Later edits will be preserved.");
    await refresh();
  }
});

function openServer(s) {
  $("#server-id").value = s?.id || "";
  $("#server-kind").value = s?.kind || "jellyfin";
  $("#server-name").value = s?.name || "";
  $("#server-url").value = s?.url || "";
  $("#server-token").value = s?.token || "";
  $("#server-dialog").showModal();
}
$("#add-server").onclick = () => openServer();
$("#close-server").onclick = () => $("#server-dialog").close();
$("#server-form").onsubmit = safely(async e => {
  e.preventDefault();
  const id = $("#server-id").value || "server-" + (Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
  const value = {
    id,
    name: $("#server-name").value,
    kind: $("#server-kind").value,
    url: $("#server-url").value,
    token: $("#server-token").value,
    enabled: true,
    libraries: settings.servers.find(s => s.id === id)?.libraries || []
  };
  settings.servers = settings.servers.filter(s => s.id !== id).concat([value]);
  await api("settings", "PUT", settings);
  $("#server-dialog").close();
  await loadSettings();
  toast("Connection saved. Test it before scanning.");
});
$("#servers").onclick = safely(async e => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.edit !== undefined) {
    openServer(settings.servers[Number(b.dataset.edit)]);
    return;
  }
  if (b.dataset.test !== undefined) {
    const i = Number(b.dataset.test),
      s = settings.servers[i];
    const result = await api(`servers/${s.id}/test`);
    $(`#server-result-${i}`).textContent = `Connected · ${result.libraries.length} libraries`;
    users[s.id] = result.users;
    $("#protect-server").value = s.id;
    renderUsers();
  }
});

function renderUsers() {
  $("#protect-user").innerHTML = (users[$("#protect-server").value] || []).map(u => `<option value="${esc(u.id)}">${esc(u.name)}${u.admin?" (administrator)":""}</option>`).join("") || '<option value="">Test this server first</option>';
}
$("#protect-server").onchange = renderUsers;
$("#protect-form").onsubmit = safely(async e => {
  e.preventDefault();
  const server_id = $("#protect-server").value,
    user_id = $("#protect-user").value;
  const user = (users[server_id] || []).find(u => u.id === user_id);
  if (!user) throw new Error("Test the server and select a viewer first");
  await api("protect", "POST", {
    server_id,
    user_id,
    name: user.name,
    max_age: Number($("#protect-age").value),
    block_unreviewed: $("#protect-unknown").checked
  });
  toast("Viewer rules queued.");
  setTimeout(safely(loadSettings), 2500);
});
$("#rules-form").onsubmit = safely(async e => {
  e.preventDefault();
  Object.assign(settings, {
    auto_apply: $("#auto-apply").checked,
    csm_enabled: $("#csm-enabled").checked,
    sync_hours: Number($("#sync-hours").value),
    batch_size: Number($("#batch-size").value),
    cache_days: Number($("#cache-days").value),
    mdblist_token: $("#mdblist-token").value
  });
  await api("settings", "PUT", settings);
  await loadSettings();
  toast("Household rules saved.");
});
safely(async () => {
  await loadSettings();
  await refresh();
})();
setInterval(safely(refresh), 7000);
