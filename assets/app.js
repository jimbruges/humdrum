// Humdrum web UI. Talks to python/main.py: REST for changes, Socket.IO for live state.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const SR = 48000;
const MAX_SECONDS = 30;

let state = null;      // last full state from the board
let meter = {};        // last meter message
let editing = null;    // pad number open in the editor
let uploadTarget = null;

// ---------------------------------------------------------------------------
// transport
// ---------------------------------------------------------------------------

const socket = io({ transports: ["websocket", "polling"] });
socket.on("connect", () => $("#conn").classList.remove("off"));
socket.on("disconnect", () => $("#conn").classList.add("off"));
socket.on("state", (s) => { state = s; render(); });
socket.on("meter", (m) => { meter = m; renderMeter(); });

async function api(method, path, body, headers = {}) {
  const opts = { method, headers };
  if (body instanceof ArrayBuffer || body instanceof Blob) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, ms = 2200) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const pad2 = (n) => String(n).padStart(2, "0");

// ---------------------------------------------------------------------------
// FX panel
// ---------------------------------------------------------------------------

const sendSettings = debounce((changes) => api("POST", "/api/settings", changes).catch((e) => toast(e.message)), 60);

function renderFx() {
  const list = $("#fx-list");
  if (list.childElementCount !== state.fx.length) {
    list.innerHTML = "";
    state.fx.forEach((fx, i) => {
      const b = document.createElement("button");
      b.className = "fx-btn";
      b.innerHTML = `<b>${fx.short}</b><small>${fx.name}</small>`;
      b.onclick = () => sendSettings({ fx_index: i, fx_bypass: false });
      list.appendChild(b);
    });
  }
  const idx = meter.fx_index ?? state.settings.fx_index;
  const bypass = meter.fx_bypass ?? state.settings.fx_bypass;
  [...list.children].forEach((b, i) => {
    b.classList.toggle("active", i === idx);
    b.hidden = !state.fx[i].available;  // e.g. the theremin needs a Modulino Distance
  });
  const theremin = state.fx[idx] && state.fx[idx].short === "THR";
  $("#hand-row").hidden = !theremin;
  $(".fx").classList.toggle("bypassed", bypass);
  $("#bypass").classList.toggle("on", bypass);
  $("#mic-mute").classList.toggle("on", !!state.settings.mic_mute);
  $(".fx").classList.toggle("muted", !!state.settings.mic_mute);
  const lvl = $("#mic-level");
  if (document.activeElement !== lvl) lvl.value = state.settings.monitor_level;
  $("#mic-level-val").textContent = Math.round(lvl.value * 100);
  const amount = $("#amount");
  if (document.activeElement !== amount) amount.value = meter.fx_amount ?? state.settings.fx_amount;
  $("#amount-val").textContent = Math.round(amount.value * 100);
}

$("#amount").addEventListener("input", (e) => {
  $("#amount-val").textContent = Math.round(e.target.value * 100);
  sendSettings({ fx_amount: +e.target.value });
});
$("#bypass").onclick = () => sendSettings({ fx_bypass: !state.settings.fx_bypass });
$("#mic-mute").onclick = () => sendSettings({ mic_mute: !state.settings.mic_mute });
$("#mic-level").addEventListener("input", (e) => {
  $("#mic-level-val").textContent = Math.round(e.target.value * 100);
  sendSettings({ monitor_level: +e.target.value });
});

// ---------------------------------------------------------------------------
// Bluetooth panel
// ---------------------------------------------------------------------------

// Volume sliders run in dB: their position 0..1000 maps from DB_MIN up to the
// top of the level's range, and 0 is silence. A plain linear 0..1.5 slider put
// all the usable volume in its first few percent once the output was fixed.
const DB_MIN = -60;
const LEVEL_TOP = 1.5;
const dbOf = (v) => 20 * Math.log10(v);
const posFromLevel = (v, top = LEVEL_TOP) => (v > 0 ? Math.max(1, Math.min(1000, Math.round(((dbOf(v) - DB_MIN) / (dbOf(top) - DB_MIN)) * 1000))) : 0);
const levelFromPos = (p, top = LEVEL_TOP) => (p > 0 ? 10 ** ((DB_MIN + (p / 1000) * (dbOf(top) - DB_MIN)) / 20) : 0);
const fmtDb = (v) => (v > 0 && dbOf(v) > DB_MIN - 0.5 ? `${Math.round(dbOf(v))} dB` : "off");

