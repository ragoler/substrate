// Agent Hotel floor plan. Same-origin API (the concierge serves this file),
// so no auth or cross-origin handling is needed in standalone mode.
"use strict";

const POLL_MS = 2000;
const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  awake: "🟢 awake",
  suspended: "💤 sleeping in GCS",
  suspending: "🌙 falling asleep…",
  waking: "⏳ waking (restoring checkpoint)…",
  vacant: "🚪 not checked in",
  unknown: "❔ unknown",
};

let currentGuest = null;      // open chat drawer
let chatBusy = false;
let lastBootIds = {};         // guest -> boot_id we last saw (memory proof)
let roomCount = 0;            // last worker count seen (for +/- Room)
let bannerTimer = null;       // transient banner display (addBanner)

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

async function api(path, opts) {
  // The public LB occasionally 502/503s a request the concierge NEVER saw
  // (its shared backend-connection pool gets churned by internet scanners;
  // "backend_connection_closed_before_data_sent"). Those responses carry the
  // LB's plain-text body — retry them, they were not delivered. Real errors
  // from the concierge carry a JSON {detail} and surface immediately.
  let resp;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, 600 * attempt));
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      if (attempt === 2) throw e;   // network-level failure: retry too
      continue;
    }
    if (resp.status < 500) break;
    let body = "";
    try { body = await resp.clone().text(); } catch {}
    let fromConcierge = false;
    try { JSON.parse(body); fromConcierge = true; } catch {}
    if (fromConcierge) break;       // genuine backend error — don't retry
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

function renderRooms(workers) {
  $("rooms").innerHTML = workers.length
    ? workers.map((w, i) => `
      <div class="room ${w.occupant ? "busy" : ""}">
        <div class="room-name">Room ${i + 1} · ${esc(w.pod)}</div>
        <div class="occupant">${w.occupant ? esc(w.occupant) : "— empty —"}</div>
      </div>`).join("")
    : `<div class="room"><div class="room-name">No workers visible yet</div>
       <div class="occupant">the WorkerPool may still be reconciling</div></div>`;
}

function renderGuests(guests) {
  $("guests").innerHTML = guests.map((g) => `
    <div class="guest ${g.status}" data-guest="${g.name}">
      <div class="top"><span class="emoji">${g.emoji}</span>
        <div><div class="name">${esc(g.name)}</div>
        <div class="tagline">${esc(g.tagline)}</div></div>
      </div>
      <div class="status"><span class="dot ${g.status}"></span>${STATUS_LABEL[g.status] || g.status}</div>
      <div class="worker">${g.worker_pod ? "in " + esc(g.worker_pod) : "&nbsp;"}</div>
    </div>`).join("");
  document.querySelectorAll(".guest").forEach((el) =>
    el.addEventListener("click", () => openDrawer(el.dataset.guest)));
}

function renderMeter(m) {
  $("m-checkedin").textContent = `${m.checked_in}/${m.capacity}`;
  $("m-workers").textContent = m.workers;
  $("m-awake").textContent = m.awake;
  $("m-suspended").textContent = m.suspended;
  // "Compute saved" = guests who exist but hold no room right now.
  const saved = m.checked_in > 0 && m.workers > 0
    ? Math.max(0, Math.round(100 * (1 - Math.min(m.workers, m.checked_in) / m.checked_in)))
    : 0;
  $("m-saved").textContent = `${saved}%`;
}

async function refresh() {
  try {
    const s = await api("/api/state");
    roomCount = s.workers.length;
    renderRooms(s.workers);
    renderGuests(s.guests);
    renderMeter(s.meter);
    $("btn-add-guest").disabled = s.meter.capacity >= s.meter.roster;
    const banner = $("banner");
    if (s.template_error) {
      banner.textContent = `⚠ ${s.template_error}`;
      banner.classList.remove("hidden");
    } else if (!bannerTimer) {
      banner.classList.add("hidden");
    }
  } catch (e) {
    const banner = $("banner");
    banner.textContent = `⚠ cannot reach the concierge: ${e.message}`;
    banner.classList.remove("hidden");
  }
}

// --- Chat drawer -----------------------------------------------------------
function addMsg(cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  $("chat-log").appendChild(div);
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
}

