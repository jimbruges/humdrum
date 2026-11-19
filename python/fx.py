"""Voice effects for the mic channel.

Everything is block-based numpy (no per-sample Python loops): the QRB2210's
Cortex-A53 cores can't afford them at 48 kHz. Recursive structures (echo,
reverb) are only vectorisable when their delay is at least as long as the chunk
being processed, so those effects split the block into chunks no longer than
their shortest delay.

Each effect maps one `amount` in 0..1 (the knob) to its parameters, and
amount == 0 is fully dry. The Filter alone also goes negative (-1..0 low-pass, 0..1 high-pass).

Only numpy is used: the App Lab Python image doesn't ship scipy, and a unit at a
gig has no internet to pip-install it. Filters are short FIRs (np.convolve) or a
moving-average high-pass, which vectorise where recursive IIRs can't.
"""

from __future__ import annotations

from collections import deque

import numpy as np

SR = 48000


class FIR:
    """Streaming FIR filter: keeps the tail of the previous block as history."""

    def __init__(self, taps: np.ndarray) -> None:
        self.taps = taps.astype(np.float32)
        self.hist = np.zeros(len(taps) - 1, np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.hist, x])
        self.hist = buf[len(buf) - len(self.hist):]
        return np.convolve(buf, self.taps, "valid").astype(np.float32)

    def reset(self) -> None:
        self.hist[:] = 0


def lowpass_taps(fc: float, n: int) -> np.ndarray:
    """Windowed-sinc low-pass, unity gain at DC."""
    m = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * fc / SR * m) * np.hamming(n)
    return h / h.sum()


def bandpass_taps(f_lo: float, f_hi: float, n: int) -> np.ndarray:
    return lowpass_taps(f_hi, n) - lowpass_taps(f_lo, n)


class MovingAverageHighPass:
    """x minus its running mean over `n` samples: removes DC and rumble cheaply."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.hist = np.zeros(n - 1, np.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.hist, x.astype(np.float64)])
        self.hist = buf[len(buf) - (self.n - 1):]
        c = np.concatenate([[0.0], np.cumsum(buf)])
        mean = (c[self.n:] - c[:-self.n]) / self.n  # one value per input sample
        return (x - mean).astype(np.float32)


class RumbleFilter:
    """A steep low cut for the mic, cheap enough for numpy: the input delayed by
    half the window, minus three stacked moving averages of it (linear phase).
    With n = 321: -26 dB at 15 Hz, -39 dB at 7 Hz, under 1 dB lost at 100 Hz,
    10 ms of delay. The Media Carrier's headset input carries a large rumble
    below 50 Hz that otherwise swamps everything once any gain is applied."""

    def __init__(self, n: int = 321, stages: int = 3) -> None:
        self.n, self.stages = n, stages
        self.span = stages * (n - 1)  # samples of history each output needs
        self.hist = np.zeros(self.span, np.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.hist, x.astype(np.float64)])
        self.hist = buf[len(buf) - self.span:]
        low = buf
        for _ in range(self.stages):
            c = np.concatenate([[0.0], np.cumsum(low)])
            low = (c[self.n:] - c[:-self.n]) / self.n  # each pass is n - 1 shorter
        mid = self.span // 2
        return (buf[mid : mid + len(x)] - low).astype(np.float32)


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

    def prime(self, history: np.ndarray) -> None:
        """Called after reset() when the effect is switched in, with the most
        recent input. Effects that read back in time fill their buffers with it,
        so they don't start from silence (an audible gap on every switch)."""


class PitchShift(Effect):
    """Two-tap granular delay-line shifter (the classic 'harmonizer' trick)."""

    WINDOW = 2048  # ~43 ms grains: smooth enough for voice, low enough latency

    def __init__(self, name: str, short: str, max_semitones: float) -> None:
        self.name, self.short, self.max_semi = name, short, max_semitones
        self.line = DelayLine(self.WINDOW * 4)
        self.phase = 0.0

    def process(self, x, amount):
        return self.shift(x, self.max_semi * amount)

    def shift(self, x: np.ndarray, semitones: float) -> np.ndarray:
        self.line.write(x)
        if abs(semitones) < 0.05:
            return x
        ratio = 2.0 ** (semitones / 12.0)
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

    def prime(self, history):
        self.line.write(history[-self.line.mask:].astype(np.float32))