const BT_SLIDERS = [["#bt-level", "bt_level", "db"], ["#pads-level", "sample_level", "db"], ["#bt-amount", "bt_fx_amount", 100]];

function renderBt() {
  const b = state.bluetooth || {};
  let status;
  if (!b.available) status = "Bluetooth isn't set up on this board yet: run host/bluetooth/install.sh once (see the README).";
  else if (!b.powered) status = "Bluetooth is <b>off</b>: the board's Bluetooth chip isn't responding, so phones can't see HUMDRUM. Restart the board to bring it back.";
  else if (b.connected) status = `Connected to <b>${escapeHtml(b.device || "a phone")}</b>.` + (state.bt_audio ? "" : " Waiting for audio: press play on the phone.");
  else status = b.discoverable
    ? "Not connected. On your phone, open Bluetooth settings and connect to <b>HUMDRUM</b>."
    : "Not connected, and hidden from new phones. Turn on <b>visible to new phones</b> to pair one.";
  $("#bt-status").innerHTML = status;
  $("#bt-now").hidden = !b.connected;
  $("#bt-title").textContent = b.title || (b.playing ? "playing" : "paused");
  $("#bt-artist").textContent = b.artist || "";
  $("#bt-pp").innerHTML = b.playing ? "&#9208;" : "&#9654;";
  $("#bt-visible").checked = !!b.discoverable;
  $("#bt-visible").disabled = !b.available;
  $("#bt-disconnect").hidden = !b.connected;

  const list = $("#bt-fx-list");
  if (list.childElementCount !== state.fx.length) {
    list.innerHTML = "";
    state.fx.forEach((fx, i) => {
      const btn = document.createElement("button");
      btn.className = "fx-btn";
      btn.dataset.i = i;
      btn.innerHTML = `<b>${fx.short}</b><small>${fx.name}</small>`;
      btn.onclick = () => sendSettings({ bt_fx_index: i, bt_fx_bypass: false });
      list.appendChild(btn);
    });
  }
  [...list.children].forEach((btn) => {
    btn.classList.toggle("active", +btn.dataset.i === state.settings.bt_fx_index);
    btn.hidden = !state.fx[+btn.dataset.i].available;  // the theremin needs a Modulino Distance
  });
  $(".bt").classList.toggle("bypassed", state.settings.bt_fx_bypass);
  $("#bt-bypass").classList.toggle("on", state.settings.bt_fx_bypass);
  $("#bt-mute").classList.toggle("on", !!state.settings.bt_mute);
  $(".bt").classList.toggle("muted", !!state.settings.bt_mute);
  for (const [id, key, scale] of BT_SLIDERS) {
    const v = state.settings[key];
    if (document.activeElement !== $(id)) $(id).value = scale === "db" ? posFromLevel(v) : v;
    $(id + "-val").textContent = scale === "db" ? fmtDb(v) : Math.round(v * scale);
  }
}

const escapeHtml = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

document.querySelectorAll("[data-bt]").forEach((b) => (b.onclick = () => api("POST", `/api/bt/${b.dataset.bt}`).catch((e) => toast(e.message))));
$("#bt-visible").onchange = (e) => api("POST", "/api/bt/discoverable", { on: e.target.checked }).catch((err) => toast(err.message));
$("#bt-disconnect").onclick = () => api("POST", "/api/bt/disconnect").catch((e) => toast(e.message));
$("#bt-bypass").onclick = () => sendSettings({ bt_fx_bypass: !state.settings.bt_fx_bypass });
$("#bt-mute").onclick = () => sendSettings({ bt_mute: !state.settings.bt_mute });
for (const [id, key, scale] of BT_SLIDERS) {
  $(id).addEventListener("input", (e) => {
    const v = scale === "db" ? levelFromPos(+e.target.value) : +e.target.value;
    $(id + "-val").textContent = scale === "db" ? fmtDb(v) : Math.round(v * scale);
    sendSettings({ [key]: v });
  });
}

// ---------------------------------------------------------------------------
// sample library
// ---------------------------------------------------------------------------

