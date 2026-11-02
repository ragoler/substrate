// Agent Hotel floor plan. Same-origin API (the concierge serves this file),
// so no auth or cross-origin handling is needed in standalone mode.
"use strict";

const POLL_MS = 2000;
const $ = (id) => document.getElementById(id);

const STATUS_LABEL = {
  awake: "🟢 awake",
  napping: "😴 asleep (snapshot on the node, uploading…)",
  suspended: "💤 asleep (snapshot in GCS)",
  suspending: "🌙 falling asleep…",
  waking: "⏳ waking (restoring snapshot)…",
  vacant: "🚪 not checked in",
  crashed: "💥 crashed — will be rewound on your next message",
  unknown: "❔ unknown",
};

// --- Client-side conversation state --------------------------------------------
// Transcripts live in the BROWSER (localStorage), one per guest, so opening a
// drawer never has to wake a sleeping guest just to show text. The guest's
// own copy lives in its RAM — that is what the resume proof is about — and is
// reconciled from /history only while the guest is awake.
// Several conversations can be in flight at once (one room each); switching
// guests never blocks on a pending reply.
const LS_KEY = "agent-hotel-transcripts-v1";
let transcripts = {};          // guest -> [{cls, text}]
try { transcripts = JSON.parse(localStorage.getItem(LS_KEY) || "{}"); } catch { transcripts = {}; }
const pending = {};            // guest -> {text, since, note} while a reply is in flight
let currentGuest = null;       // guest shown in the drawer
let lastBootIds = {};          // guest -> boot_id we last saw (memory proof)
let lastStatus = {};           // guest -> status from the last poll
let roomCount = 0;             // last worker count seen (for +/- Room)
let bannerTimer = null;        // transient banner display (addBanner)
let pollFailures = 0;          // consecutive /api/state failures
let lastState = null;

function saveTranscripts() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(transcripts)); } catch {}
}
function transcript(guest) {
  return (transcripts[guest] = transcripts[guest] || []);
}
function record(guest, cls, text) {
  transcript(guest).push({ cls, text });
  saveTranscripts();
  if (guest === currentGuest) renderDrawer();
}

