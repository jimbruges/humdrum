"""Real-time audio: mic -> FX -> line out, plus the sample voices.

One thread owns the audio devices and all DSP state. Everything else (Bridge
handlers, the web server) talks to it through `post()`, so the audio thread
never takes a lock that a slow web request could be holding.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

import audio_ports
import bt
from fx import SR, InputConditioner, make_effects
from slots import MAX_PADS, SlotBank, tidy_recording

log = logging.getLogger("engine")
MIC_WAIT_S = 0.05    # an empty mic read this long: keep the output going with silence
MIC_REOPEN_S = 0.5   # this long with no mic data at all: reopen the mic
# A capture this close to full scale has clipped in the codec: pad the codec's
# mic gain down a step (audio_ports.set_mic_gain). It isn't made up in software:
# a mic hot enough to clip is louder than the app expects, so the level drops
# to one the codec can take, and mic_gain_db stays the volume.
MIC_CLIP = 0.98
MIC_PAD_STEP_DB = 6
MIC_PAD_SETTLE_S = 0.3  # clips within this long after a step are from before it
# One full-scale pop (plugging the splitter in, a tap on the mic) isn't a hot
# voice: only clipping in MIC_CLIP_BLOCKS blocks within MIC_CLIP_WINDOW_S pads
# the codec. (Single pops ratcheted it to the 42 dB maximum.)
MIC_CLIP_BLOCKS = 3
MIC_CLIP_WINDOW_S = 1.0
# And it comes back up: after MIC_UNPAD_VOICE_S of voice (gate open) whose raw
# peaks all stayed below MIC_UNPAD_PEAK, it steps 6 dB less padding.
MIC_UNPAD_VOICE_S = 8.0
MIC_UNPAD_PEAK = 10 ** (-15 / 20)
TONE_LEVEL = 0.25    # test tones play at about -12 dBFS whatever the mix levels are
# The output is stereo so the phone's music keeps its stereo image; the mic,
# the pads and the test tones are mono and play in both channels.
CHANNELS = 2
# The Bluetooth effect knob's first BT_FX_DEADZONE of travel (two 0.02 clicks)
# leaves the music dry. The pitch effects and the Theremin pitch-shift all of
# the music at any amount above 0, so a knob nudged off zero (e.g. 0.04) made
# a song sound rough and slightly flat without being noticed.
BT_FX_DEADZONE = 0.05


def bt_fx_amount(amount: float) -> float:
    """Knob amount -> effect amount: 0 inside the dead zone, then rescaled so the
    effect still starts from nothing and reaches full strength at the top."""
    return max(0.0, (amount - BT_FX_DEADZONE) / (1.0 - BT_FX_DEADZONE))


FX_XFADE = 512     # samples (about 11 ms) the old effect fades out over while the new one fades in
FX_HISTORY = 4096  # recent input kept for priming a newly selected effect (pitch shift reads 2048 back)


class FxChain:
    """One effect slot in the signal path (the mic's, or one Bluetooth channel's).

    Switching effects used to reset the new one and cut straight to it: the old
    effect's output (an echo tail, a shifted pitch) stopped dead mid-wave, a click,
    and the pitch effects then read up to 43 ms of their now-empty delay line, a
    gap. Now the new effect is primed with the recent input and the old one keeps
    running for FX_XFADE samples while they crossfade."""

    def __init__(self, effects: list) -> None:
        self.effects = effects
        self.index = -1
        self.old = None  # the effect being faded out
        self.left = 0  # samples of crossfade still to go
        self.hist = np.zeros(FX_HISTORY, np.float32)

    def reset_all(self) -> None:
        for fx in self.effects:
            fx.reset()
        self.old, self.left = None, 0

    def process(self, index: int, x: np.ndarray, amount: float) -> np.ndarray:
        index %= len(self.effects)
        if index != self.index:
            new = self.effects[index]
            if 0 <= self.index < len(self.effects) and self.effects[self.index] is not new:
                self.old, self.left = self.effects[self.index], FX_XFADE
            new.reset()  # don't replay whatever was in its buffers last time...
            new.prime(self.hist)  # ...but start from what's playing now, not silence
            self.index = index
        y = self.effects[index].process(x, amount)
        if self.old is not None:
            n = len(x)
            k = min(n, self.left)
            t = np.ones(n, np.float32)  # how far through the crossfade, 0..1
            done = 1.0 - self.left / FX_XFADE
            t[:k] = np.linspace(done, done + k / FX_XFADE, k, endpoint=False, dtype=np.float32)
            # equal-power: two different signals faded linearly lose level midway
            y = y * np.sin(0.5 * np.pi * t) + self.old.process(x, amount) * np.cos(0.5 * np.pi * t)
            self.left -= k
            if self.left <= 0:
                self.old = None
        self.hist = np.concatenate([self.hist[len(x):], x.astype(np.float32)])[-FX_HISTORY:]
        return y

    @property
    def current(self):
        return self.effects[self.index] if self.index >= 0 else self.effects[0]


def _forward_min(a: np.ndarray, w: int) -> np.ndarray:
    """out[k] = a[k : k + w].min() for every full window, in O(n) (van Herk / Gil-Werman):
    each window spans at most two w-long chunks, so it's the min of a suffix of one
    chunk and a prefix of the next."""
    n = len(a) - w + 1
    chunks = np.concatenate([a, np.full(-len(a) % w, np.inf, a.dtype)]).reshape(-1, w)
    prefix = np.minimum.accumulate(chunks, axis=1).ravel()
    suffix = np.minimum.accumulate(chunks[:, ::-1], axis=1)[:, ::-1].ravel()
    return np.minimum(suffix[:n], prefix[w - 1 : w - 1 + n])


class Limiter:
    """Look-ahead peak limiter on the output. It does nothing until the mix would
    pass CEILING; then it turns the gain down smoothly just before the peak
    (LOOK samples ahead, a 1.3 ms ramp) and back up at RELEASE_DB_S.

    It replaces a soft clipper that bent every sample above 0.8: with the
    phone's music already at full scale and bt_level above 1, that distorted and
    squashed a good part of every loud passage."""

    CEILING = 0.97
    LOOK = 64
    RELEASE_DB_S = 30.0

    def __init__(self, channels: int) -> None:
        self.delay = np.zeros((self.LOOK, channels), np.float32)
        self.need = np.ones(self.LOOK, np.float32)  # gain each delayed sample needs
        self.gain_db = 0.0  # where the release had got to
        self.recent = np.ones(self.LOOK)  # the last gains, for the attack ramp
        self.rho = self.RELEASE_DB_S / SR  # release, dB per sample
        self.reduction_db = 0.0  # deepest gain reduction in the last block

    def process(self, x: np.ndarray) -> np.ndarray:
        n, look = len(x), self.LOOK
        if not np.isfinite(self.gain_db) or not np.isfinite(self.recent).all():
            self.__init__(self.delay.shape[1])  # a NaN once reached the state: start clean
        buf = np.concatenate([self.delay, np.nan_to_num(x.astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)])
        self.delay = buf[-look:]
        out = buf[:n]
        peak = np.max(np.abs(buf[look:]), axis=1)
        # need[k] is the most gain buf[k] can have without passing the ceiling
        need = np.concatenate([self.need, np.minimum(1.0, self.CEILING / np.maximum(peak, 1e-9))])
        self.need = need[-look:]
        if need.min() >= 1.0 and self.gain_db >= 0.0 and self.recent.min() >= 1.0:
            self.reduction_db = 0.0
            return out
        # the least gain needed over each sample and the LOOK samples after it...
        low = _forward_min(need, look + 1)
        # ...recovering at a fixed rate: g[k] = min(low[k], g[k-1] + rho, 0 dB), in closed form
        low_db = 20 * np.log10(np.maximum(low, 1e-6)).astype(np.float64)
        k = np.arange(1, n + 1) * self.rho
        g_db = np.minimum(0.0, k + np.minimum(self.gain_db, np.minimum.accumulate(low_db - k)))
        self.gain_db = float(g_db[-1])
        g = np.concatenate([self.recent, 10 ** (g_db / 20)])
        self.recent = g[-look:]
        # averaging the last LOOK + 1 gains makes a smooth ramp that still meets every need
        c = np.concatenate([[0.0], np.cumsum(g)])
        smooth = ((c[look + 1:] - c[: -look - 1]) / (look + 1)).astype(np.float32)
        self.reduction_db = float(-20 * np.log10(max(float(smooth.min()), 1e-6)))
        return np.clip(out * smooth[:, None], -1.0, 1.0)


# Following the phone's clock means reading its audio at fractional positions.
# A 32-tap windowed sinc at 2048 positions gets that within about -74 dB at
# 10 kHz; the Catmull-Rom cubic used before was only -27 dB at 10 kHz and
# -15 dB at 15 kHz, and its treble loss rose and fell as the position drifted.
SINC_TAPS = 32
SINC_PHASES = 2048
SINC_BEFORE = SINC_TAPS // 2 - 1  # history samples each read needs, before the read point
SINC_AFTER = SINC_TAPS // 2       # and after it
SINC_OFFSETS = np.arange(-SINC_BEFORE, SINC_AFTER + 1)


def _sinc_table(cutoff: float = 0.45, beta: float = 8.0) -> np.ndarray:
    """Row p: the taps for reading p / SINC_PHASES of a sample past buf[i],
    applied to buf[i - SINC_BEFORE] .. buf[i + SINC_AFTER]. Flat to 16 kHz, -1 dB at 20 kHz."""
    half = SINC_TAPS // 2
    d = SINC_OFFSETS[None, :] - np.arange(SINC_PHASES + 1)[:, None] / SINC_PHASES
    window = np.interp(d, np.linspace(-half, half, 2 * SINC_TAPS + 1), np.kaiser(2 * SINC_TAPS + 1, beta))
    h = 2 * cutoff * np.sinc(2 * cutoff * d) * window
    return (h / h.sum(axis=1, keepdims=True)).astype(np.float32)


SINC = _sinc_table()


class BtInput:
    """The phone's Bluetooth audio, read on its own thread into a short buffer.

    Bluetooth arrives on its own clock and comes and goes, so the audio thread
    never waits for it: it takes what's there and plays silence otherwise.
    """

    # A phone sends audio in bursts, on its own clock. Playing whatever has
    # arrived leaves gaps of silence whenever a burst is late: constant crackle.
    # So keep a cushion (TARGET_S) before playing, and follow the phone's clock
    # by resampling very slightly (at most DRIFT) to hold the cushion steady.
    TARGET_S = 0.08
    MAX_S = 0.5  # beyond this, drop the oldest audio
    DRIFT = 0.005
    FADE = 128  # samples of fade in/out around a gap
    # A backlog this far above the cushion (audio that piled up while the engine
    # was starting or stalled) is dropped with a short fade. Draining it at the
    # DRIFT limit instead took minutes, all of it played 0.5% (9 cents) sharp.
    RESYNC_S = 0.15

    def __init__(self) -> None:
        self.node: str | None = None
        self.level = 0.0
        self.underruns = 0
        self.resyncs = 0  # times a backlog was dropped (see RESYNC_S)
        self._buf = np.zeros((0, CHANNELS), np.float32)  # frames x channels
        self._frac = 0.0  # resampling position within _buf
        self._priming = True
        self._fill = 0.0  # buffer level, smoothed over ~2 s
        self._lock = threading.Lock()
        self._mic = None
        threading.Thread(target=self._loop, name="bt-audio", daemon=True).start()

    @property
    def fill_ms(self) -> int:
        return int(len(self._buf) * 1000 / SR)

    def read(self, n: int) -> np.ndarray:
        """The next `n` frames (n x CHANNELS), silence while there's nothing to play."""
        target = self.TARGET_S * SR
        with self._lock:
            buf = self._buf
            if self._priming:
                if len(buf) < target:
                    self.level *= 0.85  # let the meter fall while nothing arrives
                    return np.zeros((n, CHANNELS), np.float32)
                # start from the newest TARGET_S (anything older piled up while nothing
                # was reading), with silence before it as the interpolator's history
                buf = np.concatenate([np.zeros((SINC_BEFORE, CHANNELS), np.float32), buf[-int(target):]])
                self._priming, self._frac, self._fill = False, 0.0, float(len(buf) - SINC_BEFORE)
                fade_in = True
            else:
                fade_in = False
            resync = False
            # Follow the phone's clock from the *average* buffer level: it jumps with
            # every burst, and steering by that wobbles the pitch (rough sound).
            self._fill += (len(buf) - SINC_BEFORE - self._fill) * min(1.0, n / (2.0 * SR))
            ratio = 1.0 + float(np.clip((self._fill - target) / target * self.DRIFT, -self.DRIFT, self.DRIFT))
            # buf[:SINC_BEFORE] has already been played: it's kept as the sinc's history
            pos = SINC_BEFORE + self._frac + np.arange(n) * ratio
            if int(pos[-1]) + SINC_AFTER >= len(buf):  # ran dry: fade out what's left, then build the cushion again
                self.underruns += 1
                self._priming = True
                out = np.zeros((n, CHANNELS), np.float32)
                tail = buf[SINC_BEFORE:][:n]
                k = min(len(tail), self.FADE)
                if k:
                    out[:k] = tail[:k] * np.linspace(1.0, 0.0, k, dtype=np.float32)[:, None]
                self._buf = np.zeros((0, CHANNELS), np.float32)
                return out
            i = pos.astype(np.int64)
            taps = SINC[np.rint((pos - i) * SINC_PHASES).astype(np.int64)]
            # the frames this block reads, channels first, so each output sample's
            # 32 inputs are one contiguous row (the fastest gather on the A53)
            seg = np.ascontiguousarray(buf[i[0] - SINC_BEFORE : i[-1] + SINC_AFTER + 1].T)
            rows = np.lib.stride_tricks.sliding_window_view(seg, SINC_TAPS, axis=1)[:, i - i[0]]
            out = np.einsum("nt,cnt->nc", taps, rows)
            nxt = pos[-1] + ratio
            whole = int(nxt)
            self._frac = nxt - whole
            self._buf = buf[whole - SINC_BEFORE:]
            if len(self._buf) - SINC_BEFORE > target + self.RESYNC_S * SR:
                resync = True
                self._priming = True  # the next read starts again from the newest audio, faded in
                self.resyncs += 1
        if fade_in:
            k = min(n, self.FADE)
            out[:k] *= np.linspace(0.0, 1.0, k, dtype=np.float32)[:, None]
        if resync:
            k = min(n, self.FADE)
            out[n - k:] *= np.linspace(1.0, 0.0, k, dtype=np.float32)[:, None]
            log.info("bluetooth buffer at %d ms: skipped ahead to the newest audio", self.fill_ms)
        return out

    def _loop(self) -> None:
        while True:
            node = bt.audio_node()
            if not node:
                self.node = None
                time.sleep(2)
                continue
            # pw-record aimed at the phone's node only. (ALSA's pipewire:NODE=...
            # quietly records the default input, the mic, whenever the phone's
            # node is missing, e.g. while it's paused: loud noise.)
            # In stereo: the phone sends stereo, and a mono downmix flattens the music.
            cmd = ["pw-record", "--raw", "--target", node, "--format", "f32", "--rate", str(SR), "--channels", str(CHANNELS),
                   "--latency", "20ms", "-P", "{ node.dont-fallback = true, node.dont-reconnect = true }", "-"]
            frame = 4 * CHANNELS
            try:
                self._mic = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.node = node
                log.info("bluetooth audio: %s", node)
                rest = b""
                while True:
                    data = self._mic.stdout.read1(4096)
                    if not data:
                        break  # the phone's stream went away
                    data, rest = rest + data, b""
                    cut = len(data) - len(data) % frame
                    data, rest = data[:cut], data[cut:]
                    if not data:
                        continue
                    x = np.frombuffer(data, np.float32).reshape(-1, CHANNELS)
                    self.level = max(float(np.max(np.abs(x))), self.level * 0.85)
                    with self._lock:
                        self._buf = np.concatenate([self._buf, x])[-int(self.MAX_S * SR):]
            except Exception as e:
                log.warning("bluetooth audio: %s", e)
            finally:
                if self._mic:
                    self._mic.kill()
                    self._mic.wait()
                self._mic, self.node = None, None
                with self._lock:
                    self._buf = np.zeros((0, CHANNELS), np.float32)
                    self._priming = True
            time.sleep(1)


