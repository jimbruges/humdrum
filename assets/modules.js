// Humdrum modules page: what each Modulino does, in what order, live feedback
// from each one, and setting up new or mixed-up modules.
"use strict";

const $ = (s, el = document) => el.querySelector(s);

let state = null;
let meter = {};
let cardSig = "";
const knobTotals = {};  // addr -> detents turned since the page opened

// ---------------------------------------------------------------------------
// transport
// ---------------------------------------------------------------------------

const socket = io({ transports: ["websocket", "polling"] });
socket.on("connect", () => $("#conn").classList.remove("off"));
socket.on("disconnect", () => $("#conn").classList.add("off"));
socket.on("state", (s) => { state = s; render(); });
// knob turns and sliders send just the settings (and audio stats), merged into the last full state
socket.on("settings", (s) => { if (state) { Object.assign(state, s); render(); } });
socket.on("meter", (m) => { meter = m; renderLive(); });
socket.on("device", onDevice);

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
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

const fail = (e) => toast(e.message);
const hex = (a) => "0x" + (typeof a === "number" ? a.toString(16) : a).padStart(2, "0");
const KIND_NAMES = { buttons: "Buttons", knob: "Knob", pixels: "Pixels", buzzer: "Buzzer", distance: "Distance", unknown: "unknown" };
const BT_KEYS = ["prev", "play/pause", "next"];
const PAD_KEYS = ["A", "B", "C"];

// ---------------------------------------------------------------------------
// module cards
// ---------------------------------------------------------------------------

// Every module in the order it is numbered: [{kind, addr, ...}] per group
function groups() {
  const c = state.controls, t = state.topology;
  return [
    ["buttons", "Buttons", c.modules.map((m) => ({ ...m, index: m.module,
      sub: m.role === "pads" ? `pads ${m.pads[0]}–${m.pads[1]}` : "Bluetooth controls",
      roles: c.button_roles, role: m.role, keys: m.role === "pads" ? PAD_KEYS : BT_KEYS }))],
    ["knob", "Knobs", c.knobs.map((k) => ({ ...k, index: k.knob, roles: c.knob_roles, role: k.role,
      sub: `long press swaps effect / volume` }))],
    ["pixels", "Pixels", c.pixels.map((p) => ({ ...p, roles: c.pixels_roles, role: p.role, sub: "level meter" }))],
    ["distance", "Distance", t.distance ? [{ index: 0, addr: "29", sub: "theremin hand sensor" }] : []],
    ["buzzer", "Buzzer", t.buzzer ? [{ index: 0, addr: "", sub: "follows the buzzer setting on the main page" }] : []],
  ];
}

function render() {
  $("#enrol-hint").hidden = !state.enrol;
  $("#enrol-hint").textContent = state.enrol || "";
  const t = state.topology;
  $("#count").textContent = `${t.modules + t.knobs + (state.controls.pixels.length)} modules`;
  const sig = JSON.stringify([state.controls, t.distance, t.buzzer]);
  if (sig !== cardSig) {
    cardSig = sig;
    renderCards();
  }
  renderBus();
  renderSetup();
  renderLive();
}

function renderCards() {
  const wrap = $("#groups");
  wrap.innerHTML = "";
  for (const [kind, title, items] of groups()) {
    if (!items.length && (kind === "distance" || kind === "buzzer")) continue;
    const g = document.createElement("div");
    g.className = "mod-group";
    g.innerHTML = `<h3>${title} <small>${items.length || "none plugged in"}</small></h3>`;
    items.forEach((m, i) => g.appendChild(card(kind, m, i, items)));
    wrap.appendChild(g);
  }
}

