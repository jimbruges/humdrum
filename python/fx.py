"""Voice effects for the mic channel.

Everything is block-based numpy (no per-sample Python loops): the QRB2210's
Cortex-A53 cores can't afford them at 48 kHz. Recursive structures (echo,
reverb) are only vectorisable when their delay is at least as long as the chunk
being processed, so those effects split the block into chunks no longer than
their shortest delay.

Each effect maps one `amount` in 0..1 (the knob) to its parameters, and
amount == 0 is always fully dry.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

SR = 48000


class DelayLine:
    """Power-of-two ring buffer addressed by absolute sample index."""

    def __init__(self, size: int) -> None:
        n = 1 << int(np.ceil(np.log2(size)))
        self.buf = np.zeros(n, dtype=np.float32)
        self.mask = n - 1
        self.w = 0  # absolute index of the next sample to be written

    def write(self, x: np.ndarray) -> None:
        idx = (self.w + np.arange(len(x))) & self.mask
        self.buf[idx] = x
        self.w += len(x)

    def read(self, delay: int, n: int) -> np.ndarray:
        """`n` samples that were written `delay` samples before the next write."""
        idx = (self.w - delay + np.arange(n)) & self.mask
        return self.buf[idx]

    def read_frac(self, pos: np.ndarray) -> np.ndarray:
        """Linear interpolation at absolute (fractional) positions."""
        i0 = np.floor(pos).astype(np.int64)
        f = (pos - i0).astype(np.float32)
        a = self.buf[i0 & self.mask]
        b = self.buf[(i0 + 1) & self.mask]
        return a + (b - a) * f

    def clear(self) -> None:
        self.buf[:] = 0


def _chunks(x: np.ndarray, size: int):
    for i in range(0, len(x), size):
        yield i, x[i : i + size]


class Effect:
    name = "?"
    short = "?"

    def process(self, x: np.ndarray, amount: float) -> np.ndarray:
        raise NotImplementedError

    def reset(self) -> None:
        pass


class PitchShift(Effect):
    """Two-tap granular delay-line shifter (the classic 'harmonizer' trick)."""

    WINDOW = 2048  # ~43 ms grains: smooth enough for voice, low enough latency

    def __init__(self, name: str, short: str, max_semitones: float) -> None:
        self.name, self.short, self.max_semi = name, short, max_semitones
        self.line = DelayLine(self.WINDOW * 4)
        self.phase = 0.0

    def process(self, x, amount):
        self.line.write(x)
        if amount < 0.01:
            return x
        ratio = 2.0 ** (self.max_semi * amount / 12.0)
        n = len(x)
        rate = (1.0 - ratio) / self.WINDOW
        ph = (self.phase + rate * np.arange(1, n + 1)) % 1.0
        self.phase = float(ph[-1])
        ph2 = (ph + 0.5) % 1.0
        now = self.line.w - n + np.arange(n)  # absolute index of each input sample
        a = self.line.read_frac(now - (ph * self.WINDOW + 1))
        b = self.line.read_frac(now - (ph2 * self.WINDOW + 1))
        g = np.sin(np.pi * ph).astype(np.float32) ** 2  # tap a is silent at its wrap
        return a * g + b * (1.0 - g)

    def reset(self):
        self.line.clear()


class Robot(Effect):
    name, short = "Robot", "ROB"

    def __init__(self) -> None:
        self.t = 0

    def process(self, x, amount):
        if amount < 0.01:
            return x
        freq = 30.0 + 220.0 * amount
        n = np.arange(self.t, self.t + len(x))
        self.t += len(x)
        car = np.sin(2 * np.pi * freq * n / SR).astype(np.float32)
        wet = min(1.0, amount * 4)
        return x * (1 - wet) + 1.6 * x * car * wet


class Crush(Effect):
    name, short = "Bitcrush", "BIT"

    def __init__(self) -> None:
        self.hold = 0.0
        self.count = 0

    def process(self, x, amount):
        if amount < 0.01:
            return x
        bits = 12.0 - 9.0 * amount
        q = 2.0 ** (bits - 1)
        step = 1 + int(amount * 23)
        # sample & hold that keeps its phase across blocks
        idx = (self.count + np.arange(len(x))) // step * step - self.count
        held = np.where(idx < 0, self.hold, x[np.clip(idx, 0, len(x) - 1)])
        self.count = (self.count + len(x)) % step
        self.hold = float(held[-1])
        return (np.round(held * q) / q).astype(np.float32)


class Echo(Effect):
    name, short = "Echo", "ECO"
    TIME_S = 0.28

    def __init__(self) -> None:
        self.d = int(self.TIME_S * SR)
        self.line = DelayLine(self.d * 2)
        self.lp = signal.butter(1, 3500, fs=SR, output="sos")
        self.zi = signal.sosfilt_zi(self.lp) * 0

    def process(self, x, amount):
        fb = 0.2 + 0.55 * amount
        mix = 0.85 * amount
        out = np.empty_like(x)
        for i, c in _chunks(x, self.d):
            tap = self.line.read(self.d, len(c))
            tap_f, self.zi = signal.sosfilt(self.lp, tap, zi=self.zi)
            # stop feeding new input at amount 0, but let the repeats die away
            self.line.write(c * (amount > 0.01) + fb * tap_f.astype(np.float32))
            out[i : i + len(c)] = c + mix * tap
        return out

    def reset(self):
        self.line.clear()


class Reverb(Effect):
    """Schroeder/Freeverb-style: 4 parallel combs into 2 allpasses."""

    name, short = "Reverb", "VRB"
    COMBS = (1557, 1617, 1491, 1422)
    ALLPASS = (605, 480)
    CHUNK = 256  # <= shortest delay

    def __init__(self) -> None:
        scale = SR / 44100
        self.combs = [(DelayLine(int(d * scale) * 2), int(d * scale)) for d in self.COMBS]
        self.aps = [(DelayLine(int(d * scale) * 2), int(d * scale)) for d in self.ALLPASS]
        self.damp = signal.butter(1, 5000, fs=SR, output="sos")
        self.zi = signal.sosfilt_zi(self.damp) * 0

    def process(self, x, amount):
        # the tank keeps running at amount 0 so the tail rings out instead of cutting
        g = 0.72 + 0.24 * amount
        out = np.empty_like(x)
        for i, c in _chunks(x, self.CHUNK):
            n = len(c)
            inp = c * 0.12 * (amount > 0.01)
            acc = np.zeros(n, dtype=np.float32)
            for line, d in self.combs:
                y = line.read(d, n)
                line.write(inp + g * y)
                acc += y
            for line, d in self.aps:
                bo = line.read(d, n)
                line.write(acc + 0.5 * bo)
                acc = bo - acc
            out[i : i + n] = acc
        wet, self.zi = signal.sosfilt(self.damp, out, zi=self.zi)
        mix = min(1.0, amount * 1.5)
        return x * (1 - 0.35 * mix) + wet.astype(np.float32) * mix * 1.6

    def reset(self):
        for line, _ in self.combs + self.aps:
            line.clear()


class Radio(Effect):
    name, short = "Radio", "RAD"

    def __init__(self) -> None:
        self.sos = signal.butter(2, (450, 2600), btype="bandpass", fs=SR, output="sos")
        self.zi = signal.sosfilt_zi(self.sos) * 0

    def process(self, x, amount):
        y, self.zi = signal.sosfilt(self.sos, x, zi=self.zi)
        if amount < 0.01:
            return x
        drive = 1 + amount * 25
        y = np.tanh(y * drive) / np.tanh(drive) * 0.7
        mix = min(1.0, amount * 3)
        return (x * (1 - mix) + y * mix).astype(np.float32)


def make_effects() -> list[Effect]:
    return [
        PitchShift("Pitch up", "UP", 12),
        PitchShift("Pitch down", "DWN", -12),
        Robot(),
        Crush(),
        Echo(),
        Reverb(),
        Radio(),
    ]


class InputConditioner:
    """High-pass (rumble/DC) and a soft noise gate for the mic before the FX."""

    def __init__(self) -> None:
        self.hp = signal.butter(2, 80, btype="highpass", fs=SR, output="sos")
        self.zi = signal.sosfilt_zi(self.hp) * 0
        self.gate_gain = 0.0

    def process(self, x: np.ndarray, gain: float, gate_db: float) -> np.ndarray:
        y, self.zi = signal.sosfilt(self.hp, x, zi=self.zi)
        y = (y * gain).astype(np.float32)
        rms = float(np.sqrt(np.mean(y * y)) + 1e-9)
        # open at once, close over a few blocks; ramp inside the block so it never clicks
        new = 1.0 if 20 * np.log10(rms) > gate_db else self.gate_gain * 0.6
        ramp = np.linspace(self.gate_gain, new, len(y), dtype=np.float32)
        self.gate_gain = new
        return y * ramp