function esc(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

async function api(path, opts) {
  // The public LB occasionally answers 502/503 for a request the concierge
  // NEVER saw: its backend-connection pool gets churned by internet scanners
  // ("backend_connection_closed_before_data_sent"), and the GKE Gateway
  // controller's periodic re-syncs open ~10-60 s "no healthy upstream"
  // windows. Those responses carry the LB's plain-text body — retry them
  // with backoff, they were not delivered. Real errors from the concierge
  // carry a JSON {detail} and surface immediately.
  const attempts = 6;
  let resp;
  for (let attempt = 0; attempt < attempts; attempt++) {
    if (attempt) await new Promise((r) => setTimeout(r, Math.min(8000, 700 * 2 ** (attempt - 1))));
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      if (attempt === attempts - 1) throw e;   // network-level failure: retry too
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

// --- Floor plan ---------------------------------------------------------------------
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
  $("guests").innerHTML = guests.map((g) => {
    const busy = pending[g.name];
    const line = busy
      ? `<span class="dot waking"></span>💬 replying… (${Math.round((Date.now() - busy.since) / 1000)} s)`
      : `<span class="dot ${g.status}"></span>${STATUS_LABEL[g.status] || g.status}`;
    const turns = (transcripts[g.name] || []).filter((m) => m.cls === "user").length;
    return `
    <div class="guest ${g.status} ${busy ? "pending" : ""} ${g.name === currentGuest ? "selected" : ""}" data-guest="${g.name}">
      <div class="top"><span class="emoji">${g.emoji}</span>
        <div><div class="name">${esc(g.name)}</div>
        <div class="tagline">${esc(g.tagline)}</div></div>
      </div>
      <div class="status">${line}</div>
      <div class="worker">${g.worker_pod ? "in " + esc(g.worker_pod) : (turns ? `${turns} message${turns > 1 ? "s" : ""} so far` : "&nbsp;")}</div>
    </div>`;
  }).join("");
  document.querySelectorAll(".guest").forEach((el) =>
    el.addEventListener("click", () => openDrawer(el.dataset.guest)));
}

function renderMeter(m) {
  $("m-checkedin").textContent = `${m.checked_in}/${m.capacity}`;
  $("m-workers").textContent = m.workers;
  $("m-awake").textContent = m.awake;
  $("m-napping").textContent = m.napping ?? 0;
  $("m-suspended").textContent = m.suspended;
  $("m-crashed").textContent = m.crashed ?? 0;
  // "Compute saved" = guests who exist but hold no room right now.
  const saved = m.checked_in > 0 && m.workers > 0
    ? Math.max(0, Math.round(100 * (1 - Math.min(m.workers, m.checked_in) / m.checked_in)))
    : 0;
  $("m-saved").textContent = `${saved}%`;
}

function showBanner(text, cls) {
  const banner = $("banner");
  banner.textContent = text;
  banner.className = `banner ${cls || ""}`;
}
function hideBanner() { $("banner").className = "banner hidden"; }

async function refresh() {
  let s;
  try {
    s = await api("/api/state");
    pollFailures = 0;
  } catch (e) {
    pollFailures += 1;
    // One failed poll is noise (LB re-sync); only say so after a few, and
    // keep showing the last known floor plan meanwhile.
    if (pollFailures >= 3) showBanner(`⚠ reconnecting to the concierge… (${e.message})`);
    return;
  }
  lastState = s;
  roomCount = s.workers.length;
  s.guests.forEach((g) => { lastStatus[g.name] = g.status; });
  renderRooms(s.workers);
  renderGuests(s.guests);
  renderMeter(s.meter);
  $("btn-add-guest").disabled = s.meter.capacity >= s.meter.roster;
  if (s.template_error) {
    showBanner(`⚠ ${s.template_error}`);
  } else if (s.golden && s.golden.error) {
    // The template exists but Substrate could not build its golden
    // snapshot — every check-in will fail until a new image is deployed.
    showBanner(`⚠ golden snapshot failed: ${s.golden.error}`);
  } else if (s.golden && !s.golden.golden_ready) {
    showBanner("⏳ building the guests' golden snapshot — first check-ins cold-boot until it is ready");
  } else if (!bannerTimer) {
    hideBanner();
  }
  if (currentGuest) renderDrawerMeta();
}

// --- Chat drawer -----------------------------------------------------------
function renderDrawer() {
  const log = $("chat-log");
  log.innerHTML = "";
  const guest = currentGuest;
  const msgs = transcripts[guest] || [];
  if (!msgs.length) addMsgEl("system", "No conversation yet — say hello!");
  msgs.forEach((m) => addMsgEl(m.cls, m.text));
  const busy = pending[guest];
  if (busy) addMsgEl("system", busy.note || "…");
  log.scrollTop = log.scrollHeight;
}
function addMsgEl(cls, text) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.textContent = text;
  $("chat-log").appendChild(div);
}
function renderDrawerMeta() {
  const guest = currentGuest;
  const st = lastStatus[guest];
  const boot = lastBootIds[guest];
  const bits = [];
  if (st && STATUS_LABEL[st]) bits.push(STATUS_LABEL[st]);
  if (boot) bits.push(`boot_id ${boot}`);
  $("drawer-meta").textContent = bits.join(" · ");
}

async function openDrawer(guest) {
  currentGuest = guest;
  $("drawer-title").textContent = guest;
  $("drawer").classList.remove("hidden");
  renderDrawer();
  renderDrawerMeta();
  if (lastState) renderGuests(lastState.guests);
  $("chat-input").focus();
  // Reconcile with the guest's own RAM copy — only if it is awake (the
  // concierge refuses to wake a sleeper for this, and so do we).
  if (lastStatus[guest] === "awake") {
    try {
      const h = await api(`/api/guests/${guest}/history`);
      if (h.history && h.history.length && !pending[guest]) {
        transcripts[guest] = h.history.map((m) =>
          ({ cls: m.role === "user" ? "user" : "agent", text: m.text }));
        saveTranscripts();
        if (h.boot_id) lastBootIds[guest] = h.boot_id;
        if (currentGuest === guest) { renderDrawer(); renderDrawerMeta(); }
      }
    } catch { /* the local transcript stands */ }
  }
}

async function sendChat(ev) {
  ev.preventDefault();
  const text = $("chat-input").value.trim();
  const guest = currentGuest;
  if (!text || !guest) return;
  if (pending[guest]) { addBanner(`${guest} is still replying — pick another guest meanwhile`); return; }
  $("chat-input").value = "";
  record(guest, "user", text);
  const asleep = ["napping", "suspended", "crashed"].includes(lastStatus[guest]);
  pending[guest] = {
    text, since: Date.now(),
    note: asleep
      ? (lastStatus[guest] === "napping"
          ? `😴 → waking ${guest} (snapshot still on its node, ~1 s)…`
          : lastStatus[guest] === "crashed"
            ? `💥 → rewinding ${guest} to its last snapshot, then waking it…`
            : `💤 → waking ${guest} (restoring from GCS, a few seconds)…`)
      : "…",
  };
  renderDrawer();
  if (lastState) renderGuests(lastState.guests);
  try {
    const r = await api(`/api/guests/${guest}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    delete pending[guest];
    const prev = lastBootIds[guest];
    lastBootIds[guest] = r.boot_id;
    record(guest, "agent", r.reply);
    if (r.was_suspended) {
      const how = r.slept_as === "crashed" ? "a crash (rewound to the last snapshot)"
        : r.slept_as === "napping" ? "a node-local snapshot" : "a snapshot in GCS";
      record(guest, "system", prev === r.boot_id
        ? `✅ woke from ${how} with the same boot_id (${r.boot_id}) — RAM really survived`
        : `✅ woke from ${how} with the conversation intact`);
    }
  } catch (e) {
    delete pending[guest];
    record(guest, "system", `⚠ ${e.message}`);
  } finally {
    if (currentGuest === guest) renderDrawerMeta();
    refresh();
  }
}

// --- Wire-up ----------------------------------------------------------------
$("chat-form").addEventListener("submit", sendChat);
$("drawer-close").addEventListener("click", () => {
  $("drawer").classList.add("hidden");
  currentGuest = null;
  if (lastState) renderGuests(lastState.guests);
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
  showBanner(text, "info");
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { hideBanner(); bannerTimer = null; }, 5000);
}

$("btn-reset").addEventListener("click", async () => {
  if (!confirm("Reset the hotel? All conversations will be lost.")) return;
  try { await api("/api/reset", { method: "POST" }); } catch (e) { alert(e.message); }
  lastBootIds = {};
  transcripts = {};
  saveTranscripts();
  if (currentGuest) renderDrawer();
  refresh();
});

refresh();
setInterval(refresh, POLL_MS);