function card(kind, m, i, items) {
  const el = document.createElement("div");
  el.className = "mod-card";
  el.dataset.key = `${kind}:${m.addr}`;
  const title = `${KIND_NAMES[kind]}${items.length > 1 ? " " + (i + 1) : ""}`;
  el.innerHTML = `
    <div class="mod-pos mono">${i + 1}</div>
    <div class="mod-body">
      <div class="mod-title"><b>${title}</b>${m.addr ? ` <span class="mono">${hex(m.addr)}</span>` : ""}
        <small>${m.sub || ""}</small></div>
      <div class="mod-live">${liveMarkup(kind, m)}</div>
    </div>
    <div class="mod-actions"></div>`;
  const actions = $(".mod-actions", el);
  if (m.roles) {
    const sel = document.createElement("select");
    Object.entries(m.roles).forEach(([k, v]) => sel.add(new Option(v, k)));
    sel.value = m.role;
    sel.onchange = () => setRole(kind, m, sel.value);
    actions.append(sel);
  }
  if (kind === "knob") {
    const mode = document.createElement("select");
    mode.add(new Option("now: effect", "fx"));
    mode.add(new Option("now: volume", "volume"));
    if ((state.controls.knob_cycle?.[m.role] || []).includes("slot")) mode.add(new Option("now: pad slot", "slot"));
    mode.value = m.mode;
    mode.onchange = () => {
      const modes = [...state.settings.knob_modes];
      modes[m.index] = mode.value;
      api("POST", "/api/settings", { knob_modes: modes }).catch(fail);
    };
    actions.append(mode);
    const click = document.createElement("select");
    Object.entries(state.controls.knob_clicks).forEach(([k, v]) => click.add(new Option(v, k)));
    click.value = m.click;
    click.onchange = () => {
      const clicks = [...state.settings.knob_clicks];
      clicks[m.index] = click.value;
      api("POST", "/api/settings", { knob_clicks: clicks }).catch(fail);
    };
    actions.append(click);
  }
  const btns = document.createElement("span");
  btns.className = "row";
  if (items.length > 1 && ["buttons", "knob", "pixels"].includes(kind)) {
    btns.append(button("↑", "move up", i === 0, () => move(kind, items, i, -1)));
    btns.append(button("↓", "move down", i === items.length - 1, () => move(kind, items, i, 1)));
  }
  if (kind === "buttons" || kind === "pixels") {
    btns.append(button("identify", "flash this module", false, () =>
      api("POST", "/api/modules/identify", { addr: m.addr }).catch(fail)));
  }
  actions.append(btns);
  return el;
}

function button(text, title, disabled, onclick) {
  const b = document.createElement("button");
  b.className = "chip";
  b.textContent = text;
  b.title = title;
  b.disabled = disabled;
  b.onclick = onclick;
  return b;
}

function liveMarkup(kind, m) {
  if (kind === "buttons") return `<span class="keys">${m.keys.map((k, b) => `<span class="key" data-b="${b}">${k}</span>`).join("")}</span>`;
  if (kind === "knob") return `<span class="mono knob-total">0</span><span class="mono last-event"></span>`;
  if (kind === "pixels") return `<span class="leds">${"<i></i>".repeat(8)}</span>`;
  if (kind === "distance") return `<span class="bar"><i></i></span><span class="mono hand-mm"></span>`;
  return "";
}

function setRole(kind, m, value) {
  const s = state.settings;
  const changes = kind === "buttons" ? { button_roles: { ...s.button_roles, [m.addr]: value } }
    : kind === "pixels" ? { pixels_roles: { ...s.pixels_roles, [m.addr]: value } }
    : { knob_roles: Object.assign([...s.knob_roles], { [m.index]: value }) };
  api("POST", "/api/settings", changes).catch(fail);
}

function move(kind, items, i, dir) {
  const order = items.map((m) => m.addr);
  [order[i], order[i + dir]] = [order[i + dir], order[i]];
  api("POST", "/api/modules/order", { kind, order }).catch(fail);
}

// ---------------------------------------------------------------------------
// live feedback
// ---------------------------------------------------------------------------

function cardFor(kind, addr) {
  return document.querySelector(`.mod-card[data-key="${kind}:${addr}"]`);
}

function pulse(el) {
  if (!el) return;
  el.classList.remove("active");
  void el.offsetWidth;  // restart the animation
  el.classList.add("active");
}