@dataclass
class Voice:
    pad: int
    buf: np.ndarray
    pos: int = 0
    loop: bool = False
    fade: float = 1.0  # < 1 means releasing
    releasing: bool = False


class Engine:
    def __init__(self, settings: dict, bank: SlotBank) -> None:
        self.s = settings  # shared dict, read every block (writes are atomic per key)
        self.bank = bank
        self.effects = make_effects()
        self.bt_effects = make_effects()  # their own instances: separate buffers and settings
        self.bt_effects_r = make_effects()  # the right channel's (bt_effects is the left's)
        self.mic_fx = FxChain(self.effects)
        self.bt_fx_l, self.bt_fx_r = FxChain(self.bt_effects), FxChain(self.bt_effects_r)
        self.bt_in = BtInput()
        self.limiter = Limiter(CHANNELS)
        self._nonfinite_seen: set[str] = set()
        self.cond = InputConditioner()
        self.events: queue.SimpleQueue = queue.SimpleQueue()
        self.voices: dict[int, Voice] = {}
        self.focus_pad: int | None = None  # last pad triggered, shown on the LED matrix

        self.recording_pad: int | None = None
        self.rec_chunks: list[np.ndarray] = []
        self.rec_len = 0

        # read by the UI / LED matrix
        self.level_in = 0.0
        self.level_out = 0.0
        self.playing: frozenset[int] = frozenset()
        self.rec_progress = 0.0
        self.recent_out = np.zeros(2048, np.float32)  # for the buzzer pitch tracker
        self.device_status = {"input": "closed", "output": "closed"}
        # dsp_ms: average time per block in _process; limit_db: recent deepest limiter gain reduction
        self.stats = {"blocks": 0, "mic_reopens": 0, "last_error": "", "dsp_ms": 0.0, "limit_db": 0.0}
        self.level_raw = 0.0  # mic before gain and gate, to show the input is alive
        self.level_play = 0.0  # samples + Bluetooth only, for the output meter
        self.level_mic_out = 0.0  # the mic after its effect and volume
        self.tone_buf: np.ndarray | None = None
        self.tone_pos = 0
        self._mic_ok_at = 0.0
        self.on_recorded = None  # callback(pad), runs on a worker thread
        self.on_mic_pad = None  # callback(pad_db) after the codec's mic gain was padded down
        self._pad_quiet_until = 0.0
        self._clip_times: list[float] = []
        self._voice_s = 0.0  # seconds of voice since the pad last changed
        self._voice_peak = 0.0  # loudest raw sample in that voice
        self._pad_seen = -1  # the mic_pad_db those were measured at
        self._mic_carrier = False

        self._mic = None
        self._spk = None
        self._run = False
        self._thread: threading.Thread | None = None
        self._reopen = threading.Event()

    # -- public API (any thread) --------------------------------------------

    def post(self, *event) -> None:
        self.events.put(event)

    def focus_progress(self) -> tuple[int, float] | None:
        """(pad, 0..1 position) of the last-triggered pad while it is sounding."""
        pad = self.focus_pad
        v = self.voices.get(pad) if pad is not None else None
        if v is None or not len(v.buf):
            return None
        return pad, min(1.0, v.pos / len(v.buf))

    def start(self) -> None:
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        if self._thread:
            self._thread.join(timeout=2)
        self._close()

    def reopen_devices(self) -> None:
        self._reopen.set()

    # -- devices ---------------------------------------------------------------

    def _open(self) -> None:
        block = int(self.s.get("block", 512))
        carrier = audio_ports.is_carrier(str(self.s.get("output_device", ""))) or audio_ports.is_carrier(str(self.s.get("input_device", "")))
        if carrier:
            audio_ports.ensure_carrier_profile()
            audio_ports.prepare_output()
        try:
            from arduino.app_peripherals.speaker import Speaker

            # float32 stereo: no 16-bit rounding on the way to the 24-bit codec
            self._spk = Speaker(self.s.get("output_device", Speaker.JACK_SPEAKER_1), sample_rate=SR, channels=CHANNELS, format=np.float32, buffer_size=block)
            self._spk.start()
            self.device_status["output"] = "open"
            if audio_ports.is_carrier(str(self.s.get("output_device", ""))):
                audio_ports.hp_gain = int(self.s.get("hp_gain", audio_ports.hp_gain))
                audio_ports.apply_ports(list(self.s.get("carrier_outputs", [])))
        except Exception as e:
            log.warning("output device: %s", e)
            self._spk = None
            self.device_status["output"] = f"error: {e}"
        try:
            from arduino.app_peripherals.microphone import Microphone

            # float32: keeps the codec's 24 bits (the gain and filters run after this)
            self._mic = Microphone(self.s.get("input_device", Microphone.JACK_MIC_1), sample_rate=SR, channels=1, format=np.float32, buffer_size=block)
            self._mic.start()
            self.device_status["input"] = "open"
            self._mic_carrier = audio_ports.is_carrier(str(self.s.get("input_device", "")))
            if self._mic_carrier:
                audio_ports.set_mic_gain(int(self.s.get("mic_pad_db", 0)))
        except Exception as e:
            log.warning("input device: %s", e)
            self._mic = None
            self.device_status["input"] = f"error: {e}"

    def _close(self) -> None:
        for dev in (self._mic, self._spk):
            try:
                if dev:
                    dev.stop()
            except Exception:
                pass
        self._mic = self._spk = None
        self.device_status = {"input": "closed", "output": "closed"}

    # -- audio thread ------------------------------------------------------------

    def _finite(self, y: np.ndarray, where: str, effects=()) -> np.ndarray:
        """`y` with any NaN or inf replaced by silence. One NaN reaching the
        limiter used to silence the output until the app restarted; this also
        resets the effects that produced it and logs where it came from."""
        if np.isfinite(y).all():
            return y
        self.stats["nonfinite"] = self.stats.get("nonfinite", 0) + 1
        self.stats["last_error"] = f"NaN/inf audio from {where}"
        if where not in self._nonfinite_seen:
            self._nonfinite_seen.add(where)
            log.warning("NaN/inf audio from %s: replaced with silence", where)
        for fx in effects:
            fx.reset()
        return np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)

    def _to_device(self, y: np.ndarray) -> np.ndarray:
        """`y` (frames x CHANNELS, -1..1) in the channels and sample format the
        output actually opened with: a device can refuse what was asked for."""
        spk = self._spk
        ch = int(getattr(spk, "channels", CHANNELS))
        if ch == 1:
            y = y.mean(axis=1)
        elif ch > CHANNELS:
            y = np.concatenate([y, np.zeros((len(y), ch - CHANNELS), np.float32)], axis=1)
        fmt = np.dtype(getattr(spk, "format", np.float32))
        if fmt.kind == "f":
            return np.ascontiguousarray(y, dtype=fmt)
        top = float(np.iinfo(fmt).max)
        return np.ascontiguousarray(np.clip(np.rint(y * top), -top - 1, top).astype(fmt))

    def _loop(self) -> None:
        last_try = 0.0
        while self._run:
            if self._reopen.is_set():
                self._reopen.clear()
                self._close()
                last_try = 0.0
            if (self._mic is None or self._spk is None) and time.monotonic() - last_try > 5:
                last_try = time.monotonic()
                self._close()
                self._open()
                self._mic_ok_at = time.monotonic()
                if self._spk:
                    # a little pre-roll so the first blocks don't underrun
                    silence = np.zeros((int(self.s.get("block", 512)) * 2, CHANNELS), np.float32)
                    self._spk.play(self._to_device(silence))

            block = int(self.s.get("block", 512))
            x = None
            if self._mic is not None:
                try:
                    chunk = self._mic.capture()
                except Exception as e:
                    log.warning("mic read failed: %s", e)
                    self.stats["last_error"] = f"mic: {e}"
                    self._mic = None
                    continue
                now = time.monotonic()
                if chunk is not None and chunk.size:
                    self._mic_ok_at = now
                    x = chunk.reshape(-1)
                    if x.dtype.kind == "f":
                        x = x.astype(np.float32, copy=False)
                    else:  # a device that fell back to an integer format
                        x = x.astype(np.float32) / float(np.iinfo(x.dtype).max + 1)
                elif now - self._mic_ok_at < MIC_WAIT_S:
                    time.sleep(0.001)
                    continue
                else:
                    # The mic has gone quiet (an overrun it never recovers from, a
                    # dropout...). Don't let that freeze the pads and the output:
                    # play on with silence and reopen the mic.
                    x = np.zeros(block, np.float32)
                    if now - self._mic_ok_at > MIC_REOPEN_S:
                        log.warning("mic delivered nothing for %.1f s, reopening it", now - self._mic_ok_at)
                        self.stats["mic_reopens"] += 1
                        self.stats["last_error"] = "mic stalled, reopened"
                        try:
                            self._mic.stop()
                        except Exception:
                            pass
                        self._mic = None
                        self.device_status["input"] = "reopening"
                        last_try = 0.0
            else:
                x = np.zeros(block, np.float32)

            t0 = time.perf_counter()
            try:
                y = self._process(x)
            except Exception as e:
                # never let one bad block kill the audio thread for the rest of the gig
                log.exception("audio block failed")
                self.stats["last_error"] = f"block: {e}"
                y = np.zeros((len(x), CHANNELS), np.float32)
            st = self.stats
            st["blocks"] += 1
            st["dsp_ms"] = round(st["dsp_ms"] * 0.98 + (time.perf_counter() - t0) * 1e3 * 0.02, 2)
            st["limit_db"] = round(max(self.limiter.reduction_db, st["limit_db"] * 0.99), 1)

            if self._spk is not None:
                try:
                    self._spk.play(self._to_device(y))
                except Exception as e:
                    log.warning("speaker write failed: %s", e)
                    self._spk = None
            elif self._mic is None:
                time.sleep(len(x) / SR)  # nothing is clocking us

    def _process(self, x: np.ndarray) -> np.ndarray:
        s = self.s
        self._handle_events()

        raw_peak = float(np.max(np.abs(x))) if len(x) else 0.0
        if self._mic_carrier and raw_peak >= MIC_CLIP:
            self._mic_clipped()
        dry = self.cond.process(x, gain=10 ** (s.get("mic_gain_db", 0.0) / 20), gate_db=s.get("gate_db", -55.0))
        pad_now = int(s.get("mic_pad_db", 0))
        if pad_now != self._pad_seen:  # changed here or from the settings page: judge it afresh
            self._pad_seen, self._voice_s, self._voice_peak = pad_now, 0.0, 0.0
        if self._mic_carrier and self.cond.is_open and pad_now > 0:
            self._voice_s += len(x) / SR
            self._voice_peak = max(self._voice_peak, raw_peak)
            if self._voice_s >= MIC_UNPAD_VOICE_S:
                if self._voice_peak < MIC_UNPAD_PEAK and time.monotonic() >= self._pad_quiet_until:
                    self._set_mic_pad(int(s.get("mic_pad_db", 0)) - MIC_PAD_STEP_DB, "the mic has headroom")
                self._voice_s, self._voice_peak = 0.0, 0.0
        fx_i = int(s.get("fx_index", 0)) % len(self.effects)
        amount = 0.0 if s.get("fx_bypass") else float(s.get("fx_amount", 0.0))
        wet = self._finite(self.mic_fx.process(fx_i, dry, amount), f"mic effect {self.effects[fx_i].name}", (self.effects[fx_i],))

        if self.recording_pad is not None:
            take = wet if s.get("record_post_fx") else dry
            self.rec_chunks.append(take.copy())
            self.rec_len += len(take)
            max_len = s.get("max_rec_s", 10) * SR
            self.rec_progress = min(1.0, self.rec_len / max_len)
            if self.rec_len >= max_len:
                self._stop_recording()

        mix = self._finite(self._render_voices(len(x)) * s.get("sample_level", 0.9), "sample pads")

        bt_dry = self._finite(self.bt_in.read(len(x)), "bluetooth input")  # frames x CHANNELS
        bt_i = int(s.get("bt_fx_index", 0)) % len(self.bt_effects)
        left, right = self.bt_effects[bt_i], self.bt_effects_r[bt_i]
        for fl, fr in zip(self.bt_effects, self.bt_effects_r):
            if hasattr(fl, "hand"):
                fr.hand = fl.hand  # the Theremin's hand position is set on the left one
        bt_amount = 0.0 if s.get("bt_fx_bypass") else bt_fx_amount(float(s.get("bt_fx_amount", 0.0)))
        # the Bluetooth effect plays on the phone's music and the pads together,
        # one instance per channel so the music stays stereo
        phone = 0.0 if s.get("bt_mute") else s.get("bt_level", 0.8)
        play = np.stack([self.bt_fx_l.process(bt_i, mix + bt_dry[:, 0] * phone, bt_amount),
                         self.bt_fx_r.process(bt_i, mix + bt_dry[:, 1] * phone, bt_amount)], axis=1)
        play = self._finite(play, f"bluetooth effect {left.name}", (left, right))

        master = s.get("master", 0.9)
        monitor = 0.0 if s.get("mic_mute") else s.get("monitor_level", 0.8)
        # The meters (Modulino Pixels, web UI) read the mix before master: master
        # is the earphones' trim (-20 dB here), and metering after it left the
        # Bluetooth/pads meter dark unless the knob was at full.
        self.level_mic_out = max(float(np.max(np.abs(wet))) * monitor, self.level_mic_out * 0.85)
        self.level_play = max(float(np.max(np.abs(play))), self.level_play * 0.85)
        out = (play + (wet * monitor)[:, None]) * master
        if self.tone_buf is not None:
            seg = self.tone_buf[self.tone_pos : self.tone_pos + len(out)]
            out[: len(seg)] += seg[:, None]
            self.tone_pos += len(out)
            if self.tone_pos >= len(self.tone_buf):
                self.tone_buf = None
        out = self.limiter.process(out)  # never past full scale
        self.level_raw = max(float(np.max(np.abs(x))) if len(x) else 0.0, self.level_raw * 0.85)

        mono = out.mean(axis=1)
        n = min(len(mono), len(self.recent_out))
        self.recent_out = np.concatenate([self.recent_out[n:], mono[-n:]])
        self.level_in = max(float(np.max(np.abs(dry))), self.level_in * 0.85)
        self.level_out = max(float(np.max(np.abs(out))), self.level_out * 0.85)
        return out

    def _mic_clipped(self) -> None:
        """The capture clipped in the codec. Repeated clipping (a voice too hot
        for it) pads its gain down one step; a single pop doesn't."""
        now = time.monotonic()
        self.stats["mic_clips"] = self.stats.get("mic_clips", 0) + 1
        self._clip_times = [t for t in self._clip_times if now - t < MIC_CLIP_WINDOW_S] + [now]
        pad = int(self.s.get("mic_pad_db", 0))
        if len(self._clip_times) < MIC_CLIP_BLOCKS or now < self._pad_quiet_until or pad >= audio_ports.MIC_PAD_MAX_DB:
            return
        self._clip_times = []
        self._set_mic_pad(pad + MIC_PAD_STEP_DB, "the mic clipped in the codec")

    def _set_mic_pad(self, new: int, why: str) -> None:
        """Change the codec's mic padding off the audio thread (amixer takes tens of ms)."""
        new = max(0, min(audio_ports.MIC_PAD_MAX_DB, int(new)))
        self._pad_quiet_until = time.monotonic() + MIC_PAD_SETTLE_S
        self._voice_s, self._voice_peak = 0.0, 0.0

        def apply() -> None:
            audio_ports.set_mic_gain(new)
            self.s["mic_pad_db"] = new
            self._pad_quiet_until = time.monotonic() + MIC_PAD_SETTLE_S
            log.info("%s: the codec's mic gain is now %d dB below normal", why, new)
            if self.on_mic_pad:
                self.on_mic_pad(new)

        threading.Thread(target=apply, name="mic-pad", daemon=True).start()

    def _handle_events(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            kind = ev[0]
            if kind == "down":
                self._pad_down(ev[1])
            elif kind == "up":
                self._pad_up(ev[1])
            elif kind == "hold":
                self._pad_hold(ev[1])
            elif kind == "stop_all":
                self.voices.clear()
            elif kind == "slot_changed":
                self._slot_changed(ev[1])
            elif kind == "tone":
                self._tone(*ev[1:])
            elif kind == "reset_fx":
                self.mic_fx.reset_all()

    def _tone(self, freq: float = 440.0, beeps: int = 1) -> None:
        """`beeps` short beeps at `freq`: lets you tell the outputs apart by ear."""
        beep, gap = int(0.18 * SR), int(0.12 * SR)
        t = np.arange(beep) / SR
        env = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)  # 10 ms ramps, no clicks
        one = (TONE_LEVEL * np.sin(2 * np.pi * freq * t) * env).astype(np.float32)
        self.tone_buf = np.concatenate([np.concatenate([one, np.zeros(gap, np.float32)]) for _ in range(max(1, int(beeps)))])
        self.tone_pos = 0

    def _pad_down(self, pad: int) -> None:
        buf = self.bank.play_buffer(pad)
        if buf is None or self.recording_pad is not None:
            return
        mode = self.bank.slots[pad].meta.mode
        if mode == "loop" and pad in self.voices and not self.voices[pad].releasing:
            self.voices[pad].releasing = True  # second press stops the loop
            return
        self.voices[pad] = Voice(pad, buf, loop=(mode == "loop"))
        self.focus_pad = pad

    def _slot_changed(self, pad: int) -> None:
        # a looping voice picks up the edit (pitch, trim...) without restarting
        v = self.voices.get(pad)
        buf = self.bank.play_buffer(pad)
        if v is None:
            return
        if buf is None or not len(buf):
            self.voices.pop(pad)
        else:
            v.buf, v.pos = buf, v.pos % len(buf)

    def _pad_up(self, pad: int) -> None:
        if self.recording_pad == pad:
            self._stop_recording()
            return
        v = self.voices.get(pad)
        if v and self.bank.slots[pad].meta.mode == "gate":
            v.releasing = True

    def _pad_hold(self, pad: int) -> None:
        if self.recording_pad is not None or pad >= MAX_PADS:
            return
        if self.bank.slots[pad].loaded and not self.s.get("rec_overwrite", True):
            return
        self.voices.pop(pad, None)
        self.recording_pad = pad
        self.rec_chunks, self.rec_len, self.rec_progress = [], 0, 0.0
        log.info("recording pad %d", pad)

    def _stop_recording(self) -> None:
        pad, chunks = self.recording_pad, self.rec_chunks
        self.recording_pad, self.rec_chunks, self.rec_len = None, [], 0
        if pad is None or not chunks:
            return
        audio = np.concatenate(chunks)
        trim, norm = self.s.get("auto_trim", True), self.s.get("normalize", True)
        floor = self.cond.floor

        def finish() -> None:
            # loudest 20 ms window must clear the input's noise floor by 8 dB, otherwise
            # it's hiss (e.g. nothing plugged in) and normalising it would be deafening
            w = int(SR * 0.02)
            usable = len(audio) // w * w
            loudest = float(np.sqrt((audio[:usable].reshape(-1, w) ** 2).mean(axis=1)).max()) if usable else 0.0
            if loudest < max(floor * 2.5, 1e-3):
                log.info("pad %d: nothing above the noise floor was recorded, pad left unchanged", pad)
                return
            take = tidy_recording(audio, trim, norm)
            if len(take) < SR * 0.05:
                log.info("pad %d: recording too short, ignored", pad)
                return
            self.bank.set_audio(pad, take, name=f"Pad {pad + 1} · {time.strftime('%d %b %H:%M:%S')}", kind="recordings")
            if self.on_recorded:
                self.on_recorded(pad)

        threading.Thread(target=finish, daemon=True).start()

    def _render_voices(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        done = []
        for pad, v in self.voices.items():
            seg = v.buf[v.pos : v.pos + n]
            if v.loop and len(seg) < n and len(v.buf):
                reps = int(np.ceil((n - len(seg)) / len(v.buf))) + 1
                seg = np.concatenate([seg, np.tile(v.buf, reps)])[:n]
                v.pos = (v.pos + n) % len(v.buf)
            else:
                v.pos += n
            if v.releasing:
                # ~10 ms release ramp
                end = max(0.0, v.fade - n / (0.01 * SR))
                seg = seg * np.linspace(v.fade, end, len(seg), dtype=np.float32)
                v.fade = end
                if end <= 0:
                    done.append(pad)
            out[: len(seg)] += seg
            if not v.loop and v.pos >= len(v.buf):
                done.append(pad)
        for pad in done:
            self.voices.pop(pad, None)
        self.playing = frozenset(self.voices)
        return out
