"""Drive a Modulino Buzzer as a (very) rudimentary monitor speaker.

The buzzer's firmware only plays "frequency F for N ms" on a piezo, so it can't
reproduce audio. Two modes make it useful for testing without a mixer:

    pads    each pad press beeps at its own pitch (a chromatic scale up the pads)
    follow  the dominant pitch of the output is tracked and played, so you hear
            your voice (with FX) or a melodic sample as a square-ish tone
"""

from __future__ import annotations

import numpy as np

from fx import SR

MODES = ("off", "pads", "follow")
MIN_HZ, MAX_HZ = 80.0, 1200.0


def pad_freq(pad: int) -> int:
    return int(round(261.63 * 2 ** (pad / 12)))  # pad 1 = middle C


def detect_pitch(x: np.ndarray, level_gate: float = 0.02) -> float | None:
    """Autocorrelation pitch estimate (via FFT), None when quiet or unpitched."""
    if len(x) < 1024 or float(np.max(np.abs(x))) < level_gate:
        return None
    x = x - np.mean(x)
    n = 1 << int(np.ceil(np.log2(len(x) * 2)))
    spec = np.fft.rfft(x * np.hanning(len(x)), n)
    ac = np.fft.irfft(spec * np.conj(spec))[: len(x)]
    if ac[0] <= 0:
        return None
    lo, hi = int(SR / MAX_HZ), int(SR / MIN_HZ)
    # skip the slope down from lag 0: search from the first dip onwards
    dips = np.flatnonzero(np.diff(ac[: hi + 1]) > 0)
    if not len(dips) or dips[0] >= hi - 2:
        return None
    lo = max(lo, int(dips[0]))
    seg = ac[lo:hi]
    best = float(np.max(seg))
    if best / ac[0] < 0.45:  # weak periodicity: noise, consonants, drums
        return None
    # the first local peak close to the best one is the period; later peaks are
    # its multiples, and picking them would drop the pitch an octave
    peaks = np.flatnonzero((seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:]) & (seg[1:-1] > 0.85 * best)) + 1
    lag = int(peaks[0] if len(peaks) else np.argmax(seg)) + lo
    # parabolic interpolation around the peak for sub-sample accuracy
    if lo < lag < hi - 1:
        a, b, c = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = a - 2 * b + c
        if denom:
            lag = lag + 0.5 * (a - c) / denom
    return SR / lag


class BuzzerFollower:
    """Turns pitch estimates into rate-limited buzz commands."""

    def __init__(self) -> None:
        self.current = 0
        self.last_sent = 0.0

    def update(self, freq: float | None, now: float) -> tuple[int, int] | None:
        """Returns (freq, ms) to send, or None when nothing needs sending."""
        target = int(round(freq)) if freq else 0
        changed = abs(target - self.current) > max(3, self.current * 0.03)
        # tones are sent with a short duration and refreshed, so a lost
        # message or a stalled app never leaves the buzzer screaming
        if changed or (target and now - self.last_sent > 0.06):
            self.current, self.last_sent = target, now
            return (target, 90 if target else 0)
        return None