let libSig = "";
function renderLibrary() {
  const usedOn = {};
  state.slots.forEach((s) => s.loaded && (usedOn[s.sample] = (usedOn[s.sample] || []).concat(s.pad + 1)));
  const sig = JSON.stringify([state.library, usedOn]);
  if (sig === libSig) return;
  libSig = sig;
  for (const kind of ["uploads", "recordings"]) {
    const wrap = $(`#lib-${kind}`);
    wrap.innerHTML = "";
    const items = state.library.filter((it) => it.kind === kind);
    if (!items.length) wrap.innerHTML = `<p class="empty">${kind === "uploads" ? "Nothing uploaded yet." : "No recordings yet: hold a pad to record."}</p>`;
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "lib-row";
      const pads = usedOn[it.id];
      row.innerHTML = `
        <button class="chip play" aria-label="preview">&#9654;</button>
        <input class="lib-name" maxlength="40">
        <span class="meta mono">${it.seconds}s${pads ? ` · pad ${pads.join(", ")}` : ""}</span>
        <button class="chip warn del" aria-label="delete">&times;</button>`;
      const name = row.querySelector(".lib-name");
      name.value = it.name;
      name.onchange = () => api("PATCH", `/api/library/item?id=${encodeURIComponent(it.id)}`, { name: name.value }).catch((e) => toast(e.message));
      row.querySelector(".play").onclick = () => new Audio(`/api/library/audio?id=${encodeURIComponent(it.id)}`).play();
      row.querySelector(".del").onclick = async () => {
        const warn = pads ? ` It's on pad ${pads.join(", ")}, which will be emptied.` : "";
        if (!confirm(`Delete "${it.name}" from the board?${warn}`)) return;
        await api("DELETE", `/api/library/item?id=${encodeURIComponent(it.id)}`).catch((e) => toast(e.message));
      };
      wrap.appendChild(row);
    }
  }
}

$("#lib-upload").onclick = () => pickFile(null);

// ---------------------------------------------------------------------------
// knob and button roles
// ---------------------------------------------------------------------------

$("#stop-all").onclick = () => api("POST", "/api/system/stop_all");

function renderMeter() {
  const pct = (v) => `${Math.min(100, Math.round(Math.sqrt(v || 0) * 100))}%`;
  $("#m-in").style.setProperty("--w", pct(meter.in));
  $("#m-out").style.setProperty("--w", pct(meter.mic_out));
  $("#m-bt-in").style.setProperty("--w", pct(meter.bt_in));
  $("#m-play-out").style.setProperty("--w", pct(meter.play_out));
  // the more effect a chain has on, the further its meters shift from green to blue
  for (const [ids, fx] of [[["#m-in", "#m-out"], meter.mic_fx], [["#m-bt-in", "#m-play-out"], meter.bt_fx]])
    for (const id of ids) $(id).style.setProperty("--fx", `${Math.round((fx || 0) * 100)}%`);
  $("#m-raw").style.setProperty("--w", pct(meter.raw));
  renderInputHelp();
  $("#m-hand").style.setProperty("--w", `${Math.round((meter.hand || 0) * 100)}%`);
  $("#hand-mm").textContent = meter.hand_mm >= 0 ? `${meter.hand_mm} mm` : "no hand";
  if (!state) return;
  renderFx();
  const playing = new Set(meter.playing || []);
  document.querySelectorAll(".pad").forEach((el) => {
    const p = +el.dataset.pad;
    el.classList.toggle("playing", playing.has(p));
    el.classList.toggle("rec", meter.rec === p);
    const prog = el.querySelector(".prog");  // Bluetooth control tiles have none
    if (prog) prog.style.width = meter.rec === p ? `${meter.rec_progress * 100}%` : "0";
  });
}

// ---------------------------------------------------------------------------
// pads
// ---------------------------------------------------------------------------

function slotFor(p) {
  return state.slots.find((s) => s.pad === p) || { pad: p, loaded: false };
}

function drawPeaks(canvas, peaks, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.width, h = canvas.clientHeight || canvas.height;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext("2d");
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  g.fillStyle = opts.color || getComputedStyle(document.body).getPropertyValue("--ink");
  const n = peaks.length, bw = w / n;
  peaks.forEach((v, i) => {
    const bh = Math.max(1, v * h);
    g.fillRect(i * bw, (h - bh) / 2, Math.max(1, bw - 1), bh);
  });
}

const BT_TILES = {
  "bt-previous": { icon: "&#9198;", name: "previous", cmd: "previous" },
  "bt-playpause": { icon: "&#9199;", name: "play / pause", cmd: "playpause" },
  "bt-next": { icon: "&#9197;", name: "next", cmd: "next" },
};

