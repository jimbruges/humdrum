"""Humdrum — Linux side.

Glues together:
  * the MCU (Modulino pads + knob, LED matrix) over the Bridge
  * the audio engine (mic FX + sampler) in engine.py
  * the web UI / captive portal (assets/) over REST + Socket.IO
"""

from __future__ import annotations

import json
import logging
import queue
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from arduino.app_bricks.web_ui import WebUI
from arduino.app_utils import App, Bridge

import buzzer
import display
from engine import Engine
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
    "fx_index": 0,
    "fx_amount": 0.0,
    "fx_bypass": False,
    "mic_gain_db": 0.0,
    "gate_db": -55.0,
    "monitor_level": 0.8,
    "sample_level": 0.9,
    "master": 0.9,
    "record_post_fx": False,
    "rec_overwrite": True,
    "auto_trim": True,
    "normalize": True,
    "max_rec_s": 10,
    "hold_ms": 600,
    "knob_step": 0.02,
    "buzzer_mode": "off",
    "block": 512,
    "input_device": "jack:1",
    "output_device": "jack:1",
}
# what the web UI may change, with (min, max) for numbers
LIMITS = {
    "fx_index": (0, 6), "fx_amount": (0.0, 1.0), "mic_gain_db": (-12.0, 36.0), "gate_db": (-90.0, -20.0),
    "monitor_level": (0.0, 1.5), "sample_level": (0.0, 1.5), "master": (0.0, 1.5), "max_rec_s": (1, 30),
    "hold_ms": (200, 3000), "knob_step": (0.005, 0.1), "block": (256, 2048),
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
bank = SlotBank(DATA)
engine = Engine(settings, bank)
ui = WebUI()

topology = {"modules": 0, "knob": False, "addrs": "", "buzzer": False}
follower = buzzer.BuzzerFollower()
mcu_calls: queue.SimpleQueue = queue.SimpleQueue()  # Bridge.call is not allowed inside handlers
save_due = 0.0
banner: tuple[str, float] | None = None  # temporary matrix label, (text, until)


def save_settings_soon() -> None:
    global save_due
    save_due = time.monotonic() + 1.0


def apply_settings(changes: dict) -> dict:
    reopen = False
    for k, v in changes.items():
        if k not in DEFAULTS:
            continue
        default = DEFAULTS[k]
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, (int, float)):
            v = type(default)(v)
            lo, hi = LIMITS.get(k, (v, v))
            v = min(max(v, lo), hi)
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
    if reopen:
        engine.reopen_devices()
    save_settings_soon()
    push_state()
    return settings


def pads_connected() -> int:
    return topology["modules"] * 3


# ---------------------------------------------------------------------------
# MCU -> Linux (Bridge notifications; must not call back into the Bridge here)
# ---------------------------------------------------------------------------


def on_topology(modules: int, knob: bool, addrs: str, has_buzzer: bool = False) -> None:
    global banner
    topology.update(modules=int(modules), knob=bool(knob), addrs=str(addrs), buzzer=bool(has_buzzer))
    log.info("topology: %d button modules, knob=%s (%s)", modules, knob, addrs)
    banner = (f"{pads_connected():>2}P" if modules else "NO", time.monotonic() + 1.5)
    mcu_calls.put(("set_hold_ms", int(settings["hold_ms"])))
    push_state()


def on_pad_down(pad: int) -> None:
    engine.post("down", int(pad))
    if topology["buzzer"] and settings["buzzer_mode"] == "pads":
        mcu_calls.put(("buzz", buzzer.pad_freq(int(pad)), 120))


def on_pad_up(pad: int, held_ms: int = 0) -> None:
    engine.post("up", int(pad))


def on_pad_hold(pad: int) -> None:
    engine.post("hold", int(pad))


def on_knob_turn(delta: int) -> None:
    settings["fx_amount"] = min(1.0, max(0.0, settings["fx_amount"] + int(delta) * settings["knob_step"]))
    save_settings_soon()


def on_knob_click() -> None:
    settings["fx_index"] = (settings["fx_index"] + 1) % len(engine.effects)
    settings["fx_bypass"] = False
    save_settings_soon()
    push_state()


def on_knob_long() -> None:
    settings["fx_bypass"] = not settings["fx_bypass"]
    save_settings_soon()
    push_state()


Bridge.provide("topology", on_topology)
Bridge.provide("pad_down", on_pad_down)
Bridge.provide("pad_up", on_pad_up)
Bridge.provide("pad_hold", on_pad_hold)
Bridge.provide("knob_turn", on_knob_turn)
Bridge.provide("knob_click", on_knob_click)
Bridge.provide("knob_long", on_knob_long)


# ---------------------------------------------------------------------------
# State for the UI
# ---------------------------------------------------------------------------


def full_state() -> dict:
    n = max(pads_connected(), 0)
    return {
        "settings": settings,
        "fx": [{"name": fx.name, "short": fx.short} for fx in engine.effects],
        "topology": topology,
        "pads_connected": n,
        "slots": [bank.describe(p) for p in range(MAX_PADS) if p < n or bank.slots[p].loaded],
        "devices": engine.device_status,
        "version": bank.version,
    }


def push_state() -> None:
    ui.send_message("state", full_state())


def pad_states() -> bytes:
    out = bytearray(MAX_PADS)
    for p in range(MAX_PADS):
        if engine.recording_pad == p:
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
    if not _pad_ok(pad) or not bank.slots[pad].loaded:
        return JSONResponse({"error": "empty pad"}, status_code=404)
    bank.update_meta(pad, await request.json())
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


ui.expose_api("GET", "/api/state", api_state)
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
WAVE_HOLD_S = 0.25  # keep the waveform up briefly after the sound ends


def loop() -> None:
    global _last_states, _last_states_at, _last_frame, _last_meter, save_due, _wave
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

    states = pad_states()
    # resend now and then too, in case the MCU restarted
    if states != _last_states or now - _last_states_at > 2.0:
        try:
            Bridge.call("set_states", states, timeout=1)
            _last_states, _last_states_at = states, now
        except Exception as e:
            log.debug("set_states failed: %s", e)

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
        frame = display.render(banner[0], settings["fx_amount"], engine.level_out)
    elif wave_env:
        frame = display.render_wave(wave_env, _wave[1], settings["fx_amount"], engine.level_out)
    else:
        fx = engine.effects[settings["fx_index"] % len(engine.effects)]
        frame = display.render(fx.short, settings["fx_amount"], engine.level_out, dim=settings["fx_bypass"])
    if frame != _last_frame:
        try:
            Bridge.notify("draw", frame)
            _last_frame = frame
        except Exception as e:
            log.debug("draw failed: %s", e)

    if now - _last_meter > 0.1:
        _last_meter = now
        ui.send_message("meter", {
            "in": round(engine.level_in, 3),
            "out": round(engine.level_out, 3),
            "fx_index": settings["fx_index"],
            "fx_amount": round(settings["fx_amount"], 3),
            "fx_bypass": settings["fx_bypass"],
            "rec": engine.recording_pad,
            "rec_progress": round(engine.rec_progress, 3),
            "playing": sorted(engine.playing),
        })

    if save_due and now > save_due:
        save_due = 0.0
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=1))
        tmp.replace(SETTINGS_PATH)

    time.sleep(1 / 30)


# The MCU reports its modules once, at boot, which can be before this process is
# listening; ask for a fresh report as soon as the loop runs.
mcu_calls.put(("rescan",))

engine.start()
App.run(user_loop=loop)
