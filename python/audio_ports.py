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
import time

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
# Line out and ear out are fed from the left channel's DAC: the carrier's UCM
# routes them through HPHL with only RX0 (the left channel) connected
# (Lineout.conf / Earphones.conf, rx-macro HeadphoneLEnableSeq). So the jacks
# carry at most two different signals: the left channel (line out, ear out and
# the combo jack's tip) and the right (the combo jack's ring, on its own).
LEFT_ONLY = ("lineout", "earphone")
# What the two channels carry (settings "output_split"): the mix on both, or the
# mic and the music (Bluetooth and sample pads) on one channel each.
SPLITS = {
    "off": "Same mix on every jack",
    "mic_line": "Mic on line out, music on the headphone jack's right channel",
    "music_line": "Music on line out, mic on the headphone jack's right channel",
}


def ports_for(outputs: list[str], split: str = "off") -> list[str]:
    """The jacks that play: the ones picked, and with the outputs split, line
    out and the combo jack too (the right channel only comes out of the combo
    jack, the left out of all three)."""
    picked = set(outputs or [])
    if split in SPLITS and split != "off":
        picked |= {"headphones", "lineout"}
    return [p for p in PORTS if p in picked]


def left_only(ports: list[str]) -> bool:
    """Only jacks fed from the left channel play: a stereo mix would lose its
    right channel there, so it's summed to mono."""
    return bool(ports) and all(p in LEFT_ONLY for p in ports)
HP_ALWAYS_ON = ("HPHL_RDAC Switch", "HPHR_RDAC Switch", "HPHL Switch", "HPHR Switch")
HP_VOLUMES = ("HPHL Volume", "HPHR Volume")
HP_GAIN_MUTE = "0"
# The headphone amplifier's gain (1..20, 1.5 dB steps, 20 = loudest), set from
# the app's settings. Loudness belongs here, in the analog stage: turning the
# signal down digitally instead (a low master) leaves quiet passages with few
# bits and lets the amplifier's hiss through.
hp_gain = 14
# Both channels' digital gain, held at 0 dB (84 on the -84..+40 dB scale).
# PipeWire's volume for this card drives only RX_RX1, the right channel: at its
# default 40% that is +17 dB on the right and nothing on the left.
RX_GAINS = {"RX_RX0 Digital Volume": "84", "RX_RX1 Digital Volume": "84"}
# The codec's headphone compander (a compressor tied to the amplifier gain):
# music through it sounds squashed and its level pumps. The carrier's UCM
# profile (codecs/pm4125 and rx-macro HeadphoneEnableSeq) switches all four on
# whenever PipeWire activates the port. Writing the switches while playing is
# safe but only half works: the RX macro reads its switch when its
# interpolator powers up and the PM4125 its own when its DAC does (see
# rx_macro_set_compander / pm4125_codec_hphl_dac_event in the kernel), so a
# live "off" leaves the digital compressor running. prepare_output() turns them
# off before the output opens.
COMPANDER = ("RX_COMP1 Switch", "RX_COMP2 Switch", "HPHL_COMP Switch", "HPHR_COMP Switch")
# The headphone amplifier's power mode: ULP (the default) has a higher noise
# floor, audible with sensitive earphones at low volume. Also read at power-up.
HPH_MODE = ("RX_HPH PWR Mode", "LOHIFI")


def prepare_output() -> None:
    """Codec settings that only take effect when the headphone amplifier powers
    up, so they're written before the output opens (while PipeWire has the card
    suspended). If the output was already playing they apply at its next power-up."""
    try:
        _run(["amixer", "-q", "-c", CARD, "cset", f"name={HPH_MODE[0]}", HPH_MODE[1]])
        for name in COMPANDER:
            _cset(name, False)
        for name, value in RX_GAINS.items():
            _run(["amixer", "-q", "-c", CARD, "cset", f"name={name}", value])
    except Exception as e:
        log.warning("could not prepare the headphone amplifier: %s", e)