// <select> of every library sample, grouped like the library panel
function sampleSelect(current) {
  const sel = document.createElement("select");
  sel.className = "pad-sample";
  sel.add(new Option("— no sound —", ""));
  for (const [kind, label] of [["uploads", "uploads"], ["recordings", "mic recordings"]]) {
    const items = state.library.filter((it) => it.kind === kind);
    if (!items.length) continue;
    const g = document.createElement("optgroup");
    g.label = label;
    items.forEach((it) => g.appendChild(new Option(`${it.name} (${it.seconds}s)`, it.id)));
    sel.appendChild(g);
  }
  sel.value = current || "";
  return sel;
}

function renderPads() {
  const wrap = $("#pads");
  const connected = state.pads_connected;
  const extra = state.slots.filter((s) => s.loaded && s.pad >= connected).map((s) => s.pad);
  const total = Math.max(connected, extra.length ? Math.max(...extra) + 1 : 0);
  const modules = Math.ceil(total / 3);
  const roles = state.pad_roles || [];
  const t = state.topology;
  $("#no-pads").hidden = connected > 0;
  const padCount = roles.filter((r) => r === "pad").length;
  $("#topo").textContent = `${padCount} pads${t.knobs ? ` · ${t.knobs} knob${t.knobs > 1 ? "s" : ""}` : ""}${t.buzzer ? " · buzzer" : ""}${t.distance ? " · distance" : ""}${state.bluetooth?.connected ? " · bt" : ""}`;

  wrap.innerHTML = "";
  for (let m = 0; m < modules; m++) {
    const row = document.createElement("div");
    row.className = "module";
    const btModule = roles[m * 3] && roles[m * 3] !== "pad";
    row.innerHTML = `<div class="module-label">MOD ${m + 1}${btModule ? " · BT" : ""}</div>`;
    for (let k = 0; k < 3; k++) {
      const p = m * 3 + k;
      const el = document.createElement("div");
      el.dataset.pad = p;
      el.tabIndex = 0;
      el.setAttribute("role", "button");
      const tile = BT_TILES[roles[p]];
      if (tile) {
        el.className = "pad btctl";
        el.innerHTML = `
          <div class="top-row"><span class="no">${tile.icon}</span><span class="led"></span></div>
          <div><div class="name">${tile.name}</div><div class="meta">bluetooth</div></div>`;
        el.onclick = () => api("POST", `/api/bt/${tile.cmd}`).catch((e) => toast(e.message));
        row.appendChild(el);
        continue;
      }
      const s = slotFor(p);
      el.className = "pad" + (s.loaded ? " loaded" : "") + (p >= connected ? " offline" : "");
      el.innerHTML = `
        <div class="top-row"><span class="no">${pad2(p + 1)}</span><span class="led"></span></div>
        ${s.loaded
          ? `<canvas></canvas><div><div class="meta">${s.seconds}s · ${s.mode}${s.pitch ? ` · ${s.pitch > 0 ? "+" : ""}${s.pitch}st` : ""}</div></div>`
          : `<div class="plus">+</div><div class="meta">${p >= connected ? "not connected" : "empty: tap to upload"}</div>`}
        <div class="prog"></div>`;
      const sel = sampleSelect(s.loaded ? s.sample : "");
      sel.onclick = (e) => e.stopPropagation();
      sel.onchange = () => api("PATCH", `/api/slots/${p}`, { sample: sel.value }).catch((e) => toast(e.message));
      el.insertBefore(sel, el.querySelector(".prog"));
      el.onclick = () => (s.loaded ? openEditor(p) : pickFile(p));
      enableDrop(el, p);
      row.appendChild(el);
      if (s.loaded) requestAnimationFrame(() => drawPeaks(el.querySelector("canvas"), s.peaks));
    }
    wrap.appendChild(row);
  }
  renderMeter();
}

function enableDrop(el, pad) {
  el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("dragover"); });
  el.addEventListener("dragleave", () => el.classList.remove("dragover"));
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    el.classList.remove("dragover");
    const f = e.dataTransfer.files[0];
    if (f) uploadFile(pad, f);
  });
}

function pickFile(pad) {
  uploadTarget = pad;
  const input = $("#file");
  input.value = "";
  input.click();
}
$("#file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) uploadFile(uploadTarget, f);
});

