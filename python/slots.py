"""Sample library and pad slots.

Every sound lives in the on-board library, in one of two folders:
`library/uploads` (files sent from the web UI, named after the file) and
`library/recordings` (taken from the mic, named after the pad and the time).
A pad only points at a library sample and keeps its own playback settings,
so the same sound can sit on several pads and survives being unassigned.
Audio is 48 kHz mono float32 in memory and 16-bit WAV on disk.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

SR = 48000
MAX_PADS = 48
MAX_SECONDS = 30

MODES = ("oneshot", "gate", "loop")
KINDS = ("uploads", "recordings")
MATRIX_COLS = 13  # width of the UNO Q LED matrix


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.").lower()
    return s[:40] or "sample"


class Library:
    """Named WAV files on disk, indexed in library.json."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "library"
        for k in KINDS:
            (self.dir / k).mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "library.json"
        self.items: dict[str, dict] = {}
        self._cache: dict[str, np.ndarray] = {}
        self.lock = threading.Lock()
        if self.index_path.exists():
            try:
                self.items = json.loads(self.index_path.read_text()).get("items", {})
            except ValueError:
                self.items = {}
        # forget entries whose file has gone
        self.items = {i: m for i, m in self.items.items() if (self.dir / i).exists()}

    def _save(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": self.items}, indent=1))
        tmp.replace(self.index_path)

    def add(self, audio: np.ndarray, name: str, kind: str) -> str:
        kind = kind if kind in KINDS else "uploads"
        audio = audio[: SR * MAX_SECONDS].astype(np.float32)
        with self.lock:
            stem, n = _slug(name), 1
            sid = f"{kind}/{stem}.wav"
            while sid in self.items or (self.dir / sid).exists():
                n += 1
                sid = f"{kind}/{stem}-{n}.wav"
            (self.dir / sid).write_bytes(encode_wav(audio))
            self.items[sid] = {"name": name.strip()[:40] or stem, "kind": kind,
                               "seconds": round(len(audio) / SR, 2), "created": int(time.time())}
            self._cache[sid] = audio
            self._save()
        return sid

    def find_kit(self, key: str) -> str | None:
        """The library id of the kit sample `key` ("dancehall/airhorn.wav"), if it's in."""
        return next((sid for sid, it in self.items.items() if it.get("kit") == key), None)

    def mark_kit(self, sid: str, key: str) -> None:
        with self.lock:
            if sid in self.items:
                self.items[sid]["kit"] = key
                self._save()

    def audio(self, sid: str) -> np.ndarray | None:
        if sid not in self.items:
            return None
        if sid not in self._cache:
            try:
                self._cache[sid] = decode_wav((self.dir / sid).read_bytes())
            except Exception:
                return None
        return self._cache[sid]

    def name(self, sid: str) -> str:
        return self.items.get(sid, {}).get("name", "")

    def rename(self, sid: str, name: str) -> None:
        with self.lock:
            if sid in self.items and name.strip():
                self.items[sid]["name"] = name.strip()[:40]
                self._save()

    def delete(self, sid: str) -> None:
        with self.lock:
            if sid in self.items:
                (self.dir / sid).unlink(missing_ok=True)
                self.items.pop(sid)
                self._cache.pop(sid, None)
                self._save()

    def wav_bytes(self, sid: str) -> bytes | None:
        p = self.dir / sid
        return p.read_bytes() if sid in self.items and p.exists() else None

    def describe(self) -> list[dict]:
        out = [{"id": i, **m} for i, m in self.items.items()]
        out.sort(key=lambda d: (d["kind"], -d["created"] if d["kind"] == "recordings" else 0, d["name"].lower()))
        return out


@dataclass
class SlotMeta:
    sample: str = ""  # library id, "" = empty pad
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
    raw: np.ndarray | None = None  # the library sample
    play: np.ndarray | None = None  # trimmed, pitched, reversed, gain applied
    matrix_env: list[float] = field(default_factory=list)  # play buffer's shape, one value per matrix column
    peaks: list[float] = field(default_factory=list)  # the sample's outline for the web UI, worked out once

    @property
    def loaded(self) -> bool:
        return self.raw is not None and len(self.raw) > 0


class SlotBank:
    def __init__(self, root: Path) -> None:
        self.library = Library(root)
        self.dir = root / "slots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.dir / "slots.json"
        self.slots = [Slot() for _ in range(MAX_PADS)]
        self.lock = threading.Lock()
        self.version = 0  # bumped on every change, the UI uses it to refresh
        self._load_all()

    # -- persistence ---------------------------------------------------------

    def _load_all(self) -> None:
        metas = {}
        if self.meta_path.exists():
            try:
                metas = json.loads(self.meta_path.read_text())
            except ValueError:
                metas = {}
        migrated = False
        for pad in range(MAX_PADS):
            m = metas.get(str(pad))
            if m:
                known = {k: v for k, v in m.items() if k in SlotMeta.__dataclass_fields__}
                self.slots[pad].meta = SlotMeta(**known)
            old = self.dir / f"pad{pad:02d}.wav"
            if old.exists() and not self.slots[pad].meta.sample:
                # Before the library existed each pad had its own WAV: move it in.
                try:
                    audio = decode_wav(old.read_bytes())
                    name = self.slots[pad].meta.name or f"Pad {pad + 1}"
                    kind = "recordings" if name.upper().startswith("REC") else "uploads"
                    self.slots[pad].meta.sample = self.library.add(audio, name, kind)
                    old.unlink()
                    migrated = True
                except Exception:
                    old.unlink(missing_ok=True)
            self._attach(pad)
        if migrated:
            self._save_meta()

    def _attach(self, pad: int) -> None:
        s = self.slots[pad]
        s.raw = self.library.audio(s.meta.sample) if s.meta.sample else None
        if s.raw is None:
            s.meta.sample = ""
        # Computed here, not on every state push: across 12 multi-second samples
        # that took ~22 ms of the GIL each time, long enough to starve the audio
        # thread into an xrun (a 512-sample hole, heard as a click on FX switches).
        s.peaks = peaks(s.raw, 120) if s.loaded else []
        self._render(pad)

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

    def assign(self, pad: int, sid: str, mode: str | None = None) -> None:
        """Put a library sample on a pad ("" empties it). Trim, pitch and so on start fresh."""
        with self.lock:
            s = self.slots[pad]
            mode = mode or (s.meta.mode if s.loaded else "oneshot")
            s.meta = SlotMeta(sample=sid, name=self.library.name(sid), mode=mode) if sid else SlotMeta()
            self._attach(pad)
            self._save_meta()
            self.version += 1

    def load_kits(self, samples_dir: Path, lay_out: bool) -> int:
        """Add the sample kits shipped with the app (samples/<kit>/kit.json) to the
        library, once each. With `lay_out`, also put them on the pads in kit order
        (the first kit from pad 1), replacing what's there; the library keeps
        everything, so nothing is lost. Returns how many pads were laid out."""
        pad = 0
        for manifest in sorted(samples_dir.glob("*/kit.json")):
            try:
                kit = json.loads(manifest.read_text())
            except ValueError:
                continue
            for entry in kit.get("pads", []):
                key = f"{manifest.parent.name}/{entry['file']}"
                sid = self.library.find_kit(key)
                if sid is None:
                    path = manifest.parent / entry["file"]
                    if not path.exists():
                        continue
                    sid = self.library.add(decode_wav(path.read_bytes()), entry.get("name") or path.stem, "uploads")
                    self.library.mark_kit(sid, key)
                if lay_out and pad < MAX_PADS:
                    self.assign(pad, sid, entry.get("mode", "oneshot"))
                    pad += 1
        return pad

    def set_audio(self, pad: int, audio: np.ndarray, name: str = "", kind: str = "uploads") -> str:
        """Add a new sound to the library and put it on `pad`."""
        sid = self.library.add(audio, name or f"Pad {pad + 1}", kind)
        self.assign(pad, sid)
        return sid

    def update_meta(self, pad: int, changes: dict) -> None:
        with self.lock:
            s = self.slots[pad]
            for k, v in changes.items():
                if k not in SlotMeta.__dataclass_fields__ or k == "sample":
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
        """Empty the pad; the sample stays in the library."""
        self.assign(pad, "")

    def swap(self, a: int, b: int) -> None:
        with self.lock:
            self.slots[a], self.slots[b] = self.slots[b], self.slots[a]
            self._save_meta()
            self.version += 1

    def rename_sample(self, sid: str, name: str) -> None:
        self.library.rename(sid, name)
        with self.lock:
            for s in self.slots:
                if s.meta.sample == sid:
                    s.meta.name = self.library.name(sid)
            self._save_meta()
            self.version += 1

    def delete_sample(self, sid: str) -> list[int]:
        """Remove a sample from the library; pads that used it become empty."""
        pads = [i for i, s in enumerate(self.slots) if s.meta.sample == sid]
        for pad in pads:
            self.assign(pad, "")
        self.library.delete(sid)
        with self.lock:
            self.version += 1
        return pads

    # -- queries -------------------------------------------------------------

    def play_buffer(self, pad: int) -> np.ndarray | None:
        return self.slots[pad].play if 0 <= pad < MAX_PADS else None

    def wav_bytes(self, pad: int) -> bytes | None:
        return self.library.wav_bytes(self.slots[pad].meta.sample) if self.slots[pad].loaded else None

    def describe(self, pad: int) -> dict:
        s = self.slots[pad]
        d = {"pad": pad, "loaded": s.loaded}
        if s.loaded:
            d.update(asdict(s.meta))
            d["seconds"] = round(len(s.raw) / SR, 2)
            d["peaks"] = s.peaks
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
