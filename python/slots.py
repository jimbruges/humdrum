"""Sample slots: storage on disk, decoding and per-slot playback settings.

A slot is addressed by pad number (module order on the Qwiic bus x 3 + button).
Audio is kept at 48 kHz mono float32 in memory and 16-bit WAV on disk, so a
slot survives modules being unplugged and comes back when they are.
"""

from __future__ import annotations

import io
import json
import threading
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

SR = 48000
MAX_PADS = 48
MAX_SECONDS = 30

MODES = ("oneshot", "gate", "loop")
MATRIX_COLS = 13  # width of the UNO Q LED matrix


@dataclass
class SlotMeta:
    name: str = ""
    gain: float = 1.0
    mode: str = "oneshot"
    pitch: float = 0.0  # semitones
    start: float = 0.0  # trim, 0..1 of the recording
    end: float = 1.0
    reverse: bool = False


@dataclass
class Slot:
    meta: SlotMeta = field(default_factory=SlotMeta)
    raw: np.ndarray | None = None  # as recorded/uploaded
    play: np.ndarray | None = None  # trimmed, pitched, reversed, gain applied
    matrix_env: list[float] = field(default_factory=list)  # play buffer's shape, one value per matrix column

    @property
    def loaded(self) -> bool:
        return self.raw is not None and len(self.raw) > 0


