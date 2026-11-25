"""Voice effects for the mic channel.

Everything is block-based numpy (no per-sample Python loops): the QRB2210's
Cortex-A53 cores can't afford them at 48 kHz. Recursive structures (echo,
reverb) are only vectorisable when their delay is at least as long as the chunk
being processed, so those effects split the block into chunks no longer than
their shortest delay.

Each effect maps one `amount` in 0..1 (the knob) to its parameters, and
amount == 0 is fully dry. The Filter alone also goes negative (-1..0 low-pass, 0..1 high-pass).

Only numpy is used: the App Lab Python image doesn't ship scipy, and a unit at a
gig has no internet to pip-install it. Filters are short FIRs (np.convolve), or
recursive ones split into one-pole sections that numpy can sum in closed form
(HighPass).
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


class HighPass:
    """Butterworth high-pass (`order` poles at `fc`), with no added latency.

    The mic used to go through a steep linear-phase FIR low cut (three stacked
    moving averages), which delayed the voice by 10 ms. A recursive filter has next to no delay where the voice is,
    but numpy can't run a recursion sample by sample fast enough. So the filter
    is split into one-pole sections (partial fractions, complex where the poles
    are), and each section's recursion v[n] = p v[n-1] + r x[n] is summed in
    closed form for a whole block: v = p^n (p v[-1] + r cumsum(x p^-n))."""

    MAX_CHUNK = 512  # keeps |p|^-n small, so the closed form stays exact in float64

    def __init__(self, fc: float = 60.0, order: int = 4) -> None:
        k = np.arange(1, order + 1)
        proto = np.exp(1j * np.pi * (2 * k + order - 1) / (2 * order))  # analog low-pass prototype poles
        wc = 2 * SR * np.tan(np.pi * fc / SR)  # prewarped
        s = wc / proto  # low-pass -> high-pass
        p = (1 + s / (2 * SR)) / (1 - s / (2 * SR))  # bilinear transform; the zeros all land on z = 1
        g = np.real(np.prod(1 + p)) / 2 ** order  # unity gain at Nyquist
        self.k0 = float(np.real(g / np.prod(p)))
        w = 1 / p
        r = np.array([g * (1 - w[i]) ** order / np.prod(np.delete(1 - p * w[i], i)) for i in range(order)])
        # keep one pole of each conjugate pair (doubling its real part), and any real pole once
        self.poles, self.res, self.weight = [], [], []
        for i in range(order):
            if p[i].imag < -1e-12:
                continue
            self.poles.append(p[i])
            self.res.append(r[i])
            self.weight.append(2.0 if p[i].imag > 1e-12 else 1.0)
        self.poles, self.res, self.weight = np.array(self.poles), np.array(self.res), np.array(self.weight)[:, None]
        self.state = np.zeros(len(self.poles), np.complex128)
        self._pow: dict[int, tuple] = {}

    def _powers(self, n: int) -> tuple:
        if n not in self._pow:
            m = np.arange(n)
            up = self.poles[:, None] ** m[None, :]  # p^n
            self._pow[n] = (up, 1.0 / up)
        return self._pow[n]

    def __call__(self, x: np.ndarray) -> np.ndarray:
        out = np.empty(len(x), np.float32)
        for i in range(0, len(x), self.MAX_CHUNK):
            c = x[i : i + self.MAX_CHUNK].astype(np.float64)
            up, down = self._powers(len(c))
            v = up * (self.poles[:, None] * self.state[:, None] + self.res[:, None] * np.cumsum(c[None, :] * down, axis=1))
            self.state = v[:, -1]
            out[i : i + len(c)] = self.k0 * c + (np.real(v) * self.weight).sum(axis=0)
        return out

    def reset(self) -> None:
        self.state[:] = 0


class DelayLine:
    """Power-of-two ring buffer addressed by absolute sample index."""

    def __init__(self, size: int) -> None:
        n = 1 << int(np.ceil(np.log2(size)))
        self.buf = np.zeros(n, dtype=np.float32)
        self.mask = n - 1
        self.w = 0  # absolute index of the next sample to be written

    # read/write use slices (two at the wrap): index arrays cost several times
    # as much on the A53, and the reverb does 12 of these per chunk

    def write(self, x: np.ndarray) -> None:
        n, size = len(x), len(self.buf)
        if n > size:
            x, self.w, n = x[-size:], self.w + n - size, size
        at = self.w & self.mask
        first = min(n, size - at)
        self.buf[at : at + first] = x[:first]
        self.buf[: n - first] = x[first:]
        self.w += n

    def read(self, delay: int, n: int) -> np.ndarray:
        """`n` samples that were written `delay` samples before the next write."""
        at = (self.w - delay) & self.mask
        size = len(self.buf)
        if at + n <= size:
            return self.buf[at : at + n].copy()
        if n <= size:
            return np.concatenate([self.buf[at:], self.buf[: n - (size - at)]])
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

    def _glide(self, name: str, new: float, n: int) -> np.ndarray | float:
        """A gain that moves from its last block's value to `new` across this one.
        Stepping a mix level once per block, as the knob clicks, made a small
        click at every step ("zipper noise"). Returns a plain float when it
        isn't moving."""
        old = getattr(self, name, new)
        setattr(self, name, new)
        if old == new:
            return new
        return np.linspace(old, new, n, endpoint=False, dtype=np.float32) + np.float32((new - old) / n)