class Pitch(Effect):
    """Pitch on one bipolar knob, like the Filter: 0 is off, right shifts up to an
    octave higher, left down to an octave lower."""

    name, short = "Pitch", "PIT"
    bipolar = True
    labels = ("DWN", "PIT", "UP")
    DEAD = 0.06  # |amount| below this: no shift
    MAX_SEMI = 12.0

    def __init__(self) -> None:
        self.shifter = PitchShift("Pitch", "PIT", self.MAX_SEMI)

    def process(self, x, amount):
        a = abs(amount)
        semi = 0.0 if a <= self.DEAD else np.copysign(self.MAX_SEMI * (a - self.DEAD) / (1 - self.DEAD), amount)
        return self.shifter.shift(x, float(semi))

    def reset(self):
        self.shifter.reset()

    def prime(self, history):
        self.shifter.prime(history)


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
        self.lp = FIR(lowpass_taps(3500, 15))  # darkens each repeat

    def process(self, x, amount):
        fb = 0.2 + 0.55 * amount
        mix = 0.85 * amount
        out = np.empty_like(x)
        for i, c in _chunks(x, self.d):
            tap = self.line.read(self.d, len(c))
            # stop feeding new input at amount 0, but let the repeats die away
            self.line.write(c * (amount > 0.01) + fb * self.lp(tap))
            out[i : i + len(c)] = c + mix * tap
        return out

    def reset(self):
        self.line.clear()
        self.lp.reset()


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
        self.damp = FIR(lowpass_taps(5000, 15))
        self._live = False  # the tank holds something

    def process(self, x, amount):
        if amount < 0.01:
            # at 0 the output is the dry signal alone (mix is 0), so running the tank
            # is wasted work (about 1.2 ms a block on the A53, per instance): empty it
            if self._live:
                self.reset()
            return x
        self._live = True
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
        wet = self.damp(out)
        mix = min(1.0, amount * 1.5)
        return x * (1 - 0.35 * mix) + wet * mix * 1.6

    def reset(self):
        for line, _ in self.combs + self.aps:
            line.clear()
        self.damp.reset()
        self._live = False


class Radio(Effect):
    name, short = "Radio", "RAD"

    def __init__(self) -> None:
        self.band = FIR(bandpass_taps(450, 2600, 129))

    def prime(self, history):
        h = self.band.hist
        if len(history) >= len(h):
            h[:] = history[-len(h):]

    def process(self, x, amount):
        y = self.band(x)
        if amount < 0.01:
            return x
        drive = 1 + amount * 25
        y = np.tanh(y * drive) / np.tanh(drive) * 0.7
        mix = min(1.0, amount * 3)
        return (x * (1 - mix) + y * mix).astype(np.float32)


