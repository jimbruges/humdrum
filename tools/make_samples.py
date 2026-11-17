# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "scipy"]
# ///
"""Synthesise a starter kit of dancehall / sound-clash effects for Humdrum.

Everything is generated from oscillators and noise, so the files are free to
use and share. Output: 48 kHz mono 16-bit WAV (the format Humdrum stores), in
samples/dancehall/ next to this tools/ folder.

    uv run tools/make_samples.py            # writes samples/dancehall/*.wav
    uv run tools/make_samples.py out_dir    # somewhere else
"""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
from scipy import signal

SR = 48000
rng = np.random.default_rng(1985)  # fixed seed: same files every run


# -- building blocks ---------------------------------------------------------


def timeline(sec: float) -> np.ndarray:
    return np.arange(int(sec * SR)) / SR


def noise(n: int) -> np.ndarray:
    return rng.standard_normal(n)


def phase_of(freq: np.ndarray | float, n: int | None = None) -> np.ndarray:
    """Running phase in cycles for a (possibly time-varying) frequency."""
    f = np.full(n, freq, float) if np.isscalar(freq) else np.asarray(freq, float)
    return np.cumsum(f) / SR


def saw(freq, n: int | None = None) -> np.ndarray:
    """Band-limited (polyBLEP) sawtooth."""
    f = np.full(n, freq, float) if np.isscalar(freq) else np.asarray(freq, float)
    p = phase_of(f) % 1.0
    dt = np.clip(f / SR, 1e-6, 0.5)
    y = 2 * p - 1
    lo = p < dt
    t = p[lo] / dt[lo]
    y[lo] -= t + t - t * t - 1
    hi = p > 1 - dt
    t = (p[hi] - 1) / dt[hi]
    y[hi] -= t * t + t + t + 1
    return y


def sine(freq, n: int | None = None) -> np.ndarray:
    return np.sin(2 * np.pi * phase_of(freq, n))