class PitchShift(Effect):
    """Two-tap granular delay-line shifter (the classic 'harmonizer' trick)."""

    WINDOW = 2048  # ~43 ms grains: smooth enough for voice, low enough latency

    def __init__(self, name: str, short: str, max_semitones: float) -> None:
        self.name, self.short, self.max_semi = name, short, max_semitones
        self.line = DelayLine(self.WINDOW * 4)
        self.phase = 0.0
        self.on = False  # shifting (not passing the input straight through) last block

    def process(self, x, amount):
        return self.shift(x, self.max_semi * amount)

    def shift(self, x: np.ndarray, semitones: float) -> np.ndarray:
        self.line.write(x)
        on, was = abs(semitones) >= 0.05, self.on
        self.on = on
        if not on and not was:
            return x
        # The shifted voice runs up to WINDOW samples behind the input: switching
        # straight between the two jumped (a click each time the knob crossed its
        # dead zone). So that block crossfades from one to the other.
        ratio = 2.0 ** ((semitones if on else 0.0) / 12.0)
        n = len(x)
        rate = (1.0 - ratio) / self.WINDOW
        ph = (self.phase + rate * np.arange(1, n + 1)) % 1.0
        self.phase = float(ph[-1])
        ph2 = (ph + 0.5) % 1.0
        now = self.line.w - n + np.arange(n)  # absolute index of each input sample
        a = self.line.read_frac(now - (ph * self.WINDOW + 1))
        b = self.line.read_frac(now - (ph2 * self.WINDOW + 1))
        g = np.sin(np.pi * ph).astype(np.float32) ** 2  # tap a is silent at its wrap
        y = a * g + b * (1.0 - g)
        if on != was:
            t = np.linspace(0.0, 1.0, n, dtype=np.float32)
            w = np.sin(0.5 * np.pi * (t if on else 1.0 - t))  # equal power: the two aren't in phase
            y = y * w + x * np.sqrt(1.0 - w * w)
        return y

    def reset(self):
        self.line.clear()
        self.on = False

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
        self.phase = 0.0

    def process(self, x, amount):
        wet = self._glide("_wet", min(1.0, amount * 4) if amount >= 0.01 else 0.0, len(x))
        if np.all(wet == 0):
            return x
        # The carrier's phase carries on from block to block. It used to be
        # sin(2 pi f t) with t counted from the start, so every knob click (a new
        # f) jumped it to a random phase: a click at every step.
        freq = 30.0 + 220.0 * amount
        ph = self.phase + 2 * np.pi * freq / SR * np.arange(1, len(x) + 1)
        self.phase = float(ph[-1] % (2 * np.pi))
        car = np.sin(ph).astype(np.float32)
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
        n = len(x)
        mix = self._glide("_mix", 0.85 * amount, n)
        # stop feeding new input at amount 0, but let the repeats die away (faded,
        # so a repeat doesn't start or stop on a hard edge 0.28 s later)
        feed = self._glide("_feed", 1.0 if amount > 0.01 else 0.0, n)
        out = np.empty_like(x)
        for i, c in _chunks(x, self.d):
            k = len(c)
            m = mix[i : i + k] if isinstance(mix, np.ndarray) else mix
            f = feed[i : i + k] if isinstance(feed, np.ndarray) else feed
            tap = self.line.read(self.d, k)
            self.line.write(c * f + fb * self.lp(tap))
            out[i : i + k] = c + m * tap
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
        mix = self._glide("_mix", min(1.0, amount * 1.5) if amount >= 0.01 else 0.0, len(x))
        if np.all(mix == 0):
            # at 0 the output is the dry signal alone, so running the tank is
            # wasted work (about 1.2 ms a block on the A53, per instance): empty it.
            # (The mix has just glided down to 0 over a block: no click.)
            if self._live:
                self.reset()
            return x
        self._live = True
        g = 0.72 + 0.24 * amount
        out = np.empty_like(x)
        for i, c in _chunks(x, self.CHUNK):
            n = len(c)
            inp = c * 0.12
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
        mix = self._glide("_mix", min(1.0, amount * 3) if amount >= 0.01 else 0.0, len(x))
        if np.all(mix == 0):
            return x
        drive = 1 + max(amount, 0.01) * 25
        y = np.tanh(y * drive) / np.tanh(drive) * 0.7
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

    Every knob position's design is worked out once, up front (`warm`), and
    shared by all the instances (mic, Bluetooth left and right). Designing on the
    fly took 3.4 ms per instance on the A53, every block the knob moved: with two
    Bluetooth channels that alone was most of a block's 10.7 ms, and turning the
    filter made the music skip.

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
    _taps: dict = {}  # key -> impulse response, shared by every instance
    _spec: dict = {}  # (key, nfft) -> its spectrum

    def __init__(self) -> None:
        self.hist = np.zeros(self.TAPS - 1, np.float32)
        self.key = ("flat", 0)

    @classmethod
    def keys(cls) -> list:
        return [("flat", 0)] + [(kind, step) for kind in ("lp", "hp") for step in range(1, cls.STEPS + 1)]

    @classmethod
    def warm(cls, block: int) -> None:
        """Design every knob position for blocks of `block` samples, in batches
        (about a second on the A53, before the audio starts)."""
        nfft = cls._nfft(block)
        todo = [k for k in cls.keys() if (k, nfft) not in cls._spec]
        for i in range(0, len(todo), 40):
            batch = todo[i : i + 40]
            taps = cls._design_many(batch)
            spec = np.fft.rfft(taps, nfft, axis=1).astype(np.complex64)
            for k, h, s in zip(batch, taps, spec):
                cls._taps[k] = h
                cls._spec[(k, nfft)] = s

    @classmethod
    def _nfft(cls, n: int) -> int:
        return 1 << int(np.ceil(np.log2(n + cls.TAPS - 1)))

    def _key(self, amount: float):
        if abs(amount) <= self.DEAD:
            return ("flat", 0)
        t = (min(1.0, abs(amount)) - self.DEAD) / (1.0 - self.DEAD)  # 0..1 along the sweep
        return ("lp" if amount < 0 else "hp", max(1, min(self.STEPS, round(t * self.STEPS))))

    @classmethod
    def _design_many(cls, keys: list) -> np.ndarray:
        nf = 8192
        f = np.fft.rfftfreq(nf, 1 / SR)
        f[0] = 1e-3
        mags = np.zeros((len(keys), len(f)))
        for row, (kind, step) in enumerate(keys):
            if kind == "flat":
                continue
            t = step / cls.STEPS
            if kind == "lp":
                fc = cls.LP_MAX * (cls.LP_MIN / cls.LP_MAX) ** t
            else:
                fc = cls.HP_MIN * (cls.HP_MAX / cls.HP_MIN) ** t
            r = f / fc if kind == "lp" else fc / f
            mag = 1.0 / np.sqrt(1.0 + r ** 8)  # 4th-order Butterworth-like slope
            mag *= 1.0 + 0.35 * np.exp(-(np.log2(f / fc) / 0.3) ** 2)  # a little resonance at fc
            if kind == "hp":
                mag[0] = 0.0
            mags[row] = mag
        impulse = np.fft.irfft(mags, nf, axis=1)  # zero phase
        half = cls.TAPS // 2
        h = np.concatenate([impulse[:, -half:], impulse[:, : half + 1]], axis=1) * np.kaiser(cls.TAPS, 6.0)
        for row, (kind, _) in enumerate(keys):
            if kind == "flat":
                h[row] = 0.0
                h[row, half] = 1.0
        return h.astype(np.float32)

    def _spectrum(self, key, nfft: int) -> np.ndarray:
        k = (key, nfft)
        if k not in self._spec:  # a block size warm() didn't cover
            if key not in self._taps:
                self._taps[key] = self._design_many([key])[0]
            self._spec[k] = np.fft.rfft(self._taps[key], nfft).astype(np.complex64)
        return self._spec[k]

    def process(self, x, amount):
        n = len(x)
        buf = np.concatenate([self.hist, x.astype(np.float32)])
        self.hist = buf[-(self.TAPS - 1):]
        key = self._key(amount)
        if key == self.key == ("flat", 0):
            # flat is a pure delay of TAPS // 2: no need for the FFT
            start = self.TAPS // 2
            return buf[start : start + n].copy()
        nfft = self._nfft(n)
        spec = np.fft.rfft(buf, nfft)
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
        # 24 dB/octave below 60 Hz (-0.3 dB at 85 Hz, -24 dB at 30 Hz, -48 dB at
        # 15 Hz), with no latency. It replaces a moving-average high-pass plus a
        # linear-phase rumble filter, which cost the mic 10 ms.
        self.hp = HighPass(60.0, 4)
        self.gate_gain = 0.0
        self.floor = 0.0  # noise floor, linear RMS after gain
        self._levels: deque[float] = deque(maxlen=self.FLOOR_BLOCKS)
        self._hold = 0  # samples the gate stays open for after the voice drops
        self._blocks = 0

    @property
    def is_open(self) -> bool:
        return self.gate_gain >= 1.0

    def process(self, x: np.ndarray, gain: float, gate_db: float) -> np.ndarray:
        y = self.hp(x) * np.float32(gain)
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
        self._blocks += 1
        # a percentile of 10 s moves slowly: work it out every 8th block, not every one
        if len(self._levels) >= 10 and (self._blocks % 8 == 0 or self.floor == 0.0):
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