class Filter(Effect):
    """DJ-style filter on an amount of -1..1: 0 is flat (the same knob position
    that's "off" for every other effect, so switching to it changes nothing),
    turning left from there sweeps a low-pass down (20 kHz -> 150 Hz), right a
    high-pass up (20 Hz -> 6 kHz).

    24 dB/octave with a slight resonance at the cutoff. numpy can't run a
    sweepable IIR efficiently, so it's a 513-tap linear-phase FIR applied by FFT
    (overlap-save, about 0.1 ms a block), designed once per knob position and
    cached. That delays the signal by 5.3 ms while the effect is selected.
    When the knob moves, the old and new filters crossfade over the block.

    Bipolar like Pitch (amount below 0 allowed); main.py lets the knob
    go negative while it's selected and puts it back to 0 when you switch away."""

    name, short = "Filter", "FLT"
    bipolar = True
    labels = ("LP", "FLT", "HP")
    TAPS = 513
    DEAD = 0.06  # |amount| below this: flat
    LP_MIN, LP_MAX = 150.0, 20000.0
    HP_MIN, HP_MAX = 20.0, 6000.0
    STEPS = 240  # knob positions the filter is designed for (each side)

    def __init__(self) -> None:
        self.hist = np.zeros(self.TAPS - 1, np.float32)
        self.key = ("flat", 0)
        self._taps: dict = {}
        self._spec: dict = {}

    def _key(self, amount: float):
        if abs(amount) <= self.DEAD:
            return ("flat", 0)
        t = (min(1.0, abs(amount)) - self.DEAD) / (1.0 - self.DEAD)  # 0..1 along the sweep
        return ("lp" if amount < 0 else "hp", max(1, min(self.STEPS, round(t * self.STEPS))))

    def _design(self, key) -> np.ndarray:
        if key in self._taps:
            return self._taps[key]
        kind, step = key
        h = np.zeros(self.TAPS)
        if kind == "flat":
            h[self.TAPS // 2] = 1.0
        else:
            t = step / self.STEPS
            if kind == "lp":
                fc = self.LP_MAX * (self.LP_MIN / self.LP_MAX) ** t
            else:
                fc = self.HP_MIN * (self.HP_MAX / self.HP_MIN) ** t
            nf = 8192
            f = np.fft.rfftfreq(nf, 1 / SR)
            f[0] = 1e-3
            r = f / fc if kind == "lp" else fc / f
            mag = 1.0 / np.sqrt(1.0 + r ** 8)  # 4th-order Butterworth-like slope
            mag *= 1.0 + 0.35 * np.exp(-(np.log2(f / fc) / 0.3) ** 2)  # a little resonance at fc
            if kind == "hp":
                mag[0] = 0.0
            impulse = np.fft.irfft(mag, nf)  # zero phase
            half = self.TAPS // 2
            h = np.concatenate([impulse[-half:], impulse[: half + 1]]) * np.kaiser(self.TAPS, 6.0)
        self._taps[key] = h.astype(np.float32)
        return self._taps[key]

    def _spectrum(self, key, nfft: int) -> np.ndarray:
        k = (key, nfft)
        if k not in self._spec:
            if len(self._spec) > 64:
                self._spec.clear()
            self._spec[k] = np.fft.rfft(self._design(key), nfft)
        return self._spec[k]

    def process(self, x, amount):
        n = len(x)
        buf = np.concatenate([self.hist, x.astype(np.float32)])
        self.hist = buf[-(self.TAPS - 1):]
        nfft = 1 << int(np.ceil(np.log2(len(buf))))
        spec = np.fft.rfft(buf, nfft)
        key = self._key(amount)
        y = np.fft.irfft(spec * self._spectrum(key, nfft), nfft)[self.TAPS - 1: self.TAPS - 1 + n]
        if key != self.key:  # the knob moved: crossfade from the previous filter
            old = np.fft.irfft(spec * self._spectrum(self.key, nfft), nfft)[self.TAPS - 1: self.TAPS - 1 + n]
            g = np.linspace(0.0, 1.0, n, dtype=np.float32)
            y = old * (1 - g) + y * g
            self.key = key
        return y.astype(np.float32)

    def reset(self):
        self.hist[:] = 0

    def prime(self, history):
        if len(history) >= len(self.hist):
            self.hist[:] = history[-len(self.hist):]


class Theremin(Effect):
    """Hand height over a Modulino Distance plays your voice like a theremin.

    Closer is higher, as on the real thing: the voice is pitch-shifted up to an
    octave either way, gliding rather than jumping, and a sine 'ether' tone with a
    slow vibrato follows the hand over three octaves at the loudness of your voice.
    `hand` is 0 (far, or nothing in range) .. 1 (closest), set from the sensor.
    """

    name, short = "Theremin", "THR"
    RANGE_SEMI = 12
    GLIDE = 0.25  # per block: ~40 ms to settle at 512-sample blocks
    TONE_LOW_HZ, TONE_OCTAVES = 110.0, 3
    VIBRATO_HZ, VIBRATO_DEPTH = 5.5, 0.009  # about +/- 0.15 semitone

    def __init__(self) -> None:
        self.shifter = PitchShift("", "", 0)
        self.hand = 0.0
        self.h = 0.0  # glided hand position
        self.env = 0.0
        self.osc_phase = 0.0
        self.t = 0

    def process(self, x, amount):
        self.h += (self.hand - self.h) * self.GLIDE
        voice = self.shifter.shift(x, amount * self.RANGE_SEMI * (2 * self.h - 1))
        n = len(x)
        t = (self.t + np.arange(n)) / SR
        self.t += n
        if amount < 0.01:
            self.env = 0.0
            return voice
        rms = float(np.sqrt(np.mean(x * x)))
        env = rms if rms > self.env else self.env * 0.85  # follow the voice, release gently
        ramp = np.linspace(self.env, env, n, dtype=np.float32)
        self.env = env
        freq = self.TONE_LOW_HZ * 2 ** (self.TONE_OCTAVES * self.h)
        inst = freq * (1 + self.VIBRATO_DEPTH * np.sin(2 * np.pi * self.VIBRATO_HZ * t))
        phase = self.osc_phase + np.cumsum(2 * np.pi * inst / SR)
        self.osc_phase = float(phase[-1] % (2 * np.pi))
        tone = np.sin(phase).astype(np.float32) * ramp * 1.4
        return voice * (1 - 0.35 * amount) + tone * amount

    def reset(self):
        self.shifter.reset()
        self.env = 0.0

    def prime(self, history):
        self.shifter.prime(history)


def make_effects() -> list[Effect]:
    return [
        Filter(),
        Reverb(),
        Echo(),
        Radio(),
        Robot(),
        Crush(),
        Pitch(),
        Theremin(),  # only offered while a Modulino Distance is plugged in
    ]


class InputConditioner:
    """High-pass (rumble/DC) and a soft noise gate for the mic before the FX.

    The gate threshold follows the measured noise floor: an open mic input on the
    Media Carrier hisses at around -33 dBFS with nothing plugged in, far above any
    fixed threshold that would still let a quiet voice through.
    """

    # The headset input's hiss varies by about 10 dB from block to block, with
    # the odd much quieter block. A floor taken from the quietest 10% of 2 s,
    # plus 6 dB, landed below the typical hiss and the gate stood open half the
    # time on noise alone (measured on the board). So: the quietest 20% of 10 s,
    # open 10 dB above it, stay open down to 4 dB lower, and hold 0.2 s so word
    # endings aren't cut.
    FLOOR_BLOCKS = 940  # ~10 s of 512-sample blocks
    FLOOR_PERCENTILE = 20
    OPEN_DB = 10.0
    HYSTERESIS_DB = 4.0
    HOLD_S = 0.2
    DIGITAL_SILENCE = 1e-6  # capture start-up and dropouts deliver exact zeros; not the floor

    def __init__(self) -> None:
        self.hp = MovingAverageHighPass(SR // 180)  # about -3 dB at 80 Hz
        self.rumble = RumbleFilter()
        self.gate_gain = 0.0
        self.floor = 0.0  # noise floor, linear RMS after gain
        self._levels: deque[float] = deque(maxlen=self.FLOOR_BLOCKS)
        self._hold = 0  # samples the gate stays open for after the voice drops

    @property
    def is_open(self) -> bool:
        return self.gate_gain >= 1.0

    def process(self, x: np.ndarray, gain: float, gate_db: float) -> np.ndarray:
        y = self.hp(self.rumble(x)) * np.float32(gain)
        # Level without the loudest 5% of samples: a voice lifts it, while the
        # clicks and crackle a headset contact makes barely move it, so they
        # neither open the gate nor count as the noise floor.
        # (np.partition picks the same samples as a percentile, at a fraction of
        # np.percentile's cost on the A53: it ran twice per block)
        mag = np.abs(y)
        quiet = np.partition(mag, int(0.95 * (len(mag) - 1)))[: int(0.95 * (len(mag) - 1)) + 1] if len(mag) else mag
        rms = float(np.sqrt(np.mean(quiet * quiet)) + 1e-9) if len(quiet) else 1e-9
        # a low percentile over the last few seconds: one dead or quiet block can't drag it down
        if rms > self.DIGITAL_SILENCE:
            self._levels.append(rms)
        if len(self._levels) >= 10:
            levels = np.fromiter(self._levels, np.float64, len(self._levels))
            k = int(round(self.FLOOR_PERCENTILE / 100 * (len(levels) - 1)))
            self.floor = float(np.partition(levels, k)[k])
        open_db = max(gate_db, 20 * np.log10(self.floor + 1e-12) + self.OPEN_DB)
        level_db = 20 * np.log10(rms)
        # open at once, close over a few blocks; ramp inside the block so it never clicks
        if level_db > open_db or (self.is_open and level_db > open_db - self.HYSTERESIS_DB):
            new, self._hold = 1.0, int(self.HOLD_S * SR)
        elif self._hold > 0:
            new, self._hold = 1.0, self._hold - len(y)
        else:
            new = self.gate_gain * 0.6
        ramp = np.linspace(self.gate_gain, new, len(y), dtype=np.float32)
        self.gate_gain = new
        return y * ramp