// Decode anything the browser can play, resample to 48 kHz mono, send as WAV.
// Doing this here keeps the board free of codec dependencies.
async function uploadFile(pad, file) {
  toast(`decoding ${file.name}…`, 60000);
  try {
    const bytes = await file.arrayBuffer();
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const decoded = await new Promise((res, rej) => ctx.decodeAudioData(bytes.slice(0), res, rej));
    ctx.close && ctx.close();
    const seconds = Math.min(decoded.duration, MAX_SECONDS);
    const off = new OfflineAudioContext(1, Math.ceil(seconds * SR), SR);
    const src = off.createBufferSource();
    src.buffer = decoded;
    src.connect(off.destination);  // the destination downmixes to mono
    src.start();
    const rendered = await off.startRendering();
    const wav = encodeWav(rendered.getChannelData(0));
    const name = file.name.replace(/\.[^.]+$/, "").slice(0, 40);
    const headers = { "Content-Type": "audio/wav", "X-Slot-Name": encodeURIComponent(name) };
    if (pad === null) {
      toast("adding to the library…", 60000);
      await api("POST", "/api/library", wav, headers);
      toast(`${name} added to the library`);
    } else {
      toast(`uploading to pad ${pad2(pad + 1)}…`, 60000);
      await api("POST", `/api/slots/${pad}`, wav, headers);
      toast(`pad ${pad2(pad + 1)} loaded`);
    }
  } catch (e) {
    toast(`could not load: ${e.message || e}`, 4000);
  }
}

function encodeWav(f32) {
  const n = f32.length, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE");
  str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, SR, true); v.setUint32(28, SR * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, f32[i])) * 32767, true);
  return buf;
}

// ---------------------------------------------------------------------------
// slot editor
// ---------------------------------------------------------------------------

const patchSlot = debounce((pad, changes) => api("PATCH", `/api/slots/${pad}`, changes).catch((e) => toast(e.message)), 120);

function openEditor(pad) {
  editing = pad;
  fillEditor();
  $("#editor").showModal();
  requestAnimationFrame(() => drawPeaks($("#ed-wave"), slotFor(pad).peaks, { color: getComputedStyle(document.body).getPropertyValue("--accent") }));
}