def filt(x: np.ndarray, kind: str, freq, order: int = 2) -> np.ndarray:
    sos = signal.butter(order, freq, btype=kind, fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def sweep_lowpass(x: np.ndarray, cutoff: np.ndarray, poles: int = 2) -> np.ndarray:
    """One-pole lowpass (cascaded) whose cutoff moves sample by sample."""
    a = 1 - np.exp(-2 * np.pi * np.asarray(cutoff) / SR)
    y = x.astype(float)
    for _ in range(poles):
        out = np.empty_like(y)
        s = 0.0
        for i in range(len(y)):
            s += a[i] * (y[i] - s)
            out[i] = s
        y = out
    return y


def sweep_bandpass(x: np.ndarray, centre: np.ndarray, q: float) -> np.ndarray:
    """Chamberlin state-variable bandpass with a moving centre frequency."""
    f = 2 * np.sin(np.pi * np.minimum(centre, SR / 6) / SR)
    damp = 1 / q
    low = band = 0.0
    out = np.empty(len(x))
    for i in range(len(x)):
        low += f[i] * band
        high = x[i] - low - damp * band
        band += f[i] * high
        out[i] = band
    return out


def env_ar(n: int, attack: float, release: float) -> np.ndarray:
    """Flat gate with linear attack and release, `n` samples long."""
    e = np.ones(n)
    a, r = max(1, int(attack * SR)), max(1, int(release * SR))
    e[:a] = np.linspace(0, 1, a)
    e[-r:] *= np.linspace(1, 0, r)
    return e


def decay(n: int, tau: float, attack: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    e = np.exp(-t / tau)
    a = int(attack * SR)
    if a:
        e[:a] *= np.linspace(0, 1, a)
    return e


def place(out: np.ndarray, x: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(at * SR)
    j = min(len(out), i + len(x))
    out[i:j] += gain * x[: j - i]


def reverb(x: np.ndarray, seconds: float, wet: float, tone: float = 5000) -> np.ndarray:
    """Convolve with a decaying-noise impulse; returns dry + wet with the tail added."""
    n = int(seconds * SR)
    ir = noise(n) * np.exp(-np.arange(n) / SR / (seconds / 6.9))
    ir = filt(ir, "lowpass", tone)
    ir /= np.sqrt(np.sum(ir**2))
    dry = np.pad(x, (0, n))
    return dry + wet * signal.fftconvolve(dry, ir)[: len(dry)]


def echo(x: np.ndarray, delay: float, feedback: float, tail: float, tone: float = 3000) -> np.ndarray:
    """Tape-style feedback echo, darker on each repeat."""
    d = int(delay * SR)
    y = np.pad(x, (0, int(tail * SR))).astype(float)
    sos = signal.butter(1, tone, "lowpass", fs=SR, output="sos")
    for k in range(d, len(y), d):
        prev = y[k - d : k]
        seg = y[k : k + d]
        seg += feedback * signal.sosfilt(sos, prev)[: len(seg)]
    return y


def finish(x: np.ndarray, drive: float = 1.0, peak: float = 0.89) -> np.ndarray:
    """Soft-clip, remove DC, normalise to -1 dBFS, fade the edges, trim silence at the end."""
    x = filt(x, "highpass", 25)
    if drive != 1.0:
        x = np.tanh(drive * x / np.max(np.abs(x)))
    x = x / np.max(np.abs(x)) * peak
    loud = np.flatnonzero(np.abs(x) > 0.002)
    x = x[: loud[-1] + int(0.05 * SR)] if len(loud) else x
    f = int(0.004 * SR)
    x[:f] *= np.linspace(0, 1, f)
    x[-f * 10 :] *= np.linspace(1, 0, f * 10)
    return x.astype(np.float32)


# -- the sounds --------------------------------------------------------------


def horn_blast(dur: float, f0: float = 466.0) -> np.ndarray:
    """One air-horn blast: detuned brass stack, pitch scoop, valve flutter."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    scoop = 1 - 0.12 * np.exp(-t / 0.035)  # pitch swoops up into the note
    wobble = 1 + 0.004 * sine(5.3, n) + 0.002 * noise(n).cumsum() / np.sqrt(n)
    f = f0 * scoop * wobble
    x = (saw(f) + 0.8 * saw(f * 1.006) + 0.8 * saw(f * 0.993)
         + 0.5 * saw(f * 1.498) + 0.6 * saw(f * 0.5))
    x *= 1 + 0.35 * sine(31.0, n)  # the reed rattle
    x = filt(x, "bandpass", [250, 4500])
    x += 0.8 * filt(x, "bandpass", [1000, 1700])  # brassy honk formant
    x = np.tanh(2.5 * x / np.max(np.abs(x)))
    return x * env_ar(n, 0.012, min(0.07, dur / 3))


def airhorn() -> np.ndarray:
    out = np.zeros(int(2.0 * SR))
    for at, dur in [(0.0, 0.13), (0.19, 0.13), (0.38, 1.05)]:
        place(out, horn_blast(dur), at)
    return finish(reverb(out, 1.2, 0.25), drive=1.3)


def airhorn_long() -> np.ndarray:
    return finish(reverb(horn_blast(2.2), 1.2, 0.25), drive=1.3)


def boom(sec: float = 3.8) -> np.ndarray:
    n = int(sec * SR)
    t = np.arange(n) / SR
    out = np.zeros(n)
    # the crack
    out[: int(0.03 * SR)] += 1.5 * filt(noise(int(0.03 * SR)), "highpass", 1500) * decay(int(0.03 * SR), 0.008)
    # fireball: noise through a lowpass that closes from bright to dull
    cutoff = 150 + 7000 * np.exp(-t / 0.35)
    body = sweep_lowpass(noise(n), cutoff, poles=2) * decay(n, 0.7, attack=0.004)
    out += 2.5 * body / np.max(np.abs(body))
    # sub thump
    out += 1.2 * sine(30 + 70 * np.exp(-t / 0.25), n) * decay(n, 0.55, attack=0.002)
    # rolling rumble with a slow random swell
    swell = 0.6 + 0.4 * filt(noise(n), "lowpass", 3)
    rum = filt(noise(n), "lowpass", 180) * decay(n, 1.4, attack=0.05) * swell
    out += 1.2 * rum / np.max(np.abs(rum))
    # debris crackle, thinning out
    rate = 400 * np.exp(-t / 0.6)
    hits = np.flatnonzero(rng.random(n) < rate / SR)
    crackle = np.zeros(n)
    crackle[hits] = rng.uniform(-1, 1, len(hits)) ** 3  # mostly small ticks, a few pops
    crackle = filt(crackle, "bandpass", [1500, 7000])
    out += 2.5 * crackle * (t > 0.08)
    return reverb(out, 2.0, 0.3, tone=2500)


def explosion() -> np.ndarray:
    return finish(boom(), drive=2.5)


def bomb_drop() -> np.ndarray:
    fall = 2.3
    n = int(fall * SR)
    t = np.arange(n) / SR
    f = 2400 * (0.17 ** (t / fall)) * (1 + 0.006 * sine(6.0, n))
    whistle = sine(f) + 0.15 * sine(2 * f)
    whistle += 0.08 * filt(noise(n), "bandpass", [1500, 4000])  # air rushing
    whistle *= np.linspace(0.25, 1.0, n) * env_ar(n, 0.15, 0.02)
    out = np.zeros(int((fall + 4.5) * SR))
    place(out, 0.45 * whistle, 0)
    place(out, boom(), fall)
    return finish(out, drive=2.5)


def shot() -> np.ndarray:
    n = int(0.5 * SR)
    t = np.arange(n) / SR
    x = np.zeros(n)
    k = int(0.002 * SR)
    x[:k] += 2.0 * noise(k)  # the snap
    x += 1.5 * filt(noise(n), "bandpass", [700, 6000]) * decay(n, 0.02)
    x += 1.2 * filt(noise(n), "lowpass", 700) * decay(n, 0.07)
    x += 1.5 * sine(45 + 110 * np.exp(-t / 0.02), n) * decay(n, 0.05)
    return np.tanh(1.8 * x)


def fireworks() -> np.ndarray:
    out = np.zeros(int(2.0 * SR))
    for at, g in [(0.0, 1.0), (0.16, 0.9), (0.32, 0.95), (0.62, 1.0), (0.78, 0.9), (1.05, 1.0)]:
        place(out, shot(), at, g)
    return finish(reverb(out, 1.6, 0.45, tone=3500), drive=1.2)


def door_slam() -> np.ndarray:
    return finish(reverb(shot(), 1.8, 0.5, tone=3500), drive=1.2)


def dub_siren() -> np.ndarray:
    sec = 2.4
    n = int(sec * SR)
    t = np.arange(n) / SR
    lfo_rate = 3.5 + 10 * (t / sec) ** 1.5  # warble speeds up
    lfo = 2 * np.abs(2 * (phase_of(lfo_rate) % 1) - 1) - 1  # triangle
    base = 420 * 2 ** (t / sec * 0.8)  # whole thing climbs
    f = base * 2 ** (0.55 * lfo)
    x = np.tanh(4 * sine(f)) + 0.3 * saw(f)  # soft-square buzz
    x = filt(x, "lowpass", 3500) * env_ar(n, 0.01, 0.03)
    return finish(echo(x, 0.316, 0.6, 3.5, tone=2500), drive=1.1)


def laser() -> np.ndarray:
    def pew() -> np.ndarray:
        n = int(0.2 * SR)
        t = np.arange(n) / SR
        f = 180 + 3200 * np.exp(-t / 0.035)
        return (sine(f) + 0.35 * np.sign(sine(f * 0.5))) * decay(n, 0.07, attack=0.001)

    out = np.zeros(int(0.9 * SR))
    for at in (0.0, 0.17, 0.34):
        place(out, pew(), at)
    return finish(echo(out, 0.17, 0.45, 1.6, tone=4000), drive=1.4)


def riddim(bars: int = 2, bpm: float = 96) -> np.ndarray:
    """A plain dancehall drum + bass loop (dembow kick/snare pattern), loops cleanly."""
    step = 60 / bpm / 4  # one 16th
    total = bars * 16 * step
    out = np.zeros(int(total * SR))

    def kick() -> np.ndarray:
        n = int(0.35 * SR)
        t = np.arange(n) / SR
        return sine(48 + 110 * np.exp(-t / 0.03), n) * decay(n, 0.12, attack=0.001)

    def snare() -> np.ndarray:
        n = int(0.22 * SR)
        t = np.arange(n) / SR
        return (0.7 * filt(noise(n), "bandpass", [1500, 8000]) * decay(n, 0.05)
                + 0.6 * sine(190 * (1 + 0.3 * np.exp(-t / 0.01)), n) * decay(n, 0.04))

    def hat(open_: bool = False) -> np.ndarray:
        n = int((0.18 if open_ else 0.05) * SR)
        return filt(noise(n), "highpass", 8000) * decay(n, 0.06 if open_ else 0.012)

    def bass(freq: float, dur: float) -> np.ndarray:
        n = int(dur * SR)
        return np.tanh(1.5 * (sine(freq, n) + 0.2 * sine(2 * freq, n))) * env_ar(n, 0.005, 0.04)

    notes = [55.0, 55.0, 65.41, 49.0]  # A1 A1 C2 G1, one per half-bar
    for bar in range(bars):
        for s in range(16):
            at = (bar * 16 + s) * step
            if s % 4 == 0:
                place(out, kick(), at, 1.0)
            if s in (3, 6, 11, 14):  # the dembow "boom-ch-boom-chick"
                place(out, snare(), at, 0.55)
            place(out, hat(open_=s % 8 == 6), at, 0.18 if s % 2 else 0.1)
        for h in range(2):
            idx = (bar * 2 + h) % len(notes)
            place(out, bass(notes[idx], 8 * step - 0.02), (bar * 16 + 8 * h) * step, 0.7)
    return out


def rewind() -> np.ndarray:
    """The selector grabs the record and spins it back: 'pull up!' Starts on the grab."""
    loop = riddim()
    sec = 3.3
    n = int(sec * SR)
    t = np.arange(n) / SR
    speed = np.ones(n)
    grab, spin, slow = 1.3, 1.45, 2.3
    m = (t >= grab) & (t < spin)
    speed[m] = np.interp(t[m], [grab, spin], [1, -2.8])
    m = (t >= spin) & (t < slow)
    speed[m] = -2.8 + 0.25 * np.sin(2 * np.pi * 7 * (t[m] - spin))  # hand wobble
    m = t >= slow
    speed[m] = np.interp(t[m], [slow, sec], [-2.8, 0])
    pos = (np.cumsum(speed) + 1.0 * SR) % len(loop)  # start a beat in
    x = np.interp(pos, np.arange(len(loop)), loop, period=len(loop))
    # friction noise while the platter is being dragged
    x += 0.04 * filt(noise(n), "bandpass", [800, 3000]) * np.clip(-speed, 0, None) / 2.8
    x *= np.where(t >= slow, np.sqrt(np.abs(speed) / 2.8), 1.0)  # dies away as the platter stops
    x = x[int(grab * SR):]  # drop the tune before the grab
    return finish(x, drive=1.1)


def bass_drop() -> np.ndarray:
    n = int(3.0 * SR)
    t = np.arange(n) / SR
    f = 32 + 140 * np.exp(-t / 0.35)
    x = sine(f) * decay(n, 1.1, attack=0.003)
    k = int(0.01 * SR)
    x[:k] += 0.6 * filt(noise(k), "lowpass", 3000) * decay(k, 0.002)  # beater click
    x = np.tanh(2.2 * x)  # harmonics so it reads on small speakers
    return finish(x)


def riser() -> np.ndarray:
    sec = 4.0
    n = int(sec * SR)
    t = np.arange(n) / SR
    ramp = (t / sec) ** 2
    centre = 300 * (8000 / 300) ** (t / sec)
    whoosh = sweep_bandpass(noise(n), centre, q=4)
    whoosh /= np.max(np.abs(whoosh))
    f = 110 * 2 ** (3 * ramp)
    tone = saw(f) + saw(f * 1.01) + saw(f * 0.99)
    tone = filt(tone, "lowpass", 5000) / 3
    x = (whoosh + 0.35 * tone) * (0.1 + 0.9 * ramp)
    x *= 1 + 0.5 * (ramp > 0.5) * sine(4 + 20 * ramp, n)  # stutter in the last stretch
    x[-int(0.003 * SR):] *= np.linspace(1, 0, int(0.003 * SR))  # hard stop
    x = x / np.max(np.abs(x)) * 0.89
    return x.astype(np.float32)


def whistle() -> np.ndarray:
    def blow(dur: float) -> np.ndarray:
        n = int(dur * SR)
        trill = sine(28.0, n)  # the pea rattling
        f = 3100 * (1 + 0.035 * trill)
        x = sine(f) * (0.65 + 0.35 * trill) + 0.05 * filt(noise(n), "bandpass", [2500, 4500])
        return x * env_ar(n, 0.01, 0.03)

    out = np.zeros(int(2.0 * SR))
    for at, dur in [(0.0, 0.1), (0.15, 0.1), (0.3, 0.1), (0.45, 0.1), (0.62, 0.9)]:
        place(out, blow(dur), at)
    return finish(reverb(out, 0.9, 0.2))


SOUNDS = {
    "airhorn": airhorn,
    "airhorn-long": airhorn_long,
    "explosion": explosion,
    "bomb-drop": bomb_drop,
    "fireworks": fireworks,
    "door-slam": door_slam,
    "dub-siren": dub_siren,
    "laser": laser,
    "rewind": rewind,
    # Not saved: the old riddim loop's turn in the random stream, kept so the
    # sounds after it keep the noise they had. Removing it changes their texture.
    "_riddim": riddim,
    "bass-drop": bass_drop,
    "riser": riser,
    "whistle": whistle,
}


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "samples" / "dancehall"
    out.mkdir(parents=True, exist_ok=True)
    for name, make in SOUNDS.items():
        x = make()
        if name.startswith("_"):
            continue
        write_wav(out / f"{name}.wav", x)
        print(f"{name:14s} {len(x) / SR:5.2f} s")


if __name__ == "__main__":
    main()
