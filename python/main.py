"""Humdrum — Linux side.

Glues together:
  * the MCU (Modulino pads + knobs, LED matrix) over the Bridge
  * the audio engine (mic FX + sampler + Bluetooth audio) in engine.py
  * the Bluetooth helper on the board (pairing, media controls) in bt.py
  * the web UI / captive portal (assets/) over REST + Socket.IO
"""

from __future__ import annotations

import asyncio
import json
import sys
import logging
import math
import queue
import threading
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

import audio_ports
import bt as btmod
import buzzer
import display
from engine import Engine
from fx import Theremin
from slots import MAX_PADS, SlotBank, decode_wav

try:
    # the App Lab runtime only prints loggers it has configured itself
    from arduino.app_utils.logger import configure_library_logger

    configure_library_logger("humdrum", "Humdrum")
    configure_library_logger("engine", "Engine")
except ImportError:
    logging.basicConfig(level=logging.INFO)
log = logging.getLogger("humdrum")


def data_dir() -> Path:
    # /app is the app folder on the board, so this survives restarts and redeploys
    for p in (Path("/app/data"), Path(__file__).resolve().parent.parent / "data"):
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            continue
    raise RuntimeError("no writable data directory")


DATA = data_dir()
SETTINGS_PATH = DATA / "settings.json"

DEFAULTS = {
    "fx_index": 4,
    "fx_amount": 0.34,
    "fx_bypass": False,
    "mic_gain_db": 6.0,  # the codec stays at -6 dB (audio_ports.MIC_HW_GAIN); this does the boost
    "gate_db": -55.0,
    "monitor_level": 0.8,
    "sample_level": 0.5,
    "master": 0.1,
    "record_post_fx": True,
    "rec_overwrite": True,
    "auto_trim": True,
    "normalize": True,
    "max_rec_s": 24,
    "hold_ms": 1950,
    "knob_step": 0.02,
    "theremin_near_mm": 40,
    "theremin_far_mm": 350,
    "buzzer_mode": "off",
    "block": 512,
    "input_device": "jack:1",
    "output_device": "jack:1",
    "carrier_outputs": ["headphones"],  # Media Carrier jacks that play
    "mic_mute": False,
    "bt_level": 0.5,
    "bt_fx_index": 5,
    "bt_fx_amount": 0.0,
    "bt_fx_bypass": False,
    "bt_mute": False,
    # the Bluetooth knob's volume also moves the sample pads' level, by the same amount
    "bt_knob_pads": True,
    # headphone (combo jack) amplifier gain, 1..20 in 1.5 dB steps (audio_ports.hp_gain).
    # 7 suits sensitive earphones with bt_level about 1; 14 was much too loud with them.
    "hp_gain": 7,
    # dB the codec's mic gain sits below its usual -6 dB; raised automatically
    # when a hot mic clips the codec (Engine._mic_clipped), 0 resets it
    "mic_pad_db": 0,
    # What each knob controls, by knob number (plug-in order), and whether it's
    # currently on the effect or the volume (a long press swaps)
    "knob_roles": ["bt", "mic", "mic", "bt"],
    "knob_modes": ["fx", "volume", "fx", "fx"],
    # what clicking each knob does (KNOB_CLICKS)
    "knob_clicks": ["auto", "auto", "auto", "auto"],
    # What each Pixels meter shows, by its I2C address; missing = by plug-in order
    "pixels_roles": {"70": "mic", "71": "play"},
    # What each Buttons module is, by its I2C address ("40", "41", ...); missing = pads
    "button_roles": {"4d": "bt"},
    # Every address a module has been given ("40": "buttons"), so one plugged in
    # on its own can be given a free address without the rest of the chain
    "known_modules": {},
    # The order modules are numbered and shown in, by kind ("buttons", "knob",
    # "pixels"); addresses not listed follow in address order
    "module_order": {},
    # setting up one module on its own (modules page); kept over restarts
    "module_setup": False,
    # the sample kits in samples/ have been laid out on the pads (done once, see load_kits)
    "kit_loaded": False,
    # the modules in the chain the last time it was plugged in ("70": "pixels"),
    # so their addresses aren't given out while the chain is unplugged
    "chain_addrs": {},
}

KNOB_ROLES = {
    "mic": "Mic: FX or volume",
    "bt": "Bluetooth + pads FX, or phone volume",
}
KNOB_MODES = ("fx", "volume")
KNOB_CLICKS = {
    "auto": "click: next effect (FX) / mute (volume)",
    "next_fx": "click: next effect",
    "bypass": "click: effect on/off",
    "mute": "click: mute",
    "playpause": "click: play/pause phone",
    "none": "click: nothing",
}
PIXELS_ROLES = {"play": "Bluetooth + sample pads level", "mic": "Mic level", "off": "Off"}
BUTTON_ROLES = {"pads": "Sample pads", "bt": "Bluetooth controls (previous, play/pause, next)"}
MAX_KNOBS = 4
# what the web UI may change, with (min, max) for numbers
LIMITS = {
    "fx_index": (0, 7), "theremin_near_mm": (10, 300), "theremin_far_mm": (100, 1200), "fx_amount": (0.0, 1.0), "mic_gain_db": (-12.0, 36.0), "gate_db": (-90.0, -20.0), "mic_pad_db": (0, audio_ports.MIC_PAD_MAX_DB),
    "monitor_level": (0.0, 1.5), "sample_level": (0.0, 1.5), "master": (0.0, 1.5), "max_rec_s": (1, 30),
    "hold_ms": (200, 3000), "knob_step": (0.005, 0.1), "block": (256, 2048),
    "hp_gain": (1, 20), "bt_level": (0.0, 1.5), "bt_fx_index": (0, 7), "bt_fx_amount": (0.0, 1.0),
}
DEVICE_KEYS = {"input_device", "output_device", "block"}

# Pad LED states, must match the PadState enum in sketch.ino
EMPTY, LOADED, RECORDING, ARMED, PLAYING = range(5)


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            s.update({k: v for k, v in json.loads(SETTINGS_PATH.read_text()).items() if k in DEFAULTS})
        except ValueError:
            pass
    return s