function fillEditor() {
  const s = slotFor(editing);
  if (!s.loaded) { $("#editor").close(); return; }
  $("#ed-no").textContent = pad2(editing + 1);
  if (document.activeElement !== $("#ed-name")) $("#ed-name").value = s.name;
  $("#ed-gain").value = s.gain;
  $("#ed-pitch").value = s.pitch;
  $("#ed-pitch-v").textContent = (s.pitch > 0 ? "+" : "") + s.pitch;
  $("#ed-start").value = s.start;
  $("#ed-end").value = s.end;
  $("#ed-reverse").classList.toggle("on", s.reverse);
  document.querySelectorAll("#ed-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === s.mode));
  $("#ed-download").href = `/api/slots/${editing}/audio`;
  updateTrim();
  const sel = $("#ed-move");
  sel.innerHTML = "<option value=''>—</option>";
  const total = Math.max(state.pads_connected, 3);
  for (let p = 0; p < total; p++) {
    if (p === editing) continue;
    const o = slotFor(p);
    sel.add(new Option(`${pad2(p + 1)} ${o.loaded ? "(swap: " + o.name + ")" : "(empty)"}`, p));
  }
}

function updateTrim() {
  $("#trim-a").style.width = `${$("#ed-start").value * 100}%`;
  $("#trim-b").style.width = `${(1 - $("#ed-end").value) * 100}%`;
}

const renameSample = debounce((sid, name) => api("PATCH", `/api/library/item?id=${encodeURIComponent(sid)}`, { name }).catch((e) => toast(e.message)), 400);
$("#ed-name").addEventListener("input", (e) => renameSample(slotFor(editing).sample, e.target.value));
$("#ed-gain").addEventListener("input", (e) => patchSlot(editing, { gain: +e.target.value }));
$("#ed-pitch").addEventListener("input", (e) => {
  $("#ed-pitch-v").textContent = (e.target.value > 0 ? "+" : "") + e.target.value;
  patchSlot(editing, { pitch: +e.target.value });
});
for (const id of ["#ed-start", "#ed-end"]) {
  $(id).addEventListener("input", () => {
    let a = +$("#ed-start").value, b = +$("#ed-end").value;
    if (b - a < 0.01) { if (id === "#ed-start") a = b - 0.01; else b = a + 0.01; }
    $("#ed-start").value = a; $("#ed-end").value = b;
    updateTrim();
    patchSlot(editing, { start: a, end: b });
  });
}
document.querySelectorAll("#ed-mode button").forEach((b) => (b.onclick = () => {
  document.querySelectorAll("#ed-mode button").forEach((x) => x.classList.toggle("on", x === b));
  patchSlot(editing, { mode: b.dataset.mode });
}));
$("#ed-reverse").onclick = () => patchSlot(editing, { reverse: !slotFor(editing).reverse });
$("#ed-play").onclick = () => api("POST", `/api/slots/${editing}/trigger`);
$("#ed-replace").onclick = () => { $("#editor").close(); pickFile(editing); };
$("#ed-clear").onclick = async () => {
  if (!confirm(`Take the sound off pad ${pad2(editing + 1)}? It stays in the library.`)) return;
  await api("DELETE", `/api/slots/${editing}`);
  $("#editor").close();
};
$("#ed-move").onchange = async (e) => {
  if (e.target.value === "") return;
  const to = +e.target.value;
  await api("POST", "/api/slots/swap", { a: editing, b: to });
  editing = to;
  toast(`moved to pad ${pad2(to + 1)}`);
};
$("#editor").addEventListener("close", () => (editing = null));

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------

const SETTINGS = [
  { key: "master", label: "master", curve: "db", max: 1.5, fmt: fmtDb },
  { key: "hp_gain", label: "headphone amp", min: 1, max: 20, step: 1, fmt: (v) => v },
  { key: "mic_gain_db", label: "mic gain dB", min: -12, max: 36, step: 1, fmt: (v) => v },
  { key: "gate_db", label: "noise gate dB", min: -90, max: -20, step: 1, fmt: (v) => v },
  // set automatically when a hot mic clips the codec; 0 for the headset mic
  { key: "mic_pad_db", label: "mic input pad dB", min: 0, max: 42, step: 6, fmt: (v) => v },
  { key: "hold_ms", label: "hold to rec ms", min: 200, max: 3000, step: 50, fmt: (v) => v },
  { key: "max_rec_s", label: "max rec s", min: 1, max: 30, step: 1, fmt: (v) => v },
  { key: "knob_step", label: "knob speed", min: 0.005, max: 0.1, step: 0.005, fmt: (v) => Math.round(v * 1000) },
  { key: "theremin_near_mm", label: "theremin near mm", min: 10, max: 300, step: 5, fmt: (v) => v },
  { key: "theremin_far_mm", label: "theremin far mm", min: 100, max: 1200, step: 10, fmt: (v) => v },
  { key: "bt_knob_pads", label: "bt knob sets pads too", type: "toggle" },
  { key: "record_post_fx", label: "record with fx", type: "toggle" },
  { key: "rec_overwrite", label: "rec over loaded pads", type: "toggle" },
  { key: "auto_trim", label: "auto-trim silence", type: "toggle" },
  { key: "normalize", label: "normalise recordings", type: "toggle" },
  { key: "block", label: "audio block", type: "select", options: [256, 512, 1024, 2048] },
  { key: "buzzer_mode", label: "buzzer (modulino)", type: "select", options: ["off", "pads", "follow"] },
];

function buildSettings() {
  const wrap = $("#settings");
  wrap.innerHTML = "";
  for (const d of SETTINGS) {
    const row = document.createElement("label");
    row.className = "setting" + (d.type ? ` ${d.type}` : "");
    // the default is shown beside the name, and clicking it puts the setting back to it
    row.innerHTML = `<span>${d.label} <button type="button" class="dflt" title="reset to the default"></button></span>`;
    row.querySelector(".dflt").onclick = (e) => {
      e.preventDefault();
      const v = state.defaults?.[d.key];
      if (v !== undefined) sendSettings({ [d.key]: v });
    };
    let input;
    if (d.type === "toggle") {
      input = Object.assign(document.createElement("input"), { type: "checkbox", className: "switch" });
      input.onchange = () => sendSettings({ [d.key]: input.checked });
      row.append(input);
    } else if (d.type === "select") {
      input = document.createElement("select");
      d.options.forEach((o) => input.add(new Option(o, o)));
      input.onchange = () => sendSettings({ [d.key]: typeof d.options[0] === "number" ? +input.value : input.value });
      row.append(input);
    } else if (d.type === "text") {
      input = Object.assign(document.createElement("input"), { type: "text", spellcheck: false });
      input.onchange = () => sendSettings({ [d.key]: input.value.trim() });
      row.append(input);
    } else {
      const db = d.curve === "db";
      input = Object.assign(document.createElement("input"), db ? { type: "range", min: 0, max: 1000, step: 1 } : { type: "range", min: d.min, max: d.max, step: d.step });
      const out = document.createElement("output");
      input.oninput = () => {
        const v = db ? levelFromPos(+input.value, d.max) : +input.value;
        out.textContent = d.fmt(v);
        sendSettings({ [d.key]: v });
      };
      row.append(input, out);
    }
    input.dataset.key = d.key;
    wrap.appendChild(row);
  }
}

function renderSettings() {
  for (const d of SETTINGS) {
    const input = $(`[data-key="${d.key}"]`);
    if (!input || document.activeElement === input) continue;
    const v = state.settings[d.key];
    if (d.type === "toggle") input.checked = !!v;
    else input.value = d.curve === "db" ? posFromLevel(v, d.max) : v;
    const out = input.parentElement.querySelector("output");
    if (out) out.textContent = d.fmt(v);
    const dflt = input.closest(".setting")?.querySelector(".dflt");
    const dv = state.defaults?.[d.key];
    if (dflt) {
      dflt.hidden = dv === undefined;
      if (dv !== undefined) {
        const shown = d.fmt ? d.fmt(dv) : d.type === "toggle" ? (dv ? "on" : "off") : dv;
        dflt.textContent = `default ${shown}`;
        dflt.classList.toggle("is-default", v === dv);
      }
    }
  }
  const t = state.topology;
  $("#diag").innerHTML = `
    <dt>modules</dt><dd>${t.modules} (${t.addrs || "none"})</dd>
    <dt>knobs</dt><dd>${t.knobs} (${t.knob_addrs || "none"})</dd>
    <dt>pixels</dt><dd>${t.pixels_addrs || "none"}</dd>
    <dt>buzzer</dt><dd>${t.buzzer ? "yes" : "no"}</dd>
    <dt>distance</dt><dd>${t.distance ? "yes" : "no"}</dd>
    <dt>input</dt><dd>${state.devices.input}</dd>
    <dt>output</dt><dd>${state.devices.output}</dd>
    <dt>audio blocks</dt><dd>${state.audio?.blocks ?? "?"} (mic reopened ${state.audio?.mic_reopens ?? 0}×)</dd>
    <dt>audio load</dt><dd>${state.audio?.dsp_ms ?? "?"} ms of every ${((state.settings?.block ?? 512) / 48).toFixed(1)} ms block</dd>
    <dt>mic clipping</dt><dd>${state.audio?.mic_clips ? `${state.audio.mic_clips} clipped blocks, codec padded ${state.settings?.mic_pad_db ?? 0} dB` : "none"}</dd>
    <dt>limiter</dt><dd>${state.audio?.limit_db ? `turning peaks down by ${state.audio.limit_db} dB` : "idle"}</dd>
    <dt>bluetooth buffer</dt><dd>${state.audio?.bt_fill_ms ?? 0} ms, ${state.audio?.bt_underruns ?? 0} underruns</dd>
    <dt>last audio error</dt><dd>${state.audio?.last_error || "none"}</dd>
    <dt>bad bus reads</dt><dd>${t.bad_reads ?? 0}</dd>
    <dt>on the bus</dt><dd>${t.bus || "?"}</dd>`;
  renderAudio();
}


// ---------------------------------------------------------------------------
// audio devices and Media Carrier jacks
// ---------------------------------------------------------------------------

let devices = { inputs: [], outputs: [], ports: [] };

async function loadDevices() {
  try {
    devices = await api("GET", "/api/audio/devices");
  } catch (e) {
    toast(e.message);
  }
  if (state) renderAudio();
}

function fillDeviceSelect(sel, list, value) {
  const opts = [...(devices[list] || [])];
  if (value && !opts.some((o) => o.id === value)) opts.push({ id: value, label: `${value} (not connected)` });
  const sig = JSON.stringify(opts);
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = "";
    opts.forEach((o) => sel.add(new Option(o.label, o.id)));
    sel.dataset.sig = sig;
  }
  if (document.activeElement !== sel) sel.value = value;
}

