"""Audio device discovery and Media Carrier output routing.

The carrier's three jacks all hang off one codec output path. PipeWire's
"Headphones" profile routes the app's playback and the combo jack's mic, and
the codec then has a switch per jack: headphones (combo jack), line out and
ear out. Turning those switches on together plays the same signal from
several jacks at once, which also makes it easy to find out which is which.
"""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger("humdrum.audio")

CARD = "0"
PORTS = {
    "headphones": "Headphones (combo jack, also the mic input)",
    "lineout": "Line out (mono, balanced)",
    "earphone": "Ear out (small speaker or earpiece)",
}
# Line out and ear out each have a switch in the codec. The headphone path
# must stay switched on while PipeWire's Headphones profile is streaming:
# turning its RDAC or HPH switches off stops the playback DMA, and every write
# to the speaker then blocks for good (tested on the board). So the combo jack
# is muted with its amplifier gain instead, where 0 is the codec's mute step.
EXTRA_SWITCHES = {"lineout": "LO_RDAC Switch", "earphone": "EAR_RDAC Switch"}
HP_ALWAYS_ON = ("HPHL_RDAC Switch", "HPHR_RDAC Switch", "HPHL Switch", "HPHR Switch")
HP_VOLUMES = ("HPHL Volume", "HPHR Volume")
HP_GAIN_ON, HP_GAIN_MUTE = "20", "0"
CARRIER_PROFILE = "Headphones"  # the only profile that also routes the mic
# Capture gain from Arduino's Media Carrier user manual (recording section).
# PipeWire's source volume drives TX_DEC0 and at 100% that is +40 dB, which
# turns a quiet input into loud hum; the app's own mic gain goes on top.
MIC_HW_GAIN = {"ADC2 Volume": "5", "TX_DEC0 Volume": "100"}


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=5, check=True).stdout


def _cset(name: str, on: bool) -> None:
    _run(["amixer", "-q", "-c", CARD, "cset", f"name={name}", "on" if on else "off"])


def apply_ports(ports: list[str]) -> None:
    """Make exactly `ports` audible on the carrier's jacks."""
    try:
        for name in HP_ALWAYS_ON:
            _cset(name, True)
        for name in HP_VOLUMES:
            _run(["amixer", "-q", "-c", CARD, "cset", f"name={name}", HP_GAIN_ON if "headphones" in ports else HP_GAIN_MUTE])
        for port, name in EXTRA_SWITCHES.items():
            _cset(name, port in ports)
        log.info("carrier outputs: %s", ", ".join(ports) or "none")
    except Exception as e:
        log.warning("could not set carrier outputs: %s", e)


def set_mic_gain() -> None:
    try:
        for name, value in MIC_HW_GAIN.items():
            _run(["amixer", "-q", "-c", CARD, "cset", f"name={name}", value])
    except Exception as e:
        log.warning("could not set the mic gain: %s", e)


def _pw_dump() -> list[dict]:
    try:
        return json.loads(_run(["pw-dump"]))
    except Exception as e:
        log.warning("pw-dump failed: %s", e)
        return []


def _carrier_device(objs: list[dict]) -> dict | None:
    for o in objs:
        props = o.get("info", {}).get("props", {})
        if o.get("type", "").endswith("Device") and "Imola" in str(props.get("api.alsa.card.name", "")) + str(props.get("device.name", "")):
            return o
    return None


def ensure_carrier_profile() -> None:
    """Put the carrier's sound card on the profile that routes both playback and the mic."""
    objs = _pw_dump()
    dev = _carrier_device(objs)
    if dev is None:
        return
    params = dev.get("info", {}).get("params", {})
    current = [p.get("name") for p in params.get("Profile", [])]
    if CARRIER_PROFILE in current:
        return
    for p in params.get("EnumProfile", []):
        if p.get("name") == CARRIER_PROFILE:
            try:
                _run(["pw-cli", "set-param", str(dev["id"]), "Profile", json.dumps({"index": p["index"], "save": True})])
                log.info("sound card profile %s -> %s", current, CARRIER_PROFILE)
            except Exception as e:
                log.warning("could not switch the sound card profile: %s", e)
            return


def list_devices() -> dict:
    """What the app can open. The Media Carrier is one entry each way (its jacks
    are picked separately); USB audio devices follow, named as the system names them."""
    labels = {}
    for o in _pw_dump():
        props = o.get("info", {}).get("props", {})
        if props.get("node.name"):
            labels[f"pipewire:NODE={props['node.name']}"] = props.get("node.description") or props["node.name"]

    def usb(module_path: str, cls_name: str) -> list[dict]:
        try:
            module = __import__(module_path, fromlist=[cls_name])
            ids = getattr(module, cls_name).list_usb_devices()
        except Exception as e:
            log.warning("listing USB devices: %s", e)
            return []
        return [{"id": i, "label": "USB: " + labels.get(i, i.split("CARD=")[-1].split(",")[0])} for i in ids]

    return {
        "inputs": [{"id": "jack:1", "label": "Media Carrier: combo jack mic"}]
        + usb("arduino.app_peripherals.microphone", "ALSAMicrophone"),
        "outputs": [{"id": "jack:1", "label": "Media Carrier jacks"}]
        + usb("arduino.app_peripherals.speaker", "ALSASpeaker"),
        "ports": [{"id": k, "label": v} for k, v in PORTS.items()],
    }


def is_carrier(device: str) -> bool:
    return device.startswith("jack:") or "platform-sound" in device