async function openDrawer(guest) {
  currentGuest = guest;
  $("drawer-title").textContent = guest;
  $("drawer-meta").textContent = "";
  $("chat-log").innerHTML = "";
  $("drawer").classList.remove("hidden");
  try {
    const h = await api(`/api/guests/${guest}/history`);
    (h.history || []).forEach((m) =>
      addMsg(m.role === "user" ? "user" : "agent", m.text));
    if (h.boot_id) $("drawer-meta").textContent = `boot_id ${h.boot_id}`;
    if (!h.history || !h.history.length)
      addMsg("system", "No conversation yet — say hello!");
  } catch (e) {
    addMsg("system", `history unavailable: ${e.message}`);
  }
  $("chat-input").focus();
}

async function sendChat(ev) {
  ev.preventDefault();
  const text = $("chat-input").value.trim();
  if (!text || !currentGuest || chatBusy) return;
  const guest = currentGuest;
  chatBusy = true;
  $("chat-send").disabled = true;
  $("chat-input").value = "";
  addMsg("user", text);
  const wakeNote = document.createElement("div");
  wakeNote.className = "msg system";
  wakeNote.textContent = "…";
  $("chat-log").appendChild(wakeNote);
  // If the guest is asleep, the router restores its checkpoint first (~30 s).
  const timer = setTimeout(() => {
    wakeNote.textContent = `💤 → waking ${guest}… restoring checkpoint from GCS (~30 s)`;
  }, 1500);
  try {
    const r = await api(`/api/guests/${guest}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    clearTimeout(timer);
    wakeNote.remove();
    addMsg("agent", r.reply);
    const prev = lastBootIds[guest];
    lastBootIds[guest] = r.boot_id;
    let meta = `boot_id ${r.boot_id} · ${r.history_len / 2} exchanges`;
    if (r.was_suspended) {
      meta += " · woke from a snapshot";
      addMsg("system", prev === r.boot_id
        ? `✅ same boot_id (${r.boot_id}) after resume — RAM really survived`
        : "✅ resumed from checkpoint with the conversation intact");
    }
    $("drawer-meta").textContent = meta;
  } catch (e) {
    clearTimeout(timer);
    wakeNote.textContent = `⚠ ${e.message}`;
  } finally {
    chatBusy = false;
    $("chat-send").disabled = false;
    refresh();
  }
}

// --- Wire-up ----------------------------------------------------------------
$("chat-form").addEventListener("submit", sendChat);
$("drawer-close").addEventListener("click", () => {
  $("drawer").classList.add("hidden");
  currentGuest = null;
});
$("btn-open").addEventListener("click", async () => {
  $("btn-open").disabled = true;
  try { await api("/api/open", { method: "POST" }); } catch (e) { alert(e.message); }
  $("btn-open").disabled = false;
  refresh();
});
$("btn-add-guest").addEventListener("click", async () => {
  try {
    const r = await api("/api/guests/add", { method: "POST" });
    addBanner(`${r.emoji} ${r.added} (${r.tagline}) has been admitted — ${r.capacity}/${r.roster} on the roster`);
  } catch (e) { alert(e.message); }
  refresh();
});
$("btn-add-room").addEventListener("click", async () => {
  try { await api("/api/rooms", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count: roomCount + 1 }),
  }); } catch (e) { alert(e.message); }
  refresh();
});
$("btn-rm-room").addEventListener("click", async () => {
  if (roomCount <= 1) { alert("The hotel needs at least one room."); return; }
  try { await api("/api/rooms", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ count: roomCount - 1 }),
  }); } catch (e) { alert(e.message); }
  refresh();
});

function addBanner(text) {
  const banner = $("banner");
  banner.textContent = text;
  banner.classList.remove("hidden");
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => {
    banner.classList.add("hidden");
    bannerTimer = null;
  }, 5000);
}

$("btn-reset").addEventListener("click", async () => {
  if (!confirm("Reset the hotel? All conversations will be lost.")) return;
  try { await api("/api/reset", { method: "POST" }); } catch (e) { alert(e.message); }
  lastBootIds = {};
  refresh();
});

refresh();
setInterval(refresh, POLL_MS);