$("#dev-in").onchange = (e) => sendSettings({ input_device: e.target.value });
$("#dev-out").onchange = (e) => sendSettings({ output_device: e.target.value });

function renderAudio() {
  fillDeviceSelect($("#dev-in"), "inputs", state.settings.input_device);
  fillDeviceSelect($("#dev-out"), "outputs", state.settings.output_device);
  const carrier = state.audio ? state.audio.carrier : true;
  $("#ports-wrap").hidden = !carrier;
  $("#identify").disabled = !carrier;

  const wrap = $("#ports");
  const on = state.settings.carrier_outputs || [];
  if (wrap.childElementCount !== devices.ports.length) {
    wrap.innerHTML = "";
    for (const p of devices.ports) {
      const row = document.createElement("label");
      row.className = "setting toggle";
      row.innerHTML = `<span>${p.label}</span>`;
      const cb = Object.assign(document.createElement("input"), { type: "checkbox", className: "switch" });
      cb.dataset.port = p.id;
      cb.onchange = () => {
        const picked = [...wrap.querySelectorAll("input")].filter((x) => x.checked).map((x) => x.dataset.port);
        sendSettings({ carrier_outputs: picked });
      };
      row.append(cb);
      wrap.appendChild(row);
    }
  }
  wrap.querySelectorAll("input").forEach((cb) => (cb.checked = on.includes(cb.dataset.port)));
}