function onDevice(d) {
  const el = cardFor(d.kind, d.addr);
  pulse(el);
  let what = d.event;
  if (d.kind === "buttons") {
    const key = el && el.querySelector(`.key[data-b="${d.value}"]`);
    if (key) {
      key.classList.toggle("down", d.event !== "up");
      key.classList.toggle("held", d.event === "hold");
    }
    what = `${d.event} button ${PAD_KEYS[d.value] || d.value}`;
  } else if (d.kind === "knob") {
    if (d.event === "turn") {
      knobTotals[d.addr] = (knobTotals[d.addr] || 0) + d.value;
      what = `turn ${d.value > 0 ? "+" : ""}${d.value}`;
    }
    if (el) {
      $(".knob-total", el).textContent = knobTotals[d.addr] || 0;
      $(".last-event", el).textContent = what;
    }
  }
  log(`${KIND_NAMES[d.kind]} ${d.index + 1} (${hex(d.addr)}): ${what}`);
}

let lastHand = null;
function renderLive() {
  if (!state) return;
  (meter.pixels || []).forEach((level, i) => {
    const p = state.controls.pixels[i];
    const el = p && cardFor("pixels", p.addr);
    if (!el) return;
    const lit = Math.round(level * 8);
    el.querySelectorAll(".leds i").forEach((led, l) => {
      led.className = l < lit ? (l < 5 ? "g" : l < 7 ? "a" : "r") : "";
    });
  });
  const dist = cardFor("distance", "29");
  if (dist && meter.hand_mm !== undefined) {
    $(".hand-mm", dist).textContent = meter.hand_mm >= 0 ? `${meter.hand_mm} mm` : "nothing in range";
    $(".bar i", dist).style.width = `${Math.round((meter.hand || 0) * 100)}%`;
    const near = meter.hand_mm >= 0;
    if (near && lastHand !== null && Math.abs(meter.hand_mm - lastHand) > 15) pulse(dist);
    lastHand = near ? meter.hand_mm : null;
  }
}

// ---------------------------------------------------------------------------
// the bus and the event log
// ---------------------------------------------------------------------------

let lastBus = null, lastBad = null, lastEnrol = null;
function renderBus() {
  const t = state.topology;
  const TOF_ERRORS = { 1: "no reply to the ID read", 2: "unexpected ID", 3: "its setup failed" };
  const entries = (t.bus || "").split(",").filter((e) => e.includes(":")).map((e) => {
    const [addr, pin] = e.split(":");
    if (pin.startsWith("tof")) {
      const [, err, id] = pin.match(/^tof(?:!(\d)-(\w+))?/);
      return `${hex(addr)} Distance${err ? ` (not starting: ${TOF_ERRORS[err]}, ID ${hex(id)})` : ""}`;
    }
    const kind = { "7c": "buttons", "74": "knob", "76": "knob", "6c": "pixels", "3c": "buzzer" }[pin] || "unknown";
    return `${hex(addr)} ${KIND_NAMES[kind]}${pin === "?" ? " (garbled reply)" : ""}`;
  });
  const known = Object.keys(state.settings.known_modules || {}).length;
  $("#bus-diag").innerHTML = `
    <dt>answering</dt><dd>${entries.join(" · ") || "nothing"}</dd>
    <dt>bad reads</dt><dd>${t.bad_reads ?? 0}${t.bad_reads ? " (a steady climb means a loose Qwiic cable)" : ""}</dd>
    <dt>addresses used</dt><dd>${known}</dd>`;
  const strays = (state.setup?.modules || []).filter((m) => m.stray);
  $("#strays").hidden = !strays.length || state.setup.on;
  $("#strays").innerHTML = strays.map((m) => `<li><b>${KIND_NAMES[m.kind]}</b> answering at ${hex(m.addr)}, outside its
    address range, so it isn't used. ${m.next ? `<button class="chip" data-setaddr="${m.addr}" data-to="next">give free address ${hex(m.next)}</button>` : ""}
    <button class="chip warn" data-setaddr="${m.addr}" data-to="factory">reset</button></li>`).join("");
  $("#strays").querySelectorAll("[data-setaddr]").forEach((b) => (b.onclick = () =>
    api("POST", "/api/modules/address", { addr: +b.dataset.setaddr, to: b.dataset.to }).catch(fail)));
  if (lastBus !== null && t.bus !== lastBus)
    log(t.bus === "stuck" ? "bus: a module was holding the bus; freed it" : `bus: ${t.bus || "nothing answering"}`);
  if (lastBad !== null && t.bad_reads !== lastBad) log(`bad reads: ${t.bad_reads}`);
  if (state.enrol && state.enrol !== lastEnrol) log(state.enrol);
  lastBus = t.bus; lastBad = t.bad_reads; lastEnrol = state.enrol;
}