settings = load_settings()
# Python hands the GIL between threads every 5 ms by default. The audio thread
# needs ~7 of every 10.7 ms; behind a busy web request it fell late and the card
# played a block of silence. At 1 ms it gets back in after any Python-heavy work.
sys.setswitchinterval(0.001)
bank = SlotBank(DATA)
# The kits shipped in samples/ are always in the library; the first time, they're
# also put on the pads (pad 1 up), so a new board plays something out of the box.
try:
    laid_out = bank.load_kits(Path(__file__).resolve().parent.parent / "samples", lay_out=not settings.get("kit_loaded"))
    if laid_out:
        log.info("sample kit put on pads 1-%d", laid_out)
    if not settings.get("kit_loaded"):
        settings["kit_loaded"] = True
        SETTINGS_PATH.write_text(json.dumps(settings, indent=1))
except Exception:
    log.exception("loading the sample kits")
engine = Engine(settings, bank)
ui = WebUI()

topology = {"modules": 0, "knobs": 0, "knob_addrs": "", "pixels_addrs": "", "addrs": "", "buzzer": False,
            "distance": False, "bad_reads": 0, "bus": ""}
bluetooth = btmod.Bluetooth(DATA)
THEREMIN = next(i for i, fx in enumerate(engine.effects) if isinstance(fx, Theremin))
hand_mm = -1  # latest Modulino Distance reading, -1 = nothing in range
follower = buzzer.BuzzerFollower()
mcu_calls: queue.SimpleQueue = queue.SimpleQueue()  # Bridge.call is not allowed inside handlers
save_due = 0.0
state_due = 0.0  # a state push owed to the UI (knob turns), sent at most every STATE_PUSH_S
STATE_PUSH_S = 0.15
banner: tuple | None = None  # temporary matrix label: (text, until, bar row value or None)


def fx_available() -> list[int]:
    """Effects you can pick right now: the theremin needs its sensor."""
    return [i for i in range(len(engine.effects)) if i != THEREMIN or topology["distance"]]


def ensure_fx_available() -> None:
    if settings["fx_index"] not in fx_available():
        settings["fx_index"] = 0
        save_settings_soon()


def save_settings_soon() -> None:
    global save_due
    save_due = time.monotonic() + 1.0


def apply_settings(changes: dict) -> dict:
    reopen = False
    for k, v in changes.items():
        if k not in DEFAULTS or k in ("known_modules", "module_order", "module_setup", "chain_addrs", "kit_loaded"):
            continue
        default = DEFAULTS[k]
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, (int, float)):
            v = type(default)(v)
            lo, hi = LIMITS.get(k, (v, v))
            v = min(max(v, lo), hi)
        elif k == "carrier_outputs":
            v = [p for p in audio_ports.PORTS if p in (v or [])]  # known ports, fixed order
        elif k in ("knob_roles", "knob_modes", "knob_clicks"):
            allowed, fallback = {"knob_roles": (KNOB_ROLES, "mic"), "knob_modes": (KNOB_MODES, "fx"),
                                 "knob_clicks": (KNOB_CLICKS, "auto")}[k]
            v = [r if r in allowed else fallback for r in list(v or [])[:MAX_KNOBS]]
            v += DEFAULTS[k][len(v):]
        elif k == "pixels_roles":
            v = {str(a): r for a, r in dict(v or {}).items() if r in PIXELS_ROLES}
        elif k == "button_roles":
            v = {str(a): r for a, r in dict(v or {}).items() if r in BUTTON_ROLES}
        elif k == "buzzer_mode":
            v = v if v in buzzer.MODES else "off"
            if v == "off":
                mcu_calls.put(("buzz", 0, 0))
        else:
            v = str(v)
        if settings.get(k) != v:
            settings[k] = v
            reopen |= k in DEVICE_KEYS
            if k == "hold_ms":
                mcu_calls.put(("set_hold_ms", int(v)))
            if k == "hp_gain":
                audio_ports.hp_gain = int(v)
            if k == "mic_pad_db" and audio_ports.is_carrier(settings["input_device"]):
                audio_ports.set_mic_gain(int(v))
            if k in ("carrier_outputs", "hp_gain") and audio_ports.is_carrier(settings["output_device"]):
                audio_ports.apply_ports(settings["carrier_outputs"])
    ensure_fx_available()
    if reopen:
        engine.reopen_devices()
    save_settings_soon()
    _push_state_soon()  # coalesced: a full push per click starved the audio thread
    return settings


def logged(fn):
    """Bridge handlers run on the Bridge's thread, which drops exceptions silently."""
    import functools
    import traceback

    @functools.wraps(fn)
    def wrap(*args):
        try:
            return fn(*args)
        except Exception:
            log.error("%s%s failed:\n%s", fn.__name__, args, traceback.format_exc())
    return wrap


def pads_connected() -> int:
    return topology["modules"] * 3


# ---------------------------------------------------------------------------
# MCU -> Linux (Bridge notifications; must not call back into the Bridge here)
# ---------------------------------------------------------------------------


def on_topology(modules: int, knobs: int, addrs: str, flags: int = 0, knob_addrs: str = "", pixels_addrs: str = "") -> None:
    """flags: 1 = Modulino Buzzer present, 2 = Modulino Distance present."""
    global banner
    has_buzzer, has_distance = bool(int(flags) & 1), bool(int(flags) & 2)
    topology.update(modules=int(modules), knobs=int(knobs), knob_addrs=str(knob_addrs), addrs=str(addrs),
                    buzzer=bool(has_buzzer), distance=bool(has_distance), pixels_addrs=str(pixels_addrs))
    log.info("topology: %d button modules (%s), %d knobs (%s), pixels (%s), buzzer=%s, distance=%s",
             modules, addrs, int(knobs), knob_addrs, pixels_addrs, bool(has_buzzer), bool(has_distance))
    if not setup_mode:
        chain = {a: kind for kind, addrs_ in (("buttons", addrs), ("knob", knob_addrs), ("pixels", pixels_addrs))
                 for a in str(addrs_).split(",") if a}
        if chain and chain != settings["chain_addrs"]:
            settings["chain_addrs"] = chain
            settings["known_modules"].update(chain)
            save_settings_soon()
    if not has_distance:
        on_hand(-1)
    ensure_fx_available()
    banner = (f"{pad_count():>2}P" if modules else "NO", time.monotonic() + 1.5, None)
    mcu_calls.put(("set_hold_ms", int(settings["hold_ms"])))
    push_state()


# The MCU numbers each kind of module in address order ("physical"); the
# rest of the app works in the order chosen on the modules page ("logical").
TOPOLOGY_KEYS = {"buttons": "addrs", "knob": "knob_addrs", "pixels": "pixels_addrs"}


def physical_addrs(kind: str) -> list[str]:
    return [a for a in topology[TOPOLOGY_KEYS[kind]].split(",") if a]