CARRIER_PROFILE = "Headphones"  # the only profile that also routes the mic
# Capture gain held at unity in the codec. Arduino's manual suggests ADC2 5 and
# TX_DEC0 100 (about +23 dB), but a headset mic on the combo jack carries a big
# rumble below 50 Hz, and that much gain clips it inside the codec, before the
# app can filter it out: the result is loud broadband noise. A headset mic is
# also close to the mouth and loud, so the codec runs 6 dB under unity for
# headroom (ADC2 0 is its lowest analog gain); the app filters the rumble first
# and applies its own mic gain after (mic_gain_db).
MIC_HW_GAIN = {"ADC2 Volume": "0", "TX_DEC0 Volume": "78"}


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
            _run(["amixer", "-q", "-c", CARD, "cset", f"name={name}", str(hp_gain) if "headphones" in ports else HP_GAIN_MUTE])
        for port, name in EXTRA_SWITCHES.items():
            _cset(name, port in ports)
        for name, value in RX_GAINS.items():
            _run(["amixer", "-q", "-c", CARD, "cset", f"name={name}", value])
        for name in COMPANDER:
            _cset(name, False)
        log.info("carrier outputs: %s", ", ".join(ports) or "none")
    except Exception as e:
        log.warning("could not set carrier outputs: %s", e)


# A real microphone through a splitter is far hotter than the headset mic:
# voice peaks measured about 18 dB over full scale at TX_DEC0 78, clipping in
# the codec (ADC2 is already at its lowest). The ADC has the headroom; the
# decimator's output doesn't. So the app pads TX_DEC0 down further
# (mic_pad_db, stepped automatically when the capture clips, see Engine).
MIC_PAD_MAX_DB = 42  # TX_DEC0 down to 36, -48 dB


def set_mic_gain(pad_db: int = 0) -> None:
    pad_db = max(0, min(MIC_PAD_MAX_DB, int(pad_db)))
    try:
        for name, value in MIC_HW_GAIN.items():
            if name == "TX_DEC0 Volume":
                value = str(int(value) - pad_db)
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


CARRIER_NODES = ("alsa_output.platform-sound.Headphones__Headphones__sink",
                 "alsa_input.platform-sound.Headphones__Headset__source")


def carrier_nodes_missing() -> list[str]:
    """The carrier's PipeWire nodes (the jacks' output, the combo jack's mic)
    that don't exist right now. Cheap: `pw-cli ls Node` is 2 KB of text."""
    try:
        out = _run(["pw-cli", "ls", "Node"])
    except Exception:
        return []  # can't tell: don't act on it
    return [n for n in CARRIER_NODES if f'"{n}"' not in out]


def ensure_carrier_profile() -> None:
    """Put the carrier's sound card on the profile that routes both playback and
    the mic, and make sure that profile's nodes exist.

    They can be missing with the profile set: at boot PipeWire opened the card
    before its routing was switched on ("MultiMedia1: ASoC: no backend DAIs
    enabled", then "playback open failed: Invalid argument"), and WirePlumber
    gave up on both nodes. The app then opened PipeWire's dummy sink in their
    place and played into nothing, without an error. Switching the profile off
    and on again makes WirePlumber create them afresh."""
    objs = _pw_dump()
    dev = _carrier_device(objs)
    if dev is None:
        return
    params = dev.get("info", {}).get("params", {})
    current = [p.get("name") for p in params.get("Profile", [])]
    index = next((p["index"] for p in params.get("EnumProfile", []) if p.get("name") == CARRIER_PROFILE), None)
    if index is None:
        return
    try:
        if CARRIER_PROFILE not in current:
            _run(["pw-cli", "set-param", str(dev["id"]), "Profile", json.dumps({"index": index, "save": True})])
            log.info("sound card profile %s -> %s", current, CARRIER_PROFILE)
        else:
            names = {str(o.get("info", {}).get("props", {}).get("node.name", "")) for o in objs}
            missing = [n for n in CARRIER_NODES if n not in names]
            if not missing:
                return
            log.warning("sound card nodes missing (%s): switching its profile off and on", ", ".join(missing))
            off = next((p["index"] for p in params.get("EnumProfile", []) if p.get("name") == "off"), 0)
            _run(["pw-cli", "set-param", str(dev["id"]), "Profile", json.dumps({"index": off, "save": False})])
            time.sleep(1.0)
            _run(["pw-cli", "set-param", str(dev["id"]), "Profile", json.dumps({"index": index, "save": True})])
    except Exception as e:
        log.warning("could not switch the sound card profile: %s", e)
        return
    for _ in range(30):  # the nodes take a moment to appear
        if not carrier_nodes_missing():
            return
        time.sleep(0.2)
    log.warning("sound card nodes still missing: %s", ", ".join(carrier_nodes_missing()))


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
        "splits": [{"id": k, "label": v} for k, v in SPLITS.items()],
    }


def is_carrier(device: str) -> bool:
    return device.startswith("jack:") or "platform-sound" in device