function log(text) {
  const li = document.createElement("li");
  li.innerHTML = `<span class="mono">${new Date().toLocaleTimeString()}</span> `;
  li.append(text);
  const list = $("#log");
  list.prepend(li);
  while (list.childElementCount > 60) list.lastElementChild.remove();
}

$("#clear-log").onclick = () => ($("#log").innerHTML = "");
$("#rescan").onclick = () => api("POST", "/api/system/rescan").then(() => toast("rescanning")).catch(fail);

// ---------------------------------------------------------------------------
// module setup: one module plugged in on its own
// ---------------------------------------------------------------------------

let setupSig = "";
function renderSetup() {
  const setup = state.setup || { on: false, modules: [] };
  $("#setup-on").checked = setup.on;
  const list = $("#setup-list");
  list.hidden = !setup.on;
  const sig = JSON.stringify(setup);
  if (!setup.on || sig === setupSig) return;
  setupSig = sig;
  if (!setup.modules.length) {
    list.innerHTML = `<li class="help">Nothing answering. Plug the module into the UNO Q's Qwiic socket.</li>`;
    return;
  }
  const many = setup.modules.filter((m) => m.kind !== "distance").length > 1;
  list.innerHTML = (many ? `<li class="help warn">More than one module is answering. Unplug all but the one to set up.</li>` : "") +
    setup.modules.map((m) => `<li>
      <b>${KIND_NAMES[m.kind]}</b> at ${hex(m.addr)}
      ${m.factory ? "(factory address)" : m.clash ? "(another module in the chain has this address too)" : ""}
      ${m.settable ? `<span class="row">
        <select data-pick="${m.addr}">${m.choices.map((c) => `<option value="${c.addr}" ${c.addr === m.next ? "selected" : ""}>
          ${hex(c.addr)}${c.chain ? " (in the chain)" : c.used ? " (used before)" : ""}</option>`).join("")}</select>
        <button class="chip" data-setaddr="${m.addr}" data-to="pick">give this address</button>
        ${m.factory ? "" : `<button class="chip warn" data-setaddr="${m.addr}" data-to="factory">reset</button>`}
        ${["buttons", "pixels"].includes(m.kind) ? `<button class="chip" data-identify="${m.addr}">identify</button>` : ""}
      </span>` : m.kind === "distance" ? " (fixed address, nothing to set up)" : ""}
    </li>`).join("");
  list.querySelectorAll("[data-setaddr]").forEach((b) => (b.onclick = () => {
    const to = b.dataset.to === "pick" ? +list.querySelector(`[data-pick="${b.dataset.setaddr}"]`).value : b.dataset.to;
    api("POST", "/api/modules/address", { addr: +b.dataset.setaddr, to }).catch(fail);
  }));
  list.querySelectorAll("[data-identify]").forEach((b) => (b.onclick = () =>
    api("POST", "/api/modules/identify", { addr: (+b.dataset.identify).toString(16) }).catch(fail)));
}

$("#setup-on").onchange = (e) => api("POST", "/api/modules/setup", { on: e.target.checked }).catch(fail);
$("#reset-all").onclick = async () => {
  if (!confirm("Send every connected Buttons, Knob and Pixels module back to its factory address? You'll then plug them back in one at a time.")) return;
  await api("POST", "/api/system/reset_addresses").catch(fail);
};
$("#forget").onclick = async () => {
  if (!confirm("Forget every address handed out except those of the modules in the chain?")) return;
  const r = await api("POST", "/api/modules/forget").catch(fail);
  if (r) toast(`kept ${r.kept} addresses`);
};