class SlotBank:
    def __init__(self, root: Path) -> None:
        self.dir = root / "slots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "slots.json"
        self.slots = [Slot() for _ in range(MAX_PADS)]
        self.lock = threading.Lock()
        self.version = 0  # bumped on every change, the UI uses it to refresh
        self._load_all()

    # -- persistence ---------------------------------------------------------

    def _wav_path(self, pad: int) -> Path:
        return self.dir / f"pad{pad:02d}.wav"

    def _load_all(self) -> None:
        metas = {}
        if self.meta_path.exists():
            try:
                metas = json.loads(self.meta_path.read_text())
            except ValueError:
                metas = {}
        for pad in range(MAX_PADS):
            m = metas.get(str(pad))
            if m:
                known = {k: v for k, v in m.items() if k in SlotMeta.__dataclass_fields__}
                self.slots[pad].meta = SlotMeta(**known)
            p = self._wav_path(pad)
            if p.exists():
                try:
                    self.slots[pad].raw = decode_wav(p.read_bytes())
                    self._render(pad)
                except Exception:
                    p.unlink(missing_ok=True)

    def _save_meta(self) -> None:
        data = {str(i): asdict(s.meta) for i, s in enumerate(self.slots) if s.loaded}
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(self.meta_path)

    # -- edits ---------------------------------------------------------------

    def _render(self, pad: int) -> None:
        s = self.slots[pad]
        if not s.loaded:
            s.play = None
            s.matrix_env = []
            return
        m = s.meta
        n = len(s.raw)
        a, b = int(np.clip(m.start, 0, 1) * n), int(np.clip(m.end, 0, 1) * n)
        x = s.raw[a:b] if b - a > 64 else s.raw
        if m.reverse:
            x = x[::-1]
        if abs(m.pitch) > 0.01:
            # resampling pitch: shorter and higher, like a classic sampler
            ratio = 2.0 ** (m.pitch / 12.0)
            pos = np.arange(0, len(x) - 1, ratio)
            x = np.interp(pos, np.arange(len(x)), x)
        x = (x * m.gain).astype(np.float32)
        # 3 ms fades so trimmed starts and ends don't click
        f = min(144, len(x) // 4)
        if f:
            ramp = np.linspace(0, 1, f, dtype=np.float32)
            x[:f] *= ramp
            x[-f:] *= ramp[::-1]
        s.play = x
        s.matrix_env = envelope(x, MATRIX_COLS)

    def set_audio(self, pad: int, audio: np.ndarray, name: str = "") -> None:
        audio = audio[: SR * MAX_SECONDS].astype(np.float32)
        with self.lock:
            s = self.slots[pad]
            s.raw = audio
            s.meta = SlotMeta(name=name or f"PAD {pad + 1:02d}", mode=s.meta.mode if s.loaded else "oneshot")
            self._render(pad)
            self._wav_path(pad).write_bytes(encode_wav(audio))
            self._save_meta()
            self.version += 1

    def update_meta(self, pad: int, changes: dict) -> None:
        with self.lock:
            s = self.slots[pad]
            for k, v in changes.items():
                if k not in SlotMeta.__dataclass_fields__:
                    continue
                if k == "mode" and v not in MODES:
                    continue
                if k in ("gain", "pitch", "start", "end"):
                    v = float(v)
                setattr(s.meta, k, v)
            s.meta.gain = float(np.clip(s.meta.gain, 0, 4))
            s.meta.pitch = float(np.clip(s.meta.pitch, -24, 24))
            self._render(pad)
            self._save_meta()
            self.version += 1

    def clear(self, pad: int) -> None:
        with self.lock:
            self.slots[pad] = Slot()
            self._wav_path(pad).unlink(missing_ok=True)
            self._save_meta()
            self.version += 1

    def swap(self, a: int, b: int) -> None:
        with self.lock:
            self.slots[a], self.slots[b] = self.slots[b], self.slots[a]
            pa, pb = self._wav_path(a), self._wav_path(b)
            tmp = self.dir / "swap.tmp"
            if pa.exists():
                pa.replace(tmp)
            if pb.exists():
                pb.replace(pa)
            if tmp.exists():
                tmp.replace(pb)
            self._save_meta()
            self.version += 1

    # -- queries -------------------------------------------------------------

    def play_buffer(self, pad: int) -> np.ndarray | None:
        return self.slots[pad].play if 0 <= pad < MAX_PADS else None

    def wav_bytes(self, pad: int) -> bytes | None:
        p = self._wav_path(pad)
        return p.read_bytes() if p.exists() else None

    def describe(self, pad: int) -> dict:
        s = self.slots[pad]
        d = {"pad": pad, "loaded": s.loaded}
        if s.loaded:
            d.update(asdict(s.meta))
            d["seconds"] = round(len(s.raw) / SR, 2)
            d["peaks"] = peaks(s.raw, 120)
        return d


# -- audio helpers -----------------------------------------------------------


def peaks(x: np.ndarray, n: int) -> list[float]:
    if len(x) < n:
        x = np.pad(x, (0, n - len(x)))
    usable = len(x) // n * n
    return [round(float(v), 3) for v in np.abs(x[:usable]).reshape(n, -1).max(axis=1)]


def envelope(x: np.ndarray, n: int) -> list[float]:
    """Peak per column, scaled so the loudest column is 1."""
    vals = peaks(x, n)
    top = max(vals) or 1.0
    return [v / top for v in vals]


def decode_wav(data: bytes) -> np.ndarray:
    """PCM WAV (8/16/24/32-bit, any rate, any channels) -> 48 kHz mono float32."""
    with wave.open(io.BytesIO(data)) as w:
        ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        frames = min(frames, rate * MAX_SECONDS)
        raw = w.readframes(frames)
    if width == 1:
        x = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128
    elif width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        i = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        i = np.where(i & 0x800000, i - 0x1000000, i)
        x = i.astype(np.float32) / 8388608
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648
    else:
        raise ValueError("unsupported sample width")
    x = x.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        pos = np.arange(0, len(x) - 1, rate / SR)
        x = np.interp(pos, np.arange(len(x)), x)
    return x.astype(np.float32)


def encode_wav(x: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def tidy_recording(x: np.ndarray, trim: bool, normalize: bool) -> np.ndarray:
    """Cut leading/trailing silence (keeping a short pre-roll) and normalise to -1 dBFS."""
    if len(x) == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak < 1e-4:
        return x
    if trim:
        above = np.flatnonzero(np.abs(x) > peak * 0.06)
        a = max(0, above[0] - int(0.015 * SR))
        b = min(len(x), above[-1] + int(0.08 * SR))
        x = x[a:b]
    if normalize:
        x = x * (0.89 / peak)
    return x.astype(np.float32)