def ordered_addrs(kind: str) -> list[str]:
    phys = physical_addrs(kind)
    order = [a for a in settings["module_order"].get(kind, []) if a in phys]
    return order + [a for a in phys if a not in order]


def logical_index(kind: str, i: int) -> int:
    phys = physical_addrs(kind)
    return ordered_addrs(kind).index(phys[i]) if 0 <= i < len(phys) else i


def logical_pad(pad: int) -> int:
    return logical_index("buttons", pad // 3) * 3 + pad % 3


def module_addrs() -> list[str]:
    return ordered_addrs("buttons")


def device_event(kind: str, index: int, event: str, value: int = 0) -> None:
    """Live feedback for the modules page; index is the logical one."""
    addrs = ordered_addrs(kind)
    ui.send_message("device", {"kind": kind, "index": index, "addr": addrs[index] if index < len(addrs) else "",
                               "event": event, "value": value})


def module_role(module: int) -> str:
    addrs = module_addrs()
    return settings["button_roles"].get(addrs[module], "pads") if module < len(addrs) else "pads"


def pad_count() -> int:
    return sum(3 for m in range(topology["modules"]) if module_role(m) == "pads")


BT_BUTTONS = ("previous", "playpause", "next")


def on_pad_down(pad: int) -> None:
    pad = logical_pad(int(pad))
    device_event("buttons", pad // 3, "down", pad % 3)
    if module_role(pad // 3) == "bt":
        bt_command(BT_BUTTONS[pad % 3])
        return
    engine.post("down", pad)
    if topology["buzzer"] and settings["buzzer_mode"] == "pads":
        mcu_calls.put(("buzz", buzzer.pad_freq(pad), 120))


def on_pad_up(pad: int, held_ms: int = 0) -> None:
    pad = logical_pad(int(pad))
    device_event("buttons", pad // 3, "up", pad % 3)
    if module_role(pad // 3) == "pads":
        engine.post("up", pad)


def on_pad_hold(pad: int) -> None:
    pad = logical_pad(int(pad))
    device_event("buttons", pad // 3, "hold", pad % 3)
    if module_role(pad // 3) != "pads":
        return
    log.info("pad %d held: recording", pad + 1)
    engine.post("hold", pad)


def knob_role(knob: int) -> str:
    roles = settings["knob_roles"]
    return roles[knob] if 0 <= knob < len(roles) else "mic"


def knob_mode(knob: int) -> str:
    modes = settings["knob_modes"]
    return modes[knob] if 0 <= knob < len(modes) else "fx"


def knob_click(knob: int) -> str:
    clicks = settings["knob_clicks"]
    return clicks[knob] if 0 <= knob < len(clicks) else "auto"


# Volume knobs move in dB: 1.5 dB a click at the default knob speed, from
# KNOB_DB_MIN (below it: off) up to LEVEL_MAX. Linear steps of 0.05 made the
# bottom of the range a few huge jumps once the output was 48 dB louder.
KNOB_DB_MIN = -60.0
KNOB_DB_PER_STEP = 1.5  # at knob_step 0.02


def _db_pos(v: float) -> float:
    """0..1: where a level sits between KNOB_DB_MIN and LEVEL_MAX."""
    if v <= 0:
        return 0.0
    top = 20 * math.log10(LEVEL_MAX)
    return min(1.0, max(0.0, (20 * math.log10(v) - KNOB_DB_MIN) / (top - KNOB_DB_MIN)))


def _scale_db(key: str, db: float) -> None:
    v = settings[key]
    if v <= 0:
        if db <= 0:
            return
        v = 10 ** (KNOB_DB_MIN / 20)
    v *= 10 ** (db / 20)
    settings[key] = 0.0 if v < 10 ** (KNOB_DB_MIN / 20) else min(LEVEL_MAX, v)


def _step_db(key: str, delta: int) -> float | None:
    """Move a level by `delta` knob clicks in dB. Returns the dB it moved (None if
    it couldn't move), for the pads to follow."""
    global state_due
    before = settings[key]
    step_db = delta * KNOB_DB_PER_STEP * settings["knob_step"] / 0.02
    _scale_db(key, step_db)
    after = settings[key]
    save_settings_soon()
    state_due = state_due or time.monotonic() + STATE_PUSH_S
    if after == before:
        return None  # at the top or already off: nothing moved
    if before > 0 and after > 0:
        return 20 * math.log10(after / before)
    return step_db  # into or out of silence: the click's worth


def _push_state_soon() -> None:
    """Push the state to the web UI within STATE_PUSH_S, once however many changes come in."""
    global state_due
    state_due = state_due or time.monotonic() + STATE_PUSH_S


def _step(key: str, delta: int, hi: float, scale: float = 1.0) -> None:
    global state_due
    settings[key] = min(hi, max(0.0, settings[key] + delta * settings["knob_step"] * scale))
    save_settings_soon()
    # the web UI's sliders follow the knob; a full push per detent would flood it, so coalesce
    state_due = state_due or time.monotonic() + STATE_PUSH_S


def _next_fx(key: str, bypass_key: str, theremin: bool) -> None:
    available = [i for i in fx_available() if theremin or i != THEREMIN]
    pos = available.index(settings[key]) if settings[key] in available else -1
    settings[key] = available[(pos + 1) % len(available)]
    settings[bypass_key] = False
    save_settings_soon()
    _push_state_soon()


def _show(text: str, secs: float = 0.8, bar: float | None = None) -> None:
    global banner
    banner = (text, time.monotonic() + secs, bar)


# what the Bluetooth controls show on the matrix
BT_ICONS = {"previous": "<<", "next": ">>", "play": ">", "pause": "|"}


def bt_command(cmd: str) -> None:
    if cmd == "playpause":
        cmd = "pause" if bluetooth.state.get("playing") else "play"
    _show(BT_ICONS.get(cmd, "BT") if bluetooth.state.get("connected") else "NO", 1.0)
    threading.Thread(target=bluetooth.command, args=(cmd,), daemon=True).start()


LEVEL_MAX = 1.5  # top of the knob's volume range (LIMITS for monitor_level and bt_level)

# per role: (fx index, fx amount, bypass, level key, effects) settings it drives
KNOB_KEYS = {
    "mic": ("fx_index", "fx_amount", "fx_bypass", "monitor_level"),
    "bt": ("bt_fx_index", "bt_fx_amount", "bt_fx_bypass", "bt_level"),
}


def on_knob_turn(knob: int, delta: int = 0) -> None:
    knob = logical_index("knob", int(knob))
    device_event("knob", knob, "turn", int(delta))
    role, mode = knob_role(knob), knob_mode(knob)
    idx_key, amount_key, bypass_key, level_key = KNOB_KEYS[role]
    if mode == "fx":
        _step(amount_key, int(delta), 1.0)
        settings[bypass_key] = False  # turning the effect up means you want to hear it
        if role == "bt":
            _show("B" + engine.bt_effects[settings[idx_key]].short[:2], bar=settings[amount_key])
    else:
        moved_db = _step_db(level_key, int(delta))
        if role == "bt" and settings.get("bt_knob_pads", True) and moved_db is not None:
            # the pads follow by the same number of dB, so the balance holds
            _scale_db("sample_level", moved_db)
        # e.g. "B65": where the level sits on the knob's dB range, with a bar under it
        pos = _db_pos(settings[level_key])
        _show(("M" if role == "mic" else "B") + f"{min(99, round(pos * 100)):02d}", bar=pos)


def on_knob_click(knob: int = 0) -> None:
    knob = logical_index("knob", int(knob))
    device_event("knob", knob, "click")
    role, mode = knob_role(knob), knob_mode(knob)
    idx_key, amount_key, bypass_key, level_key = KNOB_KEYS[role]
    action = knob_click(knob)
    if action == "auto":
        action = "next_fx" if mode == "fx" else "mute"
    log.info("knob %d click (%s %s): %s", int(knob) + 1, role, mode, action)
    effects = engine.effects if role == "mic" else engine.bt_effects
    prefix = "" if role == "mic" else "B"
    if action == "next_fx":
        _next_fx(idx_key, bypass_key, theremin=True)
        # the amount carries over to the next effect; show this knob's own amount
        # (without bar= the matrix showed the mic's fx_amount, even for the Bluetooth knob)
        _show(prefix + effects[settings[idx_key]].short[: 3 - len(prefix)], 1.0, bar=settings[amount_key])
    elif action == "bypass":
        settings[bypass_key] = not settings[bypass_key]
        _show("OFF" if settings[bypass_key] else prefix + effects[settings[idx_key]].short[: 3 - len(prefix)], 1.0, bar=settings[amount_key])
    elif action == "mute":
        key = "mic_mute" if role == "mic" else "bt_mute"
        settings[key] = not settings[key]
        _show("OFF" if settings[key] else ("MIC" if role == "mic" else "BT"), 1.0, bar=settings[level_key] / LEVEL_MAX)
    elif action == "playpause":
        bt_command("playpause")
    save_settings_soon()
    _push_state_soon()


def on_knob_long(knob: int = 0) -> None:
    """Swap the knob between its effect and its volume."""
    k = logical_index("knob", int(knob))
    device_event("knob", k, "long")
    modes = list(settings["knob_modes"])
    while len(modes) <= k:
        modes.append("fx")
    modes[k] = "volume" if knob_mode(k) == "fx" else "fx"
    settings["knob_modes"] = modes
    log.info("knob %d hold: now %s %s", k + 1, knob_role(k), modes[k])
    _show("VOL" if modes[k] == "volume" else "FX", 1.2)
    save_settings_soon()
    push_state()


def pixels_addrs() -> list[str]:
    return ordered_addrs("pixels")


def pixels_role(i: int) -> str:
    addrs = pixels_addrs()
    default = ("play", "mic")[i] if i < 2 else "off"
    return settings["pixels_roles"].get(addrs[i], default) if i < len(addrs) else default


def fx_tint(role: str) -> float:
    idx_key, amount_key, bypass_key, _ = KNOB_KEYS[role]
    return 0.0 if settings[bypass_key] else round(float(settings[amount_key]), 2)


def meter_byte(level: float) -> int:
    """Peak level -> 0..255 on a -42..0 dB scale (one LED is about 5 dB)."""
    if level <= 0:
        return 0
    db = 20 * math.log10(level)
    return int(round(min(1.0, max(0.0, (db + 42) / 42)) * 255))


BUS_HELD_MSG = ("A module is holding the Qwiic bus's clock line low, so nothing answers. It's usually the one just "
                "given a new address, or a new one still on its factory address: unplug it and plug it back in. If "
                "you can't tell which, unplug modules one at a time from the end of the chain until the rest come back.")


def on_bus(report: str) -> None:
    global enrol_msg
    report = str(report)
    # "lines:<SDA*2 + SCL>:...": nothing answered; the sketch read the two lines
    held = report.startswith("lines:") and not int(report.split(":")[1]) & 1
    if held:
        enrol_msg = BUS_HELD_MSG
    elif enrol_msg == BUS_HELD_MSG:
        enrol_msg = ""
    if report.startswith("lines:"):
        log.warning("Qwiic bus: nothing answering (%s)", "clock held low by a module" if held else report)
        report = ""
    topology["bus"] = report
    if report == "stuck":
        log.warning("Qwiic bus: a module was holding SDA low; clocked it free")
    else:
        log.info("Qwiic bus: %s", report or "nothing answering")
    if not setup_mode:
        remember_modules()
    push_state()


# ---------------------------------------------------------------------------
# Module setup: one module plugged in on its own, straight into the UNO Q.
# A module can come back from a mix-up holding an address that another module
# in the chain already has; then the two answer together and neither works.
# On its own it can be seen whatever address it holds, and reset or given a
# free one. Automatic setup is paused meanwhile, so nothing moves by itself.
# ---------------------------------------------------------------------------

MODULE_KINDS = {"7c": "buttons", "74": "knob", "76": "knob", "6c": "pixels", "3c": "buzzer", "tof": "distance"}
MODULE_ADDRS = {"buttons": [a for a in range(0x40, 0x60) if a != 0x44],
                "knob": list(range(0x60, 0x68)), "pixels": list(range(0x70, 0x78))}
FACTORY_ADDRS = {"buttons": (0x3E,), "knob": (0x3A, 0x3B), "pixels": (0x36,)}
setup_mode = settings["module_setup"]


def bus_modules() -> list[dict]:
    out = []
    for item in topology["bus"].split(","):
        addr, _, pin = item.partition(":")
        try:
            a = int(addr, 16)
        except ValueError:
            continue
        kind = "distance" if pin.startswith("tof") else MODULE_KINDS.get(pin, "unknown")
        out.append({"addr": a, "kind": kind, "factory": a in FACTORY_ADDRS.get(kind, ()),
                    "settable": kind in MODULE_ADDRS,
                    # answering outside its kind's range, so not used: give it a free address
                    "stray": kind in MODULE_ADDRS and a not in MODULE_ADDRS[kind] and a not in FACTORY_ADDRS[kind]})
    return out


def remember_modules() -> None:
    known = settings["known_modules"]
    for m in bus_modules():
        key = f"{m['addr']:02x}"
        if m["settable"] and not m["factory"] and known.get(key) != m["kind"]:
            known[key] = m["kind"]
            save_settings_soon()


def next_free_addr(kind: str, current: int = 0) -> int:
    taken = {int(a, 16) for a in (*settings["known_modules"], *settings["chain_addrs"])}
    return next((a for a in MODULE_ADDRS.get(kind, []) if a not in taken or a == current), 0)


def setup_state() -> dict:
    mods = bus_modules()
    for m in mods:
        free = next_free_addr(m["kind"]) if m["settable"] else 0
        m["next"] = free
        # every address it could be given: "chain" = a module in the chain has it,
        # "used" = handed out before, maybe to a module that's since moved on
        m["choices"] = [{"addr": a, "chain": f"{a:02x}" in settings["chain_addrs"],
                         "used": f"{a:02x}" in settings["known_modules"]}
                        for a in MODULE_ADDRS.get(m["kind"], [])]
        # the address it holds is already taken by another module in the chain
        m["clash"] = (not m["factory"] and m["settable"] and setup_mode
                      and settings["chain_addrs"].get(f"{m['addr']:02x}") == m["kind"])
    return {"on": setup_mode, "modules": mods}


async def api_module_order(request: Request):
    """Body: {"kind": "knob", "order": ["61", "60"]}. Knob jobs follow their knob."""
    body = await request.json()
    kind = body.get("kind")
    if kind not in TOPOLOGY_KEYS:
        return JSONResponse({"error": "unknown kind"}, status_code=400)
    before = ordered_addrs(kind)
    order = [str(a) for a in body.get("order", []) if str(a) in before]
    order += [a for a in before if a not in order]
    if kind == "knob":
        for key in ("knob_roles", "knob_modes", "knob_clicks"):
            old = list(settings[key])
            new = [old[before.index(a)] if before.index(a) < len(old) else DEFAULTS[key][0] for a in order]
            settings[key] = (new + old[len(new):])[:MAX_KNOBS]
    settings["module_order"] = {**settings["module_order"], kind: order}
    save_settings_soon()
    push_state()
    return {"ok": True}


async def api_module_forget():
    """Forget every address handed out except those of the chain, so they can be given out again."""
    now = {**settings["chain_addrs"], **{a: k for k in TOPOLOGY_KEYS for a in physical_addrs(k)}}
    settings["known_modules"] = now
    save_settings_soon()
    push_state()
    return {"ok": True, "kept": len(now)}


async def api_module_identify(request: Request):
    """Body: {"addr": "41"}. Buttons flash their LEDs, Pixels flash white."""
    addr = str((await request.json()).get("addr", ""))
    try:
        mcu_calls.put(("identify", int(addr, 16)))
    except ValueError:
        return JSONResponse({"error": "bad address"}, status_code=400)
    return {"ok": True}


async def api_module_setup(request: Request):
    global setup_mode
    setup_mode = settings["module_setup"] = bool((await request.json()).get("on"))
    save_settings_soon()
    mcu_calls.put(("set_auto_enrol", not setup_mode))
    if not setup_mode:
        mcu_calls.put(("rescan",))
    push_state()
    return {"ok": True}


async def api_module_address(request: Request):
    """Body: {"addr": 0x45, "to": "factory" | "next" | an address}. Only while in setup mode."""
    body = await request.json()
    mod = next((m for m in bus_modules() if m["addr"] == int(body.get("addr", -1))), None)
    if not setup_mode and not (mod and mod["stray"]):
        return JSONResponse({"error": "turn on module setup first"}, status_code=409)
    if not mod or not mod["settable"]:
        return JSONResponse({"error": "no module that can change address there"}, status_code=404)
    if body.get("to") == "factory":
        to = 0
    elif isinstance(body.get("to"), int):
        to = body["to"]
        if to not in MODULE_ADDRS[mod["kind"]]:
            return JSONResponse({"error": f"0x{to:02x} isn't a {mod['kind']} address"}, status_code=400)
        settings["known_modules"][f"{to:02x}"] = mod["kind"]
        save_settings_soon()
    else:
        to = next_free_addr(mod["kind"])
        if not to:
            return JSONResponse({"error": f"no free {mod['kind']} addresses left"}, status_code=409)
        settings["known_modules"][f"{to:02x}"] = mod["kind"]
        save_settings_soon()
    mcu_calls.put(("set_address", mod["addr"], to))
    global enrol_msg
    where = f"address 0x{to:02x}" if to else "its factory address"
    enrol_msg = (f"Module 0x{mod['addr']:02x} sent to {where}. Unplug it and plug it back in to make it take effect"
                 + (", then turn module setup off and put it back in the chain." if to else
                    ". Then turn module setup off and add it to the chain, and it will be set up there."))
    push_state()
    return {"ok": True, "to": to}


# What the person setting up modules should do next, shown at the top of the web UI
enrol_msg = ""


def on_enrol(addr: int, state: int) -> None:
    global enrol_msg
    addr, state = int(addr), int(state)
    if state == 1:
        enrol_msg = (f"New module found and given address 0x{addr:02x}. Unplug that module and plug it back in "
                     "to finish setting it up. Add one new module at a time.")
        _show("NEW", 3.0)
    elif state == 2:
        enrol_msg = ""
        _show("OK", 1.5)
    elif state == 3:
        settings["known_modules"] = {}
        save_settings_soon()
        enrol_msg = ("Every module was sent back to its factory address. Unplug them all, then plug them back in "
                     "one at a time, in the order you want them numbered, replugging each one when asked.")
    log.info("module setup: %s", enrol_msg or f"0x{addr:02x} ready")
    push_state()


Bridge.provide("enrol", logged(on_enrol))
Bridge.provide("bus", logged(on_bus))
Bridge.provide("topology", logged(on_topology))
Bridge.provide("pad_down", logged(on_pad_down))
Bridge.provide("pad_up", logged(on_pad_up))
Bridge.provide("pad_hold", logged(on_pad_hold))
Bridge.provide("knob_turn", logged(on_knob_turn))
Bridge.provide("knob_click", logged(on_knob_click))


def hand_closeness(mm: int) -> float:
    """0 = far or nothing in range, 1 = at the near limit."""
    near, far = settings["theremin_near_mm"], max(settings["theremin_far_mm"], settings["theremin_near_mm"] + 10)
    if mm < 0 or mm >= far:
        return 0.0
    return min(1.0, (far - mm) / (far - near))


def on_hand(mm: int) -> None:
    global hand_mm
    hand_mm = int(mm)
    engine.effects[THEREMIN].hand = engine.bt_effects[THEREMIN].hand = hand_closeness(hand_mm)


def on_bad_reads(count: int) -> None:
    # corrupted Modulino reads the sketch threw away; a steady climb means a flaky Qwiic cable
    topology["bad_reads"] = int(count)
    log.warning("Qwiic bus: %d corrupted Modulino reads rejected so far", int(count))


Bridge.provide("bad_reads", logged(on_bad_reads))
Bridge.provide("knob_long", logged(on_knob_long))
Bridge.provide("hand", logged(on_hand))


# ---------------------------------------------------------------------------
# State for the UI
# ---------------------------------------------------------------------------


def full_state() -> dict:
    n = max(pads_connected(), 0)
    roles = [module_role(m) for m in range(topology["modules"])]
    return {
        "settings": settings,
        "defaults": {k: v for k, v in DEFAULTS.items() if isinstance(v, (int, float, str))},
        "fx": [{"name": fx.name, "short": fx.short, "available": i in fx_available()}
               for i, fx in enumerate(engine.effects)],
        "topology": topology,
        "pads_connected": n,
        # per pad: "pad", or the Bluetooth control it is
        "pad_roles": [("pad" if roles[p // 3] == "pads" else f"bt-{BT_BUTTONS[p % 3]}") for p in range(n)],
        "controls": {
            "knobs": [{"knob": k, "addr": a, "role": knob_role(k), "mode": knob_mode(k), "click": knob_click(k)}
                      for k, a in enumerate(ordered_addrs("knob"))],
            "knob_clicks": KNOB_CLICKS,
            "pixels": [{"index": i, "addr": a, "role": pixels_role(i)} for i, a in enumerate(pixels_addrs())],
            "pixels_roles": PIXELS_ROLES,
            "modules": [{"module": m, "addr": a, "role": roles[m], "pads": [m * 3 + 1, m * 3 + 3]} for m, a in enumerate(module_addrs())],
            "knob_roles": KNOB_ROLES,
            "button_roles": BUTTON_ROLES,
        },
        "library": bank.library.describe(),
        "enrol": enrol_msg,
        "setup": setup_state(),
        "bluetooth": bluetooth.state,
        "bt_audio": bool(engine.bt_in.node),
        "slots": [bank.describe(p) for p in range(MAX_PADS) if p < n or bank.slots[p].loaded],
        "devices": engine.device_status,
        "audio": {"carrier": audio_ports.is_carrier(settings["output_device"]), **engine.stats,
                  "bt_underruns": engine.bt_in.underruns, "bt_fill_ms": engine.bt_in.fill_ms},
        "version": bank.version,
    }


def _pixels_logical() -> list[int]:
    """The meter levels last sent, in the logical order."""
    phys = physical_addrs("pixels")
    return [_last_meters[phys.index(a)] if phys.index(a) < len(_last_meters) else 0 for a in pixels_addrs()]


def push_state() -> None:
    ui.send_message("state", full_state())


def pad_states() -> bytes:
    out = bytearray(MAX_PADS)
    bts = bluetooth.state
    for p in range(MAX_PADS):
        if p // 3 < topology["modules"] and module_role(p // 3) == "bt":
            # previous/next light while a phone is connected; play/pause flashes while it plays
            if not bts["connected"]:
                out[p] = EMPTY
            elif p % 3 == 1:
                out[p] = ARMED if bts["playing"] else LOADED
            else:
                out[p] = LOADED
        elif engine.recording_pad == p:
            out[p] = RECORDING
        elif p in engine.playing:
            out[p] = PLAYING
        elif bank.slots[p].loaded:
            out[p] = LOADED
    return bytes(out)


def on_recorded(pad: int) -> None:
    engine.post("slot_changed", pad)
    push_state()


engine.on_recorded = on_recorded


def on_mic_pad(pad_db: int) -> None:
    save_settings_soon()
    push_state()


engine.on_mic_pad = on_mic_pad
_bt_was_connected = False


def on_bt_change(state: dict) -> None:
    global _bt_was_connected
    if state["connected"] != _bt_was_connected:
        _bt_was_connected = state["connected"]
        _show("BT" if state["connected"] else "NO", 1.5)
    push_state()


bluetooth.on_change = on_bt_change


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------


def _pad_ok(pad: int) -> bool:
    return 0 <= pad < MAX_PADS


async def api_state():
    return full_state()


async def api_settings(request: Request):
    return apply_settings(await request.json())


async def api_upload(pad: int, request: Request):
    """Body: a WAV file. The browser decodes any format and sends 48 kHz mono WAV."""
    if not _pad_ok(pad):
        return JSONResponse({"error": "bad pad"}, status_code=400)
    body = await request.body()
    try:
        audio = decode_wav(body)
    except Exception as e:
        return JSONResponse({"error": f"could not read WAV: {e}"}, status_code=415)
    name = unquote(request.headers.get("x-slot-name", ""))[:24]
    bank.set_audio(pad, audio, name=name)
    engine.post("slot_changed", pad)
    push_state()
    return bank.describe(pad)


async def api_slot_patch(pad: int, request: Request):
    """Body: {"sample": library id} to put a sample on the pad, or playback settings."""
    if not _pad_ok(pad):
        return JSONResponse({"error": "bad pad"}, status_code=400)
    body = await request.json()
    if "sample" in body:
        sid = str(body["sample"] or "")
        if sid and sid not in bank.library.items:
            return JSONResponse({"error": "no such sample"}, status_code=404)
        bank.assign(pad, sid)
    elif not bank.slots[pad].loaded:
        return JSONResponse({"error": "empty pad"}, status_code=404)
    else:
        bank.update_meta(pad, body)
    engine.post("slot_changed", pad)
    push_state()
    return bank.describe(pad)


async def api_slot_delete(pad: int):
    if _pad_ok(pad):
        bank.clear(pad)
        engine.post("slot_changed", pad)
        push_state()
    return {"ok": True}


async def api_slot_audio(pad: int):
    data = bank.wav_bytes(pad) if _pad_ok(pad) else None
    if data is None:
        return JSONResponse({"error": "empty pad"}, status_code=404)
    name = bank.slots[pad].meta.name or f"pad{pad + 1}"
    return Response(data, media_type="audio/wav", headers={"Content-Disposition": f'attachment; filename="{name}.wav"'})


async def api_slot_trigger(pad: int):
    engine.post("down", pad)
    engine.post("up", pad)
    return {"ok": True}


async def api_swap(request: Request):
    body = await request.json()
    a, b = int(body["a"]), int(body["b"])
    if _pad_ok(a) and _pad_ok(b) and a != b:
        bank.swap(a, b)
        engine.post("slot_changed", a)
        engine.post("slot_changed", b)
        push_state()
    return {"ok": True}


async def api_system(action: str):
    if action == "rescan":
        mcu_calls.put(("rescan",))
    elif action == "reset_addresses":
        mcu_calls.put(("reset_addresses",))
    elif action == "stop_all":
        engine.post("stop_all")
    elif action == "reopen_audio":
        engine.reopen_devices()
    else:
        return JSONResponse({"error": "unknown action"}, status_code=400)
    return {"ok": True}


async def api_audio_devices():
    return audio_ports.list_devices()


async def api_audio_tone(request: Request):
    body = await request.json()
    engine.post("tone", float(body.get("freq", 440)), int(body.get("beeps", 1)))
    return {"ok": True}


IDENTIFY_TONES = [(440.0, 1), (660.0, 2), (880.0, 3)]  # one per port, in PORTS order
_identifying = threading.Lock()


async def api_audio_identify():
    """Beep out of each carrier jack in turn: 1 beep, 2 beeps, 3 beeps."""
    if not audio_ports.is_carrier(settings["output_device"]):
        return JSONResponse({"error": "output isn't the Media Carrier"}, status_code=400)
    if not _identifying.acquire(blocking=False):
        return {"ok": True}

    def run() -> None:
        try:
            for port, (freq, beeps) in zip(audio_ports.PORTS, IDENTIFY_TONES):
                audio_ports.apply_ports([port])
                ui.send_message("audio_test", {"port": port, "label": audio_ports.PORTS[port], "beeps": beeps})
                engine.post("tone", freq, beeps)
                time.sleep(0.3 * beeps + 1.0)
        finally:
            audio_ports.apply_ports(settings["carrier_outputs"])
            ui.send_message("audio_test", {"done": True})
            _identifying.release()

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "order": [{"port": p, "beeps": b} for p, (_, b) in zip(audio_ports.PORTS, IDENTIFY_TONES)]}


async def api_audio_debug():
    """Where the audio thread is right now: for diagnosing stalls."""
    import sys
    import traceback

    frames = sys._current_frames()
    out = {}
    for t in threading.enumerate():
        if t.name == "audio" and t.ident in frames:
            out["audio"] = traceback.format_stack(frames[t.ident])[-8:]
    return {"stats": engine.stats, "devices": engine.device_status, **out}


ui.expose_api("GET", "/api/audio/debug", api_audio_debug)
async def api_library():
    return bank.library.describe()


async def api_library_add(request: Request):
    """Body: a WAV file (the browser converts any format). Adds it to the uploads folder."""
    try:
        audio = decode_wav(await request.body())
    except Exception as e:
        return JSONResponse({"error": f"could not read WAV: {e}"}, status_code=415)
    sid = bank.library.add(audio, unquote(request.headers.get("x-slot-name", "")) or "upload", "uploads")
    push_state()
    return {"id": sid, **bank.library.items[sid]}


async def api_library_patch(id: str, request: Request):
    if id not in bank.library.items:
        return JSONResponse({"error": "no such sample"}, status_code=404)
    bank.rename_sample(id, str((await request.json()).get("name", "")))
    push_state()
    return {"id": id, **bank.library.items[id]}


async def api_library_delete(id: str):
    for pad in bank.delete_sample(id):
        engine.post("slot_changed", pad)
    push_state()
    return {"ok": True}


async def api_library_audio(id: str):
    data = bank.library.wav_bytes(id)
    if data is None:
        return JSONResponse({"error": "no such sample"}, status_code=404)
    return Response(data, media_type="audio/wav")


BT_COMMANDS = {"previous", "playpause", "next", "play", "pause", "discoverable", "disconnect"}


async def api_bt(cmd: str, request: Request):
    if cmd not in BT_COMMANDS:
        return JSONResponse({"error": "unknown command"}, status_code=400)
    try:
        args = await request.json()
    except Exception:
        args = {}
    reply = await asyncio.to_thread(bluetooth.command, cmd, **(args or {}))
    push_state()
    return reply


ui.expose_api("GET", "/api/library", api_library)
ui.expose_api("POST", "/api/library", api_library_add)
ui.expose_api("PATCH", "/api/library/item", api_library_patch)
ui.expose_api("DELETE", "/api/library/item", api_library_delete)
ui.expose_api("GET", "/api/library/audio", api_library_audio)
ui.expose_api("POST", "/api/bt/{cmd}", api_bt)
ui.expose_api("GET", "/api/audio/devices", api_audio_devices)
ui.expose_api("POST", "/api/audio/tone", api_audio_tone)
ui.expose_api("POST", "/api/audio/identify", api_audio_identify)
ui.expose_api("GET", "/api/state", api_state)
ui.expose_api("POST", "/api/modules/setup", api_module_setup)
ui.expose_api("POST", "/api/modules/order", api_module_order)
ui.expose_api("POST", "/api/modules/identify", api_module_identify)
ui.expose_api("POST", "/api/modules/forget", api_module_forget)
ui.expose_api("POST", "/api/modules/address", api_module_address)
ui.expose_api("POST", "/api/settings", api_settings)
ui.expose_api("POST", "/api/slots/swap", api_swap)
ui.expose_api("POST", "/api/slots/{pad}", api_upload)
ui.expose_api("PATCH", "/api/slots/{pad}", api_slot_patch)
ui.expose_api("DELETE", "/api/slots/{pad}", api_slot_delete)
ui.expose_api("GET", "/api/slots/{pad}/audio", api_slot_audio)
ui.expose_api("POST", "/api/slots/{pad}/trigger", api_slot_trigger)
ui.expose_api("POST", "/api/system/{action}", api_system)

ui.on_connect(lambda sid: push_state())
ui.on_message("pad", lambda sid, data: api_trigger_sync(int(data.get("pad", -1))))


def api_trigger_sync(pad: int) -> None:
    if _pad_ok(pad):
        engine.post("down", pad)
        engine.post("up", pad)


# ---------------------------------------------------------------------------
# Main loop: MCU calls, LEDs, matrix, meters
# ---------------------------------------------------------------------------

_last_states = b""
_last_states_at = 0.0
_last_frame = b""
_last_meter = 0.0
_wave: tuple[int, float, float] | None = None  # (pad, progress, show until)
_blocks_seen = (0, 0.0)
_auto_enrol_at = 0.0
_last_meters, _last_meters_at = b"", 0.0  # (engine block count, when it last changed): audio watchdog
WAVE_HOLD_S = 0.25  # keep the waveform up briefly after the sound ends


def loop() -> None:
    global _last_states, _last_states_at, _last_frame, _last_meter, save_due, state_due, _wave
    now = time.monotonic()

    while not mcu_calls.empty():
        name, *args = mcu_calls.get()
        try:
            Bridge.call(name, *args, timeout=2)
        except Exception as e:
            log.warning("MCU call %s failed: %s", name, e)

    if topology["buzzer"] and settings["buzzer_mode"] == "follow":
        cmd = follower.update(buzzer.detect_pitch(engine.recent_out), now)
        if cmd:
            try:
                Bridge.notify("buzz", *cmd)
            except Exception as e:
                log.debug("buzz failed: %s", e)

    global _auto_enrol_at
    if now - _auto_enrol_at > 5.0:  # the sketch starts with it off; resend in case it restarted
        _auto_enrol_at = now
        mcu_calls.put(("set_auto_enrol", not setup_mode))

    states = pad_states()
    # resend now and then too, in case the MCU restarted
    if states != _last_states or now - _last_states_at > 2.0:
        try:
            phys = bytes(states[logical_pad(p)] if logical_pad(p) < len(states) else 0 for p in range(len(states)))
            Bridge.call("set_states", phys, timeout=1)
            _last_states, _last_states_at = states, now
        except Exception as e:
            log.debug("set_states failed: %s", e)

    # Pixels level meters, about 20 times a second and only when they change
    global _last_meters, _last_meters_at
    if pixels_addrs() and now - _last_meters_at > 0.05:
        _last_meters_at = now
        source = {"play": engine.level_play, "mic": engine.level_mic_out, "off": 0.0}
        order = pixels_addrs()
        roles = [pixels_role(order.index(a)) for a in physical_addrs("pixels")]
        tint = {"play": fx_tint("bt"), "mic": fx_tint("mic"), "off": 0.0}
        meters = bytes([meter_byte(source[r]) for r in roles] + [int(tint[r] * 255) for r in roles])
        if meters != _last_meters:
            try:
                Bridge.call("set_meters", meters, timeout=1)
                _last_meters = meters
            except Exception as e:
                log.debug("set_meters failed: %s", e)

    playing = engine.focus_progress()
    if playing:
        _wave = (playing[0], playing[1], now + WAVE_HOLD_S)
    elif _wave and _wave[1] < 1.0:
        _wave = (_wave[0], 1.0, _wave[2])  # it finished between two frames: show it complete
    wave_env = bank.slots[_wave[0]].matrix_env if _wave and now < _wave[2] else None

    if engine.recording_pad is not None:
        blink = int(now * 4) % 2 == 0
        frame = display.render("REC" if blink else "", engine.rec_progress, engine.level_in)
    elif banner and now < banner[1]:
        frame = display.render(banner[0], settings["fx_amount"] if banner[2] is None else banner[2], engine.level_out)
    elif wave_env:
        frame = display.render_wave(wave_env, _wave[1], settings["fx_amount"], engine.level_out)
    else:
        fx = engine.effects[settings["fx_index"] % len(engine.effects)]
        # the theremin swaps the level row for the hand height, so you can see what you're playing
        row6 = engine.effects[THEREMIN].hand if settings["fx_index"] == THEREMIN else engine.level_out
        frame = display.render(fx.short, settings["fx_amount"], row6, dim=settings["fx_bypass"])
    if frame != _last_frame:
        try:
            Bridge.notify("draw", frame)
            _last_frame = frame
        except Exception as e:
            log.debug("draw failed: %s", e)

    # The audio thread blocks inside the speaker write if the sound card stops
    # taking data; say so in the UI rather than going silent without a word.
    global _blocks_seen
    blocks = engine.stats["blocks"]
    if blocks != _blocks_seen[0]:
        if engine.stats.get("stalled"):
            engine.stats["stalled"] = False
            push_state()
        _blocks_seen = (blocks, now)
    elif now - _blocks_seen[1] > 2 and engine.device_status.get("output") == "open" and not engine.stats.get("stalled"):
        engine.stats["stalled"] = True
        engine.stats["last_error"] = "audio stopped: the sound card isn't taking data"
        log.warning("audio engine stalled for 2 s")
        push_state()

    if now - _last_meter > 0.1:
        _last_meter = now
        ui.send_message("meter", {
            "in": round(engine.level_in, 3),
            "raw": round(engine.level_raw, 4),
            "floor_db": round(20 * math.log10(engine.cond.floor + 1e-12), 1),
            "out": round(engine.level_out, 3),
            "fx_index": settings["fx_index"],
            "fx_amount": round(settings["fx_amount"], 3),
            "fx_bypass": settings["fx_bypass"],
            "hand_mm": hand_mm,
            "hand": round(engine.effects[THEREMIN].hand, 3),
            "rec": engine.recording_pad,
            "rec_progress": round(engine.rec_progress, 3),
            "playing": sorted(engine.playing),
            "mic_out": round(engine.level_mic_out, 3),
            "bt_in": round(engine.bt_in.level, 3),
            "play_out": round(engine.level_play, 3),
            # how much effect each chain has on, 0..1, to tint its meters
            "mic_fx": fx_tint("mic"),
            "bt_fx": fx_tint("bt"),
            "pixels": [round(b / 255, 2) for b in _pixels_logical()],
        })

    if state_due and now > state_due:
        state_due = 0.0
        push_state()
    if save_due and now > save_due:
        save_due = 0.0
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=1))
        tmp.replace(SETTINGS_PATH)

    time.sleep(1 / 30)


# The MCU reports its modules once, at boot, which can be before this process is
# listening; ask for a fresh report as soon as the loop runs.
mcu_calls.put(("set_auto_enrol", not setup_mode))  # the sketch waits for this before setting up modules
mcu_calls.put(("rescan",))

engine.start()
App.run(user_loop=loop)
