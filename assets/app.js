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
  const amount = $("#amount");
  if (document.activeElement !== amount) amount.value = meter.fx_amount ?? state.settings.fx_amount;
  $("#amount-val").textContent = Math.round(amount.value * 100);
}

$("#amount").addEventListener("input", (e) => {
  $("#amount-val").textContent = Math.round(e.target.value * 100);
  sendSettings({ fx_amount: +e.target.value });
});
$("#bypass").onclick = () => sendSettings({ fx_bypass: !state.settings.fx_bypass });
$("#stop-all").onclick = () => api("POST", "/api/system/stop_all");

function renderMeter() {
  const pct = (v) => `${Math.min(100, Math.round(Math.sqrt(v || 0) * 100))}%`;
  $("#m-in").style.setProperty("--w", pct(meter.in));
  $("#m-out").style.setProperty("--w", pct(meter.out));
  $("#m-hand").style.setProperty("--w", `${Math.round((meter.hand || 0) * 100)}%`);
  $("#hand-mm").textContent = meter.hand_mm >= 0 ? `${meter.hand_mm} mm` : "no hand";
  if (!state) return;
  renderFx();
  const playing = new Set(meter.playing || []);
  document.querySelectorAll(".pad").forEach((el) => {
    const p = +el.dataset.pad;
    el.classList.toggle("playing", playing.has(p));
    el.classList.toggle("rec", meter.rec === p);
    el.querySelector(".prog").style.width = meter.rec === p ? `${meter.rec_progress * 100}%` : "0";
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

function renderPads() {
  const wrap = $("#pads");
  const connected = state.pads_connected;
  const extra = state.slots.filter((s) => s.loaded && s.pad >= connected).map((s) => s.pad);
  const total = Math.max(connected, extra.length ? Math.max(...extra) + 1 : 0);
  const modules = Math.ceil(total / 3);
  $("#no-pads").hidden = connected > 0;
  $("#topo").textContent = `${connected} pads${state.topology.knob ? " · knob" : ""}${state.topology.buzzer ? " · buzzer" : ""}${state.topology.distance ? " · distance" : ""}`;

  wrap.innerHTML = "";
  for (let m = 0; m < modules; m++) {
    const row = document.createElement("div");
    row.className = "module";
    row.innerHTML = `<div class="module-label">MOD ${m + 1}</div>`;
    for (let k = 0; k < 3; k++) {
      const p = m * 3 + k;
      const s = slotFor(p);
      const el = document.createElement("button");
      el.className = "pad" + (s.loaded ? " loaded" : "") + (p >= connected ? " offline" : "");
      el.dataset.pad = p;
      el.innerHTML = `
        <div class="top-row"><span class="no">${pad2(p + 1)}</span><span class="led"></span></div>
        ${s.loaded
          ? `<canvas></canvas><div><div class="name"></div><div class="meta">${s.seconds}s · ${s.mode}${s.pitch ? ` · ${s.pitch > 0 ? "+" : ""}${s.pitch}st` : ""}</div></div>`
          : `<div class="plus">+</div><div class="meta">${p >= connected ? "not connected" : "empty"}</div>`}
        <div class="prog"></div>`;
      if (s.loaded) el.querySelector(".name").textContent = s.name;
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
  if (f && uploadTarget !== null) uploadFile(uploadTarget, f);
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
    toast(`uploading to pad ${pad2(pad + 1)}…`, 60000);
    const name = file.name.replace(/\.[^.]+$/, "").slice(0, 24);
    await api("POST", `/api/slots/${pad}`, wav, { "Content-Type": "audio/wav", "X-Slot-Name": encodeURIComponent(name) });
    toast(`pad ${pad2(pad + 1)} loaded`);
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

$("#ed-name").addEventListener("input", (e) => patchSlot(editing, { name: e.target.value }));
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
  if (!confirm(`Clear pad ${pad2(editing + 1)}?`)) return;
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
  { key: "monitor_level", label: "mic monitor", min: 0, max: 1.5, step: 0.01, fmt: (v) => Math.round(v * 100) },
  { key: "sample_level", label: "sample level", min: 0, max: 1.5, step: 0.01, fmt: (v) => Math.round(v * 100) },
  { key: "master", label: "master", min: 0, max: 1.5, step: 0.01, fmt: (v) => Math.round(v * 100) },
  { key: "mic_gain_db", label: "mic gain dB", min: -12, max: 36, step: 1, fmt: (v) => v },
  { key: "gate_db", label: "noise gate dB", min: -90, max: -20, step: 1, fmt: (v) => v },
  { key: "hold_ms", label: "hold to rec ms", min: 200, max: 3000, step: 50, fmt: (v) => v },
  { key: "max_rec_s", label: "max rec s", min: 1, max: 30, step: 1, fmt: (v) => v },
  { key: "knob_step", label: "knob speed", min: 0.005, max: 0.1, step: 0.005, fmt: (v) => Math.round(v * 1000) },
  { key: "theremin_near_mm", label: "theremin near mm", min: 10, max: 300, step: 5, fmt: (v) => v },
  { key: "theremin_far_mm", label: "theremin far mm", min: 100, max: 1200, step: 10, fmt: (v) => v },
  { key: "record_post_fx", label: "record with fx", type: "toggle" },
  { key: "rec_overwrite", label: "rec over loaded pads", type: "toggle" },
  { key: "auto_trim", label: "auto-trim silence", type: "toggle" },
  { key: "normalize", label: "normalise recordings", type: "toggle" },
  { key: "block", label: "audio block", type: "select", options: [256, 512, 1024, 2048] },
  { key: "buzzer_mode", label: "buzzer (modulino)", type: "select", options: ["off", "pads", "follow"] },
  { key: "input_device", label: "input device", type: "text" },
  { key: "output_device", label: "output device", type: "text" },
];

function buildSettings() {
  const wrap = $("#settings");
  wrap.innerHTML = "";
  for (const d of SETTINGS) {
    const row = document.createElement("label");
    row.className = "setting" + (d.type ? ` ${d.type}` : "");
    row.innerHTML = `<span>${d.label}</span>`;
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
      input = Object.assign(document.createElement("input"), { type: "range", min: d.min, max: d.max, step: d.step });
      const out = document.createElement("output");
      input.oninput = () => { out.textContent = d.fmt(+input.value); sendSettings({ [d.key]: +input.value }); };
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
    else input.value = v;
    const out = input.parentElement.querySelector("output");
    if (out) out.textContent = d.fmt(v);
  }
  const t = state.topology;
  $("#diag").innerHTML = `
    <dt>modules</dt><dd>${t.modules} (${t.addrs || "none"})</dd>
    <dt>knob</dt><dd>${t.knob ? "yes" : "no"}</dd>
    <dt>buzzer</dt><dd>${t.buzzer ? "yes" : "no"}</dd>
    <dt>distance</dt><dd>${t.distance ? "yes" : "no"}</dd>
    <dt>input</dt><dd>${state.devices.input}</dd>
    <dt>output</dt><dd>${state.devices.output}</dd>`;
}

document.querySelectorAll("[data-sys]").forEach((b) => (b.onclick = async () => {
  const action = b.dataset.sys;
  if (action === "reset_addresses" && !confirm("Reset all button modules to factory address? You will need to re-plug them one at a time.")) return;
  await api("POST", `/api/system/${action}`);
  toast("done");
}));

// ---------------------------------------------------------------------------

let lastVersion = -1, lastPads = -1;
function render() {
  renderFx();
  renderSettings();
  // rebuilding the pad grid is the only expensive bit; skip it when nothing changed
  const key = `${state.version}/${state.pads_connected}`;
  if (key !== `${lastVersion}/${lastPads}`) {
    lastVersion = state.version; lastPads = state.pads_connected;
    renderPads();
    if (editing !== null) fillEditor();
  }
}

buildSettings();
api("GET", "/api/state").then((s) => { state = s; render(); }).catch(() => {});

// Phones open captive portals in a stripped-down web view where file pickers
// are unreliable; nudge people towards the real browser.
if (/CaptiveNetworkSupport|wispr|CaptivePortalLogin/i.test(navigator.userAgent) || !window.OfflineAudioContext) {
  $("#cna-hint").hidden = false;
  $("#cna-hint .url").textContent = location.origin;
}