// what the mic meter means right now
function renderInputHelp() {
  const db = (v) => (v > 0 ? Math.round(20 * Math.log10(v)) : -120);
  const raw = db(meter.raw), floor = meter.floor_db;
  let msg;
  if (state && state.audio && state.audio.stalled) msg = "Audio has stopped: the sound card stopped taking data. Restart the app (stop and start it in App Lab).";
  else if (state && state.devices.input !== "open") msg = `Mic input: ${state.devices.input}.`;
  else if (raw <= -100) msg = "No signal at all from the mic input. Is the input device right, and the mic plugged in?";
  else if (floor > -35) msg = `The input is noisy (noise floor ${Math.round(floor)} dB). A 3-pole mic in the combo jack, a loose plug, or a headset mic the Media Carrier doesn't power does this.`;
  else msg = `Mic level ${raw} dB, noise floor ${Math.round(floor)} dB.`;
  $("#in-help").textContent = msg;
  // the mic card says so too: that noise ends up in everything you hear
  const noisy = floor > -35 && raw > -100;
  $("#mic-warn").hidden = !noisy;
  if (noisy) $("#mic-warn").textContent = (state && state.settings.mic_mute ? "" : "Mute the mic: ") +
    `its input is just loud noise (noise floor ${Math.round(floor)} dB), which leaks into everything you hear. ` +
    "Headset mics in the Media Carrier's combo jack can do this; a USB mic (settings → audio in) avoids it.";
}

const TEST_HELP = $("#test-status").innerHTML;
socket.on("audio_test", (m) => {
  $("#test-status").innerHTML = m.done ? TEST_HELP : `Now playing: <b>${m.label}</b> (${m.beeps} beep${m.beeps > 1 ? "s" : ""}).`;
});

$("#tone").onclick = () => api("POST", "/api/audio/tone", { freq: 440, beeps: 2 }).catch((e) => toast(e.message));
$("#identify").onclick = () => api("POST", "/api/audio/identify").catch((e) => toast(e.message));
$("#refresh-devices").onclick = loadDevices;

document.querySelectorAll("[data-sys]").forEach((b) => (b.onclick = async () => {
  const action = b.dataset.sys;
  await api("POST", `/api/system/${action}`);
  toast("done");
}));

// ---------------------------------------------------------------------------

let lastPadKey = "";
function render() {
  $("#enrol-hint").hidden = !state.enrol;
  $("#enrol-hint").textContent = state.enrol || "";
  renderFx();
  renderBt();
  renderLibrary();
  renderSettings();
  // rebuilding the pad grid is the only expensive bit; skip it when nothing changed
  const key = `${state.version}/${state.pads_connected}/${(state.pad_roles || []).join()}/${state.library.length}/${state.library.map((i) => i.name).join()}`;
  if (key !== lastPadKey) {
    lastPadKey = key;
    renderPads();
    if (editing !== null) fillEditor();
  }
}

buildSettings();
loadDevices();
api("GET", "/api/state").then((s) => { state = s; render(); }).catch(() => {});

// Phones open captive portals in a stripped-down web view where file pickers
// are unreliable; nudge people towards the real browser.
if (/CaptiveNetworkSupport|wispr|CaptivePortalLogin/i.test(navigator.userAgent) || !window.OfflineAudioContext) {
  $("#cna-hint").hidden = false;
  $("#cna-hint .url").textContent = location.origin;
}
